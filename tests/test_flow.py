"""The flow end to end with fake agents."""

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

BUILD = re.compile(r"^Implement the agreed plan, \S+, in (\S+?)(?:, a copy of [XY])?\.")


class Fake:
    """Answers each prompt from what is on disk. `blockers(folder)` is how many blockers it finds in a
    candidate; `agree(line)` its vote on another's blocker; `approve(text)` its vote on a plan;
    `base(folders)` the candidate a fix plan builds on."""

    def __init__(self, name, blockers=lambda folder: 0, agree=lambda line: True, approve=lambda text: True,
                 base=lambda folders: "X"):
        self.name, self.state, self.session_id = name, RunState(), None
        self.blockers, self.agree, self.approve, self.base = blockers, agree, approve, base
        self.prompts, self.sessions, self.builds, self.folders = [], 0, 0, {}

    def reset(self):
        self.state = RunState()

    def abort(self):
        pass

    def run(self, prompt, cwd):
        if not self.session_id:
            self.session_id, self.sessions = f"s-{self.name}-{self.sessions + 1}", self.sessions + 1
        self.prompts.append(prompt)
        out = re.search(r"(?:Write to|Write a plan to|answer to|, to) (\S+?):?(?:\s|$)", prompt)
        path = pathlib.Path(out.group(1)) if out else None
        if "\nCANDIDATES\n" in prompt:
            self.folders = {label: pathlib.Path(folder) for label, folder in re.findall(r"^- ([XY]): (\S+)$", prompt, re.M)}
            found = [{"candidate": label, "file": "base.txt", "line": n, "problem": f"{label} wrong {n}"}
                     for label, folder in self.folders.items() for n in range(1, self.blockers(folder) + 1)]
            path.write_text(json.dumps({"blockers": found}), encoding="utf-8")
        elif prompt.startswith("BLOCKERS"):
            listed = pathlib.Path(re.search(r"^- (\S+)$", prompt, re.M).group(1)).read_text(encoding="utf-8")
            votes = {n: self.agree(line) for n, line in re.findall(r"^(\d+)\. (.*)$", listed, re.M)}
            path.write_text(json.dumps({"blocker": votes, "why": {n: "the code is right" for n, ok in votes.items() if not ok}}),
                            encoding="utf-8")
        elif prompt.startswith("PLANS"):
            plans = {n: pathlib.Path(p).read_text(encoding="utf-8") for n, p in re.findall(r"^- (\d): (\S+)$", prompt, re.M)}
            votes = {n: self.approve(text) for n, text in plans.items()}
            path.write_text(json.dumps({"approve": votes, "why": {n: "misses something" for n, ok in votes.items() if not ok}}),
                            encoding="utf-8")
        elif prompt.startswith("AGREED BLOCKERS"):
            path.write_text(f"Base: {self.base(self.folders)}\nfix the blockers ({self.name})", encoding="utf-8")
        elif prompt.startswith("OBJECTIONS"):
            head = f"Base: {self.base(self.folders)}\n" if "Base: X" in prompt else ""
            path.write_text(f"{head}revised plan ({self.name})", encoding="utf-8")
        elif prompt.startswith("TASK"):
            path.write_text(f"plan ({self.name}): add feature.txt", encoding="utf-8")
        elif BUILD.match(prompt):
            self.builds += 1
            work = pathlib.Path(BUILD.match(prompt).group(1))
            (work / "feature.txt").write_text(f"built by {self.name}\n", encoding="utf-8")
            (work / f"build-{self.builds}.txt").write_text(self.name, encoding="utf-8")
            pathlib.Path(re.search(r"changed to (\S+)\.$", prompt, re.M).group(1)).write_text("done", encoding="utf-8")
        self.state.finished = True
        return {"ok": True, "text": "done", "reason": "completed"}


