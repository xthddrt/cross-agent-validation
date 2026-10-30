"""The flow end to end with fake agents: plan, agree, build, review, publish."""

import json
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from cav import flow as flow_mod
from cav.agents import RunState
from cav.flow import Flow
from cav.gitops import git
from tests.test_agents import make_repo, with_origin



class Fake:
    """Answers each step's prompt. `accept(turn)`, `approve(cycle)` and `plan_text(attempt)` steer it."""

    def __init__(self, name, plan_text=None, accept=lambda turn: True, approve=lambda cycle: True, base="X"):
        self.name, self.state, self.session_id = name, RunState(), None
        self.plan_text = plan_text or (lambda attempt: f"{name} plan: change feature.txt; check it exists.")
        self.accept, self.approve, self.base = accept, approve, base
        self.prompts, self.sessions, self.turns, self.reviews, self.fixes = [], 0, 0, 0, 0

    def reset(self):
        self.state = RunState()

    def abort(self):
        pass

    def run(self, prompt, cwd):
        if not self.session_id:
            self.session_id, self.sessions = f"s-{self.name}", self.sessions + 1
        self.prompts.append(prompt)
        if "Write a plan to" in prompt:
            path = pathlib.Path(re.search(r"Write a plan to (\S+), at most", prompt).group(1))
            text = self.plan_text(sum("Write a plan to" in p for p in self.prompts))
            if "BRANCHES TO RECONCILE" in prompt:
                text = f"Base: Candidate {self.base}\n{text}"
            path.write_text(text, encoding="utf-8")
        elif "The other agent's plan" in prompt:
            self.turns += 1
            reply = pathlib.Path(re.search(r"writing JSON to (\S+):$", prompt, re.M).group(1))
            data = ({"accept": True} if self.accept(self.turns) else
                    {"accept": False, "plan": f"{'Base: Candidate ' + self.base + chr(10) if 'Base:' in prompt else ''}"
                                              f"{self.name} revised plan {self.turns}", "why": "it misses a check"})
            reply.write_text(json.dumps(data), encoding="utf-8")
        elif prompt.startswith("Implement this plan"):
            work = pathlib.Path(re.search(r", in (\S+)\.(?: It starts| Write)", prompt).group(1))
            (work / "feature.txt").write_text(f"built by {self.name}\n", encoding="utf-8")
            pathlib.Path(re.search(r"in a few lines, to (\S+)\. Change", prompt).group(1)).write_text("built", encoding="utf-8")
        elif prompt.startswith("Review the change"):
            self.reviews += 1
            reply = pathlib.Path(re.search(r"writing JSON to (\S+): ", prompt).group(1))
            data = ({"approve": True} if self.approve(self.reviews) else
                    {"approve": False, "defects": [{"file": "feature.txt", "line": 1, "problem": "wrong text", "fix": "fix it"}]})
            reply.write_text(json.dumps(data), encoding="utf-8")
        elif prompt.startswith("The reviewer found"):
            self.fixes += 1
            work = pathlib.Path(re.search(r"Fix only these in (\S+), run", prompt).group(1))
            (work / "feature.txt").write_text(f"fixed {self.fixes} by {self.name}\n", encoding="utf-8")
        self.state.finished = True
        return {"ok": True, "text": "done", "reason": "completed"}


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
        return [(item["step"], item.get("speaker") or item.get("agent"), item.get("verdict")) for item in flow.state["history"]]

    def test_plan_agree_build_review_publish(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        with mock.patch("cav.flow.random.sample", side_effect=lambda items, k: list(items)[:k]):
            code, flow = self.run_flow(fakes)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("plan", None, "planned"), ("agree", "deepseek", "accepted"),
                                            ("build", "deepseek", "built"), ("review", "kimi", "approved")])
        agreed = (flow.run_dir / "plans" / "agreed.md").read_text(encoding="utf-8")
        self.assertIn("kimi plan", agreed, "deepseek spoke first and accepted kimi's plan as written")
        self.assertEqual(git("--git-dir", str(self.origin), "show", f"{self.branch}:feature.txt"), "built by deepseek")
        self.assertTrue(flow.state["outcome"].startswith("merged and pushed"))
        self.assertEqual([fake.sessions for fake in fakes.values()], [1, 1], "one session per agent for the whole run")
        for fake in fakes.values():
            for prompt in fake.prompts:
                self.assertNotIn("deepseek", prompt)
                self.assertNotIn("kimi", prompt)
                self.assertNotIn("yours", prompt)

    def test_an_over_long_plan_is_sent_back_once(self):
        fakes = {"deepseek": Fake("deepseek", plan_text=lambda attempt: "word " * (450 if attempt == 1 else 50)),
                 "kimi": Fake("kimi")}
        code, flow = self.run_flow(fakes)
        self.assertEqual(code, 0, flow.state)
        retry = [p for p in fakes["deepseek"].prompts if "Write a plan to" in p][1]
        self.assertIn("450 words; cut it to 400", retry)

    def test_no_accept_in_four_turns_is_a_coin_flip(self):
        fakes = {name: Fake(name, accept=lambda turn: False) for name in ("deepseek", "kimi")}
        with mock.patch("cav.flow.random.choice", return_value="kimi") as flip:
            code, flow = self.run_flow(fakes)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([item["verdict"] for item in flow.state["history"] if item["step"] == "agree"], ["revised"] * 4)
        flip.assert_called_once()
        self.assertIn("coin flip chose kimi", flow.state["plan"]["how"])
        for fake in fakes.values():
            for prompt in fake.prompts:
                self.assertNotIn("deepseek", prompt, "revised plans are numbered, not named after their author")
                self.assertNotIn("kimi", prompt)
        self.assertIn("kimi revised plan 2", (flow.run_dir / "plans" / "agreed.md").read_text(encoding="utf-8"))

    def test_defects_get_two_fixes_then_publish_with_open_defects(self):
        fakes = {"deepseek": Fake("deepseek"), "kimi": Fake("kimi", approve=lambda cycle: False)}
        code, flow = self.run_flow(fakes)
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _, _ in self.steps(flow)][-5:], ["review", "fix", "review", "fix", "review"])
        self.assertEqual(fakes["deepseek"].fixes, 2)
        self.assertEqual(len(flow.state["final"]["open_defects"]), 1)

    def test_failing_checks_publish_nothing(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, checks=['{python} -c "import sys; sys.exit(1)"'])
        self.assertEqual(code, 2)
        self.assertEqual(flow.state["outcome"], "checks fail")
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base)
        self.assertEqual(fakes["deepseek"].fixes, 2, "a failing check is sent back to the builder")

    def test_no_push_keeps_the_result_local(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, push=False)
        self.assertEqual((code, flow.state["outcome"]), (0, "done, kept local"))
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base)

    def test_two_branches_are_reconciled_blind_from_the_plans_base(self):
        shas = {}
        for name in ("feat-a", "feat-b"):
            git("checkout", "-q", "-b", name, self.base, cwd=self.work)
            (self.work / f"{name}.txt").write_text(name, encoding="utf-8")
            git("add", "-A", cwd=self.work)
            git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", f"note {name}", cwd=self.work)
            shas[name] = git("rev-parse", "HEAD", cwd=self.work)
        git("checkout", "-q", self.branch, cwd=self.work)
        fakes = {name: Fake(name, base="Y") for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        labels = flow.state["labels"]
        for label, item in labels.items():
            self.assertFalse((flow.run_dir / "sources" / f"candidate-{label}" / ".git").exists())
        built = git("rev-parse", f"refs/cav/{flow.run_id}/result", cwd=self.work)
        self.assertEqual(git("merge-base", "--is-ancestor", labels["Y"]["commit"], built, cwd=self.work, check=False), "")
        self.assertTrue(git("merge-base", built, labels["Y"]["commit"], cwd=self.work) == labels["Y"]["commit"],
                        "the build starts from the branch the plan names")
        for fake in fakes.values():
            for prompt in fake.prompts:
                for leak in ("feat-a", "feat-b", "note feat"):
                    self.assertNotIn(leak, prompt)

    def test_a_plan_without_a_base_line_is_sent_back(self):
        self.assertEqual(Flow.plan_problem("no base here", {"X": "x", "Y": "y"}),
                         'the plan must start with "Base: Candidate X" or "Base: Candidate Y"')
        self.assertIsNone(Flow.plan_problem("**Base:** Candidate y\nplan", {"X": "x", "Y": "y"}))


if __name__ == "__main__":
    unittest.main()
