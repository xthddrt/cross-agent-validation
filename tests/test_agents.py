"""Agent adapters and the event log."""

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from cav import agents as agent_mod
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


class KimiBlockingTest(unittest.TestCase):
    def agent(self):
        return agent_mod.KimiAgent(agent_mod.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro"),
                                   "log.txt")


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
        with mock.patch.object(agent_mod, "_opencode_cli", return_value="opencode"):  # installed or not
            agent = agent_mod.OpenCodeAgent(agent_mod.AgentSpec("deepseek", "opencode"), "log.txt")
        with mock.patch.dict(os.environ, {"PWD": "C:/somewhere/else"}):
            self.assertEqual(agent._env("C:/run/wt/r1/deepseek")["PWD"], "C:/run/wt/r1/deepseek")


class ClaudeAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-claude-"))
        (self.tmp / "claude.exe").write_text("", encoding="utf-8")
        (self.tmp / "ponytail" / ".claude-plugin").mkdir(parents=True)
        (self.tmp / "ponytail" / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        env = {"CAV_CLAUDE_CLI": str(self.tmp / "claude.exe"), "CAV_PONYTAIL_DIR": str(self.tmp / "ponytail")}
        with mock.patch.dict(os.environ, env):
            spec = agent_mod.AgentSpec("claude", "claude", "opus", "high")
            self.agent = agent_mod.build(spec, self.tmp / "claude.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_unattended_on_the_model_and_effort_under_ponytail(self):
        cmd = self.agent._command("TASK do it")
        self.assertEqual(cmd[-1], "TASK do it")
        for flag, value in (("--permission-mode", "bypassPermissions"), ("--model", "opus"), ("--effort", "high"),
                            ("--output-format", "stream-json")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("never stop to ask", cmd[cmd.index("--append-system-prompt") + 1])
        self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], str(self.tmp / "ponytail"), "ponytail's own plugin")
        self.assertNotIn("--resume", cmd)
        self.agent.session_id = "s-1"
        self.assertEqual(self.agent._command("next")[-3:], ["--resume", "s-1", "next"], "one session per round")

    def test_stream_is_read_into_progress_and_the_answer(self):
        seen = []
        self.agent.emit = lambda type_, name, data: seen.append(type_)
        for event in ({"type": "system", "subtype": "init", "session_id": "s-9", "model": "claude-opus-5-5"},
                      {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                                                     "input": {"file_path": "a.txt"}}]}},
                      {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                "content": "written"}]}},
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": "DONE"}]}},
                      {"type": "result", "subtype": "success", "is_error": False, "result": "DONE"}):
            self.agent._consume(json.dumps(event))
        self.assertEqual(self.agent.session_id, "s-9")
        self.assertEqual(self.agent.state.text, "DONE")
        self.assertIsNone(self.agent.state.error)
        self.assertEqual(seen, ["agent.model", "agent.tool.start", "agent.tool.end", "agent.text"])


if __name__ == "__main__":
    unittest.main()
