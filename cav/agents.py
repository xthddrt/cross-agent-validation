"""Agent adapters: drive OpenCode and Kimi Code headlessly and report progress."""

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

GIT_DIR = r"C:\Program Files\Git\cmd"


def _noop(*args, **kwargs):
    return None


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
    """Live progress for one agent run. Read by the orchestrator's monitor."""
    started: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    messages: int = 0
    detail: str = "starting"
    finished: bool = False
    error: str | None = None
    session: str | None = None
    text: str = ""

    def touch(self, detail=None, messages=None):
        self.last_activity = time.time()
        if detail is not None:
            self.detail = detail
        if messages is not None:
            self.messages = messages

    @property
    def elapsed(self):
        return time.time() - self.started

    @property
    def idle(self):
        return time.time() - self.last_activity


class Agent:
    kind = "base"

    def __init__(self, spec, log_path, emit=None, thinking=False):
        self.spec = spec
        self.name = spec.name
        self.log_path = pathlib.Path(log_path)
        self.state = RunState()
        self._abort = threading.Event()
        self.emit = emit or _noop
        self.thinking = thinking

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

    def available(self):
        raise NotImplementedError

    def run(self, prompt, cwd):
        raise NotImplementedError

    def close(self):
        pass


def _opencode_cli():
    # The desktop bundle is preferred over whatever is on PATH: an older opencode CLI
    # sharing the same data directory fails with "Database is not empty and has no session table".
    explicit = os.environ.get("CAV_OPENCODE_CLI")
    candidates = [explicit]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for folder in ("ai.opencode.desktop", "@opencodedesktop"):
            candidates.append(str(pathlib.Path(local) / "Programs" / folder
                                  / "resources" / "opencode-cli.exe"))
    candidates.append(shutil.which("opencode"))
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("opencode CLI not found; set CAV_OPENCODE_CLI")


class OpenCodeAgent(Agent):
    kind = "opencode"

    def __init__(self, spec, log_path, title_prefix="cav", emit=None, thinking=False):
        super().__init__(spec, log_path, emit, thinking)
        self.cli = _opencode_cli()
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

    def available(self):
        return True

    def _env(self):
        env = os.environ.copy()
        path = env.get("PATH", "")
        if GIT_DIR.lower() not in path.lower():
            env["PATH"] = path + os.pathsep + GIT_DIR
        return env

    def _command(self, prompt):
        cmd = [self.cli, "run", "--format", "json", "--auto",
               "--title", f"{self.title_prefix} {self.name}"]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        if self.session_id:
            cmd += ["--session", self.session_id]
        return cmd + [prompt]

    def run(self, prompt, cwd):
        cmd = self._command(prompt)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._event("agent.launch", {"model": self.spec.model, "cli": self.cli})
        with open(self.log_path, "w", encoding="utf-8", errors="replace") as log:
            self.state.touch("launched")
            self._proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True,
                                          encoding="utf-8", errors="replace", env=self._env())
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
                self.state.error = f"opencode exit {self._proc.returncode}"
        self.state.finished = True
        self.state.touch("finished" if not self.state.error else "failed")
        self._event("agent.done", {"ok": self.state.error is None,
                                   "reason": self.state.error or "completed"})
        return {"ok": self.state.error is None, "text": self.state.text,
                "reason": self.state.error or "completed", "session": self.session_id}

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
            self.state.session = self.session_id
        part = event.get("part") or {}
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            self.state.text = part["text"]
            self.state.touch("writing")
            self._event("agent.text", {"text": part["text"]})
        elif kind == "tool":
            tool = part.get("tool", "?")
            detail = part.get("state") or {}
            self._event("agent.tool.start", {"tool": tool, "input": detail.get("input")})
            self._event("agent.tool.end", {"tool": tool, "exit": _find_exit(detail),
                                           "output_tail": (detail.get("output") or "")[-800:]})
            self.state.touch(f"tool:{tool}")
        elif kind == "step-start":
            self.state.touch("step", self.state.messages + 1)
            self._event("agent.step", {"messages": self.state.messages})
        else:
            self.state.touch()


def _kimi_home():
    return pathlib.Path(os.environ.get("CAV_KIMI_HOME") or (pathlib.Path.home() / ".kimi-code"))


