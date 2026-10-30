"""Tree comparison, review validation, acceptance, publication, events and the dashboard."""

import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from cav import agents as agent_mod
from cav import compare, config, events, prompts, review
from cav.agents import RunState
from cav.gitops import git
from cav.orchestrator import Orchestrator
from cav.web import Dashboard

ROUND_RE = re.compile(r"Round (\d+)")
REVIEW_JSON_RE = re.compile(r"REVIEW: write this JSON to (.+)")
NAMES_RE = re.compile(r"You are (\w+)\. You and (\w+) ")


def make_repo(root):
    repo = pathlib.Path(root) / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_bytes(b"base\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


class StructuredFake:
    """Writes its proposal's files and a valid JSON review.

    In a review round it approves its own starting candidate and, unless `approve` is false,
    the other's; findings default to the other's candidate.
    """

    def __init__(self, name, files, approve=True, findings=None):
        self.name = name
        self.files = files
        self.approve = approve
        self.findings = findings or []
        self.state = RunState()
        self.calls = 0

    def abort(self):
        pass

    def reset(self):
        self.state = RunState()

    def run(self, prompt, cwd):
        self.calls += 1
        round_no = int(ROUND_RE.search(prompt).group(1))
        path = pathlib.Path(REVIEW_JSON_RE.search(prompt).group(1).strip())
        files = self.files(round_no)
        for name, text in files.items():
            target = pathlib.Path(cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        data = {"round": round_no, "agent": self.name, "notes": "fake"}
        if '"approve"' in prompt:
            other = NAMES_RE.search(prompt).group(2)
            data["approve"] = {self.name: True, other: self.approve}
            data["findings"] = [dict({"candidate": other}, **finding) for finding in self.findings]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.state.finished = True
        self.state.touch("finished")
        return {"ok": True, "text": "done", "reason": "completed"}


class FlakyFake(StructuredFake):
    """Produces no review at all in the rounds listed in skip_rounds."""

    def __init__(self, name, files, skip_rounds=()):
        super().__init__(name, files)
        self.skip_rounds = set(skip_rounds)

    def run(self, prompt, cwd):
        if int(ROUND_RE.search(prompt).group(1)) in self.skip_rounds:
            self.state.finished = True
            self.state.touch("finished")
            return {"ok": True, "text": "", "reason": "completed"}
        return super().run(prompt, cwd)


class NoReviewFirstFake(StructuredFake):
    """Finishes its first attempt without writing a review."""

    def run(self, prompt, cwd):
        if self.calls == 0:
            self.calls += 1
            self.state.finished = True
            return {"ok": True, "text": "", "reason": "completed"}
        return super().run(prompt, cwd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-v2-"))
        self.repo, self.base = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def case(self, **overrides):
        values = dict(
            name="flow", host_repo=self.repo, base=self.base,
            agents={"deepseek": config.AgentSpec("deepseek", "opencode", bootstrap_ref=self.base),
                    "kimi": config.AgentSpec("kimi", "kimi", bootstrap_ref=self.base)},
            checks=[], max_rounds=2, round_seconds=30, stall_seconds=5, retries=0,
        )
        values.update(overrides)
        return config.Case(**values)

    def run_with(self, case, fakes, **kwargs):
        def fake_build(spec, log_path, title_prefix=""):
            return fakes[spec.name]

        with mock.patch("cav.agents.build", side_effect=fake_build):
            orchestrator = Orchestrator(case, self.tmp / "run", **kwargs)
            return orchestrator, orchestrator.run()


class CompareTest(Base):
    def commit(self, name, text, message):
        (self.repo / name).write_bytes(text.encode())
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", message, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def trees(self, left, right):
        return (git("rev-parse", f"{left}^{{tree}}", cwd=self.repo),
                git("rev-parse", f"{right}^{{tree}}", cwd=self.repo))

    def test_paths_split_into_identical_and_differing(self):
        left = self.commit("a.c", "int a;\n// old\n", "left")
        right = self.commit("a.c", "int a;\n// new\n", "comment")
        diff = compare.compare_trees(self.repo / ".git", *self.trees(left, right))
        self.assertEqual(diff["differing"], ["a.c"], "a comment change is still for the agents to decide")
        self.assertEqual(diff["identical"], ["base.txt"])
        self.assertEqual(diff["stats"]["a.c"], {"added": 1, "removed": 1})

    def test_stats_cover_renamed_and_non_ascii_paths(self):
        left = self.commit("old_name.txt", "same line\n" * 5, "left")
        git("mv", "old_name.txt", "new_name.txt", cwd=self.repo)
        right = self.commit("café.txt", "x\n", "rename and add")
        diff = compare.compare_trees(self.repo / ".git", *self.trees(left, right))
        self.assertEqual(diff["stats"]["old_name.txt"], {"added": 0, "removed": 5})
        self.assertEqual(diff["stats"]["new_name.txt"], {"added": 5, "removed": 0})
        self.assertEqual(diff["stats"]["café.txt"], {"added": 1, "removed": 0})

class ReviewValidationTest(Base):
    def trees(self):
        return {"deepseek": self.repo, "kimi": self.repo}

    def data(self, **overrides):
        base = {"round": 1, "agent": "deepseek", "approve": {"deepseek": True, "kimi": True},
                "findings": [], "notes": "x"}
        base.update(overrides)
        return base

    def must_fix(self, candidate="kimi", **change):
        return dict({"id": "F-01", "candidate": candidate, "severity": "MUST_FIX",
                     "path": "base.txt", "line": 1, "summary": "wrong", "fix": "fix it"}, **change)

    def test_clean_review_is_valid(self):
        self.assertEqual(review.validate(self.data(), "deepseek", 1, self.trees()), [])

    def test_counts_are_derived_from_findings(self):
        data = self.data(counts={"must_fix": 9}, approve={"deepseek": True, "kimi": False},
                         findings=[self.must_fix(), self.must_fix(id="F-02", severity="NIT")])
        self.assertEqual(review.validate(data, "deepseek", 1, self.trees()), [])
        self.assertEqual(data["counts"], {"must_fix": 1, "should_fix": 0, "nit": 1})

    def test_approval_is_withheld_only_with_a_must_fix_on_that_candidate(self):
        errors = review.validate(self.data(approve={"deepseek": True, "kimi": False}), "deepseek", 1, self.trees())
        self.assertTrue(any("without a MUST_FIX" in error for error in errors), errors)
        data = self.data(findings=[self.must_fix()])
        errors = review.validate(data, "deepseek", 1, self.trees())
        self.assertTrue(any("approves kimi but lists a MUST_FIX" in error for error in errors), errors)
        data = self.data(approve={"deepseek": True, "kimi": False}, findings=[self.must_fix(candidate="deepseek")])
        self.assertTrue(review.validate(data, "deepseek", 1, self.trees()),
                        "a MUST_FIX on one candidate does not justify refusing the other")

    def test_style_findings_never_block(self):
        data = self.data(findings=[self.must_fix(severity="SHOULD_FIX")])
        self.assertEqual(review.validate(data, "deepseek", 1, self.trees()), [])

    def test_findings_point_at_a_real_line_in_their_candidate(self):
        trees = {"deepseek": self.repo, "kimi": self.tmp}
        data = self.data(approve={"deepseek": True, "kimi": False}, findings=[self.must_fix()])
        self.assertTrue(any("not a file in kimi" in e for e in review.validate(data, "deepseek", 1, trees)))
        data = self.data(approve={"deepseek": True, "kimi": False}, findings=[self.must_fix(line=99)])
        self.assertTrue(any("outside base.txt" in e for e in review.validate(data, "deepseek", 1, self.trees())))
        data = self.data(approve={"deepseek": True, "kimi": False}, findings=[self.must_fix(path="../x")])
        self.assertTrue(any("escapes" in e for e in review.validate(data, "deepseek", 1, self.trees())))

    def test_approvals_must_name_exactly_both_candidates(self):
        for approve in ({"deepseek": True}, {"deepseek": True, "kimi": "yes"}, None,
                        {"deepseek": True, "kimi": True, "other": True}):
            with self.subTest(approve=approve):
                self.assertTrue(review.validate(self.data(approve=approve), "deepseek", 1, self.trees()))

    def test_independent_round_review_is_notes_only(self):
        data = {"round": 1, "agent": "deepseek", "notes": "approach"}
        result = review.Review("deepseek", 1, self.tmp / "r.json", data, review.validate(data, "deepseek", 1))
        self.assertTrue(result.valid)
        self.assertEqual(result.approvals, {})

    def test_malformed_review_fields_are_rejected_not_crashed(self):
        cases = {
            "findings": {"findings": ["F-01"]},
            "unhashable id": {"findings": [{"id": ["F-01"], "candidate": "kimi", "severity": "NIT",
                                            "path": "base.txt", "line": 1}]},
            "path is not a string": {"findings": [{"id": "F-01", "candidate": "kimi", "severity": "NIT",
                                                   "path": 3, "line": 1}]},
            "no candidate": {"findings": [{"id": "F-01", "severity": "NIT", "path": "base.txt", "line": 1}]},
            "no line": {"findings": [{"id": "F-01", "candidate": "kimi", "severity": "NIT", "path": "base.txt"}]},
            "wrong agent": {"agent": "kimi"},
        }
        for label, overrides in cases.items():
            with self.subTest(field=label):
                self.assertTrue(review.validate(self.data(**overrides), "deepseek", 1, self.trees()),
                                f"{label} should be rejected")

    def test_prompt_states_the_review_rules(self):
        text = "\n".join(prompts.review_block("deepseek", "kimi", 2, "a.json", reviewing=True, planning=False))
        self.assertIn('"approve": {"deepseek": true|false, "kimi": true|false}', text)
        self.assertIn("never block", text)
        self.assertNotIn('"counts"', text, "agents are not asked for numbers the orchestrator derives")


class ApprovesFrom(StructuredFake):
    """Withholds approval of the other candidate (with a MUST_FIX) before round `first`."""

    def __init__(self, name, files, first):
        super().__init__(name, files)
        self.first = first

    def run(self, prompt, cwd):
        early = int(ROUND_RE.search(prompt).group(1)) < self.first
        self.approve = not early
        self.findings = [{"id": "F-01", "severity": "MUST_FIX", "path": "base.txt", "line": 1,
                          "summary": "wrong", "fix": "fix"}] if early else []
        return super().run(prompt, cwd)


class AcceptanceTest(Base):
    def branch(self, name, files):
        git("checkout", "-q", "-B", name, self.base, cwd=self.repo)
        for path, text in files.items():
            (self.repo / path).write_bytes(text.encode())
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", name, cwd=self.repo)
        sha = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "--detach", self.base, cwd=self.repo)
        return sha

    def two_starts(self, **overrides):
        starts = {"deepseek": self.branch("ds", {"shared.txt": "deepseek\n"}),
                  "kimi": self.branch("km", {"shared.txt": "kimi\n"})}
        case = self.case(**overrides)
        for name, sha in starts.items():
            case.agents[name].bootstrap_ref = sha
        return case, starts

    def test_mutual_approval_publishes_a_reviewed_starting_candidate(self):
        """What is published is a candidate both agents reviewed, not a proposal written after."""
        case, starts = self.two_starts(max_rounds=1)
        fakes = {name: StructuredFake(name, lambda r: {"shared.txt": "rewritten after review\n"})
                 for name in ("deepseek", "kimi")}
        orchestrator, state = self.run_with(case, fakes)
        self.assertEqual(state["phase"], "converged")
        self.assertEqual(state["history"][-1]["verdict"], "accepted")
        published = state["final"]["published_from"]["agent"]
        self.assertEqual(state["final"]["commit"], starts[published])
        self.assertNotIn(state["final"]["commit"],
                         [result["commit"] for result in state["history"][-1]["results"].values()])
        self.assertIn("shared.txt", (orchestrator.run_dir / "REPORT.md").read_text(encoding="utf-8"))
        self.assertEqual(sum(fake.calls for fake in fakes.values()), 2, "one agent run each, no confirmation")

    def test_original_branches_are_checked_before_they_can_be_accepted(self):
        check = "{python} -c \"import sys; sys.exit(open('shared.txt').read() != 'kimi\\n')\""
        case, starts = self.two_starts(max_rounds=1, checks=[check])
        fakes = {name: StructuredFake(name, lambda r: {"shared.txt": "kimi\n"}) for name in ("deepseek", "kimi")}
        _, state = self.run_with(case, fakes)
        entry = state["history"][0]
        self.assertEqual({name: c["ok"] for name, c in entry["start_checks"].items()},
                         {"deepseek": False, "kimi": True})
        self.assertEqual(entry["verdict"], "diverged", "an approved candidate that fails the checks is not accepted")

    def test_publication_prefers_the_candidate_closest_to_its_original(self):
        """Neither agent is a default: of the two approved candidates, the one that changed less
        from its original branch is published, whichever agent is listed first."""
        for first in ("deepseek", "kimi"):
            with self.subTest(listed_first=first):
                self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-v2-"))
                self.repo, self.base = make_repo(self.tmp)
                fakes = {
                    "deepseek": ApprovesFrom("deepseek", lambda r: {"shared.txt": "a\nb\nc\n", "more.txt": "x\n"}
                                             if r == 1 else {}, first=2),
                    "kimi": ApprovesFrom("kimi", lambda r: {"shared.txt": "a\n"} if r == 1 else {}, first=2),
                }
                case = self.case(max_rounds=2)
                order = [first] + [name for name in case.agents if name != first]
                case.agents = {name: case.agents[name] for name in order}
                _, state = self.run_with(case, fakes)
                self.assertEqual(state["final"]["round"], 2)
                published = state["final"]["published_from"]
                self.assertEqual(published["agent"], "kimi")
                self.assertEqual(state["final"]["commit"], state["history"][0]["results"]["kimi"]["commit"],
                                 "round 2 approved round 1's proposals, so one of those is published")
                self.assertEqual(published["changes"]["kimi"], {"lines_changed": 1, "files_changed": 1})
                self.assertEqual(published["changes"]["deepseek"], {"lines_changed": 4, "files_changed": 2})

    def test_refusal_with_finding_blocks_publication(self):
        finding = {"id": "F-01", "severity": "MUST_FIX", "path": "base.txt", "line": 1,
                   "summary": "wrong", "fix": "fix"}
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}, approve=False, findings=[finding]),
        }
        orchestrator, state = self.run_with(self.case(max_rounds=1), fakes)
        self.assertEqual(state["phase"], "disagreement")
        self.assertFalse((orchestrator.run_dir / "REPORT.md").exists())
        self.assertTrue((orchestrator.run_dir / "SUMMARY.md").exists())
        self.assertIsNone(state.get("final"))

    def test_refusal_without_finding_fails_the_round(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}, approve=False),
        }
        with self.assertRaises(RuntimeError):
            self.run_with(self.case(max_rounds=1), fakes)

    def test_resume_finalizes_an_accepted_round(self):
        case = self.case(max_rounds=1)
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}),
        }
        with mock.patch.object(Orchestrator, "finalize", return_value="next"):
            _, state = self.run_with(case, fakes)
        self.assertTrue(state["history"][-1]["converged"])
        self.assertIsNone(state.get("final"))
        _, state = self.run_with(case, fakes, resume=True)
        self.assertEqual(state["phase"], "converged")
        self.assertEqual(state["final"]["round"], 1, "the accepted round is published, not re-run")
        self.assertEqual(sum(fake.calls for fake in fakes.values()), 2)

    def test_a_review_left_by_an_interrupted_run_is_not_accepted(self):
        """Resuming after a run died mid-round restarts that round: an old review is not a result."""
        stale = self.tmp / "run" / "reviews" / "r1" / "kimi-review.json"
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({
            "round": 1, "agent": "kimi", "approve": {"deepseek": True, "kimi": True},
            "findings": []}), encoding="utf-8")
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": FlakyFake("kimi", lambda r: {"shared.txt": "same\n"}, skip_rounds={1}),
        }
        with self.assertRaises(RuntimeError):
            self.run_with(self.case(max_rounds=1), fakes)

    def test_solve_bootstraps_both_agents_from_base(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"task.txt": "impl\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"task.txt": "impl\n"}),
        }
        case = self.case(bootstrap="base", max_rounds=1)
        orchestrator, state = self.run_with(case, fakes)
        self.assertEqual(state["history"][0]["phase"], "implement")
        self.assertEqual(state["history"][0]["verdict"], "independent", "an unreviewed round is never accepted")
        self.assertIsNone(state.get("final"))
        for name in ("deepseek", "kimi"):
            self.assertEqual(orchestrator.exchange.sha(f"candidate/{name}/r0"), self.base)

    def test_missing_review_is_retried_before_the_round_fails(self):
        deepseek = NoReviewFirstFake("deepseek", lambda r: {"shared.txt": "deepseek\n"})
        kimi = StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"})
        _, state = self.run_with(self.case(max_rounds=1, retries=1), {"deepseek": deepseek, "kimi": kimi})
        self.assertEqual(state["phase"], "converged")
        log = (self.tmp / "run" / "run.log").read_text(encoding="utf-8")
        self.assertIn("retry 1 for: deepseek", log, "a missing review is retried, not a failed round")

    def test_resume_after_a_failed_round_uses_the_last_results(self):
        first = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": FlakyFake("kimi", lambda r: {"shared.txt": "kimi\n"}, skip_rounds=(1,)),
        }
        with self.assertRaises(RuntimeError):
            self.run_with(self.case(max_rounds=2), first)

        second = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "same\n"}),
        }

        def fake_build(spec, log_path, title_prefix=""):
            return second[spec.name]

        with mock.patch("cav.agents.build", side_effect=fake_build):
            orchestrator = Orchestrator(self.case(max_rounds=2), self.tmp / "run", resume=True)
            state = orchestrator.run()

        self.assertEqual(state["phase"], "converged")
        self.assertTrue(state["history"][0].get("failed"), "the failed round stays in the history")
        self.assertEqual(state["history"][-1]["round"], 2, "resume starts after the failed round")


