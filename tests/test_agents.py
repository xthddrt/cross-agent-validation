"""Agent adapters and the event log."""

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from cav import agents as agent_mod
from cav import config, events
from cav.gitops import git


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


class EventsTest(unittest.TestCase):
    def test_order_redaction_and_clip(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            writer = events.EventWriter(tmp / "events.jsonl", "run")
            writer.emit("one", 1, "kimi", {"text": "x" * 9000})
            writer.emit("two", 1, "kimi", {"header": "Authorization: Bearer abcdefghijklmnop"})
            writer.emit("three", 1, "kimi", {"token": "abc123",
                                             "nested": {"password": "hunter2", "note": "ok"}})
            payload, _ = events.tail(tmp / "events.jsonl")
            self.assertEqual([item["seq"] for item in payload], [1, 2, 3])
            self.assertIn("truncated", payload[0]["data"]["text"])
            self.assertIn("[redacted]", payload[1]["data"]["header"])
            self.assertEqual(payload[2]["data"]["token"], "[redacted]")
            self.assertEqual(payload[2]["data"]["nested"]["password"], "[redacted]")
            self.assertEqual(payload[2]["data"]["nested"]["note"], "ok")
            resumed = events.EventWriter(tmp / "events.jsonl", "run")
            self.assertEqual(resumed.emit("four")["seq"], 4, "a resumed run continues the sequence")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class KimiBlockingTest(unittest.TestCase):
    def agent(self):
        return agent_mod.KimiAgent(config.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro"),
                                   "log.txt")

    def test_pending_approval_wins_over_busy(self):
        """Kimi reports busy=True while waiting for approval; the block must still be visible."""
        agent = self.agent()
        self.assertEqual(agent._pending({"busy": True, "main_turn_active": True,
                                         "pending_interaction": "approval"}), "waiting:approval")
        self.assertIsNone(agent._pending({"busy": True, "pending_interaction": "none"}))
        self.assertIsNone(agent._pending({}))

    def test_permission_mismatch_fails_fast(self):
        agent = self.agent()
        agent.session_id = "s"
        agent._call = lambda *args, **kwargs: {"data": {"permission": "ask"}}
        with self.assertRaises(RuntimeError):
            agent._verify_permission("yolo")
        agent._call = lambda *args, **kwargs: {"data": {"permission": "yolo"}}
        agent._verify_permission("yolo")
        agent._call = lambda *args, **kwargs: {"data": {}}
        agent._verify_permission("yolo")

    def fake_api(self, agent, responses):
        calls = []

        def call(method, path, body=None, timeout=60):
            calls.append((method, path, body))
            return next(value for key, value in responses.items() if path.endswith(key))
        agent.session_id = "s"
        agent._call = call
        return calls

    def test_approval_is_answered_for_this_session_only(self):
        """Kimi's dangerous-command guard asks even in yolo mode; a CAV session approves it."""
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/snapshot": {"data": {"pending_approvals": [
                {"approval_id": "a1", "tool_name": "Bash", "action": "Running: rm -rf junk"}]}},
            "/interactions/a1": {"data": {"outcome": "approved"}}})
        self.assertEqual(agent._approve_pending(), 1)
        self.assertEqual(calls[-1], ("POST", "/api/v1/sessions/s/interactions/a1",
                                     {"kind": "approval", "decision": "approved", "scope": "session"}))

    def test_question_is_dismissed_so_the_agent_goes_on(self):
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/snapshot": {"data": {"pending_questions": [
                {"question_id": "q1", "questions": [{"question": "Which colour?"}]}]}},
            "/interactions/q1": {"data": {"outcome": "dismissed"}}})
        self.assertEqual(agent._dismiss_questions(), 1)
        self.assertEqual(calls[-1], ("POST", "/api/v1/sessions/s/interactions/q1",
                                     {"kind": "question", "dismissed": True}))

    def test_a_reply_is_seen_in_a_session_longer_than_one_page(self):
        """Kimi lists only the newest 50 messages: once a session is longer, the number of replies
        on the page stops growing, and a finished turn must still be seen."""
        agent = self.agent()
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-kimi-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        agent.log_path = tmp / "kimi.log"
        agent.base = "http://kimi"
        history = [{"id": f"m{n}", "role": "assistant" if n % 2 else "user", "created_at": f"2026-10-30T14:{n:02d}:00Z",
                    "content": [{"type": "text", "text": f"old {n}"}]} for n in range(60)]
        submitted = []

        def call(method, path, body=None, timeout=60):
            if path.endswith("/prompts"):
                submitted.append(body)
                history.extend([{"id": "new-user", "role": "user", "created_at": "2026-10-30T15:00:00Z", "content": []},
                                {"id": "new", "role": "assistant", "created_at": "2026-10-30T15:00:05Z",
                                 "content": [{"type": "text", "text": "done"}]}])
                return {"data": {"prompt_id": "p"}}
            if path.endswith("/messages"):  # newest first, one page of 50
                return {"data": {"items": sorted(history, key=lambda m: m["created_at"], reverse=True)[:50],
                                 "has_more": True}}
            if path.endswith("/status"):
                return {"data": {}}
            return {"data": {"busy": False, "last_turn_reason": "completed"}}
        agent.session_id = "s"
        agent._call = call
        with mock.patch.object(agent_mod, "KIMI_POLL_SECONDS", 0):
            result = agent.run("judge", tmp)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "done")
        self.assertEqual(len(submitted), 1)


    def test_opencode_session_directory_is_the_worktree(self):
        """OpenCode reads the session directory from PWD; an inherited one points elsewhere."""
        agent = agent_mod.OpenCodeAgent(config.AgentSpec("deepseek", "opencode"), "log.txt")
        with mock.patch.dict(os.environ, {"PWD": "C:/somewhere/else"}):
            self.assertEqual(agent._env("C:/run/wt/r1/deepseek")["PWD"], "C:/run/wt/r1/deepseek")


if __name__ == "__main__":
    unittest.main()
