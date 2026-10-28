"""End-to-end loop test with fake agents, plus the guards that protect a commit."""

import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from cav import config
from cav.agents import RunState
from cav.gitops import Repo, git
from cav.orchestrator import Orchestrator, parse_must_fix

REVIEW_RE = re.compile(r"review to this exact path: (.+)")
ROUND_RE = re.compile(r"Round (\d+)")


class FakeAgent:
    """Stands in for a real agent: edits a file and writes the review the orchestrator expects."""

    def __init__(self, name):
        self.name = name
        self.state = RunState()
        self.session_id = None

    def abort(self):
        pass

    def content(self, round_no):
        return "agreed\n" if round_no >= 2 else f"{self.name}\n"

    def run(self, prompt, cwd):
        round_no = int(ROUND_RE.search(prompt).group(1))
        review = pathlib.Path(REVIEW_RE.search(prompt).group(1).strip())
        (pathlib.Path(cwd) / "shared.txt").write_text(self.content(round_no), encoding="utf-8")
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(f"# Round {round_no} review\n\nMUST_FIX: 0\n", encoding="utf-8")
        self.state.finished = True
        self.state.touch("finished")
        return {"ok": True, "text": "done", "reason": "completed"}


def make_repo(root):
    repo = pathlib.Path(root) / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-test-"))
        self.repo, self.base = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def case(self, rounds=2):
        return config.Case(
            name="flow",
            host_repo=self.repo,
            base=self.base,
            agents={
                "deepseek": config.AgentSpec("deepseek", "opencode", bootstrap_ref=self.base),
                "kimi": config.AgentSpec("kimi", "kimi", bootstrap_ref=self.base),
            },
            checks=[],
            max_rounds=rounds,
            round_seconds=30,
            stall_seconds=5,
            poll_seconds=0.05,
            retries=0,
        )

    def test_two_rounds_converge(self):
        fakes = {"deepseek": FakeAgent("deepseek"), "kimi": FakeAgent("kimi")}

        def fake_build(spec, log_path, title_prefix=""):
            return fakes[spec.name]

        with mock.patch("cav.agents.build", side_effect=fake_build):
            orchestrator = Orchestrator(self.case(), self.tmp / "run")
            state = orchestrator.run()

        self.assertEqual(state["phase"], "converged")
        self.assertEqual(len(state["history"]), 2)
        self.assertFalse(state["history"][0]["converged"])
        self.assertFalse(state["history"][0]["trees_equal"])
        self.assertTrue(state["history"][1]["converged"])

        final = state["final"]
        self.assertEqual(final["tree"], state["history"][1]["results"]["deepseek"]["tree"])
        self.assertEqual(final["tree"], state["history"][1]["results"]["kimi"]["tree"])
        self.assertEqual(git("show", f"{final['commit']}:shared.txt", cwd=self.repo).strip(), "agreed")

        for name in ("deepseek", "kimi"):
            signoff = orchestrator.run_dir / "signoff" / f"{name}.json"
            self.assertTrue(signoff.exists(), name)
        self.assertTrue((orchestrator.run_dir / "REPORT.md").exists())
        self.assertTrue((orchestrator.run_dir / "state.json").exists())

    def test_review_parsing(self):
        path = self.tmp / "review.md"
        path.write_text("MUST_FIX: 3\nMUST_FIX: 0\n", encoding="utf-8")
        self.assertEqual(parse_must_fix(path), 0)
        path.write_text("no verdict here\n", encoding="utf-8")
        self.assertIsNone(parse_must_fix(path))
        self.assertIsNone(parse_must_fix(self.tmp / "missing.md"))

    def test_conflict_markers_block_commit(self):
        (self.repo / "bad.txt").write_text("<<<<<<< HEAD\nours\n>>>>>>> theirs\n", encoding="utf-8")
        sha, problem = Repo(self.repo).commit_all(self.repo, "should not commit")
        self.assertIsNone(sha)
        self.assertIn("conflict markers", problem)

    def test_snapshot_excludes_and_leaves_repo_alone(self):
        secondary = self.tmp / "secondary"
        secondary.mkdir()
        git("init", "-q", cwd=secondary)
        git("config", "user.email", "t@example.com", cwd=secondary)
        git("config", "user.name", "t", cwd=secondary)
        (secondary / "base.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=secondary)
        git("commit", "-q", "-m", "base", cwd=secondary)
        before = git("rev-parse", "HEAD", cwd=secondary)
        (secondary / "changed.txt").write_text("work\n", encoding="utf-8")
        (secondary / "reconciliation").mkdir()
        (secondary / "reconciliation" / "note.md").write_text("meta\n", encoding="utf-8")

        spec = config.AgentSpec("kimi", "kimi", bootstrap_exclude=["reconciliation"])
        orchestrator = Orchestrator(self.case(), self.tmp / "snap")
        sha, problem = orchestrator.snapshot(Repo(secondary), spec)

        self.assertIsNone(problem)
        files = git("ls-tree", "-r", "--name-only", sha, cwd=secondary).splitlines()
        self.assertIn("changed.txt", files)
        self.assertNotIn("reconciliation/note.md", files)
        self.assertEqual(git("rev-parse", "HEAD", cwd=secondary), before)
        status = git("status", "--porcelain", cwd=secondary)
        self.assertIn("changed.txt", status)
        self.assertIn("reconciliation/", status)


class ConfigTest(unittest.TestCase):
    def test_shipped_case_loads(self):
        case = config.load(pathlib.Path(__file__).resolve().parents[1] / "cases" / "msmon-five-phase.json")
        self.assertEqual(sorted(case.agents), ["deepseek", "kimi"])
        self.assertEqual(case.agents["kimi"].bootstrap_exclude, ["reconciliation"])
        self.assertEqual(len(case.checks), 4)
        self.assertEqual(case.other("kimi"), "deepseek")


if __name__ == "__main__":
    unittest.main()