class IsolationTest(Base):
    def test_two_runs_do_not_share_a_push_target(self):
        """A repo-global remote would let a second run repoint the first run's pushes."""
        case = self.case(max_rounds=1)
        first = Orchestrator(case, self.tmp / "run-one")
        second = Orchestrator(case, self.tmp / "run-two")
        first.bootstrap()
        second.bootstrap()
        self.assertEqual(git("remote", cwd=self.repo, check=False), "")
        self.assertTrue(first.exchange.sha("candidate/deepseek/r0"))
        self.assertTrue(second.exchange.sha("candidate/deepseek/r0"))
        # pushing through the first run must not appear in the second exchange
        sha = first.exchange.sha("candidate/deepseek/r0")
        first.repo.push(sha, "candidate/probe/r1")
        self.assertTrue(first.exchange.sha("candidate/probe/r1"))
        self.assertIsNone(second.exchange.sha("candidate/probe/r1"))

    def test_each_agent_receives_the_other_candidate_as_a_patch(self):
        (self.repo / "kimi.txt").write_bytes(b"kimi work\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi work", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "-b", "deepseek-start", self.base, cwd=self.repo)
        (self.repo / "deepseek.txt").write_bytes(b"deepseek work\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "deepseek work", cwd=self.repo)
        deepseek_ref = git("rev-parse", "HEAD", cwd=self.repo)
        case = self.case(max_rounds=1)
        case.agents["deepseek"].bootstrap_ref = deepseek_ref
        case.agents["kimi"].bootstrap_ref = kimi_ref
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "same\n"}),
        }
        orchestrator, _ = self.run_with(case, fakes)
        rounds = orchestrator.run_dir / "rounds" / "r1"
        self.assertIn("kimi.txt", (rounds / "deepseek-other.patch").read_text(encoding="utf-8"))
        self.assertIn("deepseek.txt", (rounds / "kimi-other.patch").read_text(encoding="utf-8"))
        prompt = (rounds / "deepseek-context.md").read_text(encoding="utf-8")
        self.assertIn(str(rounds / "candidates" / "kimi"), prompt)
        self.assertNotIn("baseline", prompt.lower())


