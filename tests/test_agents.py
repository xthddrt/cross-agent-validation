"""Agent adapters, the event log and case files."""

import json
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

    def test_agent_messages_are_not_truncated(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            writer = events.EventWriter(tmp / "events.jsonl", "run")
            writer.emit("agent.text", 1, "kimi", {"text": "x" * 9000})
            payload, _ = events.tail(tmp / "events.jsonl")
            self.assertEqual(len(payload[0]["data"]["text"]), 9000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tail_reads_only_new_complete_lines(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-events-"))
        try:
            path = tmp / "events.jsonl"
            events.EventWriter(path, "run").emit("one", 1, None, {})
            found, offset = events.tail(path)
            self.assertEqual([item["type"] for item in found], ["one"])
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write('{"seq": 2, "type": "half')
            found, after = events.tail(path, offset)
            self.assertEqual((found, after), ([], offset), "a partial line waits for the next call")
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write('"}\n')
            found, after = events.tail(path, offset)
            self.assertEqual([item["type"] for item in found], ["half"])
            path.write_text("", encoding="utf-8")
            self.assertEqual(events.tail(path, after), ([], 0), "a replaced log starts over")
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

    def test_ponytail_is_activated_at_the_cav_level(self):
        agent = self.agent()
        calls = self.fake_api(agent, {
            "/skills": {"data": {"skills": [{"name": agent_mod.PONYTAIL_SKILL}]}},
            ":activate": {"data": {"activated": True}},
            "/sessions/s": {"data": {"busy": False, "last_turn_reason": "completed"}}})
        agent._activate_ponytail()
        self.assertIn(("POST", f"/api/v1/sessions/s/skills/{agent_mod.PONYTAIL_SKILL}:activate",
                       {"args": agent_mod.PONYTAIL_LEVEL}), calls)

    def test_missing_ponytail_fails_instead_of_running_without_it(self):
        agent = self.agent()
        self.fake_api(agent, {"/skills": {"data": {"skills": [{"name": "other"}]}}})
        with self.assertRaisesRegex(RuntimeError, "ponytail"):
            agent._activate_ponytail()

    def test_opencode_runs_ponytail_at_the_cav_level(self):
        agent = agent_mod.OpenCodeAgent(config.AgentSpec("deepseek", "opencode"), "log.txt")
        self.assertEqual(agent._env()["PONYTAIL_DEFAULT_MODE"], agent_mod.PONYTAIL_LEVEL)

    def test_opencode_session_directory_is_the_worktree(self):
        """OpenCode reads the session directory from PWD; an inherited one points elsewhere."""
        agent = agent_mod.OpenCodeAgent(config.AgentSpec("deepseek", "opencode"), "log.txt")
        with mock.patch.dict(os.environ, {"PWD": "C:/somewhere/else"}):
            self.assertEqual(agent._env("C:/run/wt/r1/deepseek")["PWD"], "C:/run/wt/r1/deepseek")


class AdapterCaptureTest(unittest.TestCase):
    def test_kimi_capture_emits_an_updated_part_once(self):
        with tempfile.TemporaryDirectory(prefix="cav-capture-") as tmp:
            emitted = []
            agent = agent_mod.KimiAgent(config.AgentSpec("kimi", "kimi"), pathlib.Path(tmp) / "kimi.log",
                                        emit=lambda kind, name, data: emitted.append((kind, data)))
            message = {"id": "m", "role": "assistant",
                       "content": [{"id": "p", "type": "text", "text": "hello"}]}
            agent._capture([message])
            agent._capture([message])
            message["content"][0]["text"] = "hello, updated"
            agent._capture([message])
            agent._transcript.close()
            texts = [data["text"] for kind, data in emitted if kind == "agent.text"]
            self.assertEqual(texts, ["hello", "hello, updated"])
            self.assertEqual(len((pathlib.Path(tmp) / "kimi.log").read_text().splitlines()), 2)


class ConfigTest(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[1] / "cases"

    def test_shipped_cases_load(self):
        case = config.load(self.ROOT / "msmon-five-phase.local.json")
        self.assertEqual(sorted(case.agents), ["deepseek", "kimi"])
        self.assertEqual(case.agents["kimi"].bootstrap_ref, "origin/candidate/kimi-phase-1-5")
        self.assertEqual(len(case.checks), 4)
        self.assertEqual(case.other("kimi"), "deepseek")
        self.assertEqual(case.agents["deepseek"].model, "deepseek/deepseek-flash#high")
        self.assertEqual(case.agents["kimi"].model, "kimi-code/k3.1-pro")
        self.assertEqual(case.agents["kimi"].thinking, "high")
        config.load(self.ROOT / "msmon-five-phase.json")

    def test_tool_case_has_no_shared_base(self):
        case = config.load(self.ROOT / "cross-agent-validation.json")
        self.assertIsNone(case.base)
        self.assertEqual(case.agents["deepseek"].bootstrap_ref, "HEAD")
        self.assertEqual(case.secondary_repo.name, "cross-agent-validation")


class AgentLifecycleTest(unittest.TestCase):
    def test_reset_clears_abort_and_failure_state(self):
        agent = agent_mod.Agent(config.AgentSpec("kimi", "kimi"), "log.txt")
        agent.state.error = "round timeout"
        agent.abort()
        agent.reset()
        self.assertFalse(agent._abort.is_set())
        self.assertIsNone(agent.state.error)

    def test_opencode_abort_kills_running_process(self):
        spec = config.AgentSpec("deepseek", "opencode")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        agent._proc = mock.Mock()
        agent._proc.poll.return_value = None
        agent.abort()
        agent._proc.kill.assert_called_once_with()

    def test_aborted_opencode_keeps_the_monitor_reason(self):
        class Proc:
            returncode = -9

            def __init__(self):
                self.stdout = iter(())

            def wait(self):
                return -9

        spec = config.AgentSpec("deepseek", "opencode")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"), \
                mock.patch("cav.agents.subprocess.Popen", return_value=Proc()):
            agent = agent_mod.OpenCodeAgent(spec, pathlib.Path(tmp) / "run.log")
            agent.state.error = "blocked: waiting:permission"
            agent.abort()
            result = agent.run("prompt", tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "blocked: waiting:permission")


class ModelPinningTest(unittest.TestCase):
    def test_opencode_command_pins_model(self):
        spec = config.AgentSpec("deepseek", "opencode", model="deepseek/deepseek-flash#high")
        with mock.patch("cav.agents._opencode_cli", return_value="opencode-cli.exe"):
            agent = agent_mod.OpenCodeAgent(spec, "log.txt")
        command = agent._command("hello")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "deepseek/deepseek-flash#high")
        self.assertEqual(command[-1], "hello")

    def test_kimi_profile_pins_model_and_thinking(self):
        spec = config.AgentSpec("kimi", "kimi", model="kimi-code/k3.1-pro", thinking="high")
        agent = agent_mod.KimiAgent(spec, "log.txt")
        self.assertEqual(agent._agent_config(),
                         {"model": "kimi-code/k3.1-pro", "permission_mode": "yolo", "thinking": "high"})


class CaseFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cav-case-"))
        self.repo, self.base = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def case(self, **overrides):
        values = dict(name="flow", host_repo=self.repo, base=self.base,
                      agents={"deepseek": config.AgentSpec("deepseek", "opencode"),
                              "kimi": config.AgentSpec("kimi", "kimi")})
        values.update(overrides)
        return config.Case(**values)

    def test_removed_settings_are_rejected(self):
            for key, value in (("canonical_agent", "deepseek"), ("carry_forward_limit", 20),
                               ("convergence", "strict"), ("strict_paths", []), ("formatter", "x")):
                path = self.tmp / f"{key}.json"
                path.write_text(json.dumps({
                    "name": "x", "repo": {"host": str(self.repo), "base": self.base},
                    "agents": {"deepseek": {"kind": "opencode", "bootstrap_ref": self.base},
                               "kimi": {"kind": "kimi", "bootstrap_ref": self.base}},
                    key: value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, key):
                    config.load(path)

    def test_task_prompt_and_file_are_exclusive(self):
            path = self.tmp / "case.json"
            path.write_text(json.dumps({
                "name": "x", "repo": {"host": str(self.repo), "base": self.base},
                "agents": {"deepseek": {"kind": "opencode", "bootstrap_ref": self.base},
                           "kimi": {"kind": "kimi", "bootstrap_ref": self.base}},
                "task": {"prompt": "a", "prompt_file": "b.md"},
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                config.load(path)



if __name__ == "__main__":
    unittest.main()
