"""The dashboard server: one token, starts and stops runs, serves each run's state."""

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

from cav.gitops import git, resolve_ref, resolve_repo
from cav.web import Home
from tests.test_agents import make_repo


class HomeTest(unittest.TestCase):
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

    def post(self, path, body):
        request = urllib.request.Request(f"{self.base}/api/{path}", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(request, timeout=5).read())

    def get(self, path):
        return json.loads(urllib.request.urlopen(f"{self.base}/{path}", timeout=5).read())

    def test_a_run_is_started_listed_shown_and_stopped(self):
        real, launched = subprocess.Popen, []

        def popen(args, *rest, **options):  # catch the run's launch; git still needs the real Popen
            if list(args[1:4]) == ["-m", "cav", "run"]:
                launched.append(list(args))
                return mock.Mock(pid=4242)
            return real(args, *rest, **options)
        with mock.patch("cav.web.subprocess.Popen", side_effect=popen):
            run_id = self.post("start", {"repos": [{"repo": str(self.repo), "branches": ["", ""]}, {"repo": ""}],
                                         "text": "Do the thing"})["id"]
        run_dir = self.tmp / "runs" / run_id
        self.assertEqual(launched[0][launched[0].index("--spec") + 1], str(run_dir / "spec.json"))
        self.assertEqual(json.loads((run_dir / "spec.json").read_text(encoding="utf-8")),
                         {"repos": [{"repo": str(self.repo), "branches": ["", ""]}], "task": "Do the thing", "checks": [],
                          "agents": ["deepseek", "kimi"]},
                         "an empty repo row is ignored; no branches means a task; no agents named means the defaults")
        self.assertEqual((run_dir / "input.md").read_text(encoding="utf-8").strip(), "Do the thing")
        blind = self.tmp / "blind"
        blind.mkdir()
        (blind / "c1-plan-1.md").write_text("the plan", encoding="utf-8")
        (run_dir / "state.json").write_text(json.dumps({"phase": "review", "history": [], "blind": str(blind)}),
                                            encoding="utf-8")
        page = urllib.request.urlopen(f"{self.base}/run/{run_id}/api/file?path=c1-plan-1.md", timeout=5)
        self.assertEqual(page.read().decode("utf-8"), "the plan", "the dashboard opens a file the agents wrote")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{self.base}/run/{run_id}/api/file?path=../runs/serve.json", timeout=5)
        with mock.patch("cav.web.alive", return_value=True):
            self.assertEqual([(run["id"], run["phase"]) for run in self.get("api/runs")], [(run_id, "review")])
            self.assertEqual(self.get(f"run/{run_id}/api/state")["input"].strip(), "Do the thing")
            with mock.patch("cav.web.subprocess.run") as kill:
                self.post("stop", {"id": run_id})
        self.assertIn("4242", kill.call_args[0][0])
        self.assertEqual(json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["phase"], "stopped")

    def test_bad_requests_are_refused(self):
        repo = lambda *branches: [{"repo": str(self.repo), "branches": list(branches)}]
        for body, error in (({"repos": [{"repo": str(self.tmp / "nope")}], "text": "x"}, "not a git repository or GitHub URL"),
                            ({"repos": [], "text": "x"}, "at least one repository"),
                            ({"repos": repo("a", "a"), "text": "x"}, "the two branch sets are the same"),
                            ({"repos": repo("a", ""), "text": "x"}, "not found"),
                            ({"repos": repo(), "text": ""}, "give the task"),
                            ({"repos": repo("a", ""), "text": ""}, "give the plan")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("start", body)
            self.assertIn(error, json.loads(caught.exception.read())["error"])
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base.rsplit("/", 1)[0] + "/wrong-token/api/runs", timeout=5)

    def test_a_github_url_reuses_its_clone_and_finds_fetched_branches(self):
        clones = self.tmp / "clones"
        clone = clones / "repo"
        shutil.copytree(self.repo, clone)
        git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=clone)
        git("update-ref", "refs/remotes/origin/feature", "HEAD", cwd=clone)  # as a fetch would leave it
        with mock.patch("cav.gitops.git", side_effect=lambda *args, **kw: "" if args[0] == "fetch" else git(*args, **kw)):
            self.assertEqual(resolve_repo("https://github.com/owner/repo", clones), clone.resolve())
        self.assertEqual(resolve_ref(clone, "feature"), git("rev-parse", "HEAD", cwd=clone))
        git("remote", "set-url", "origin", "https://github.com/someone-else/repo.git", cwd=clone)
        with self.assertRaisesRegex(ValueError, "already exists"):
            resolve_repo("https://github.com/owner/repo", clones)
        with self.assertRaises(urllib.error.HTTPError):
            self.get("run/../api/state")

    def test_a_reused_pid_is_not_alive_or_stopped(self):
        import os
        from cav import web

        run_dir = self.tmp / "runs" / "reused"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps({"phase": "review"}), encoding="utf-8")
        meta = run_dir / "run.json"
        meta.write_text(json.dumps({"pid": os.getpid(), "started": 100}), encoding="utf-8")
        with mock.patch("cav.web.process_start", return_value=101), mock.patch("cav.web.subprocess.run") as kill:
            self.assertFalse(web.alive(os.getpid(), 100))
            self.assertTrue(web.alive(os.getpid(), 101))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("stop", {"id": run_dir.name})
        self.assertEqual(caught.exception.code, 400)
        kill.assert_not_called()

        meta.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        os.utime(meta, (100, 100))
        with mock.patch("cav.web.process_start", return_value=101), mock.patch("cav.web.subprocess.run") as kill:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("stop", {"id": run_dir.name})
        self.assertEqual(caught.exception.code, 400)
        kill.assert_not_called()

    def test_launch_and_resume_record_process_identity(self):
        import os
        import types
        from cav import cli, web

        launched = self.tmp / "launched"
        launched.mkdir()
        with mock.patch("cav.web.subprocess.Popen", return_value=mock.Mock(pid=4242)), \
                mock.patch("cav.web.process_start", return_value=123):
            self.home.launch(launched, ["run", "--spec", "spec.json"], "repo")
        self.assertEqual(json.loads((launched / "run.json").read_text(encoding="utf-8"))["started"], 123)

        resumed = self.tmp / "resumed"
        resumed.mkdir()
        (resumed / "run.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        flow = mock.Mock(run_dir=resumed)
        flow.run.return_value = 0
        with mock.patch("cav.cli.Flow.resume", return_value=flow):
            self.assertEqual(cli.cmd_resume(types.SimpleNamespace(run_dir=str(resumed))), 0)
        data = json.loads((resumed / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(data["started"], web.process_start(os.getpid()))

    def test_a_cli_run_is_live_and_not_resumed_while_it_runs(self):
        import os
        import types
        from cav import cli, web

        run_dir = self.home.runs_dir / "cli-run"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps({"phase": "review", "resume": {"next": "review"}, "repo": "r"}),
                                            encoding="utf-8")
        (run_dir / "run.json").write_text(json.dumps({"pid": 1, "repo": "r", "started": 1.0}), encoding="utf-8")
        spec = self.tmp / "spec.json"
        spec.write_text(json.dumps({"repos": [{"repo": str(self.repo)}], "task": "t"}), encoding="utf-8")

        def running():
            self.assertTrue(web.meta_alive(run_dir / "run.json"))
            with mock.patch.object(Home, "launch") as launch:
                with self.assertRaisesRegex(ValueError, "still running"):
                    self.home.resume("cli-run")
            launch.assert_not_called()
            self.assertEqual(next(run for run in self.home.runs() if run["id"] == "cli-run")["phase"], "review")
            return 0

        flow = mock.Mock(run_dir=run_dir, state={"repo": "r"})
        flow.run.side_effect = running
        with mock.patch("cav.cli.Flow", return_value=flow):
            self.assertEqual(cli.cmd_run(types.SimpleNamespace(spec=str(spec), run_dir=str(run_dir))), 0)
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["started"], web.process_start(os.getpid()))
        self.assertEqual(data["repo"], "r")

    def test_a_cli_run_reads_a_utf8_bom_spec(self):
        import types
        from cav import cli

        run_dir = self.home.runs_dir / "bom-run"
        run_dir.mkdir()
        spec = self.tmp / "bom-spec.json"
        spec.write_text(json.dumps({"repos": []}), encoding="utf-8-sig")
        flow = mock.Mock(run_dir=run_dir, state={"repo": "r"})
        flow.run.return_value = 0
        with mock.patch("cav.cli.Flow", return_value=flow):
            self.assertEqual(cli.cmd_run(types.SimpleNamespace(spec=str(spec), run_dir=str(run_dir))), 0)

    def test_stop_leaves_unreadable_state_for_inspection(self):
        run_dir = self.tmp / "runs" / "damaged"
        run_dir.mkdir()
        (run_dir / "state.json").write_bytes(b"")
        (run_dir / "run.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        with mock.patch("cav.web.alive", return_value=True), mock.patch("cav.web.subprocess.run"):
            self.post("stop", {"id": run_dir.name})
        self.assertEqual((run_dir / "state.json").read_bytes(), b"")

    def test_stop_state_writes_are_atomic_and_retry_a_locked_replace(self):
        import os

        run_dir = self.tmp / "runs" / "atomic"
        run_dir.mkdir()
        state = run_dir / "state.json"
        state.write_text(json.dumps({"phase": "review"}), encoding="utf-8")
        (run_dir / "run.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        real_write = pathlib.Path.write_text

        def interrupted(path, text, *args, **kwargs):
            if path.name.startswith("state.json"):
                with path.open("w", encoding="utf-8") as handle:
                    handle.write(text[:1])
                raise OSError("interrupted")
            return real_write(path, text, *args, **kwargs)

        with mock.patch("cav.web.alive", return_value=True), mock.patch("cav.web.subprocess.run"), \
                mock.patch.object(pathlib.Path, "write_text", interrupted):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("stop", {"id": run_dir.name})
        self.assertEqual(caught.exception.code, 500)
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["phase"], "review")

        real_replace, attempts = os.replace, []

        def locked_once(source, destination):
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError("locked")
            return real_replace(source, destination)

        with mock.patch("cav.web.alive", return_value=True), mock.patch("cav.web.subprocess.run"), \
                mock.patch("cav.events.os.replace", side_effect=locked_once), mock.patch("cav.events.time.sleep"):
            self.post("stop", {"id": run_dir.name})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["phase"], "stopped")

    def test_github_refs_prefer_the_fetched_branch(self):
        clone = self.tmp / "clone"
        shutil.copytree(self.repo, clone)
        old = git("rev-parse", "HEAD", cwd=clone)
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "--allow-empty", "-q", "-m", "new", cwd=clone)
        new = git("rev-parse", "HEAD", cwd=clone)
        git("update-ref", "refs/remotes/origin/feature", new, cwd=clone)
        git("branch", "-f", "feature", old, cwd=clone)
        self.assertEqual(resolve_ref(clone, "feature", prefer_origin=True), new)
        self.assertEqual(resolve_ref(clone, "feature"), old)


if __name__ == "__main__":
    unittest.main()
