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

BUILD = re.compile(r"^Implement the agreed plan, \S+, in (\S+?),")


def first_repo(folder):
    return sorted(path for path in pathlib.Path(folder).iterdir() if path.is_dir())[0]


class Fake:
    """Answers each prompt from what is on disk. `blockers(folder)` is how many blockers it finds in a
    candidate (judged on its first repo); `agree(line)` its vote on another's blocker; `approve(text)` its
    vote on a plan; `base(folders)` the candidate a fix plan builds on; `audit(folder)` how many it
    finds when auditing. A build changes every repo."""

    def __init__(self, name, blockers=lambda folder: 0, agree=lambda line: True, approve=lambda text: True,
                 base=lambda folders: "X", audit=lambda folder: 0):
        self.name, self.state, self.session_id = name, RunState(), None
        self.blockers, self.agree, self.approve, self.base = blockers, agree, approve, base
        self.audit = audit
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
            self.folders = {label: first_repo(folder) for label, folder in re.findall(r"^- ([XY]): (\S+) \(checks", prompt, re.M)}
            count = self.audit if "\nAUDIT." in prompt else self.blockers
            found = [{"candidate": label, "file": f"{folder.name}/base.txt", "line": n, "problem": f"{label} wrong {n}"}
                     for label, folder in self.folders.items() for n in range(1, count(folder) + 1)]
            requirements = [{"requirement": "feature.txt exists", "evidence": "feature.txt:1",
                             "met": {label: (folder / "feature.txt").exists() for label, folder in self.folders.items()}}]
            path.write_text(json.dumps({"requirements": requirements, "blockers": found}), encoding="utf-8")
        elif prompt.startswith("VOTED DOWN"):
            listed = pathlib.Path(re.search(r"^- (\S+)$", prompt, re.M).group(1)).read_text(encoding="utf-8")
            keep = {n: False for n, _ in re.findall(r"^(\d+)\. (.*)$", listed, re.M)}
            path.write_text(json.dumps({"keep": keep, "why": {n: "the code still shows it" for n, kept in keep.items() if kept}}),
                            encoding="utf-8")
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
            for repo in (path for path in work.iterdir() if path.is_dir()):
                (repo / "feature.txt").write_text(f"built by {self.name}\n", encoding="utf-8")
                (repo / f"build-{self.builds}.txt").write_text(self.name, encoding="utf-8")
            pathlib.Path(re.search(r"changed to (\S+)\.$", prompt, re.M).group(1)).write_text("done", encoding="utf-8")
        self.state.finished = True
        return {"ok": True, "text": "done", "reason": "completed"}


