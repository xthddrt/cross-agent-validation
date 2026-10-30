"""The dashboards, the home server, and the repo and push helpers they and the flow use."""

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

from cav import flow as flow_mod
from cav.gitops import git
from cav.web import Dashboard, Home


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


def with_origin(tmp):
    """A work clone whose origin is a local bare repo, standing in for GitHub."""
    seed, base = make_repo(tmp)
    origin = tmp / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(origin))
    work = tmp / "work"
    git("clone", "-q", str(origin), str(work))
    return work, origin, base


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
                with urllib.request.urlopen(base + "/api/divergence?round=1", timeout=5) as response:
                    self.assertEqual(json.loads(response.read()), {}, "a round not yet compared is empty")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/wrong/api/state", timeout=5)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/api/diff?round=1&path=../secret", timeout=5)
                self.assertEqual(caught.exception.code, 400, "a path outside the tree is refused")
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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

    def move_origin(self, name, text):
        other = self.tmp / "other"
        git("clone", "-q", str(self.origin), str(other))
        self.commit(other, name, text)
        git("push", "-q", "origin", f"HEAD:{self.branch}", cwd=other)

    def test_merges_when_origin_moved(self):
        agreed = self.commit(self.work, "feature.txt", "agreed\n")
        self.move_origin("theirs.txt", "someone else\n")
        pushed = flow_mod.push_result(self.work, self.branch, agreed, "t")
        self.assertNotEqual(pushed, agreed)
        files = git("--git-dir", str(self.origin), "ls-tree", "-r", "--name-only", self.branch).splitlines()
        self.assertIn("feature.txt", files)
        self.assertIn("theirs.txt", files)

    def test_refuses_a_conflicting_merge(self):
        agreed = self.commit(self.work, "base.txt", "ours\n")
        self.move_origin("base.txt", "theirs\n")
        before = git("--git-dir", str(self.origin), "rev-parse", self.branch)
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            flow_mod.push_result(self.work, self.branch, agreed, "t")
        self.assertEqual(git("--git-dir", str(self.origin), "rev-parse", self.branch), before)


class HomeTest(unittest.TestCase):
    def launches(self):
        """Catch the pipeline launch only; git still needs the real Popen."""
        real, launched = subprocess.Popen, []

        def popen(args, *rest, **options):
            if list(args[1:4]) == ["-m", "cav", "run"]:
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

    def test_start_launches_a_run_process(self):
        patch, launched = self.launches()
        with patch:
            with self.post({"repo": str(self.repo), "prompt": "Do the thing"}) as response:
                body = json.loads(response.read())
        self.assertEqual(len(launched), 1)
        args = launched[0]
        self.assertEqual(args[1:4], ["-m", "cav", "run"])
        self.assertIn(str(self.repo), args)
        run_dir = self.tmp / "runs" / body["id"]
        self.assertEqual((run_dir / "prompt.md").read_text(encoding="utf-8").strip(), "Do the thing")
        self.assertFalse((run_dir / "pipeline.json").exists(), "a flow run keeps its status in state.json")

        with urllib.request.urlopen(self.base + "/api/runs", timeout=5) as response:
            runs = json.loads(response.read())
        self.assertEqual([run["id"] for run in runs], [body["id"]])
        self.assertEqual(runs[0]["prompt"].strip(), "Do the thing")
        (run_dir / "pipeline.json").write_text("{}", encoding="utf-8")  # as the form wrote it before
        (run_dir / "state.json").write_text(json.dumps({"flow": True, "phase": "judge", "round": 1}), encoding="utf-8")
        with urllib.request.urlopen(self.base + f"/run/{body['id']}/", timeout=5) as response:
            self.assertTrue(response.geturl().endswith(f"/run/{body['id']}/"), "a flow run opens its own dashboard")
        with urllib.request.urlopen(self.base + "/api/runs", timeout=5) as response:
            self.assertEqual(json.loads(response.read())[0]["phase"], "judge")


if __name__ == "__main__":
    unittest.main()