def built(n):
    return lambda folder: (pathlib.Path(folder) / f"build-{n}.txt").exists()


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
            flow = Flow(str(self.work), self.tmp / "runs" / "r1", **options)
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

    def assert_blind(self, fakes, flow, leaks=()):
        """No prompt names an agent or a branch or points into the run; nothing the agents can reach
        has git history or the run's bookkeeping."""
        for fake in fakes.values():
            for prompt in fake.prompts:
                for word in ("deepseek", "kimi", str(flow.run_dir), *leaks):
                    self.assertNotIn(word, prompt)
        folders = [flow.blind] + [path for path in flow.blind.rglob("*") if path.is_dir()]
        self.assertFalse([path for path in folders if git_ok("rev-parse", "--git-dir", cwd=path)])
        self.assertFalse(list(flow.blind.rglob("state.json")))

    def test_a_task_is_planned_built_reviewed_and_kept_as_a_branch(self):
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(self.steps(flow), [("plan", "planned"), ("vote", "agreed"), ("build", "built"),
                                            ("review", "reviewed"), ("confirm", "no blocker in X and Y")])
        branch = flow.state["final"]["branch"]
        self.assertEqual(git("rev-parse", branch, cwd=self.work), flow.state["final"]["commit"])
        self.assertTrue(git("show", f"{branch}:feature.txt", cwd=self.work).startswith("built by"))
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base, "nothing is pushed")
        self.assertEqual([fake.sessions for fake in fakes.values()], [2, 2], "a fresh session for the review round")
        self.assert_blind(fakes, flow)

    def test_rejected_plans_are_revised_until_both_approve(self):
        fakes = {name: Fake(name, approve=lambda text: "revised" in text) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)][:4], ["plan", "vote", "revise", "vote"])
        revise = next(p for p in fakes["kimi"].prompts if p.startswith("OBJECTIONS"))
        self.assertIn("misses something", revise)

    def test_a_branch_with_no_agreed_blocker_is_the_result_at_once(self):
        shas = self.branches()
        on_b = lambda folder: int((pathlib.Path(folder) / "feat-b.txt").exists())
        fakes = {name: Fake(name, blockers=on_b) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, plan="Add a note.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)], ["review", "confirm"])
        self.assertEqual(flow.state["final"]["commit"], shas["feat-a"])
        self.assertTrue(fakes["kimi"].prompts[0].startswith("AGREED PLANS"))
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b", "note feat"))

    def test_a_blocker_voted_down_is_dropped(self):
        self.branches()
        fakes = {"deepseek": Fake("deepseek", blockers=lambda folder: 1), "kimi": Fake("kimi", agree=lambda line: False)}
        code, flow = self.run_flow(fakes, plan="Add a note.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(flow.state["history"][1]["agreed"], {"X": [], "Y": []})
        self.assertTrue(all(not item["agreed"] for item in flow.state["history"][0]["blockers"]))

    def test_agreed_blockers_get_a_fix_plan_then_a_rebuild_from_its_base(self):
        shas = self.branches()
        on_a = lambda folders: next(label for label, folder in folders.items() if (folder / "feat-a.txt").exists())
        fakes = {name: Fake(name, blockers=lambda folder: int(not (folder / "feature.txt").exists()), base=on_a)
                 for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, plan="Add feature.txt.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["review", "confirm", "plan", "vote", "build", "review", "confirm"])
        self.assertEqual(flow.state["history"][4]["start"], shas["feat-a"], "both rebuild from the fix plan's base")
        fix = next(p for p in fakes["kimi"].prompts if p.startswith("AGREED BLOCKERS"))
        self.assertIn("#1", pathlib.Path(re.search(r"^- (\S+)$", fix, re.M).group(1)).read_text(encoding="utf-8"))
        review = [p for p in fakes["kimi"].prompts if "\nCANDIDATES\n" in p][1]
        self.assertIn("AGREED BLOCKERS", review, "a later review checks the agreed blockers")
        self.assertEqual([fake.sessions for fake in fakes.values()], [2, 2])
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b"))

    def test_every_failed_review_gets_a_fix_plan_that_carries_all_earlier_agreed_plans(self):
        fewer = lambda folder: 0 if built(3)(folder) else 2 if built_by("kimi")(folder) else 1
        fakes = {name: Fake(name, blockers=fewer) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["plan", "vote", "build", "review", "confirm", "plan", "vote", "build",
                          "review", "confirm", "plan", "vote", "build", "review", "confirm"])
        fixes = [p for p in fakes["kimi"].prompts if p.startswith("AGREED BLOCKERS")]
        earlier = [re.findall(r"^- (\S+)$", p.split("AGREED PLANS", 1)[1].split("\n\n", 1)[0], re.M) for p in fixes]
        self.assertEqual([len(paths) for paths in earlier], [1, 2], "the task plan, then it and the first fix plan")
        self.assertEqual(earlier[1][0], earlier[0][0], "the task plan stays first")
        self.assertTrue(pathlib.Path(earlier[1][1]).read_text(encoding="utf-8").startswith("Base:"))
        reviews = [p for p in fakes["kimi"].prompts if "\nCANDIDATES\n" in p]
        self.assertEqual([len(re.findall(r"^- \S+$", p.split("\n\nCANDIDATES", 1)[0], re.M)) for p in reviews], [1, 2, 3],
                         "each review checks every plan agreed so far")
        self.assertIn("undoes an agreed plan", next(p for p in fakes["kimi"].prompts if p.startswith("PLANS")
                                                     and "agreed blocker" in p))

    def test_answers_are_checked(self):
        path = self.tmp / "answer.json"
        path.write_text(json.dumps({"approve": {"1": False, "2": True}}), encoding="utf-8")
        self.assertEqual(Flow.vote_problem(path), 'plan 1 is rejected without a "why"')
        path.write_text(json.dumps({"blocker": {"1": True}}), encoding="utf-8")
        self.assertIn("each of 1 to 2", Flow.confirm_problem(path, 2))
        path.write_text(json.dumps({"blockers": [{"candidate": "Z", "problem": "x"}]}), encoding="utf-8")
        self.assertIn('"candidate"', Flow.review_problem(path, {}))
        plan = self.tmp / "plan.md"
        plan.write_text("no base", encoding="utf-8")
        self.assertEqual(Flow.plan_problem(plan, True), 'the plan must start with "Base: X" or "Base: Y"')
        self.assertIsNone(Flow.plan_problem(plan, False))


if __name__ == "__main__":
    unittest.main()
