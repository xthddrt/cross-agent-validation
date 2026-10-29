"""Tree comparison, review validation, acceptance, publication, events and the dashboard."""

import json
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
REVIEW_JSON_RE = re.compile(r"review as JSON to this exact path: (.+)")
LEDGER_RE = re.compile(r"^- (CF-\d+)\s", re.M)


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
    """Writes a valid JSON review and the files it claims to have changed.

    Unless `dispositions` is given, it keeps every ledger item its prompt lists, as an agent
    that decides on each one would.
    """

    def __init__(self, name, files, approve=True, findings=None, dispositions=None):
        self.name = name
        self.files = files
        self.approve = approve
        self.findings = findings or []
        self.dispositions = dispositions
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
        data = {
            "round": round_no,
            "agent": self.name,
            "behavior_approved": self.approve,
            "other_behavior_approved": self.approve,
            "findings": list(self.findings),
            "areas": [],
            "changes": [{"path": name, "why": "test"} for name in files],
            "incorporated_from_other": [],
            "carry_forward_dispositions": (
                self.dispositions if self.dispositions is not None else
                [{"item_id": item, "status": "retained", "why": "test"} for item in LEDGER_RE.findall(prompt)]),
            "remaining_disagreements": [],
        }
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


class DispositionRetryFake(StructuredFake):
    """Omits carry-forward dispositions on its first attempt."""

    def run(self, prompt, cwd):
        self.dispositions = ([] if self.calls == 0 else
                             [{"item_id": "CF-01", "status": "retained", "why": "test"}])
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

    def test_ledger_lists_every_differing_path_largest_first(self):
        left = self.commit("small.txt", "a\n", "left")
        (self.repo / "big.txt").write_bytes(b"1\n2\n3\n")
        right = self.commit("small.txt", "b\n", "right")
        items = compare.ledger(compare.compare_trees(self.repo / ".git", *self.trees(left, right)))
        self.assertEqual([(item["id"], item["path"]) for item in items],
                         [("CF-01", "big.txt"), ("CF-02", "small.txt")])


