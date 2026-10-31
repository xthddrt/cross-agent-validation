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
                         {"repos": [{"repo": str(self.repo), "branches": ["", ""]}], "task": "Do the thing", "checks": []},
                         "an empty repo row is ignored; no branches means a task")
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


if __name__ == "__main__":
    unittest.main()
