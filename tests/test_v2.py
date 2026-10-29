"""Tests for schema v2: divergence, safe settlement, structured reviews, functional acceptance."""

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

from cav import compare, config, events, review
from cav.agents import RunState
from cav.gitops import Repo, git
from cav.orchestrator import Orchestrator
from cav.web import Dashboard

ROUND_RE = re.compile(r"Round (\d+)")
REVIEW_MD_RE = re.compile(r"review to this exact path: (.+)")
REVIEW_JSON_RE = re.compile(r"structured review \(authoritative\) to: (.+)")
CONFIRM_MARK = "is being finalised"


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
    """Writes a valid JSON review and the files it claims to have changed."""

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
        md = pathlib.Path(REVIEW_MD_RE.search(prompt).group(1).strip())
        js = pathlib.Path(REVIEW_JSON_RE.search(prompt).group(1).strip())
        confirming = CONFIRM_MARK in prompt
        files = {} if confirming else self.files(round_no)
        for name, text in files.items():
            target = pathlib.Path(cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        findings = [] if confirming else list(self.findings)
        blocking = sum(1 for item in findings if item["kind"] == "behavioral")
        data = {
            "round": round_no,
            "agent": self.name,
            "counts": {"must_fix_total": len(findings), "blocking_must_fix": blocking,
                       "should_fix": 0, "nit": 0},
            "behavior_approved": True if confirming else self.approve,
            "other_behavior_approved": True if confirming else self.approve,
            "findings": findings,
            "areas": [],
            "changes": [{"path": name, "why": "test"} for name in files],
            "incorporated_from_other": [],
            "carry_forward_dispositions": self.dispositions or [],
            "remaining_disagreements": [],
        }
        js.parent.mkdir(parents=True, exist_ok=True)
        js.write_text(json.dumps(data, indent=2), encoding="utf-8")
        md.write_text(review.render(data), encoding="utf-8")
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
            checks=[], max_rounds=2, round_seconds=30, stall_seconds=5,
            poll_seconds=0.05, retries=0, version=2, convergence="functional",
            canonical_agent="deepseek",
        )
        values.update(overrides)
        return config.Case(**values)

    def run_with(self, case, fakes, **kwargs):
        def fake_build(spec, log_path, title_prefix=""):
            return fakes[spec.name]

        with mock.patch("cav.agents.build", side_effect=fake_build):
            orchestrator = Orchestrator(case, self.tmp / "run", **kwargs)
            return orchestrator, orchestrator.run()


