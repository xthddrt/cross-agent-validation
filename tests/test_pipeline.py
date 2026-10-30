"""Pipeline (plan stage, code stage, merge and push) and the home server, with fake agents."""

import json
import pathlib
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

    def test_no_agreement_summarises_both_stages(self):
        fakes = {name: NeverAgrees(name) for name in ("deepseek", "kimi")}
        code, state = self.run_pipeline(fakes)

        self.assertEqual(code, 2, state)
        self.assertEqual(state["outcome"], "no agreement")
        run_dir = self.tmp / "runs" / "p1"
        self.assertEqual(state["plan"]["outcome"], "disagreement")
        self.assertTrue((run_dir / "plan" / "SUMMARY.md").exists())
        summary = (run_dir / "code" / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("feature.txt", summary)
        self.assertIn("MUST_FIX on kimi's candidate [F-01] `base.txt:1`", summary)
        implement = next(p for p in fakes["deepseek"].prompts if "Implement the task" in p)
        self.assertIn("each agent implements its own", implement)
        self.assertIn(str(run_dir / "plans" / "deepseek.md"), implement)
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", f"refs/heads/{state['branch']}"),
                         self.base, "nothing is pushed without agreement")


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

    def test_no_agreed_plan_stops_before_any_code(self):
        fakes = {name: NeverAgrees(name) for name in ("deepseek", "kimi")}
        code, state, run_dir = self.run_pipeline(fakes)
        self.assertEqual(code, 2, state)
        self.assertEqual(state["outcome"], "no agreement on the plan")
        self.assertTrue((run_dir / "plan" / "SUMMARY.md").exists())
        self.assertFalse((run_dir / "code").exists(), "two different plans are not implemented")

    def test_an_agreed_plan_must_name_its_base(self):
        fakes = {name: PlanFake(name, "- keep both\n") for name in ("deepseek", "kimi")}
        code, state, _ = self.run_pipeline(fakes)
        self.assertEqual(code, 1)
        self.assertIn("names no base", state["detail"])

    def test_plan_base_line_is_read_leniently(self):
        self.assertEqual(pipeline_mod.plan_base("# Plan\n**Base:** Candidate y\n"), "Y")
        self.assertIsNone(pipeline_mod.plan_base("Base: Candidate Z"))


class HomeBranchTest(HomeTest):
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