class UnitTest(Base):
    def test_oscillation_detection(self):
        orchestrator = Orchestrator(self.case(), self.tmp / "osc")
        orchestrator.state["history"] = [
            {"round": 1, "results": {"deepseek": {"tree": "a"}, "kimi": {"tree": "b"}}},
            {"round": 2, "results": {"deepseek": {"tree": "c"}, "kimi": {"tree": "d"}}},
        ]
        self.assertFalse(orchestrator.oscillating(2))
        orchestrator.state["history"].append(
            {"round": 3, "results": {"deepseek": {"tree": "a"}, "kimi": {"tree": "b"}}})
        self.assertTrue(orchestrator.oscillating(3))

    def test_each_agent_starts_from_its_own_candidate_and_writes_one_proposal(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        case = self.case(max_rounds=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "same\n"}),
        }
        orchestrator, state = self.run_with(case, fakes)
        self.assertEqual(state["history"][0]["starts"], {"deepseek": self.base, "kimi": kimi_ref})
        for name in ("deepseek", "kimi"):
            prompt = (orchestrator.run_dir / "rounds" / "r1" / f"{name}-context.md").read_text(encoding="utf-8")
            self.assertIn("Judge each against the task only", prompt)
            self.assertNotIn("yours", prompt, "candidates are named by agent, not by ownership")

    def test_removed_settings_are_rejected(self):
        for key, value in (("canonical_agent", "deepseek"), ("carry_forward_limit", 20),
                           ("convergence", "strict"), ("strict_paths", []), ("formatter", "x")):
            path = self.tmp / f"{key}.json"
            path.write_text(json.dumps({
                "name": "x", "repo": {"host": str(self.repo), "base": self.base},
                "agents": {"deepseek": {"kind": "opencode", "bootstrap_ref": self.base},
                           "kimi": {"kind": "kimi", "bootstrap_ref": self.base}},
                key: value}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, key):
                config.load(path)

    def test_acceptance_rule(self):
        """Both approve both starting candidates and those passed the checks; an independent
        round is never accepted."""
        orchestrator = Orchestrator(self.case(), self.tmp / "rule")

        def verdict(phase="reconcile", checks=True, **approves):
            results = {name: {"blocking": 0, "approves": {"deepseek": True, "kimi": True}}
                       for name in ("deepseek", "kimi")}
            results["kimi"]["approves"].update(approves)
            start_checks = {name: {"ok": checks} for name in results}
            return orchestrator.blockers(results, start_checks, phase)["verdict"]

        self.assertEqual(verdict(), "accepted", "different trees are fine when both approve")
        self.assertEqual(verdict(checks=False), "diverged")
        self.assertEqual(verdict(deepseek=False), "diverged")
        self.assertEqual(verdict(kimi=False), "diverged", "approving your own candidate is not enough")
        self.assertEqual(verdict(phase="implement"), "independent", "nothing was reviewed in round 1")

    def test_task_prompt_and_file_are_exclusive(self):
        path = self.tmp / "case.json"
        path.write_text(json.dumps({
            "name": "x", "repo": {"host": str(self.repo), "base": self.base},
            "agents": {"deepseek": {"kind": "opencode", "bootstrap_ref": self.base},
                       "kimi": {"kind": "kimi", "bootstrap_ref": self.base}},
            "task": {"prompt": "a", "prompt_file": "b.md"},
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load(path)


class EventsTest(unittest.TestCase):
    def test_order_redaction_and_clip(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            writer = events.EventWriter(tmp / "events.jsonl", "run")
            writer.emit("one", 1, "kimi", {"text": "x" * 9000})
            writer.emit("two", 1, "kimi", {"header": "Authorization: Bearer abcdefghijklmnop"})
            writer.emit("three", 1, "kimi", {"token": "abc123",
                                             "nested": {"password": "hunter2", "note": "ok"}})
            payload, _ = events.tail(tmp / "events.jsonl")
            self.assertEqual([item["seq"] for item in payload], [1, 2, 3])
            self.assertIn("truncated", payload[0]["data"]["text"])
            self.assertIn("[redacted]", payload[1]["data"]["header"])
            self.assertEqual(payload[2]["data"]["token"], "[redacted]")
            self.assertEqual(payload[2]["data"]["nested"]["password"], "[redacted]")
            self.assertEqual(payload[2]["data"]["nested"]["note"], "ok")
            resumed = events.EventWriter(tmp / "events.jsonl", "run")
            self.assertEqual(resumed.emit("four")["seq"], 4, "a resumed run continues the sequence")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_agent_messages_are_not_truncated(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            writer = events.EventWriter(tmp / "events.jsonl", "run")
            writer.emit("agent.text", 1, "kimi", {"text": "x" * 9000})
            payload, _ = events.tail(tmp / "events.jsonl")
            self.assertEqual(len(payload[0]["data"]["text"]), 9000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tail_reads_only_new_complete_lines(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            path = tmp / "events.jsonl"
            events.EventWriter(path, "run").emit("one", 1, None, {})
            found, offset = events.tail(path)
            self.assertEqual([item["type"] for item in found], ["one"])
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write('{"seq": 2, "type": "half')
            found, after = events.tail(path, offset)
            self.assertEqual((found, after), ([], offset), "a partial line waits for the next call")
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write('"}\n')
            found, after = events.tail(path, offset)
            self.assertEqual([item["type"] for item in found], ["half"])
            path.write_text("", encoding="utf-8")
            self.assertEqual(events.tail(path, after), ([], 0), "a replaced log starts over")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class KimiBlockingTest(unittest.TestCase):
    def agent(self):
        return agent_mod.KimiAgent(config.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro"),
                                   "log.txt")

    def test_pending_approval_wins_over_busy(self):
        """Kimi reports busy=True while waiting for approval; the block must still be visible."""
        agent = self.agent()
        self.assertEqual(agent._pending({"busy": True, "main_turn_active": True,
                                         "pending_interaction": "approval"}), "waiting:approval")
        self.assertIsNone(agent._pending({"busy": True, "pending_interaction": "none"}))
        self.assertIsNone(agent._pending({}))

    def test_permission_mismatch_fails_fast(self):
        agent = self.agent()
        agent.session_id = "s"
        agent._call = lambda *args, **kwargs: {"data": {"permission": "ask"}}
        with self.assertRaises(RuntimeError):
            agent._verify_permission("yolo")
        agent._call = lambda *args, **kwargs: {"data": {"permission": "yolo"}}
        agent._verify_permission("yolo")
        agent._call = lambda *args, **kwargs: {"data": {}}
        agent._verify_permission("yolo")

    def fake_api(self, agent, responses):
        calls = []

        def call(method, path, body=None, timeout=60):
            calls.append((method, path, body))
            return next(value for key, value in responses.items() if path.endswith(key))
        agent.session_id = "s"
        agent._call = call
        return calls

    def test_approval_is_answered_for_this_session_only(self):
        """Kimi's dangerous-command guard asks even in yolo mode; a CAV session approves it."""
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/snapshot": {"data": {"pending_approvals": [
                {"approval_id": "a1", "tool_name": "Bash", "action": "Running: rm -rf junk"}]}},
            "/interactions/a1": {"data": {"outcome": "approved"}}})
        self.assertEqual(agent._approve_pending(), 1)
        self.assertEqual(calls[-1], ("POST", "/api/v1/sessions/s/interactions/a1",
                                     {"kind": "approval", "decision": "approved", "scope": "session"}))

    def test_question_is_dismissed_so_the_agent_goes_on(self):
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/snapshot": {"data": {"pending_questions": [
                {"question_id": "q1", "questions": [{"question": "Which colour?"}]}]}},
            "/interactions/q1": {"data": {"outcome": "dismissed"}}})
        self.assertEqual(agent._dismiss_questions(), 1)
        self.assertEqual(calls[-1], ("POST", "/api/v1/sessions/s/interactions/q1",
                                     {"kind": "question", "dismissed": True}))

    def test_ponytail_is_activated_at_the_cav_level(self):
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/skills": {"data": {"skills": [{"name": agent_mod.PONYTAIL_SKILL}]}},
            ":activate": {"data": {"activated": True}},
            "/sessions/s": {"data": {"busy": False, "last_turn_reason": "completed"}}})
        agent._activate_ponytail()
        self.assertIn(("POST", f"/api/v1/sessions/s/skills/{agent_mod.PONYTAIL_SKILL}:activate",
                       {"args": agent_mod.PONYTAIL_LEVEL}), calls)

    def test_missing_ponytail_fails_instead_of_running_without_it(self):
        agent = self.agent()
        self.fake_api(agent, {"/skills": {"data": {"skills": [{"name": "other"}]}}})
        with self.assertRaisesRegex(RuntimeError, "ponytail"):
            agent._activate_ponytail()

    def test_opencode_runs_ponytail_at_the_cav_level(self):
        agent = agent_mod.OpenCodeAgent(config.AgentSpec("deepseek", "opencode"), "log.txt")
        self.assertEqual(agent._env()["PONYTAIL_DEFAULT_MODE"], agent_mod.PONYTAIL_LEVEL)

    def test_opencode_session_directory_is_the_worktree(self):
        """OpenCode reads the session directory from PWD; an inherited one points elsewhere."""
        agent = agent_mod.OpenCodeAgent(config.AgentSpec("deepseek", "opencode"), "log.txt")
        with mock.patch.dict(os.environ, {"PWD": "C:/somewhere/else"}):
            self.assertEqual(agent._env("C:/run/wt/r1/deepseek")["PWD"], "C:/run/wt/r1/deepseek")


