"""Pipeline (plan stage, code stage, merge and push) and the home server, with fake agents."""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from cav import config
from cav import pipeline as pipeline_mod
from cav.gitops import git
from cav.orchestrator import Orchestrator
from cav.pipeline import Pipeline
from cav.web import Home
from tests.test_v2 import ROUND_RE, StructuredFake, make_repo

BLOCKING = {"id": "F-01", "severity": "MUST_FIX", "path": "base.txt", "line": 1,
            "summary": "the other candidate is wrong", "resolution": "fix it"}


DECISION_RE = re.compile(r"DECISION: write this JSON to (.+)")
PLAN_LINE_RE = re.compile(r"^- Plan (\d): (.+)$", re.M)


def decide(fake, prompt, prefer=None):
    """Answer a forced-choice round: pick the plan whose text mentions `prefer`, else Plan 1."""
    round_no = int(ROUND_RE.search(prompt).group(1))
    plans = {int(n): pathlib.Path(path.strip()).read_text(encoding="utf-8") for n, path in PLAN_LINE_RE.findall(prompt)}
    choice = next((n for n, text in sorted(plans.items()) if prefer and prefer in text), 1)
    data = {"round": round_no, "agent": fake.name, "choice": choice,
            "defects": [{"summary": f"{fake.name} wants a test for the empty string", "fix": "add one"}],
            "reason": "fake"}
    path = pathlib.Path(DECISION_RE.search(prompt).group(1).strip())
    path.write_text(json.dumps(data), encoding="utf-8")
    fake.state.finished = True
    fake.state.touch("finished")
    return {"ok": True, "text": "done", "reason": "completed"}


def is_plan(prompt):
    return "each propose a plan" in prompt


class StageFake(StructuredFake):
    """Writes a plan in the plan stage and a feature file in the code stage; blocks round 1, agrees in 2."""

    def __init__(self, name):
        super().__init__(name, None)
        self.prompts = []

    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        name, plan = self.name, is_plan(prompt)
        first = int(ROUND_RE.search(prompt).group(1)) == 1
        self.approve, self.findings = not first, [BLOCKING] if first else []
        self.files = lambda r: ({"CAV-PLAN.md": "agreed plan\n" if r >= 2 else f"{name} plan\n"} if plan
                                else {"feature.txt": "agreed\n" if r >= 2 else f"{name}\n"})
        return super().run(prompt, cwd)


class NeverAgrees(StageFake):
    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        if DECISION_RE.search(prompt):
            return decide(self, prompt)
        name, plan = self.name, is_plan(prompt)
        self.files = lambda r: {"CAV-PLAN.md" if plan else "feature.txt": f"{name} {r}\n"}
        self.approve, self.findings = False, [BLOCKING]
        return StructuredFake.run(self, prompt, cwd)


