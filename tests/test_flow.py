"""The loop end to end with fake agents: judge or plan, agree, build (both), judge, publish."""

import json
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from cav.agents import RunState
from cav.flow import Flow
from cav.gitops import git, git_ok
from tests.test_agents import with_origin


class Fake:
    """Answers each step from what is on disk. `approve(folder)` judges a candidate, `approve_plan(text)`
    a plan; `prefer` and `keep_plan` pick one; `base(folders)` is the candidate a plan builds on."""

    def __init__(self, name, approve=lambda folder: True, prefer=lambda folders: "X", base=lambda folders: "X",
                 plan_text=None, approve_plan=lambda text: True, keep_plan=lambda texts: 1):
        self.name, self.state, self.session_id = name, RunState(), None
        self.approve, self.prefer, self.base = approve, prefer, base
        self.plan_text = plan_text or (lambda attempt: f"{name} plan: add feature.txt.")
        self.approve_plan, self.keep_plan = approve_plan, keep_plan
        self.prompts, self.sessions, self.plans, self.builds = [], 0, 0, 0

    def reset(self):
        self.state = RunState()

    def abort(self):
        pass

    def run(self, prompt, cwd):
        if not self.session_id:
            self.session_id, self.sessions = f"s-{self.name}-{self.sessions + 1}", self.sessions + 1
        self.prompts.append(prompt)
        if "1. Judge each candidate" in prompt:
            folders = dict(re.findall(r"- Candidate ([XY]): (\S+) \(checks", prompt))
            approve = {label: self.approve(pathlib.Path(folder)) for label, folder in folders.items()}
            defects = [{"candidate": label, "file": "base.txt", "line": 1, "problem": "wrong"}
                       for label, ok in approve.items() if not ok]
            reply = re.search(r"Write JSON to (\S+):", prompt).group(1)
            pathlib.Path(reply).write_text(json.dumps({"approve": approve, "defects": defects,
                                                       "prefer": self.prefer(folders)}), encoding="utf-8")
            self.write_plan(re.search(r"Write a plan to (\S+), at most", prompt).group(1),
                            f"Base: Candidate {self.base(folders)}\n")
        elif "PLANS (authors not shown)" in prompt:
            plans = {int(n): pathlib.Path(path).read_text(encoding="utf-8")
                     for n, path in re.findall(r"- Plan (\d): (\S+)", prompt)}
            approve = {str(n): self.approve_plan(text) for n, text in plans.items()}
            reply = re.search(r"Write JSON to (\S+):", prompt).group(1)
            pathlib.Path(reply).write_text(json.dumps({
                "approve": approve, "why": {n: "it misses a criterion" for n, ok in approve.items() if not ok},
                "prefer": self.keep_plan(plans)}), encoding="utf-8")
        elif "Write a plan to" in prompt:
            self.write_plan(re.search(r"Write a plan to (\S+), at most", prompt).group(1), "")
        elif prompt.startswith("Implement this plan"):
            self.builds += 1
            work = pathlib.Path(re.search(r", in (\S+)\. (?:It starts|Write)", prompt).group(1))
            (work / "feature.txt").write_text(f"built by {self.name}\n", encoding="utf-8")
            pathlib.Path(re.search(r"in a few lines, to (\S+)\. Change", prompt).group(1)).write_text("built",
                                                                                                    encoding="utf-8")
        self.state.finished = True
        return {"ok": True, "text": "done", "reason": "completed"}

    def write_plan(self, path, head):
        self.plans += 1
        pathlib.Path(path).write_text(head + self.plan_text(self.plans), encoding="utf-8")


