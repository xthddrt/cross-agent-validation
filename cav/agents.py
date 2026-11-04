"""Agent adapters: drive OpenCode, Kimi Code and Claude Code headlessly and report progress."""

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .gitops import NO_WINDOW

WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""
# Folders an agent gets on PATH even when the app that started CAV did not have them: git, and Node for
# ponytail's Claude hooks. Windows installers put them in Program Files; on Linux, user installs (Node, the
# Claude CLI) go to ~/.local/bin, which a desktop session started before that folder existed lacks.
TOOL_DIRS = ((r"C:\Program Files\Git\cmd", r"C:\Program Files\nodejs") if WINDOWS
             else (str(pathlib.Path.home() / ".local" / "bin"),))
KIMI_POLL_SECONDS = 3
# Every agent always runs under the ponytail ruleset at this level. OpenCode reads it from the
# environment; Kimi keeps no level, so each Kimi session activates the skill with it; Claude loads
# ponytail's plugin for the session, whose hooks read the level from the environment too.
PONYTAIL_LEVEL = "ultra"
PONYTAIL_SKILL = "ponytail:ponytail"
# agent name -> (kind, model); a run uses two of them, the first two by default. DeepSeek's effort is the
# model's #variant; Kimi and Claude get theirs from the flow (high). Exact ids, not aliases that move.
AGENTS = {
    "deepseek": ("opencode", "deepseek/deepseek-flash#high"),
    "kimi": ("kimi", "kimi-code/k3.1-pro"),
    "claude": ("claude", "claude-opus-5-5"),
}
# what the dashboard shows for each agent: the model and effort a run asks for
SHOWN = {
    "deepseek": "DeepSeek V4.1 Flash 1017, high effort",
    "kimi": "Kimi k3.1-pro, high thinking",
    "claude": "Claude Opus 5.5, high effort",
}
DEFAULT_MODELS = {name: AGENTS[name] for name in ("deepseek", "kimi")}
# Claude Code loads the user's own CLAUDE.md, which may tell it to ask first; nobody answers in a run
NOBODY_ANSWERS = ("Nobody answers questions during this session, whatever other instructions say: never stop to "
                  "ask; decide for yourself and finish the task.")


def models_for(names):
    """The two agents a run uses, by name; none named means the defaults."""
    if not names:
        return DEFAULT_MODELS
    names = list(names)
    unknown = [name for name in names if name not in AGENTS]
    if unknown or len(names) != 2 or names[0] == names[1]:
        raise ValueError(f"a run needs two different agents of {', '.join(AGENTS)}")
    return {name: AGENTS[name] for name in names}


@dataclass
class AgentSpec:
    name: str
    kind: str
    model: str | None = None
    thinking: str | None = None