class ClassificationTest(Base):
    def commit(self, name, text, message):
        (self.repo / name).write_bytes(text.encode())
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", message, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def trees(self, left, right):
        return (git("rev-parse", f"{left}^{{tree}}", cwd=self.repo),
                git("rev-parse", f"{right}^{{tree}}", cwd=self.repo))

    def test_classes(self):
        left = self.commit("a.c", "int a;\n// old\n", "left")
        right = self.commit("a.c", "int a;\n// new\n", "comment")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["comments_candidate"], ["a.c"])
        self.assertEqual(diff["totals"]["substantive"], 0)

        right = self.commit("a.c", "int a;   \r\n// old\n", "formatting")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["formatting_candidate"], ["a.c"])

        right = self.commit("a.c", "int b;\n// new\n", "behaviour")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["substantive"], ["a.c"])

    def test_python_comments_are_cosmetic_but_docstrings_are_content(self):
        left = self.commit("m.py", "x = 1\n# old note\n", "left")
        right = self.commit("m.py", "x = 1\n# new note\n", "py comment")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["comments_candidate"], ["m.py"])

        doc_a = 'def f():\n    """Doc.\n    # alpha\n    """\n    return 1\n'
        doc_b = doc_a.replace("# alpha", "# beta")
        left = self.commit("doc.py", doc_a, "docstring a")
        right = self.commit("doc.py", doc_b, "docstring b")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["substantive"], ["doc.py"],
                         "a `#` inside a docstring is content, not a comment")

    def test_a_hash_inside_a_nested_fstring_does_not_hide_a_change(self):
        """`f"{row["key#id"]}"` is one string in Python 3.12+: a `#` in it is not a comment start."""
        left = self.commit("f.py", 'label = f"{row["key#id"]}" + "alpha"\n', "left")
        right = self.commit("f.py", 'label = f"{row["key#id"]}" + "beta"\n', "right")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b)
        self.assertEqual(diff["files"]["substantive"], ["f.py"])

    def test_strict_paths_are_never_cosmetic(self):
        left = self.commit("x.h", "#define A 1\n", "left")
        right = self.commit("x.h", "#define  A 1   \n", "formatting")
        tree_a, tree_b = self.trees(left, right)
        diff = compare.compare_trees(self.repo / ".git", tree_a, tree_b, strict_paths=["**/*.h"])
        self.assertEqual(diff["files"]["substantive"], ["x.h"])

    def test_carry_forward_is_capped(self):
        left = self.commit("one.txt", "a\n", "left")
        right = self.commit("one.txt", "b\n", "right")
        (self.repo / "two.txt").write_bytes(b"b\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "two", cwd=self.repo)
        right = git("rev-parse", "HEAD", cwd=self.repo)
        tree_a, tree_b = self.trees(left, right)
        ledger = compare.carry_forward(self.repo / ".git", tree_b, tree_a, limit=1)
        self.assertEqual(len(ledger["items"]), 1)
        self.assertEqual(ledger["total"], 2)
        self.assertEqual(ledger["omitted"], 1)


class SettlementTest(Base):
    def test_normalizes_eol_and_trailing_space_only(self):
        worktree = self.tmp / "wt"
        worktree.mkdir()
        (worktree / "a.c").write_bytes(b"int a;   \r\n\r\n\r\n\r\nint b;\r\n")
        (worktree / "driver.h").write_bytes(b"int h;   \r\n")
        changed = compare.apply_safe_normalization(worktree, ["**/*.h"])
        self.assertIn("a.c", changed)
        self.assertNotIn("driver.h", changed)
        self.assertEqual((worktree / "a.c").read_bytes(), b"int a;\n\n\n\nint b;\n")
        self.assertEqual((worktree / "driver.h").read_bytes(), b"int h;   \r\n")

    def test_blank_lines_survive(self):
        worktree = self.tmp / "wt2"
        worktree.mkdir()
        (worktree / "a.yml").write_bytes(b"a: 1\n\n\n\n\nb: 2\n")
        compare.apply_safe_normalization(worktree, [])
        self.assertEqual((worktree / "a.yml").read_bytes(), b"a: 1\n\n\n\n\nb: 2\n")


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

    def test_counts_must_match_findings(self):
        data = self.data(counts={"must_fix_total": 2, "blocking_must_fix": 0,
                                 "should_fix": 0, "nit": 0})
        self.assertTrue(any("must_fix_total" in error
                            for error in review.validate(data, "deepseek", 1, self.repo)))

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
                               review.validate(data, "deepseek", 1, self.repo), False)
        self.assertTrue(result.valid)
        self.assertEqual(result.blocking, 0)

    def test_missing_disposition_is_invalid(self):
        errors = review.validate(self.data(), "deepseek", 1, self.repo,
                                 required_dispositions=["CF-01"])
        self.assertTrue(any("carry-forward" in error for error in errors))

    def test_finding_without_a_line_is_invalid(self):
        data = self.data(findings=[{"id": "F-01", "severity": "MUST_FIX", "kind": "behavioral",
                                    "path": "base.txt", "summary": "wrong"}],
                         counts={"must_fix_total": 1, "blocking_must_fix": 1,
                                 "should_fix": 0, "nit": 0},
                         behavior_approved=False, other_behavior_approved=False)
        errors = review.validate(data, "deepseek", 1, self.repo)
        self.assertTrue(any("no line" in error for error in errors), errors)

    def test_malformed_review_fields_are_rejected_not_crashed(self):
        cases = {
            "findings": {"findings": ["F-01"]},
            "changes": {"changes": ["cav/cli.py"]},
            "remaining_disagreements": {"remaining_disagreements": ["review.py"]},
            "carry_forward_dispositions": {"carry_forward_dispositions": ["CF-01"]},
            "areas": {"areas": ["review"]},
        }
        for label, overrides in cases.items():
            with self.subTest(field=label):
                errors = review.validate(self.data(**overrides), "deepseek", 1, self.repo)
                self.assertTrue(errors, f"{label} should be rejected")