class ReviewValidationTest(Base):
    def data(self, **overrides):
        base = {
            "round": 1, "agent": "deepseek", "counts": {
                "must_fix_total": 0, "blocking_must_fix": 0, "should_fix": 0, "nit": 0},
            "behavior_approved": True, "other_behavior_approved": True,
            "findings": [], "areas": [], "changes": [], "remaining_disagreements": [],
        }
        base.update(overrides)
        return base

    def test_clean_review_is_valid(self):
        self.assertEqual(review.validate(self.data(), "deepseek", 1, self.repo), [])

    def test_counts_are_recomputed_from_findings(self):
        data = self.data(counts={"must_fix_total": 9, "blocking_must_fix": 9,
                                 "should_fix": 9, "nit": 9})
        self.assertEqual(review.validate(data, "deepseek", 1, self.repo), [])
        self.assertEqual(data["counts"], {"must_fix_total": 0, "blocking_must_fix": 0,
                                          "should_fix": 0, "nit": 0})

    def test_blocking_count_is_derived_from_findings(self):
        data = self.data(
            counts={"must_fix_total": 0, "blocking_must_fix": 0, "should_fix": 0, "nit": 0},
            behavior_approved=False, other_behavior_approved=False,
            findings=[{"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral",
                       "path": "base.txt", "line": 1}])
        self.assertEqual(review.validate(data, "deepseek", 1, self.repo), [])
        self.assertEqual(data["counts"]["blocking_must_fix"], 1)
        self.assertEqual(data["counts"]["must_fix_total"], 1)

    def test_prompt_states_the_review_rules(self):
        text = prompts.review_rules("a.json", ["CF-01"])
        self.assertIn("blocks agreement", text)
        self.assertIn('no "behavioral"', text)
        self.assertIn('"equivalent"', text)
        self.assertIn("CF-01", text)
        self.assertNotIn('"counts"', text, "agents are not asked for numbers the orchestrator derives")

    def test_equivalent_kind_is_allowed_for_remaining_differences(self):
        data = self.data(remaining_disagreements=[
            {"area": "observer summary envelope", "kind": "equivalent", "note": "both behave the same"}])
        self.assertEqual(review.validate(data, "deepseek", 1, self.repo), [])
        data = self.data(remaining_disagreements=[{"area": "x", "kind": "equivalent"}])
        data["behavior_approved"] = True
        self.assertEqual(review.validate(data, "deepseek", 1, self.repo), [])

    def test_cited_line_must_exist(self):
        data = self.data(findings=[{"id": "F-01", "severity": "SHOULD_FIX", "kind": "cosmetic",
                                    "path": "base.txt", "line": 99}],
                         counts={"must_fix_total": 0, "blocking_must_fix": 0,
                                 "should_fix": 1, "nit": 0})
        errors = review.validate(data, "deepseek", 1, self.repo)
        self.assertTrue(any("outside base.txt" in error for error in errors))

    def test_refusal_without_finding_is_invalid(self):
        data = self.data(behavior_approved=False)
        errors = review.validate(data, "deepseek", 1, self.repo)
        self.assertTrue(any("without a blocking finding" in error for error in errors))

    def test_cosmetic_must_fix_does_not_block(self):
        data = self.data(findings=[{"id": "F-01", "severity": "MUST_FIX", "kind": "cosmetic",
                                    "path": "base.txt", "line": 1}],
                         counts={"must_fix_total": 1, "blocking_must_fix": 0,
                                 "should_fix": 0, "nit": 0})
        result = review.Review("deepseek", 1, self.tmp / "r.json", data,
                               review.validate(data, "deepseek", 1, self.repo))
        self.assertTrue(result.valid)
        self.assertEqual(result.blocking, 0)

    def test_missing_disposition_is_invalid(self):
        errors = review.validate(self.data(), "deepseek", 1, self.repo,
                                 required_dispositions=["CF-01"])
        self.assertTrue(any("carry-forward" in error for error in errors))

    def test_disposition_needs_a_reason(self):
        for why in (None, "  ", 3):
            with self.subTest(why=why):
                entry = {"item_id": "CF-01", "status": "intentionally_dropped"}
                if why is not None:
                    entry["why"] = why
                data = self.data(carry_forward_dispositions=[entry])
                errors = review.validate(data, "deepseek", 1, self.repo,
                                         required_dispositions=["CF-01"])
                self.assertTrue(any("needs a why" in error for error in errors), errors)

    def test_disposition_status_is_checked_without_a_ledger(self):
        data = self.data(carry_forward_dispositions=[
            {"item_id": "CF-01", "status": "ignored", "why": "x"}])
        errors = review.validate(data, "deepseek", 1, self.repo)
        self.assertTrue(any("invalid disposition" in error for error in errors), errors)

    def test_finding_without_a_line_is_invalid(self):
        data = self.data(findings=[{"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral",
                                    "path": "base.txt", "summary": "wrong"}],
                         counts={"must_fix_total": 1, "blocking_must_fix": 1,
                                 "should_fix": 0, "nit": 0},
                         behavior_approved=False, other_behavior_approved=False)
        errors = review.validate(data, "deepseek", 1, self.repo)
        self.assertTrue(any("line missing" in error for error in errors), errors)

    def test_malformed_review_fields_are_rejected_not_crashed(self):
        cases = {
            "findings": {"findings": ["F-01"]},
            "changes": {"changes": ["cav/cli.py"]},
            "remaining_disagreements": {"remaining_disagreements": ["review.py"]},
            "carry_forward_dispositions": {"carry_forward_dispositions": ["CF-01"]},
            "areas": {"areas": ["review"]},
            "behavior_approved": {"behavior_approved": "true"},
            "unhashable id": {"findings": [{"id": ["F-01"], "severity": "NIT", "kind": "cosmetic",
                                            "path": "base.txt", "line": 1}],
                              "counts": {"must_fix_total": 0, "blocking_must_fix": 0,
                                         "should_fix": 0, "nit": 1}},
            "path is not a string": {"findings": [{"id": "F-01", "severity": "NIT",
                                                   "kind": "cosmetic", "path": 3, "line": 1}],
                                     "counts": {"must_fix_total": 0, "blocking_must_fix": 0,
                                                "should_fix": 0, "nit": 1}},
        }
        for label, overrides in cases.items():
            with self.subTest(field=label):
                errors = review.validate(self.data(**overrides), "deepseek", 1, self.repo)
                self.assertTrue(errors, f"{label} should be rejected")