def with_origin(tmp):
    """A work clone whose origin is a local bare repo, standing in for GitHub."""
    seed, base = make_repo(tmp)
    origin = tmp / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(origin))
    work = tmp / "work"
    git("clone", "-q", str(origin), str(work))
    return work, origin, base


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-pipe-"))
        self.work, self.origin, self.base = with_origin(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pipeline(self, fakes):
        with mock.patch("cav.agents.build", side_effect=lambda spec, log_path, title_prefix="": fakes[spec.name]):
            pipeline = Pipeline(str(self.work), "Add a feature file.", self.tmp / "runs" / "p1")
            code = pipeline.run()
        return code, pipeline.state

    def test_agreed_plan_then_agreed_code_is_pushed(self):
        fakes = {name: StageFake(name) for name in ("deepseek", "kimi")}
        code, state = self.run_pipeline(fakes)

        self.assertEqual(code, 0, state)
        self.assertEqual(state["outcome"], "merged and pushed")
        self.assertEqual(state["plan"]["outcome"], "agreed")
        run_dir = self.tmp / "runs" / "p1"
        # round 2 approved both round-1 plans, so the agreed plan is one of them
        self.assertIn((run_dir / "plans" / "agreed.md").read_text(encoding="utf-8").strip(),
                      ("deepseek plan", "kimi plan"))
        implement = next(p for p in fakes["kimi"].prompts if "Implement the task" in p)
        self.assertIn("PLAN (both agents agreed on it", implement)
        self.assertIn(str(run_dir / "plans" / "agreed.md"), implement)
        pushed = f"refs/heads/{state['branch']}"
        self.assertIn(git("--git-dir", str(self.origin), "show", f"{pushed}:feature.txt"), ("deepseek", "kimi"))
        files = git("--git-dir", str(self.origin), "ls-tree", "-r", "--name-only", pushed).splitlines()
        self.assertNotIn("CAV-PLAN.md", files, "the plan stays out of the pushed code")

    def test_no_push_keeps_an_agreed_result_local(self):
        fakes = {name: StageFake(name) for name in ("deepseek", "kimi")}
        with mock.patch("cav.agents.build", side_effect=lambda spec, log_path, title_prefix="": fakes[spec.name]):
            pipeline = Pipeline(str(self.work), "Add a feature file.", self.tmp / "runs" / "p1", push=False)
            code = pipeline.run()
        self.assertEqual(code, 0)
        self.assertEqual(pipeline.state["outcome"], "agreed, not pushed")
        self.assertIn("--no-push", pipeline.state["detail"])
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", f"refs/heads/{pipeline.state['branch']}"),
                         self.base, "origin is untouched")

    def test_a_plan_stage_without_agreement_is_forced_to_one_plan(self):
        fakes = {name: NeverAgrees(name) for name in ("deepseek", "kimi")}
        code, state = self.run_pipeline(fakes)

        self.assertEqual(code, 2, f"the code stage still has to agree; the plan stage cannot fail to: {state}")
        run_dir = self.tmp / "runs" / "p1"
        self.assertTrue(state["plan"]["outcome"].startswith("forced:"), state["plan"])
        plan_state = json.loads((run_dir / "plan" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual([item["round"] for item in plan_state["history"]], [1, 2, 3, 4])
        self.assertEqual(plan_state["history"][-1]["verdict"], "forced", "round 4 decides")
        agreed = (run_dir / "plans" / "agreed.md").read_text(encoding="utf-8")
        self.assertIn("## Defects to fix while implementing", agreed)
        self.assertIn("wants a test for the empty string", agreed)
        implement = next(p for p in fakes["deepseek"].prompts if "Implement the task" in p)
        self.assertIn("PLAN (chosen in the plan stage; implement it, including the defects", implement)
        decide_prompt = next(p for p in fakes["kimi"].prompts if DECISION_RE.search(p))
        self.assertNotIn("kimi 3", decide_prompt, "the plans are files; their text and authors are not inlined")
        self.assertIn("who wrote which is not shown", decide_prompt)
        self.assertTrue((run_dir / "code" / "SUMMARY.md").exists())
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", f"refs/heads/{state['branch']}"),
                         self.base, "nothing is pushed without agreement on the code")


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-merge-"))
        self.work, self.origin, self.base = with_origin(self.tmp)
        self.branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, repo, name, text):
        (repo / name).write_text(text, encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", name, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    def orchestrator(self):
        case = config.Case(name="m", host_repo=self.work, base=self.base, branch=self.branch,
                           agents={"deepseek": config.AgentSpec("deepseek", "opencode"),
                                   "kimi": config.AgentSpec("kimi", "kimi")})
        return Orchestrator(case, self.tmp / "run", push=True, merge=True)

    def move_origin(self, name, text):
        other = self.tmp / "other"
        git("clone", "-q", str(self.origin), str(other))
        self.commit(other, name, text)
        git("push", "-q", "origin", f"HEAD:{self.branch}", cwd=other)

    def test_merges_when_origin_moved(self):
        agreed = self.commit(self.work, "feature.txt", "agreed\n")
        self.move_origin("theirs.txt", "someone else\n")
        pushed = self.orchestrator().push_origin(agreed)
        self.assertNotEqual(pushed, agreed)
        files = git("--git-dir", str(self.origin), "ls-tree", "-r", "--name-only", self.branch).splitlines()
        self.assertIn("feature.txt", files)
        self.assertIn("theirs.txt", files)

    def test_refuses_a_conflicting_merge(self):
        agreed = self.commit(self.work, "base.txt", "ours\n")
        self.move_origin("base.txt", "theirs\n")
        before = git("--git-dir", str(self.origin), "rev-parse", self.branch)
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            self.orchestrator().push_origin(agreed)
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), before)