class AcceptanceTest(Base):
    def test_functional_accepts_different_trees(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}),
        }
        orchestrator, state = self.run_with(self.case(), fakes)
        self.assertEqual(state["phase"], "converged")
        self.assertEqual(state["final"]["verdict"], "functional")
        self.assertIsNotNone((orchestrator.run_dir / "WAIVED.md").exists())
        self.assertTrue((orchestrator.run_dir / "reviews" / "r1-confirm" / "kimi-review.json").exists())
        self.assertTrue((orchestrator.run_dir / "signoff" / "deepseek.json").exists())
        final_tree = state["final"]["tree"]
        canonical_tree = state["history"][-1]["results"]["deepseek"]["tree"]
        self.assertEqual(final_tree, canonical_tree, "functional acceptance publishes canonical")
        self.assertIn("shared.txt", (orchestrator.run_dir / "WAIVED.md").read_text(encoding="utf-8"))

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
        self.assertFalse((orchestrator.run_dir / "signoff" / "kimi.json").exists())
        self.assertIsNone(state.get("final"))

    def test_refusal_without_finding_fails_the_round(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}, approve=False),
        }
        with self.assertRaises(RuntimeError):
            self.run_with(self.case(max_rounds=1), fakes)

    def test_strict_still_requires_identical_trees(self):
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "same\n"}),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "same\n"}),
        }
        orchestrator, state = self.run_with(self.case(convergence="strict", max_rounds=1), fakes)
        self.assertEqual(state["final"]["verdict"], "strict")

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
        case = self.case(max_rounds=1, carry_forward_limit=5)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        with self.assertRaises(RuntimeError):
            self.run_with(case, fakes)

    def test_carry_forward_disposition_is_retried_before_the_round_fails(self):
        (self.repo / "shared.txt").write_bytes(b"kimi candidate\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi candidate", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        # The baseline owner (deepseek) is the one that must dispose of the other candidate's work.
        deepseek = DispositionRetryFake("deepseek", lambda r: {"shared.txt": "deepseek\n"})
        kimi = StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"})
        case = self.case(max_rounds=1, carry_forward_limit=5, retries=1)
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

    def test_non_baseline_candidate_patch_is_not_empty(self):
        (self.repo / "extra.txt").write_bytes(b"kimi work\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "kimi work", cwd=self.repo)
        kimi_ref = git("rev-parse", "HEAD", cwd=self.repo)
        case = self.case(max_rounds=1)
        case.agents["kimi"].bootstrap_ref = kimi_ref
        fakes = {
            "deepseek": StructuredFake("deepseek", lambda r: {"shared.txt": "deepseek\n"},
                                       dispositions=[{"item_id": "CF-01", "status": "reoffered",
                                                      "why": "test"}]),
            "kimi": StructuredFake("kimi", lambda r: {"shared.txt": "kimi\n"}),
        }
        orchestrator, _ = self.run_with(case, fakes)
        patch = orchestrator.run_dir / "rounds" / "r1" / "other-kimi.patch"
        self.assertTrue(patch.exists())
        self.assertIn("extra.txt", patch.read_text(encoding="utf-8"))


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

    def test_baseline_prefers_the_only_passing_candidate(self):
        orchestrator = Orchestrator(self.case(), self.tmp / "base")
        previous = {
            "deepseek": {"commit": "aaa", "tree": "t1", "checks_ok": False},
            "kimi": {"commit": "bbb", "tree": "t2", "checks_ok": True},
        }
        commit, reason, owner = orchestrator.choose_baseline(previous)
        self.assertEqual(commit, "bbb")
        self.assertEqual(owner, "kimi")
        self.assertIn("passed", reason)

    def test_blank_task_basis(self):
        orchestrator = Orchestrator(self.case(), self.tmp / "basis")
        self.assertEqual(orchestrator.assurance_basis(), "checks + mutual review (no task prompt)")

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
            payload = events.read(tmp / "events.jsonl", 0)
            self.assertEqual([item["seq"] for item in payload], [1, 2])
            self.assertIn("truncated", payload[0]["data"]["text"])
            self.assertIn("[redacted]", payload[1]["data"]["header"])
            self.assertEqual(len(events.read(tmp / "events.jsonl", 1)), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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
                with urllib.request.urlopen(base + "/api/events?after=0", timeout=5) as response:
                    self.assertEqual(len(json.loads(response.read())), 1)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/wrong/api/state", timeout=5)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