def built(n):
    return lambda folder: (pathlib.Path(folder) / f"build-{n}.txt").exists()


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-flow-"))
        self.work, self.origin, self.base = with_origin(self.tmp)
        self.branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_flow(self, fakes, branches=(), repos=None, **options):
        repos = repos or [{"repo": str(self.work), "branches": list(branches)}]
        with mock.patch("cav.flow.agent_mod.build", side_effect=lambda spec, log, title_prefix="": fakes[spec.name]):
            flow = Flow(repos, self.tmp / "runs" / "r1", **options)
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
                                            ("review", "reviewed"), ("confirm", "no blocker in X and Y"),
                                            ("audit", "reviewed"), ("confirm", "no blocker in X and Y")])
        audit = [p for p in fakes["kimi"].prompts if "\nCANDIDATES\n" in p][1]
        self.assertIn("AUDIT. The last review found no blocker in X and Y", audit)
        self.assertIn("A blocker is a defect you would refuse to ship", audit)
        self.assertNotIn('"notes"', audit, "what is not a blocker is not logged")
        for fake in fakes.values():
            vote = next(p for p in fake.prompts if p.startswith("PLANS"))
            written = re.search(r"Write a plan to (\S+)", fake.prompts[0]).group(1)
            self.assertNotIn(written, vote, "a voter is not shown the file it wrote")
        branch = flow.state["final"]["branch"]
        self.assertEqual(git("rev-parse", branch, cwd=self.work), flow.state["final"]["commits"]["work"])
        self.assertTrue(git("show", f"{branch}:feature.txt", cwd=self.work).startswith("built by"))
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), self.base, "nothing is pushed")
        self.assertEqual([fake.sessions for fake in fakes.values()], [3, 3], "fresh sessions for the review and the audit")
        self.assert_blind(fakes, flow)

    def test_rejected_plans_are_revised_until_both_approve(self):
        fakes = {name: Fake(name, approve=lambda text: "revised" in text) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)][:4], ["plan", "vote", "revise", "vote"])
        revise = next(p for p in fakes["kimi"].prompts if p.startswith("OBJECTIONS"))
        self.assertIn("misses something", revise)


    def test_code_a_planner_changes_is_restored_before_the_vote(self):
        class Eager(Fake):  # writes its plan, then starts coding anyway
            def run(self, prompt, cwd):
                if prompt.startswith("TASK"):
                    self.code = first_repo(re.search(r"^CODE\n- (\S+)$", prompt, re.M).group(1))
                    (self.code / "sneaky.txt").write_text("built early", encoding="utf-8")
                    (self.code / "base.txt").write_text("changed early", encoding="utf-8")
                elif prompt.startswith("PLANS"):
                    self.saw = {path.name: path.read_text(encoding="utf-8") for path in self.code.iterdir()}
                return super().run(prompt, cwd)

        fakes = {"deepseek": Fake("deepseek"), "kimi": Eager("kimi")}
        original = (self.work / "base.txt").read_text(encoding="utf-8")
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 0, flow.state)
        self.assertIn("Change no code", fakes["kimi"].prompts[0])
        self.assertNotIn("sneaky.txt", fakes["kimi"].saw, "the vote reads the code as it was")
        self.assertEqual(fakes["kimi"].saw["base.txt"], original)

    def test_a_blocker_voted_down_is_dropped(self):
        self.branches()
        fakes = {"deepseek": Fake("deepseek", blockers=lambda folder: 1), "kimi": Fake("kimi", agree=lambda line: False)}
        code, flow = self.run_flow(fakes, plan="Add a note.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual(flow.state["history"][1]["agreed"], {"X": [], "Y": []})
        self.assertTrue(all(not item["agreed"] for item in flow.state["history"][0]["blockers"]))
        self.assertTrue([p for p in fakes["deepseek"].prompts if p.startswith("VOTED DOWN")], "a voted-down blocker comes back once")
        self.assertNotIn("disputed", flow.state, "withdrawn, it is gone")


    def test_an_audit_of_one_clear_candidate_votes_only_on_that_one(self):
        self.branches()
        on_b = lambda folder: int((folder / "feat-b.txt").exists())
        fakes = {name: Fake(name, blockers=on_b, audit=on_b) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, plan="Add a note.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)], ["review", "confirm", "audit", "confirm"])
        clear = flow.state["history"][1]["verdict"][-1]
        audit = [p for p in fakes["kimi"].prompts if "\nAUDIT." in p][0]
        self.assertIn(f"Review {clear} again from scratch", audit)
        self.assertIn("read it only to compare", audit)
        self.assertEqual(flow.state["history"][2]["blockers"], [], "the blocked candidate's audit blockers are dropped")
        self.assertEqual(sum(p.startswith("BLOCKERS") for fake in fakes.values() for p in fake.prompts), 2,
                         "votes only on the review's blockers")
        self.assertTrue(git("show", f"{flow.state['final']['branch']}:feat-a.txt", cwd=self.work))

    def test_agreed_blockers_get_a_fix_plan_then_a_rebuild_from_its_base(self):
        shas = self.branches()
        on_a = lambda folders: next(label for label, folder in folders.items() if (folder / "feat-a.txt").exists())
        fakes = {name: Fake(name, blockers=lambda folder: int(not (folder / "feature.txt").exists()), base=on_a)
                 for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, plan="Add feature.txt.", branches=["feat-a", "feat-b"])
        self.assertEqual(code, 0, flow.state)
        self.assertEqual([step for step, _ in self.steps(flow)],
                         ["review", "confirm", "plan", "vote", "build", "review", "confirm", "audit", "confirm"])
        self.assertEqual(flow.state["history"][4]["start"], {"work": shas["feat-a"]}, "both rebuild from the fix plan's base")
        fix = next(p for p in fakes["kimi"].prompts if p.startswith("AGREED BLOCKERS"))
        self.assertIn("#1", pathlib.Path(re.search(r"^- (\S+)$", fix, re.M).group(1)).read_text(encoding="utf-8"))
        review = [p for p in fakes["kimi"].prompts if "\nCANDIDATES\n" in p][1]
        self.assertIn("AGREED BLOCKERS", review, "a later review checks the agreed blockers")
        self.assertEqual([fake.sessions for fake in fakes.values()], [3, 3])
        self.assertIn("with the tests it names", next(p for p in fakes["kimi"].prompts if BUILD.match(p)))
        self.assertIn("Tests only where the task asks for them.", fix)
        self.assert_blind(fakes, flow, leaks=("feat-a", "feat-b"))

    def test_a_run_stopped_in_a_build_resumes_at_that_build_and_finishes(self):
        class Crash(Fake):
            def run(self, prompt, cwd):
                if BUILD.match(prompt) and self.builds:  # the fix build: the machine goes down
                    raise RuntimeError("the machine restarted")
                return super().run(prompt, cwd)

        fakes = {name: Crash(name, blockers=built(1)) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt.")
        self.assertEqual(code, 1)
        self.assertEqual(flow.state["resume"]["next"], "build")
        fresh = {name: Fake(name) for name in ("deepseek", "kimi")}
        with mock.patch("cav.flow.agent_mod.build", side_effect=lambda spec, log, title_prefix="": fresh[spec.name]):
            resumed = Flow.resume(flow.run_dir)
            code = resumed.run()
        self.assertEqual(code, 0, resumed.state)
        self.assertEqual(self.steps(resumed), [("plan", "planned"), ("vote", "agreed"), ("build", "built"),
                                               ("review", "reviewed"), ("confirm", "blocked"), ("plan", "planned"),
                                               ("vote", "agreed"), ("build", "interrupted"), ("build", "built"),
                                               ("review", "reviewed"), ("confirm", "no blocker in X and Y"),
                                               ("audit", "reviewed"), ("confirm", "no blocker in X and Y")])
        self.assertTrue(BUILD.match(fresh["kimi"].prompts[0]), "it picks up at the build, not the plan")
        self.assertEqual(resumed.blind, flow.blind)
        self.assert_blind(fresh, resumed)
        with self.assertRaises(ValueError):
            Flow.resume(flow.run_dir)  # a finished run has nothing to resume


    def test_several_repos_are_one_candidate_and_get_the_result_branch_each(self):
        other = self.tmp / "second"
        other.mkdir()
        git("init", "-q", cwd=other)
        (other / "base.txt").write_text("other\n", encoding="utf-8")
        git("add", "-A", cwd=other)
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "base", cwd=other)
        fakes = {name: Fake(name) for name in ("deepseek", "kimi")}
        code, flow = self.run_flow(fakes, task="Add feature.txt to both.",
                                   repos=[{"repo": str(self.work)}, {"repo": str(other)}])
        self.assertEqual(code, 0, flow.state)
        branch = flow.state["final"]["branch"]
        for repo, name in ((self.work, "work"), (other, "second")):
            self.assertEqual(git("rev-parse", branch, cwd=repo), flow.state["final"]["commits"][name])
            self.assertTrue(git("show", f"{branch}:feature.txt", cwd=repo).startswith("built by"))
        review = next(p for p in fakes["kimi"].prompts if "\nCANDIDATES\n" in p)
        folder = pathlib.Path(re.search(r"^- X: (\S+) \(checks", review, re.M).group(1))
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["second", "work"], "one folder per repo")


if __name__ == "__main__":
    unittest.main()