class AdapterCaptureTest(unittest.TestCase):
    def test_kimi_capture_emits_an_updated_part_once(self):
        with tempfile.TemporaryDirectory(prefix="cav-capture-") as tmp:
            emitted = []
            agent = agent_mod.KimiAgent(config.AgentSpec("kimi", "kimi"), pathlib.Path(tmp) / "kimi.log",
                                        emit=lambda kind, name, data: emitted.append((kind, data)))
            message = {"id": "m", "role": "assistant",
                       "content": [{"id": "p", "type": "text", "text": "hello"}]}
            agent._capture([message])
            agent._capture([message])
            message["content"][0]["text"] = "hello, updated"
            agent._capture([message])
            agent._transcript.close()
            texts = [data["text"] for kind, data in emitted if kind == "agent.text"]
            self.assertEqual(texts, ["hello", "hello, updated"])
            self.assertEqual(len((pathlib.Path(tmp) / "kimi.log").read_text().splitlines()), 2)


class DashboardTest(unittest.TestCase):
    def test_endpoints_are_read_only_and_tokenised(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-web-"))
        try:
            (tmp / "state.json").write_text(json.dumps({"case": "x", "phase": "init"}), encoding="utf-8")
            (tmp / "events.jsonl").write_text(
                json.dumps({"seq": 1, "type": "run.start", "data": {}}) + "\n", encoding="utf-8")
            dashboard = Dashboard(tmp, port=0)
            server = dashboard.make_server()
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}/{dashboard.token}"
                with urllib.request.urlopen(base + "/api/state", timeout=5) as response:
                    self.assertEqual(json.loads(response.read())["case"], "x")
                with urllib.request.urlopen(base + "/api/divergence?round=1", timeout=5) as response:
                    self.assertEqual(json.loads(response.read()), {}, "a round not yet compared is empty")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/wrong/api/state", timeout=5)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/api/diff?round=1&path=../secret", timeout=5)
                self.assertEqual(caught.exception.code, 400, "a path outside the tree is refused")
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