class RepoTest(unittest.TestCase):
    def test_github_folder_with_another_origin_is_refused(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-repo-"))
        try:
            work, origin, _ = with_origin(tmp)
            target = tmp / "clones" / "thing"
            shutil.copytree(work, target)
            with self.assertRaisesRegex(ValueError, "already exists"):
                pipeline_mod.resolve_repo("https://github.com/someone/thing", clone_root=tmp / "clones")
            git("remote", "set-url", "origin", "https://github.com/someone/thing.git", cwd=target)
            self.assertEqual(pipeline_mod.resolve_repo("https://github.com/someone/thing", clone_root=tmp / "clones"),
                             target.resolve(), "an existing clone of the same repo is reused")
            with self.assertRaisesRegex(ValueError, "not a git repository"):
                pipeline_mod.resolve_repo(str(tmp / "missing"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HomeTest(unittest.TestCase):
    def launches(self):
        """Catch the pipeline launch only; git still needs the real Popen."""
        real, launched = subprocess.Popen, []

        def popen(args, *rest, **options):
            if list(args[1:4]) == ["-m", "cav", "pipeline"]:
                launched.append(list(args))
                return mock.Mock()
            return real(args, *rest, **options)
        return mock.patch("cav.web.subprocess.Popen", side_effect=popen), launched

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-home-"))
        self.repo, _ = make_repo(self.tmp)
        self.home = Home(self.tmp / "runs", port=0)
        self.server = self.home.make_server()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/{self.home.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, body, content_type="application/json"):
        request = urllib.request.Request(self.base + "/api/start", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": content_type}, method="POST")
        return urllib.request.urlopen(request, timeout=5)

    def test_start_launches_a_pipeline_process(self):
        patch, launched = self.launches()
        with patch:
            with self.post({"repo": str(self.repo), "prompt": "Do the thing"}) as response:
                body = json.loads(response.read())
        self.assertEqual(len(launched), 1)
        args = launched[0]
        self.assertEqual(args[1:4], ["-m", "cav", "pipeline"])
        self.assertIn(str(self.repo), args)
        run_dir = self.tmp / "runs" / body["id"]
        self.assertEqual((run_dir / "prompt.md").read_text(encoding="utf-8").strip(), "Do the thing")
        self.assertEqual(json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))["stage"], "starting")

        with urllib.request.urlopen(self.base + "/api/runs", timeout=5) as response:
            runs = json.loads(response.read())
        self.assertEqual([run["id"] for run in runs], [body["id"]])
        with urllib.request.urlopen(self.base + f"/run/{body['id']}/", timeout=5) as response:
            self.assertTrue(response.geturl().endswith(f"/run/{body['id']}/plan/"), "a pipeline opens on its stage")
        with urllib.request.urlopen(self.base + f"/run/{body['id']}/api/pipeline", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["prompt"].strip(), "Do the thing")

    def test_start_refuses_bad_input(self):
        for body, content_type, code in (({"repo": str(self.tmp / "nope"), "prompt": "x"}, "application/json", 400),
                                         ({"repo": str(self.repo), "prompt": "  "}, "application/json", 400),
                                         ({"repo": str(self.repo), "prompt": "x"}, "text/plain", 415)):
            patch, launched = self.launches()
            with patch:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post(body, content_type)
            self.assertEqual(caught.exception.code, code, body)
            self.assertEqual(launched, [])
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base.rsplit("/", 1)[0] + "/wrong/api/runs", timeout=5)

    def test_token_is_kept_across_restarts(self):
        self.home.started(self.server)
        self.assertEqual(Home(self.tmp / "runs").token, self.home.token)


class PlanFake(StructuredFake):
    """Writes the given plan in the plan stage and `merged.txt` in the code stage; approves all."""

    def __init__(self, name, plan):
        super().__init__(name, None)
        self.plan, self.prompts = plan, []

    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        plan = is_plan(prompt)
        self.files = lambda r: {"CAV-PLAN.md": self.plan} if plan else {"merged.txt": f"{self.name}\n"}
        return super().run(prompt, cwd)


class ReconcileTest(unittest.TestCase):
    """Two branches: a blind reconciliation plan first, then both implement from the plan's base."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-recon-"))
        self.work, self.origin, self.base = with_origin(self.tmp)
        self.shas = {}
        for name in ("feat-a", "feat-b"):
            git("checkout", "-q", "-b", name, self.base, cwd=self.work)
            (self.work / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            git("add", "-A", cwd=self.work)
            git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", f"secret author note {name}",
                cwd=self.work)
            self.shas[name] = git("rev-parse", "HEAD", cwd=self.work)
        git("checkout", "-q", "master", cwd=self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pipeline(self, fakes):
        with mock.patch("cav.agents.build", side_effect=lambda spec, log_path, title_prefix="": fakes[spec.name]):
            pipeline = Pipeline(str(self.work), "Reconcile the two branches.", self.tmp / "runs" / "p1",
                                branches=["feat-a", "feat-b"])
            code = pipeline.run()
        return code, pipeline.state, self.tmp / "runs" / "p1"

    def test_blind_plan_then_both_implement_from_the_agreed_base(self):
        fakes = {name: PlanFake(name, "Base: Candidate X\n- keep X\n") for name in ("deepseek", "kimi")}
        code, state, run_dir = self.run_pipeline(fakes)

        self.assertEqual(code, 0, state)
        self.assertEqual(state["outcome"], "merged and pushed")
        labels = state["labels"]
        self.assertEqual({item["commit"] for item in labels.values()}, set(self.shas.values()))
        for label, item in labels.items():
            folder = run_dir / "sources" / f"candidate-{label}"
            self.assertFalse((folder / ".git").exists(), "no history: authors and messages would identify it")
            self.assertTrue((folder / f"{item['ref']}.txt").exists())
        plan_r0 = git("--git-dir", str(run_dir / "plan" / "exchange.git"), "rev-parse", "candidate/kimi/r0")
        self.assertEqual(plan_r0, self.base, "the plan is written in the branches' common ancestor")
        code_r0 = git("--git-dir", str(run_dir / "code" / "exchange.git"), "rev-parse", "candidate/kimi/r0")
        self.assertEqual(code_r0, labels["X"]["commit"], "both implement from the base the plan names")
        for fake in fakes.values():
            for prompt in fake.prompts:
                self.assertIn("Candidate X:", prompt)
                for leak in ("feat-a", "feat-b", "secret author note"):
                    self.assertNotIn(leak, prompt)
        implement = next(p for p in fakes["kimi"].prompts if "Implement the task" in p)
        self.assertIn("starts from Candidate X, the plan's base", implement)
        self.assertIn("A departure from the plan is a MUST_FIX", implement)

    def test_an_unagreed_reconciliation_plan_is_forced_to_one_base(self):
        fakes = {"deepseek": ForcedPlanFake("deepseek", "Base: Candidate X\n- deepseek\n", prefer="Candidate Y"),
                 "kimi": ForcedPlanFake("kimi", "Base: Candidate Y\n- kimi\n", prefer="Candidate Y")}
        code, state, run_dir = self.run_pipeline(fakes)
        self.assertEqual(state["plan_base"], "Y", state)
        code_r0 = git("--git-dir", str(run_dir / "code" / "exchange.git"), "rev-parse", "candidate/kimi/r0")
        self.assertEqual(code_r0, state["labels"]["Y"]["commit"], "both implement from the forced plan's base")

    def test_an_agreed_plan_must_name_its_base(self):
        fakes = {name: PlanFake(name, "- keep both\n") for name in ("deepseek", "kimi")}
        code, state, _ = self.run_pipeline(fakes)
        self.assertEqual(code, 1)
        self.assertIn("names no base", state["detail"])

    def test_plan_base_line_is_read_leniently(self):
        self.assertEqual(pipeline_mod.plan_base("# Plan\n**Base:** Candidate y\n"), "Y")
        self.assertIsNone(pipeline_mod.plan_base("Base: Candidate Z"))


class ForcedPlanFake(StructuredFake):
    """Plan stage: writes its own plan and never approves the other's; decides for `prefer`.
    Code stage: writes merged.txt and approves."""

    def __init__(self, name, plan, prefer=None, must_fix=1):
        super().__init__(name, None)
        self.plan, self.prefer, self.must_fix, self.prompts = plan, prefer, must_fix, []

    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        if DECISION_RE.search(prompt):
            return decide(self, prompt, self.prefer)
        if is_plan(prompt):
            self.files = lambda r: {"CAV-PLAN.md": self.plan}
            self.approve = False
            self.findings = [dict(BLOCKING, id=f"F-{i}") for i in range(1, self.must_fix + 1)]
        else:
            self.files = lambda r: {"merged.txt": f"{self.name}\n"}
            self.approve, self.findings = True, []
        return super().run(prompt, cwd)


class ForcedChoiceTest(unittest.TestCase):
    """The plan stage's last round: a blind forced choice between the two plans."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-forced-"))
        self.repo, self.base = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_plan_stage(self, fakes, rounds=3):
        case = config.Case(name="forced", host_repo=self.repo, base=self.base, bootstrap="base", mode="plan",
                           agents={"deepseek": config.AgentSpec("deepseek", "opencode"),
                                   "kimi": config.AgentSpec("kimi", "kimi")},
                           max_rounds=rounds, round_seconds=30, stall_seconds=5, retries=0,
                           task={"prompt": "Plan it."})
        with mock.patch("cav.agents.build", side_effect=lambda spec, log_path, title_prefix="": fakes[spec.name]):
            orchestrator = Orchestrator(case, self.tmp / "run")
            return orchestrator, orchestrator.run()

    def plan_of(self, orchestrator, commit):
        return git("--git-dir", str(orchestrator.exchange.path), "show", f"{commit}:CAV-PLAN.md")

    def test_one_plan_both_approve_ends_the_stage(self):
        """Approving the other's plan is enough; it need not approve both."""
        fakes = {"deepseek": StructuredFake("deepseek", lambda r: {"CAV-PLAN.md": "deepseek plan\n"},
                                            approve=False, findings=[BLOCKING]),
                 "kimi": StructuredFake("kimi", lambda r: {"CAV-PLAN.md": "kimi plan\n"})}
        orchestrator, state = self.run_plan_stage(fakes)
        self.assertEqual(state["final"]["round"], 2)
        self.assertEqual(state["history"][1]["blockers"]["agreed_on"], ["deepseek"])
        self.assertEqual(self.plan_of(orchestrator, state["final"]["commit"]), "deepseek plan")

    def test_same_choice_wins_and_its_defects_travel_with_it(self):
        fakes = {name: ForcedPlanFake(name, f"{name} plan\n", prefer="kimi plan") for name in ("deepseek", "kimi")}
        orchestrator, state = self.run_plan_stage(fakes)
        decision = state["final"]["forced"]
        self.assertEqual((decision["winner"], decision["method"]), ("kimi", "both chose it"))
        self.assertEqual(self.plan_of(orchestrator, state["final"]["commit"]), "kimi plan")
        sources = sorted(item["from"] for item in decision["defects"])
        self.assertEqual(sources, ["deepseek", "deepseek", "kimi"],
                         "both pickers' defects plus the round-2 MUST_FIX against the winner")
        prompt = (orchestrator.run_dir / "rounds" / "r3" / "kimi-context.md").read_text(encoding="utf-8")
        self.assertNotIn("deepseek's", prompt)
        self.assertNotIn("kimi's", prompt)

    def test_a_split_goes_to_the_plan_with_fewer_open_must_fix(self):
        fakes = {"deepseek": ForcedPlanFake("deepseek", "deepseek plan\n", prefer="deepseek plan", must_fix=2),
                 "kimi": ForcedPlanFake("kimi", "kimi plan\n", prefer="kimi plan", must_fix=1)}
        _, state = self.run_plan_stage(fakes)
        decision = state["final"]["forced"]
        self.assertEqual(decision["winner"], "deepseek", "kimi's plan has 2 open MUST_FIX, deepseek's 1")
        self.assertIn("fewer open MUST_FIX (1 vs 2)", decision["method"])

    def test_a_tied_split_is_a_recorded_coin_flip(self):
        fakes = {"deepseek": ForcedPlanFake("deepseek", "deepseek plan\n", prefer="deepseek plan"),
                 "kimi": ForcedPlanFake("kimi", "kimi plan\n", prefer="kimi plan")}
        with mock.patch("cav.orchestrator.random.choice", return_value="kimi") as flip:
            _, state = self.run_plan_stage(fakes)
        flip.assert_called_once()
        self.assertEqual(state["final"]["forced"]["winner"], "kimi")
        self.assertIn("coin flip", state["final"]["forced"]["method"])


class HomeBranchTest(unittest.TestCase):
    setUp, tearDown, launches, post = HomeTest.setUp, HomeTest.tearDown, HomeTest.launches, HomeTest.post

    def test_start_passes_both_branches_or_refuses(self):
        for name in ("one", "two"):
            git("branch", name, cwd=self.repo)
        for branches, code in ((["one"], 400), (["one", "missing"], 400), (["one", "one"], 400)):
            patch, launched = self.launches()
            with patch, self.assertRaises(urllib.error.HTTPError) as caught:
                self.post({"repo": str(self.repo), "prompt": "x", "branches": branches})
            self.assertEqual(caught.exception.code, code, branches)
            self.assertEqual(launched, [])
        patch, launched = self.launches()
        with patch:
            self.post({"repo": str(self.repo), "prompt": "x", "branches": ["one", "two"]}).close()
        self.assertEqual(launched[0][-4:], ["--branch", "one", "--branch", "two"])


if __name__ == "__main__":
    unittest.main()