def _find_exit(value):
    """OpenCode nests execution status differently between versions; find `exit` anywhere."""
    if isinstance(value, dict):
        if isinstance(value.get("exit"), int):
            return value["exit"]
        for nested in value.values():
            found = _find_exit(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_exit(nested)
            if found is not None:
                return found
    return None


@dataclass
class RunState:
    """Live progress for one agent run. Read by the flow's monitor."""
    last_activity: float = field(default_factory=time.time)
    messages: int = 0
    detail: str = "starting"
    finished: bool = False
    error: str | None = None
    text: str = ""

    def touch(self, detail=None, messages=None):
        self.last_activity = time.time()
        if detail is not None:
            self.detail = detail
        if messages is not None:
            self.messages = messages

    @property
    def idle(self):
        return time.time() - self.last_activity


class Agent:
    def __init__(self, spec, log_path):
        self.spec = spec
        self.name = spec.name
        self.log_path = pathlib.Path(log_path)
        self.state = RunState()
        self._abort = threading.Event()
        self.emit = lambda *args: None

    def _event(self, type_, data=None):
        try:
            self.emit(type_, self.name, data or {})
        except Exception:
            pass

    def abort(self):
        self._abort.set()

    def reset(self):
        self._abort.clear()
        self.state = RunState()

    def run(self, prompt, cwd):
        raise NotImplementedError


class CliAgent(Agent):
    def __init__(self, spec, log_path, title_prefix="cav"):
        super().__init__(spec, log_path)
        self.session_id = None
        self.title_prefix = title_prefix
        self._proc = None

    def abort(self):
        super().abort()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _env(self, cwd):
        env = os.environ.copy()
        env["PONYTAIL_DEFAULT_MODE"] = PONYTAIL_LEVEL
        # OpenCode takes the session directory from PWD, not the process directory.
        env["PWD"] = str(cwd)
        env["PATH"] = with_tool_dirs(env.get("PATH", ""))
        return env

    def run(self, prompt, cwd):
        cmd = self._command(prompt)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._event("agent.launch", {"model": self.spec.model, "shown": SHOWN.get(self.name), "cli": self.cli})
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as log:
            self.state.touch("launched")
            self._proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                          env=self._env(cwd), creationflags=NO_WINDOW)
            for line in self._proc.stdout:
                log.write(line)
                log.flush()
                if self._abort.is_set():
                    self._proc.kill()
                    break
                self._consume(line)
            self._proc.wait()
        if self.state.error is None:
            if self._abort.is_set():
                self.state.error = "aborted"
            elif self._proc.returncode != 0:
                self.state.error = f"{self.name_in_errors} exit {self._proc.returncode}"
        self.state.finished = True
        self.state.touch("finished" if not self.state.error else "failed")
        self._event("agent.done", {"ok": self.state.error is None, "reason": self.state.error or "completed"})
        return {"ok": self.state.error is None, "text": self.state.text,
                "reason": self.state.error or "completed", "session": self.session_id}


def with_tool_dirs(path):
    """PATH with every existing TOOL_DIRS folder it lacks appended."""
    have = {os.path.normcase(folder) for folder in path.split(os.pathsep) if folder}
    extra = [folder for folder in TOOL_DIRS if os.path.normcase(folder) not in have and os.path.isdir(folder)]
    return os.pathsep.join([path, *extra] if path else extra)


def config_home():
    """Where desktop apps keep their own files: %APPDATA% on Windows, ~/.config on Linux."""
    if WINDOWS:
        return pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home() / "AppData" / "Roaming")
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")


def _newest(root, pattern):
    """Files under root matching pattern, newest first: an app's own downloads, one folder per version."""
    return [str(path) for path in sorted(pathlib.Path(root).glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)]


def _first(candidates, error):
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(error)


def _opencode_cli():
    # The desktop bundle is preferred over whatever is on PATH: an older opencode CLI
    # sharing the same data directory fails with "Database is not empty and has no session table".
    candidates = [os.environ.get("CAV_OPENCODE_CLI")]
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates += [str(pathlib.Path(local) / "Programs" / folder / "resources" / "opencode-cli.exe")
                           for folder in ("ai.opencode.desktop", "@opencodedesktop")]
    else:
        candidates.append("/opt/OpenCode/resources/opencode-cli")  # the Linux desktop package's bundle
    # then the newest CLI the desktop app downloaded for itself
    candidates += _newest(config_home() / "ai.opencode.desktop" / "cli", f"*/opencode-cli{EXE}")
    candidates.append(shutil.which("opencode"))
    return _first(candidates, "opencode CLI not found; set CAV_OPENCODE_CLI")


class OpenCodeAgent(CliAgent):
    name_in_errors = "opencode"

    def __init__(self, spec, log_path, title_prefix="cav"):
        super().__init__(spec, log_path, title_prefix)
        self.cli = _opencode_cli()

    def _command(self, prompt):
        cmd = [self.cli, "run", "--format", "json", "--auto",
               "--title", f"{self.title_prefix} {self.name}"]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        if self.session_id:
            cmd += ["--session", self.session_id]
        return cmd + [prompt]

    def _consume(self, line):
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if event.get("sessionID"):
            self.session_id = event["sessionID"]
        part = event.get("part") or {}
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            self.state.text = part["text"]
            self.state.touch("writing")
            self._event("agent.text", {"text": part["text"]})
        elif kind == "tool":
            tool = part.get("tool", "?")
            detail = part.get("state") or {}
            call = part.get("callID") or part.get("id")
            self._event("agent.tool.start", {"tool": tool, "call": call, "input": detail.get("input")})
            self._event("agent.tool.end", {"tool": tool, "call": call, "exit": _find_exit(detail),
                                           "output_tail": (detail.get("output") or "")[-4000:]})
            self.state.touch(f"tool:{tool}")
        elif kind == "step-start":
            # OpenCode reports a command only once it ends, so a long one shows here as a quiet step
            self.state.touch("thinking or running a command (OpenCode shows it when it ends)", self.state.messages + 1)
            self._event("agent.step", {"messages": self.state.messages})
        else:
            self.state.touch()


