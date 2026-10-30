"""The flow end to end with fake agents: judge or plan, agree rounds, one build each, judge, publish."""

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
    a plan; `prefer(folders)` is the candidate it would keep; `base(folders)` the one a plan builds on."""

    def __init__(self, name, approve=lambda folder: True, prefer=lambda folders: "X", base=lambda folders: "X",
                 plan_text=None, approve_plan=lambda text: True):
        self.name, self.state, self.session_id = name, RunState(), None
        self.approve, self.prefer, self.base, self.approve_plan = approve, prefer, base, approve_plan
        self.plan_text = plan_text or (lambda attempt: f"{name} plan {attempt}: add feature.txt.")
        self.prompts, self.sessions, self.plans, self.builds = [], 0, 0, 0

    def reset(self):
        self.state = RunState()

    def abort(self):
        pass

    def run(self, prompt, cwd):
        if not self.session_id:
            self.session_id, self.sessions = f"s-{self.name}-{self.sessions + 1}", self.sessions + 1
            self.folders = {}  # what this session has seen: a later prompt in the cycle does not repeat it
        self.prompts.append(prompt)
        reply = re.search(r"Write to (\S+):", prompt)
        if "Approve each candidate" in prompt:
            folders = self.folders = dict(re.findall(r"^- ([XY]): (\S+) \(checks", prompt, re.M))
            approve = {label: self.approve(pathlib.Path(folder)) for label, folder in folders.items()}
            defects = [{"candidate": label, "file": "base.txt", "line": 1, "problem": "wrong"}
                       for label, ok in approve.items() if not ok]
            pathlib.Path(reply.group(1)).write_text(json.dumps(
                {"approve": approve, "defects": defects, "prefer": self.prefer(folders)}), encoding="utf-8")
            plan = re.search(r"Then write a plan to (\S+) \(", prompt)
            if plan:
                self.write_plan(plan.group(1), f"Base: {self.base(folders)}\n")
        elif "Approve each plan" in prompt:
            plans = {n: pathlib.Path(path).read_text(encoding="utf-8")
                     for n, path in re.findall(r"^- (\d): (\S+)$", prompt, re.M)}
            approve = {n: self.approve_plan(text) for n, text in plans.items()}
            pathlib.Path(reply.group(1)).write_text(json.dumps(
                {"approve": approve, "why": {n: f"plan {n} misses a criterion" for n, ok in approve.items() if not ok}}),
                encoding="utf-8")
        elif "Write one plan from those two" in prompt:
            self.write_plan(re.search(r"can approve, to (\S+) \(", prompt).group(1),
                            f"Base: {self.base(self.folders)}\nrevised " if self.folders else "revised ")
        elif "Write a plan to" in prompt:
            self.write_plan(re.search(r"Write a plan to (\S+) \(", prompt).group(1), "")
        elif prompt.startswith("Implement "):
            self.builds += 1
            work = pathlib.Path(re.search(r"^Implement the agreed plan, \S+, in (\S+?)(?:, a copy of [XY])?\.$",
                                          prompt, re.M).group(1))
            (work / "feature.txt").write_text(f"built by {self.name}\n", encoding="utf-8")
            (work / f"build-{self.builds}.txt").write_text(self.name, encoding="utf-8")
            pathlib.Path(re.search(r"what you did to (\S+)\.$", prompt, re.M).group(1)).write_text("built", encoding="utf-8")
        self.state.finished = True
        return {"ok": True, "text": "done", "reason": "completed"}

    def write_plan(self, path, head):
        self.plans += 1
        pathlib.Path(path).write_text(head + self.plan_text(self.plans), encoding="utf-8")


def has(name):
    """A judge or base rule: the candidate whose folder holds this file."""
    return lambda folder: (pathlib.Path(folder) / name).exists()


def built_by(name):
    return lambda folder: (pathlib.Path(folder) / "feature.txt").exists() and \
        (pathlib.Path(folder) / "feature.txt").read_text(encoding="utf-8").strip() == f"built by {name}"


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
        self.assertEqual([fake.builds for fake in fakes.values()], [1, 1], "both agents build, once")
        self.assertEqual([fake.sessions for fake in fakes.values()], [2, 2], "one session per cycle: plan, agree and build share one; the judge of the builds starts fresh")
        self.assert_blind(fakes, flow)

    def test_a_branch_both_approve_is_the_result_at_once(self):
        shas = self.branches()
        fakes = {name: Fake(name, approve=has("feat-a.txt")) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, branches=["feat-a", "feat-b"], push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("judge", "accepted")])
        self.assertEqual(flow.state["final"]["commit"], shas["feat-a"])
        self.assertNotIn("prefer", fakes["kimi"].prompts[0], "the branches' judge plans; it is not asked to pick")
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b", "note feat"))

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
        self.assertIn("Defects found in the base", (flow.run_dir / "plans" / "c1-agreed.md").read_text(encoding="utf-8"))
        vote = next(p for p in fakes["kimi"].prompts if "Approve each plan" in p)
        ballots = [pathlib.Path(path) for path in re.findall(r"^- \d: (\S+)$", vote, re.M)]
        self.assertTrue(all("Defects found in the base" in path.read_text(encoding="utf-8") for path in ballots),
                        "the voters see each plan as the builders will get it")
        self.assertIn("REVIEWS", vote, "the first vote lists both reviews; each judge has written only one")
        for fake in fakes.values():
            self.assertEqual(sum(p.startswith("TASK") for p in fake.prompts), fake.sessions,
                             "the task is given once per session, in the cycle's first prompt")
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b"))

    def test_rejected_plans_are_revised_until_both_approve(self):
        fakes = {name: Fake(name, approve_plan=lambda text: "revised" in text) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("plan", "planned"), ("agree", "rejected"), ("revise", "revised"),
                                            ("agree", "agreed"), ("build", "built"), ("judge", "accepted")])
        revise = next(p for p in fakes["deepseek"].prompts if "Write one plan from those two" in p)
        self.assertIn("OBJECTIONS", revise)
        self.assertIn("misses a criterion", revise, "the revisers read why each plan was rejected")
        self.assertIn("round 2", flow.state["plan"]["how"])

    def test_no_plan_with_both_approvals_after_the_last_round_is_a_coin_flip(self):
        fakes = {name: Fake(name, approve_plan=lambda text: False) for name in ("deepseek", "kimi")}
        with mock.patch("cav.flow.random.choice", side_effect=lambda items: 2 if tuple(items) == (1, 2) else items[0]):
            code, flow = self.run_flow(fakes, push=False, max_plan_rounds=3)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["plan", "agree", "revise", "agree", "revise", "agree", "build", "judge"])
        self.assertIn("coin flip chose plan 2", flow.state["plan"]["how"])
        self.assertIn("Objections to this plan", (flow.run_dir / "plans" / "c1-agreed.md").read_text(encoding="utf-8"))

    def test_the_final_judge_goes_by_approval_then_by_what_both_would_keep(self):
        fakes = {"deepseek": Fake("deepseek", approve=built_by("kimi"), prefer=lambda folders: next(
                     label for label, folder in folders.items() if built_by("deepseek")(folder))),
                 "kimi": Fake("kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(git("show", f"{flow.state['final']['commit']}:feature.txt", cwd=self.work), "built by kimi",
                         "both approve kimi's build, though deepseek would keep its own")
        keep = lambda folders: next(label for label, folder in folders.items() if built_by("kimi")(folder))
        fakes = {name: Fake(name, approve=lambda folder: False, prefer=keep) for name in ("deepseek", "kimi")}
        shutil.rmtree(self.tmp / "runs", ignore_errors=True)
        code, flow = self.run_flow(fakes, push=False, max_builds=2)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["plan", "agree", "build", "judge", "agree", "build", "judge"], "two build cycles, then the pick")
        self.assertEqual(self.steps(flow)[-1], ("judge", "forced"))
        self.assertEqual(git("show", f"{flow.state['final']['commit']}:feature.txt", cwd=self.work), "built by kimi")
        self.assertEqual(len(flow.state["final"]["open_defects"]), 2, "each judge's defect against it stays open")

    def test_rejected_builds_go_round_again_from_an_agreed_build(self):
        second = lambda folder: (pathlib.Path(folder) / "build-2.txt").exists()
        fakes = {name: Fake(name, approve=second) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("plan", "planned"), ("agree", "agreed"), ("build", "built"),
                                            ("judge", "rejected"), ("agree", "agreed"), ("build", "built"),
                                            ("judge", "accepted")])
        first, again = flow.state["history"][2], flow.state["history"][5]
        self.assertIn(again["start"], first["commits"].values(), "the second build starts from a first build")
        self.assertEqual([fake.sessions for fake in fakes.values()], [3, 3], "a fresh session at each of the 3 cycles")
        self.assertIn("Defects found in the base", (flow.run_dir / "plans" / "c2-agreed.md").read_text(encoding="utf-8"))
        self.assert_blind(fakes, flow)

    def test_failing_checks_are_never_accepted_or_pushed(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, checks=['{python} -c "import sys; sys.exit(1)"'], max_builds=1)
        self.assertEqual(code, 2)
        self.assertEqual(flow.state["outcome"], "checks fail")
        self.assertEqual(self.steps(flow)[-1], ("judge", "forced"))
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base)

    def test_an_over_long_plan_is_sent_back_once(self):
        fakes = {"deepseek": Fake("deepseek", plan_text=lambda attempt: "word " * (1050 if attempt == 1 else 50)),
                 "kimi": Fake("kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual(code, 0, flow.state)
        retry = [p for p in fakes["deepseek"].prompts if "Write a plan to" in p][1]
        self.assertIn("1050 words; cut it to 1000", retry)

    def test_answers_are_checked(self):
        self.assertEqual(Flow.plan_problem("no base here", {"X": {}, "Y": {}}),
                         'the plan must start with "Base: X" or "Base: Y"')
        self.assertIsNone(Flow.plan_problem("**Base:** Candidate y\nplan", {"X": {}, "Y": {}}))
        self.assertIsNone(Flow.plan_problem("Base: X\nplan", {"X": {}, "Y": {}}))
        reply = self.tmp / "reply.json"
        reply.write_text(json.dumps({"approve": {"1": False, "2": True}}), encoding="utf-8")
        self.assertEqual(Flow.agree_problem(reply), 'plan 1 is rejected without a "why"')
        reply.write_text(json.dumps({"approve": {"1": False, "2": True}, "why": {"1": "word " * 251}}), encoding="utf-8")
        self.assertIn("cut it to 250", Flow.agree_problem(reply))


if __name__ == "__main__":
    unittest.main()
