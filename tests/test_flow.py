"""End-to-end loop test with fake agents, plus the guards that protect a commit."""

import pathlib
import re
import shutil
import tempfile
import unittest
import os
from unittest import mock

from cav import agents as agent_mod
from cav import config
from cav.agents import RunState
from cav.gitops import Repo, git, git_ok
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

    def reset(self):
        self.state = RunState()

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
        log = (orchestrator.run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("PROG", log, "progress lines must reach run.log, not only stdout")

    def test_snapshot_works_without_commits(self):
        fresh = self.tmp / "no-commits"
        fresh.mkdir()
        git("init", "-q", cwd=fresh)
        git("config", "user.email", "t@example.com", cwd=fresh)
        git("config", "user.name", "t", cwd=fresh)
        (fresh / "a.txt").write_text("first\n", encoding="utf-8")

        spec = config.AgentSpec("kimi", "kimi")
        orchestrator = Orchestrator(self.case(), self.tmp / "snap2")
        sha, problem = orchestrator.snapshot(Repo(fresh), spec)

        self.assertIsNone(problem)
        self.assertIn("a.txt", git("ls-tree", "-r", "--name-only", sha, cwd=fresh).splitlines())
        self.assertFalse(git_ok("rev-parse", "--verify", "--quiet", "HEAD", cwd=fresh),
                         "snapshot must not create a HEAD in the source repo")

    def test_snapshot_works_without_git_identity(self):
        fresh = self.tmp / "no-identity"
        fresh.mkdir()
        git("init", "-q", cwd=fresh)
        (fresh / "a.txt").write_text("first\n", encoding="utf-8")

        blocked = {"GIT_CONFIG_GLOBAL": str(self.tmp / "no-global.cfg"),
                   "GIT_CONFIG_SYSTEM": str(self.tmp / "no-system.cfg")}
        with mock.patch.dict(os.environ, blocked):
            orchestrator = Orchestrator(self.case(), self.tmp / "snap3")
            sha, problem = orchestrator.snapshot(Repo(fresh), config.AgentSpec("kimi", "kimi"))
            self.assertIsNone(problem)
            self.assertTrue(sha)
            (fresh / "b.txt").write_text("second\n", encoding="utf-8")
            committed, problem = Repo(fresh).commit_all(fresh, "tool-made commit")
            self.assertIsNone(problem)
            self.assertTrue(committed)

    def test_merge_unrelated_histories(self):
        other = self.tmp / "other"
        other.mkdir()
        git("init", "-q", cwd=other)
        git("config", "user.email", "t@example.com", cwd=other)
        git("config", "user.name", "t", cwd=other)
        (other / "b.txt").write_text("other\n", encoding="utf-8")
        git("add", "-A", cwd=other)
        git("commit", "-q", "-m", "other", cwd=other)

        git("remote", "add", "other", str(other), cwd=self.repo)
        git("fetch", "other", cwd=self.repo)
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=other)
        worktree = self.tmp / "unrelated-wt"
        git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=self.repo)

        ok, conflicts, message = Repo(self.repo).merge(worktree, f"other/{branch}")
        self.assertTrue(ok, message)
        self.assertTrue((worktree / "base.txt").exists())
        self.assertTrue((worktree / "b.txt").exists())

    def test_merge_works_without_git_identity(self):
        git("switch", "-c", "feature", cwd=self.repo)
        (self.repo / "feature.txt").write_text("f\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "feature",
            cwd=self.repo)
        git("switch", "-", cwd=self.repo)
        for key in ("user.name", "user.email"):
            git("config", "--local", "--unset", key, cwd=self.repo, check=False)

        worktree = self.tmp / "identity-wt"
        git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=self.repo)
        blocked = {"GIT_CONFIG_GLOBAL": str(self.tmp / "no-global.cfg"),
                   "GIT_CONFIG_SYSTEM": str(self.tmp / "no-system.cfg")}
        with mock.patch.dict(os.environ, blocked):
            ok, conflicts, message = Repo(self.repo).merge(worktree, "feature")
        self.assertTrue(ok, message)
        self.assertTrue((worktree / "feature.txt").exists())

    def test_merge_reports_failure_without_conflicts(self):
        worktree = self.tmp / "bad-merge-wt"
        git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=self.repo)
        ok, conflicts, message = Repo(self.repo).merge(worktree, "no-such-ref")
        self.assertFalse(ok)
        self.assertEqual(conflicts, [])
        self.assertIn("not something we can merge", message)

    def test_review_parsing(self):
        path = self.tmp / "review.md"
        path.write_text("MUST_FIX: 0\n", encoding="utf-8")
        self.assertEqual(parse_must_fix(path), 0)
        path.write_text("MUST_FIX: 3\nMUST_FIX: 0\n", encoding="utf-8")
        self.assertIsNone(parse_must_fix(path))
        path.write_text("MUST_FIX: 0 trailing\n", encoding="utf-8")
        self.assertIsNone(parse_must_fix(path))
        path.write_text("no verdict here\n", encoding="utf-8")
        self.assertIsNone(parse_must_fix(path))
        self.assertIsNone(parse_must_fix(self.tmp / "missing.md"))

    def test_checks_run_python_placeholder(self):
        case = self.case()
        case.checks = ['{python} -c "print(\'ok\')"']
        orchestrator = Orchestrator(case, self.tmp / "checks")
        result = orchestrator.execute_checks("deepseek", self.repo, self.tmp / "checks" / "out.json")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["commands"][0]["exit"], 0)

    def test_reused_check_result_keeps_each_agent_label(self):
        case = self.case()
        case.checks = ['{python} -c "print(1)"']
        orchestrator = Orchestrator(case, self.tmp / "checks-cache")
        results = {name: {"tree": "same"} for name in ("deepseek", "kimi")}
        worktrees = {name: {"path": self.repo} for name in ("deepseek", "kimi")}
        report = orchestrator.run_checks(1, results, worktrees)
        self.assertEqual(report["kimi"]["agent"], "kimi")
        self.assertEqual(report["deepseek"]["agent"], "deepseek")
        self.assertIsNot(report["kimi"], report["deepseek"])

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
        self.assertEqual(case.agents["kimi"].bootstrap_ref, "origin/candidate/kimi-phase-1-5")
        self.assertEqual(len(case.checks), 4)
        self.assertEqual(case.other("kimi"), "deepseek")

    def test_shipped_case_pins_models(self):
        case = config.load(pathlib.Path(__file__).resolve().parents[1] / "cases" / "msmon-five-phase.json")
        self.assertEqual(case.agents["deepseek"].model, "deepseek/deepseek-flash#high")
        self.assertEqual(case.agents["kimi"].model, "kimi-code/k3.1-pro")
        self.assertEqual(case.agents["kimi"].thinking, "high")


    def test_tool_case_has_no_shared_base(self):
        case = config.load(pathlib.Path(__file__).resolve().parents[1]
                           / "cases" / "cross-agent-validation.json")
        self.assertIsNone(case.base)
        self.assertEqual(case.agents["deepseek"].bootstrap_ref, "HEAD")
        self.assertEqual(case.secondary_repo.name, "cross-agent-validation")