class AcceptanceTest(Base):
    def test_mutual_approval_accepts_different_trees(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}),
        }
        orchestrator, state = self.run_with(self.case(), fakes)
        self.assertEqual(state["phase"], "converged")
        self.assertEqual(state["history"][-1]["verdict"], "accepted")
        published = state["final"]["published_from"]["agent"]
        self.assertEqual(state["final"]["tree"], state["history"][-1]["results"][published]["tree"],
                         "acceptance publishes one of the two approved trees unchanged")
        self.assertIn("shared.txt", (orchestrator.run_dir / "REPORT.md").read_text(encoding="utf-8"))
        for gone in ("WAIVED.md", "TASK.md", "signoff", "final"):
            self.assertFalse((orchestrator.run_dir / gone).exists(), gone)

    def test_approved_candidate_is_published_without_another_agent_round(self):
        """Nothing is rewritten after agreement, so there is nothing to re-check or re-approve."""
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "value   \n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "value\n"}),
        }
        _, state = self.run_with(self.case(max_rounds=1), fakes)
        published = state["final"]["published_from"]["agent"]
        self.assertEqual(state["final"]["commit"], state["history"][-1]["results"][published]["commit"])
        self.assertEqual(sum(fake.calls for fake in fakes.values()), 2, "one agent run each, no confirmation")

    def test_publication_prefers_the_candidate_closest_to_its_original(self):
        """Neither agent is a default: the one that needed fewer changes is published, whichever
        is listed first."""
        for first in ("deepseek", "kimi"):
            with self.subTest(listed_first=first):
                self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-v2-"))
                self.repo, self.base = make_repo(self.tmp)
                fakes = {
                    "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "a\nb\nc\n",
                                                                       "more.txt": "x\n"}),
                    "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "a\n"}),
                }
                case = self.case(max_rounds=1)
                order = [first] + [name for name in case.agents if name != first]
                case.agents = {name: case.agents[name] for name in order}
                _, state = self.run_with(case, fakes)
                published = state["final"]["published_from"]
                self.assertEqual(published["agent"], "kimi")
                self.assertEqual(published["changes"]["kimi"], {"lines_changed": 1, "files_changed": 1})
                self.assertEqual(published["changes"]["deepseek"], {"lines_changed": 4, "files_changed": 2})
                self.assertIn("fewest changes", published["reason"])

    def test_refusal_with_finding_blocks_publication(self):
        finding = {"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral",
                   "path": "shared.txt", "line": 1, "summary": "wrong", "resolution": "fix"}
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"},
                                   approve=False, findings=[finding]),
        }
        orchestrator, state = self.run_with(self.case(max_rounds=1), fakes)
        self.assertEqual(state["phase"], "disagreement")
        self.assertFalse((orchestrator.run_dir / "REPORT.md").exists())
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
            "round": 1, "agent": "kimi", "behavior_approved": True, "other_behavior_approved": True,
            "counts": {"must_fix_total": 0, "blocking_must_fix": 0, "should_fix": 0, "nit": 0},
            "findings": []}), encoding="utf-8")
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": FlakyFake("kimi", lambda r: {"shared.txt": "same\n"}, skip_rounds={1}),
        }
        with self.assertRaises(RuntimeError):
            self.run_with(self.case(max_rounds=1), fakes)

    def test_retained_carry_forward_items_are_not_reported_as_dropped(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        deepseek = StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"},
                                  dispositions=[{"item_id": "CF-01", "status": "retained", "why": "kept"}])
        kimi = StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"})
        case = self.case(max_rounds=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        orchestrator, state = self.run_with(case, {"deepseek": deepseek, "kimi": kimi})
        self.assertEqual(state["phase"], "converged")
        self.assertNotIn("declined", (orchestrator.run_dir / "REPORT.md").read_text(encoding="utf-8"))

    def test_dropped_items_are_reported_for_each_agent(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        drop = [{"item_id": "CF-01", "status": "intentionally_dropped", "why": "mine is better"}]
        deepseek = StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}, dispositions=drop)
        kimi = StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}, dispositions=drop)
        case = self.case(max_rounds=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        orchestrator, _ = self.run_with(case, {"deepseek": deepseek, "kimi": kimi})
        report = (orchestrator.run_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("kimi's changes deepseek declined", report)
        self.assertIn("deepseek's changes kimi declined", report)
        self.assertIn("`shared.txt`: mine is better", report)

    def test_solve_bootstraps_both_agents_from_base(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"task.txt": "impl\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"task.txt": "impl\n"}),
        }
        case = self.case(bootstrap="base", max_rounds=1)
        orchestrator, state = self.run_with(case, fakes)
        self.assertEqual(state["history"][0]["phase"], "implement")
        for name in ("deepseek", "kimi"):
            self.assertEqual(orchestrator.exchange.sha(f"candidate/{name}/r0"), self.base)

    def test_carry_forward_requires_dispositions(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        finding = {"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral",
                   "path": "shared.txt", "line": 1}
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"},
                                       dispositions=[]),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}, approve=False,
                                   findings=[finding]),
        }
        case = self.case(max_rounds=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        with self.assertRaises(RuntimeError):
            self.run_with(case, fakes)

    def test_carry_forward_disposition_is_retried_before_the_round_fails(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        # deepseek leaves its decision out on the first attempt and is retried.
        deepseek = DispositionRetryFake("deepseek", lambda r: {"shared.txt": "deepseek\n"})
        kimi = StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"})
        case = self.case(max_rounds=1, retries=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        _, state = self.run_with(case, {"deepseek": deepseek, "kimi": kimi})
        self.assertEqual(state["phase"], "converged")
        log = (self.tmp / "run" / "run.log").read_text(encoding="utf-8")
        self.assertIn("retry 1 for: deepseek", log,
                      "a missing disposition must be retried, not fail the round")

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
        self.assertIn("equal standing", prompt)
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

    def test_each_agent_starts_from_its_own_candidate_with_its_own_ledger(self):
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
        self.assertEqual(state["history"][0]["ledger"], [{"id": "CF-01", "path": "shared.txt"}])
        for name in ("deepseek", "kimi"):
            prompt = (orchestrator.run_dir / "rounds" / "r1" / f"{name}-context.md").read_text(encoding="utf-8")
            self.assertIn("CF-01  shared.txt", prompt, f"{name} must decide on the same path")

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
        """Checks pass, no behavioural MUST_FIX, both approve both, nothing behavioural left open."""
        orchestrator = Orchestrator(self.case(), self.tmp / "rule")

        def verdict(**change):
            results = {name: {"tree": name, "blocking": 0, "counts": {"must_fix_total": 0},
                              "approved": True, "other_approved": True, "open_behavioural": False,
                              "unexplained": []} for name in ("deepseek", "kimi")}
            results["kimi"].update(change)
            checks = {name: {"ok": change.get("checks", True)} for name in results}
            return orchestrator.blockers(results, checks)["verdict"]

        self.assertEqual(verdict(), "accepted", "different trees are fine when both approve")
        for change in ({"checks": False}, {"blocking": 1}, {"approved": False},
                       {"other_approved": False}, {"open_behavioural": True}):
            with self.subTest(**change):
                self.assertEqual(verdict(**change), "diverged")

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