def _kimi_home():
    return pathlib.Path(os.environ.get("CAV_KIMI_HOME") or (pathlib.Path.home() / ".kimi-code"))


class KimiAgent(Agent):
    def __init__(self, spec, log_path, title_prefix="cav"):
        super().__init__(spec, log_path)
        self.title_prefix = title_prefix
        self.base = None
        self.token = None
        self.session_id = None
        self._since = ""  # created_at of the newest message before this prompt
        self._part_fingerprints = {}
        self._transcript = None

    def _connect(self):
        home = _kimi_home()
        instances = sorted((home / "server" / "instances").glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if not instances:
            raise RuntimeError("Kimi Code server not running (no instance file)")
        info = json.loads(instances[0].read_text(encoding="utf-8"))
        self.base = f"http://{info['host']}:{info['port']}"
        self.token = (home / "server.token").read_text(encoding="utf-8").strip()

    def _call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"kimi {method} {path}: HTTP {error.code} {error.read().decode('utf-8', 'replace')[:300]}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise RuntimeError(f"kimi {method} {path}: {error}")

    def _model(self):
        if self.spec.model:
            return self.spec.model
        config = _kimi_home() / "config.toml"
        if config.exists():
            match = re.search(r'(?m)^default_model\s*=\s*"([^"]+)"', config.read_text(encoding="utf-8"))
            if match:
                return match.group(1)
        raise RuntimeError("no Kimi model configured; set agents.<name>.model in the case file")

    def _agent_config(self):
        config = {"model": self._model(), "permission_mode": "yolo"}
        if self.spec.thinking:
            config["thinking"] = self.spec.thinking
        return config

    def _messages(self):
        data = self._call("GET", f"/api/v1/sessions/{self.session_id}/messages")["data"]
        # Kimi lists newest first; oldest first keeps events in order and the last text the last
        return sorted(data.get("items", []), key=lambda message: message.get("created_at") or "")

    def _capture(self, items):
        """Emit new and updated parts; also keep the raw transcript the adapter never wrote."""
        for message_index, message in enumerate(items):
            for index, part in enumerate(message.get("content") or []):
                message_id = message.get("id") or f"#{message_index}"
                part_id = part.get("id") or part.get("part_id") or index
                key = f"{message_id}:{part_id}"
                fingerprint = hashlib.sha256(
                    json.dumps(part, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                if self._part_fingerprints.get(key) == fingerprint:
                    continue
                self._part_fingerprints[key] = fingerprint
                self._transcript.write(json.dumps(part, ensure_ascii=False) + "\n")
                kind = part.get("type")
                if kind == "text" and part.get("text"):
                    self._event("agent.text", {"text": part["text"]})
                elif kind == "tool_use":
                    self._event("agent.tool.start", {"tool": part.get("tool_name"),
                                                     "call": part.get("tool_call_id"),
                                                     "input": part.get("input")})
                elif kind == "tool_result":
                    content = part.get("output") or part.get("content") or ""
                    if not isinstance(content, str):
                        content = json.dumps(content)
                    # Kimi lists a result before its call, so the dashboard pairs them by id
                    self._event("agent.tool.end", {"tool": part.get("tool_name"),
                                                   "call": part.get("tool_call_id"),
                                                   "output_tail": content[-4000:]})
        self._transcript.flush()

    def _pending(self, detail):
        pending = (detail or {}).get("pending_interaction")
        if pending in (None, "", "none"):
            return None
        return f"waiting:{pending}"

    def abort(self):
        super().abort()
        # Stop the turn in Kimi too: an abandoned turn keeps running, or keeps waiting on a
        # prompt someone might approve, while a retry edits the same worktree.
        if self.session_id and not self.base:  # a resumed run's agent, stopping the stopped run's turn
            try:
                self._connect()
            except (OSError, ValueError, KeyError, RuntimeError):  # Kimi not running: no turn left to stop
                return
        if self.base and self.session_id:
            try:
                self._call("POST", f"/api/v1/sessions/{self.session_id}:abort", {}, timeout=15)
            except RuntimeError:
                pass

    def _approve_pending(self):
        """Approve what Kimi asks even in yolo mode (its dangerous-command guard, e.g. rm -rf).

        Only this session's approvals: the guard itself stays on for every other Kimi session.
        Returns how many were approved; a question is left alone and still blocks the run.
        """
        snapshot = self._call("GET", f"/api/v1/sessions/{self.session_id}/snapshot").get("data") or {}
        approved = 0
        for item in snapshot.get("pending_approvals") or []:
            interaction = item.get("approval_id") or item.get("interaction_id")
            if not interaction:
                continue
            self._call("POST", f"/api/v1/sessions/{self.session_id}/interactions/{interaction}",
                       {"kind": "approval", "decision": "approved", "scope": "session"})
            self._event("agent.approved", {"tool": item.get("tool_name"), "action": item.get("action")})
            approved += 1
        return approved

    def _dismiss_questions(self):
        """Nobody answers during a run: dismiss a question so Kimi decides for itself and goes on.

        The round prompt already says never to ask; this covers a question asked anyway.
        """
        snapshot = self._call("GET", f"/api/v1/sessions/{self.session_id}/snapshot").get("data") or {}
        dismissed = 0
        for item in snapshot.get("pending_questions") or []:
            interaction = item.get("question_id") or item.get("interaction_id")
            if not interaction:
                continue
            self._call("POST", f"/api/v1/sessions/{self.session_id}/interactions/{interaction}",
                       {"kind": "question", "dismissed": True})
            asked = "; ".join(q.get("question", "") for q in item.get("questions") or [] if isinstance(q, dict))
            self._event("agent.dismissed", {"question": asked})
            dismissed += 1
        return dismissed

    def _activate_ponytail(self):
        skills = self._call("GET", f"/api/v1/sessions/{self.session_id}/skills").get("data") or {}
        names = [item.get("name") for item in (skills.get("skills") if isinstance(skills, dict) else skills) or []]
        if PONYTAIL_SKILL not in names:
            raise RuntimeError("the ponytail plugin is not installed in Kimi Code; CAV runs both agents "
                               "under ponytail (install it from Kimi's plugins with "
                               "https://github.com/DietrichGebert/ponytail)")
        self._call("POST", f"/api/v1/sessions/{self.session_id}/skills/{PONYTAIL_SKILL}:activate",
                   {"args": PONYTAIL_LEVEL})
        deadline = time.time() + 180
        while time.time() < deadline and not self._abort.is_set():
            detail = self._call("GET", f"/api/v1/sessions/{self.session_id}")["data"]
            if not (detail.get("busy") or detail.get("main_turn_active")) and detail.get("last_turn_reason"):
                self._event("agent.skill", {"skill": PONYTAIL_SKILL, "args": PONYTAIL_LEVEL})
                return
            time.sleep(KIMI_POLL_SECONDS)
        raise RuntimeError(f"kimi did not finish activating {PONYTAIL_SKILL} {PONYTAIL_LEVEL}")

    def _verify_permission(self, requested):
        """A silently different permission mode means the first tool call blocks on a human."""
        try:
            status = self._call("GET", f"/api/v1/sessions/{self.session_id}/status").get("data") or {}
        except RuntimeError:
            return
        effective = status.get("permission")
        if effective and effective != requested:
            raise RuntimeError(
                f"kimi session runs with permission {effective!r}, not {requested!r}; "
                f"a tool call would block on a human approval")

    def run(self, prompt, cwd):
        try:
            return self._run(prompt, cwd)
        finally:
            self.close()  # also when an API call raises: the log handle must not outlive the run

    def close(self):
        if self._transcript:
            self._transcript.close()
            self._transcript = None

    def _run(self, prompt, cwd):
        if not self.base:
            self._connect()
        cwd = str(pathlib.Path(cwd).resolve())
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._transcript = open(self.log_path, "a", encoding="utf-8", errors="replace")
        if not self.session_id:
            # a new session unless the caller set one to continue
            workspace = self._call("POST", "/api/v1/workspaces", {"root": cwd, "name": pathlib.Path(cwd).name})["data"]
            session = self._call("POST", "/api/v1/sessions", {
                "title": f"{self.title_prefix} {self.name}",
                "metadata": {"cwd": cwd},
                "workspace_id": workspace["id"],
            })["data"]
            self.session_id = session["id"]
            self._call("POST", f"/api/v1/sessions/{self.session_id}/profile", {
                "agent_config": self._agent_config(),
            })
            self._verify_permission(self._agent_config()["permission_mode"])
            self._activate_ponytail()
            self._part_fingerprints = {}
        self._since = max((message.get("created_at") or "" for message in self._messages()), default="")
        self._event("agent.launch", {"model": self._model(), "shown": SHOWN.get(self.name), "session": self.session_id})
        self._call("POST", f"/api/v1/sessions/{self.session_id}/prompts",
                   {"content": [{"type": "text", "text": prompt}]})
        self.state.touch("running")

        idle, seen = 0, False
        while not self._abort.is_set():
            detail = self._call("GET", f"/api/v1/sessions/{self.session_id}")["data"]
            items = self._messages()
            self._capture(items)
            # Kimi lists only the newest messages, so a long session's total stops growing; count
            # the replies newer than this prompt instead
            count = sum(1 for m in items if m.get("role") == "assistant" and (m.get("created_at") or "") > self._since)
            busy = detail.get("busy", False) or detail.get("main_turn_active", False)
            seen = seen or busy
            waiting = self._pending(detail)
            if waiting == "waiting:approval" and self._approve_pending():
                self.state.touch("approved")
                idle = 0
            elif waiting == "waiting:question" and self._dismiss_questions():
                self.state.touch("question dismissed")
                idle = 0
            elif waiting:
                # Kimi reports busy=True while it waits for approval, so this must be checked
                # before the busy branch: otherwise the block is invisible until the round timeout.
                self.state.detail = waiting
                idle += 1
            elif count:
                self.state.touch("writing" if busy else "finishing", count)
                idle = 0
            elif busy:
                self.state.touch("running")
                idle = 0
            else:
                idle += 1
            reason = detail.get("last_turn_reason")
            if not busy and count and reason in ("completed", "failed", "cancelled"):
                break
            if seen and not busy and reason == "failed" and not count:
                self.state.error = "kimi turn failed"
                break
            if idle * KIMI_POLL_SECONDS > 120:
                self.state.error = f"kimi never produced output ({self.state.detail})"
                break
            time.sleep(KIMI_POLL_SECONDS)

        if not self.state.error:
            status = self._call("GET", f"/api/v1/sessions/{self.session_id}/status").get("data") or {}
            if self.spec.model and status.get("model") and status["model"] != self.spec.model:
                self.state.error = f"ran {status['model']} instead of {self.spec.model}"
            elif self.spec.thinking and status.get("thinking_level") not in (None, self.spec.thinking):
                self.state.error = f"thinking {status.get('thinking_level')} instead of {self.spec.thinking}"
        text = self._last_assistant_text()
        self.state.text = text
        if self._abort.is_set() and not self.state.error:
            self.state.error = "aborted"
        self.state.finished = True
        self.state.touch("finished" if not self.state.error else "failed")
        self._event("agent.done", {"ok": self.state.error is None,
                                   "reason": self.state.error or "completed"})
        return {"ok": self.state.error is None, "text": text,
                "reason": self.state.error or "completed", "session": self.session_id}

    def _last_assistant_text(self):
        text = ""
        for message in self._messages():
            if message.get("role") != "assistant":
                continue
            for part in message.get("content", []):
                if part.get("type") == "text" and part.get("text"):
                    text = part["text"]
        return text


def _claude_cli():
    # the installed CLI, else the newest one the Claude desktop app keeps for its Code tab
    candidates = [os.environ.get("CAV_CLAUDE_CLI"), shutil.which("claude"),
                  str(pathlib.Path.home() / ".local" / "bin" / f"claude{EXE}")]
    candidates += _newest(config_home() / "Claude" / "claude-code", f"*/*/claude{EXE}")
    return _first(candidates, "Claude Code CLI not found; set CAV_CLAUDE_CLI")


def _ponytail_plugin():
    """ponytail's plugin folder, the copy installed for Kimi unless CAV_PONYTAIL_DIR names one."""
    path = pathlib.Path(os.environ.get("CAV_PONYTAIL_DIR") or _kimi_home() / "plugins" / "managed" / "ponytail")
    if not (path / ".claude-plugin" / "plugin.json").exists():
        raise RuntimeError(f"ponytail's Claude plugin not found at {path}; CAV runs every agent under ponytail "
                           "(set CAV_PONYTAIL_DIR to its folder)")
    return str(path)


class ClaudeAgent(CliAgent):
    name_in_errors = "claude"

    def __init__(self, spec, log_path, title_prefix="cav"):
        super().__init__(spec, log_path, title_prefix)
        self.cli = _claude_cli()
        self.plugin = _ponytail_plugin()

    def _command(self, prompt):
        # bypassPermissions: no tool call may wait on an approval nobody gives (Kimi runs in yolo)
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "bypassPermissions", "--append-system-prompt", NOBODY_ANSWERS,
               "--plugin-dir", self.plugin, "--name", f"{self.title_prefix} {self.name}"]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        if self.spec.thinking:
            cmd += ["--effort", self.spec.thinking]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd + [prompt]

    def _consume(self, line):
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if event.get("session_id"):
            self.session_id = event["session_id"]
        kind = event.get("type")
        content = (event.get("message") or {}).get("content")
        if kind == "system" and event.get("subtype") == "init":
            self._event("agent.model", {"model": event.get("model")})
            self.state.touch("started")
        elif kind == "assistant" and isinstance(content, list):
            self.state.touch("step", self.state.messages + 1)
            for part in content:
                if part.get("type") == "text" and part.get("text"):
                    self.state.text = part["text"]
                    self.state.touch("writing")
                    self._event("agent.text", {"text": part["text"]})
                elif part.get("type") == "tool_use":
                    self.state.touch(f"tool:{part.get('name', '?')}")
                    self._event("agent.tool.start", {"tool": part.get("name"), "call": part.get("id"),
                                                     "input": part.get("input")})
        elif kind == "user" and isinstance(content, list):
            for part in content:
                if part.get("type") == "tool_result":
                    output = part.get("content")
                    if not isinstance(output, str):
                        output = json.dumps(output)
                    self._event("agent.tool.end", {"tool": None, "call": part.get("tool_use_id"),
                                                   "output_tail": output[-4000:]})
            self.state.touch()
        elif kind == "result":
            if event.get("is_error") or event.get("subtype") != "success":
                # a usage limit or an API error ends the turn here; say which
                self.state.error = f"claude {event.get('subtype')}: {str(event.get('result') or '')[:300]}"
            elif event.get("result"):
                self.state.text = event["result"]
        else:
            self.state.touch()


def build(spec, log_path, title_prefix="cav"):
    if spec.kind == "opencode":
        return OpenCodeAgent(spec, log_path, title_prefix)
    if spec.kind == "kimi":
        return KimiAgent(spec, log_path, title_prefix)
    if spec.kind == "claude":
        return ClaudeAgent(spec, log_path, title_prefix)
    raise ValueError(f"unknown agent kind {spec.kind}")