def has(name):
    """A judge or base rule: the candidate whose folder holds this file."""
    return lambda folder: (pathlib.Path(folder) / name).exists()


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-flow-"))
        self.work, self.origin, self.base = with_origin(self.tmp)
        self.branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_flow(self, fakes, **options):
        with mock.patch("cav.flow.agent_mod.build", side_effect=lambda spec, log, title_prefix="": fakes[spec.name]):
            flow = Flow(str(self.work), "Add feature.txt.", self.tmp / "runs" / "r1", **options)
            code = flow.run()
        return code, flow

    def steps(self, flow):
        return [(item["step"], item.get("verdict")) for item in flow.state["history"]]

    def branches(self):
        shas = {}
        for name in ("feat-a", "feat-b"):
            git("checkout", "-q", "-b", name, self.base, cwd=self.work)
            (self.work / f"{name}.txt").write_text(name, encoding="utf-8")
            git("add", "-A", cwd=self.work)
            git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", f"note {name}", cwd=self.work)
            shas[name] = git("rev-parse", "HEAD", cwd=self.work)
        git("checkout", "-q", self.branch, cwd=self.work)
        return shas

    def label_of(self, flow, ref):
        return next(label for label, item in flow.state["labels"].items() if item["ref"] == ref)

    def assert_blind(self, fakes, flow, leaks=()):
        """No prompt names an agent or a branch or points into the run; nothing the agents can reach
        has git history or the run's bookkeeping."""
        for fake in fakes.values():
            for prompt in fake.prompts:
                for word in ("deepseek", "kimi", "yours", "your plan", str(flow.run_dir), *leaks):
                    self.assertNotIn(word, prompt)
        self.assertFalse(flow.blind.is_relative_to(flow.run_dir))
        folders = [flow.blind] + [path for path in flow.blind.rglob("*") if path.is_dir()]
        self.assertFalse([path for path in folders if git_ok("rev-parse", "--git-dir", cwd=path)],
                         "no git repo is reachable from any folder the agents use")
        self.assertFalse(list(flow.blind.rglob("state.json")))

    def test_task_plan_agree_build_judge_publish(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("plan", "planned"), ("agree", "agreed"), ("build", "built"),
                                            ("judge", "accepted")])
        self.assertTrue(flow.state["outcome"].startswith("merged and pushed"))
        self.assertTrue(git("--git-dir", str(self.origin), "show", f"{self.branch}:feature.txt").startswith("built by"))
        self.assertEqual([fake.builds for fake in fakes.values()], [1, 1], "both agents build")
        self.assertEqual([fake.sessions for fake in fakes.values()], [4, 4], "a fresh session for every step")
        self.assert_blind(fakes, flow)

    def test_a_branch_both_approve_is_the_result_at_once(self):
        shas = self.branches()
        fakes = {name: Fake(name, approve=has("feat-a.txt")) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, branches=["feat-a", "feat-b"], push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("judge", "accepted")])
        self.assertEqual(flow.state["final"]["commit"], shas["feat-a"])
        for label in flow.state["labels"]:
            self.assertFalse((flow.blind / "candidates" / "c1" / f"candidate-{label}" / ".git").exists())
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b", "note feat"))

    def test_approval_decides_not_preference(self):
        self.branches()
        fakes = {"deepseek": Fake("deepseek", approve=has("feat-a.txt"), prefer=lambda folders: "Y"),
                 "kimi": Fake("kimi", approve=lambda folder: True,
                              prefer=lambda folders: next(l for l, f in folders.items() if has("feat-b.txt")(f)))}
        code, flow = self.run_flow(fakes, branches=["feat-a", "feat-b"], push=False)
        self.assertEqual(code, 0, flow.state)
        label = self.label_of(flow, "feat-a")
        self.assertEqual(flow.state["final"]["candidate"], label, "both approve feat-a, though neither prefers it")
        self.assertEqual(flow.state["decided"], f"both approved Candidate {label}")

    def test_no_shared_approval_agrees_a_base_and_both_build_from_it(self):
        shas = self.branches()
        on_a = lambda folders: next(label for label, folder in folders.items() if has("feat-a.txt")(folder))
        fakes = {"deepseek": Fake("deepseek", approve=lambda f: has("feature.txt")(f) or has("feat-a.txt")(f), base=on_a),
                 "kimi": Fake("kimi", approve=lambda f: has("feature.txt")(f) or has("feat-b.txt")(f), base=on_a)}
        code, flow = self.run_flow(fakes, branches=["feat-a", "feat-b"], push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("judge", "rejected"), ("agree", "agreed"), ("build", "built"),
                                            ("judge", "accepted")])
        build = flow.state["history"][2]
        self.assertEqual(build["start"], shas["feat-a"], "both builds start from the agreed base")
        for commit in build["commits"].values():
            self.assertEqual(git("merge-base", commit, shas["feat-a"], cwd=self.work), shas["feat-a"])
        agreed = (flow.run_dir / "plans" / "c1-agreed.md").read_text(encoding="utf-8")
        self.assertIn("Defects found in the base", agreed, "the judges' defects against the base go to the builders")
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b"))

    def test_plans_without_both_approvals_go_to_the_plan_both_would_keep(self):
        keep = lambda texts: next(n for n, text in texts.items() if "deepseek plan" in text)
        fakes = {"deepseek": Fake("deepseek", approve_plan=lambda text: "deepseek" in text, keep_plan=keep),
                 "kimi": Fake("kimi", approve_plan=lambda text: "kimi" in text, keep_plan=keep)}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        agreed = (flow.run_dir / "plans" / "c1-agreed.md").read_text(encoding="utf-8")
        self.assertIn("deepseek plan", agreed)
        self.assertIn("Objections to this plan", agreed)
        self.assertIn("both would keep plan", flow.state["plan"]["how"])

    def test_a_split_on_plans_is_a_coin_flip(self):
        fakes = {"deepseek": Fake("deepseek", approve_plan=lambda text: False, keep_plan=lambda texts: 1),
                 "kimi": Fake("kimi", approve_plan=lambda text: False, keep_plan=lambda texts: 2)}
        with mock.patch("cav.flow.random.choice", side_effect=lambda items: 2 if tuple(items) == (1, 2) else items[0]) as flip:
            code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        flip.assert_any_call((1, 2))
        self.assertIn("coin flip chose plan 2", flow.state["plan"]["how"])

    def test_the_last_judge_keeps_the_result_both_would_keep(self):
        keep = lambda folders: next(label for label, folder in folders.items()
                                    if (pathlib.Path(folder) / "feature.txt").read_text() == "built by kimi\n")
        fakes = {name: Fake(name, approve=lambda folder: False, prefer=keep) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, push=False, max_builds=2)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["plan", "agree", "build", "judge", "agree", "build", "judge"])
        self.assertEqual(self.steps(flow)[-1], ("judge", "forced"))
        self.assertEqual(git("show", f"{flow.state['final']['commit']}:feature.txt", cwd=self.work), "built by kimi")
        self.assertEqual(len(flow.state["final"]["open_defects"]), 2, "each judge's defect against it stays open")

    def test_failing_checks_are_never_accepted_or_pushed(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, checks=['{python} -c "import sys; sys.exit(1)"'], max_builds=1)
        self.assertEqual(code, 2)
        self.assertEqual(flow.state["outcome"], "checks fail")
        self.assertEqual(self.steps(flow)[-1], ("judge", "forced"))
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base)

    def test_an_over_long_plan_is_sent_back_once(self):
        fakes = {"deepseek": Fake("deepseek", plan_text=lambda attempt: "word " * (550 if attempt == 1 else 50)),
                 "kimi": Fake("kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        retry = [p for p in fakes["deepseek"].prompts if "Write a plan to" in p][1]
        self.assertIn("550 words; cut it to 500", retry)

    def test_answers_are_checked(self):
        self.assertEqual(Flow.plan_problem("no base here", {"X": {}, "Y": {}}),
                         'the plan must start with "Base: Candidate X" or "Base: Candidate Y"')
        self.assertIsNone(Flow.plan_problem("**Base:** Candidate y\nplan", {"X": {}, "Y": {}}))
        reply = self.tmp / "reply.json"
        reply.write_text(json.dumps({"approve": {"1": False, "2": True}, "prefer": 2}), encoding="utf-8")
        self.assertEqual(Flow.agree_problem(reply), 'plan 1 is rejected without a "why"')


if __name__ == "__main__":
    unittest.main()