class AgentLifecycleTest(unittest.TestCase):
    def test_reset_clears_abort_and_failure_state(self):
        agent = agent_mod.Agent(config.AgentSpec("kimi", "kimi"), "log.txt")
        agent.state.error = "round timeout"
        agent.abort()
        agent.reset()
        self.assertFalse(agent._abort.is_set())
        self.assertIsNone(agent.state.error)

    def test_opencode_abort_kills_running_process(self):
        spec = config.AgentSpec("deepseek", "opencode")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        agent._proc = mock.Mock()
        agent._proc.poll.return_value = None
        agent.abort()
        agent._proc.kill.assert_called_once_with()

    def test_aborted_opencode_keeps_the_monitor_reason(self):
        class Proc:
            returncode = -9

            def __init__(self):
                self.stdout = iter(())

            def wait(self):
                return -9

        spec = config.AgentSpec("deepseek", "opencode")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"), \
                mock.patch("cav.agents.subprocess.Popen", return_value=Proc()):
            agent = agent_mod.OpenCodeAgent(spec, pathlib.Path(tmp) / "run.log")
            agent.state.error = "blocked: waiting:permission"
            agent.abort()
            result = agent.run("prompt", tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "blocked: waiting:permission")


class ModelPinningTest(unittest.TestCase):
    def test_opencode_command_pins_model(self):
        spec = config.AgentSpec("deepseek", "opencode", model="deepseek/deepseek-flash#high")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        command = agent._command("hello")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "deepseek/deepseek-flash#high")
        self.assertEqual(command[-1], "hello")

    def test_kimi_profile_pins_model_and_thinking(self):
        spec = config.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro", thinking="high")
        agent = agent_mod.KimiAgent(spec, "log.txt")
        self.assertEqual(agent._agent_config(),
                         {"model": "kimi-code/k3.1-pro", "permission_mode": "yolo", "thinking": "high"})


if __name__ == "__main__":
    unittest.main()