class KimiAgent(Agent):
    kind = "kimi"

    def __init__(self, spec, log_path, title_prefix="cav", emit=None, thinking=False):
        super().__init__(spec, log_path, emit, thinking)
        self.title_prefix = title_prefix
        self.base = None
        self.token = None
        self.session_id = None
        self._baseline = 0
        self._prompt_id = None
        self._seen_parts = 0
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
        except urllib.error.URLError as error:
            raise RuntimeError(f"kimi {method} {path}: {error.reason}")

    def _model(self):
        if self.spec.model:
            return self.spec.model
        config = _kimi_home() / "config.toml"
        if config.exists():
            match = re.search(r'(?m)^default_model\s*=\s*"([^"]+)"', config.read_text(encoding="utf-8"))
            if match:
                return match.group(1)
        raise RuntimeError("no Kimi model configured; set agents.<name>.model in the case file")

    def available(self):
        self._connect()
        return True

    def _agent_config(self):
        config = {"model": self._model(), "permission_mode": "yolo"}
        if self.spec.thinking:
            config["thinking"] = self.spec.thinking
        return config

    def _messages(self):
        data = self._call("GET", f"/api/v1/sessions/{self.session_id}/messages")["data"]
        return data.get("items", [])

    def _capture(self, items):
        """Emit only parts not seen before; also keeps the raw transcript the adapter never wrote."""
        flat = [part for message in items for part in (message.get("content") or [])]
        if self._transcript is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._transcript = open(self.log_path, "a", encoding="utf-8", errors="replace")
        for part in flat[self._seen_parts:]:
            self._transcript.write(json.dumps(part, ensure_ascii=False) + "\n")
            kind = part.get("type")
            if kind == "text" and part.get("text"):
                self._event("agent.text", {"text": part["text"]})
            elif kind == "thinking" and self.thinking:
                self._event("agent.thinking", {"text": part.get("text") or ""})
            elif kind == "tool_use":
                self._event("agent.tool.start", {"tool": part.get("tool_name"),
                                                 "input": part.get("input")})
            elif kind == "tool_result":
                content = part.get("output") or part.get("content") or ""
                if not isinstance(content, str):
                    content = json.dumps(content)
                self._event("agent.tool.end", {"tool": part.get("tool_name"),
                                               "output_tail": content[-800:]})
        self._seen_parts = len(flat)
        self._transcript.flush()

    def _pending(self, detail):
        pending = (detail or {}).get("pending_interaction")
        if pending in (None, "", "none"):
            return None
        return f"waiting:{pending}"

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
        if not self.base:
            self._connect()
        cwd = str(pathlib.Path(cwd).resolve())
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._transcript = open(self.log_path, "w", encoding="utf-8", errors="replace")
        workspace = self._call("POST", "/api/v1/workspaces", {"root": cwd, "name": pathlib.Path(cwd).name})["data"]
        session = self._call("POST", "/api/v1/sessions", {
            "title": f"{self.title_prefix} {self.name}",
            "metadata": {"cwd": cwd},
            "workspace_id": workspace["id"],
        })["data"]
        self.session_id = session["id"]
        self.state.session = self.session_id
        self._call("POST", f"/api/v1/sessions/{self.session_id}/profile", {
            "agent_config": self._agent_config(),
        })
        self._verify_permission(self._agent_config()["permission_mode"])
        self._baseline = self._assistant_count()
        self._seen_parts = 0
        self._event("agent.launch", {"model": self._model(), "session": self.session_id})
        submitted = self._call("POST", f"/api/v1/sessions/{self.session_id}/prompts",
                               {"content": [{"type": "text", "text": prompt}]})
        self._prompt_id = (submitted.get("data") or {}).get("prompt_id")
        self.state.touch("running")

        idle = 0
        while not self._abort.is_set():
            detail = self._call("GET", f"/api/v1/sessions/{self.session_id}")["data"]
            items = self._messages()
            self._capture(items)
            count = sum(1 for m in items if m.get("role") == "assistant")
            busy = detail.get("busy", False) or detail.get("main_turn_active", False)
            waiting = self._pending(detail)
            if waiting:
                # Kimi reports busy=True while it waits for approval, so this must be checked
                # before the busy branch: otherwise the block is invisible until the round timeout.
                self.state.detail = waiting
                idle += 1
            elif count > self._baseline:
                self.state.touch("writing" if busy else "finishing", count - self._baseline)
                idle = 0
            elif busy:
                self.state.touch("running")
                idle = 0
            else:
                idle += 1
            reason = detail.get("last_turn_reason")
            if not busy and count > self._baseline and reason in ("completed", "failed", "cancelled"):
                break
            if not busy and reason == "failed" and count == self._baseline:
                self.state.error = "kimi turn failed"
                break
            if idle * self._interval() > 120:
                self.state.error = f"kimi never produced output ({self.state.detail})"
                break
            time.sleep(self._interval())

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
        if self._transcript:
            self._transcript.close()
            self._transcript = None
        return {"ok": self.state.error is None, "text": text,
                "reason": self.state.error or "completed", "session": self.session_id}

    def _interval(self):
        return 3

    def _assistant_count(self):
        return sum(1 for message in self._messages() if message.get("role") == "assistant")

    def _last_assistant_text(self):
        text = ""
        for message in self._messages():
            if message.get("role") != "assistant":
                continue
            for part in message.get("content", []):
                if part.get("type") == "text" and part.get("text"):
                    text = part["text"]
        return text


def build(spec, log_path, title_prefix="cav", emit=None, thinking=False):
    if spec.kind == "opencode":
        return OpenCodeAgent(spec, log_path, title_prefix, emit, thinking)
    if spec.kind == "kimi":
        return KimiAgent(spec, log_path, title_prefix, emit, thinking)
    raise ValueError(f"unknown agent kind {spec.kind}")
