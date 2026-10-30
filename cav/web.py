"""The dashboard: a stdlib HTTP server over runs/. One link (token kept in runs/serve.json) lists every
run, starts one, stops one, and shows each run live."""

import json
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events as events_mod
from .gitops import NO_WINDOW, resolve_ref, resolve_repo

WEB = pathlib.Path(__file__).resolve().parent / "web"
ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN_ID = re.compile(r"^[\w.-]+$")
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
ENDED = ("done", "failed", "stopped")


def read_json(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def alive(pid):
    """Whether a process is still running. os.kill(pid, 0) would terminate it on Windows."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    import ctypes
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if not parts or parts[0] != self.app.token:
                return self.send(404, "not found", "text/plain")
            method(self, parts[1:], query)
        except GONE:
            return
        except Exception as error:  # keep the dashboard alive for a bad request
            try:
                self.send(500, json.dumps({"error": str(error)}))
            except GONE:
                return

    def do_GET(self):
        self.dispatch(self.app.get)

    def do_POST(self):
        self.dispatch(self.app.post)


class Home:
    def __init__(self, runs_dir=None, port=7700, host="127.0.0.1"):
        self.runs_dir = pathlib.Path(runs_dir or ROOT / "runs").resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.host, self.port = host, port
        self.settings = self.runs_dir / "serve.json"
        self.token = read_json(self.settings).get("token") or secrets.token_urlsafe(12)

    def make_server(self):
        server = ThreadingHTTPServer((self.host, self.port), type("BoundHandler", (Handler,), {"app": self}))
        server.daemon_threads = True
        return server

    def serve_forever(self):
        server = self.make_server()
        url = f"http://{self.host}:{server.server_address[1]}/{self.token}/"
        self.settings.write_text(json.dumps({"token": self.token, "url": url}, indent=2), encoding="utf-8")
        print(f"dashboard {url}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()

    # ------------------------------------------------------------------ runs

    def run_path(self, run_id):
        path = self.runs_dir / run_id
        ok = RUN_ID.match(run_id) and not run_id.startswith(".")
        return path if ok and ((path / "state.json").exists() or (path / "run.json").exists()) else None

    def runs(self):
        out = []
        for path in self.runs_dir.iterdir():
            state, meta = read_json(path / "state.json"), read_json(path / "run.json")
            if not path.is_dir() or not (state or meta):
                continue
            phase = state.get("phase") or "starting"
            if phase not in ENDED and meta.get("pid") and not alive(meta["pid"]):
                phase = "stopped"  # the run's process ended without recording an outcome
            text = next((p.read_text(encoding="utf-8", errors="replace") for p in (path / "input.md", path / "prompt.md")
                         if p.exists()), "")
            stamps = [p.stat().st_mtime for p in (path / "state.json", path / "run.json") if p.exists()]
            out.append({"id": path.name, "repo": state.get("repo") or meta.get("repo"), "phase": phase,
                        "outcome": state.get("outcome"), "detail": state.get("detail"), "text": text[:400],
                        "running": phase not in ENDED, "updated": max(stamps)})
        return sorted(out, key=lambda item: item["updated"], reverse=True)

    def start(self, repos, text):
        """Start a run as its own process, so it outlives this server. `repos`: [{"repo", "branches":
        [set 1, set 2]}]; a blank branch is that repo's main."""
        repos = [{"repo": str(item.get("repo") or "").strip().strip('"'),
                  "branches": [str(ref or "").strip() for ref in (list(item.get("branches") or []) + ["", ""])[:2]]}
                 for item in repos or () if str(item.get("repo") or "").strip()]
        branched = any(ref for item in repos for ref in item["branches"])
        if not repos:
            raise ValueError("give at least one repository")
        if branched and all(item["branches"][0] == item["branches"][1] for item in repos):
            raise ValueError("the two branch sets are the same")
        text = (text or "").strip()
        if not text:
            raise ValueError("give the plan" if branched else "give the task")
        paths = []
        for item in repos:
            path = resolve_repo(item["repo"])  # a GitHub URL is cloned (or its clone fetched) here, so its branches can be checked
            for ref in item["branches"]:
                if ref:
                    resolve_ref(path, ref)
            paths.append(path)
        slug = "-".join(re.sub(r"[^\w.-]+", "-", path.name).strip("-.") for path in paths)[:60] or "repo"
        run_id = f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        while (self.runs_dir / run_id).exists():
            run_id += "x"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "input.md").write_text(text + "\n", encoding="utf-8")
        (run_dir / "spec.json").write_text(json.dumps({"repos": repos, "plan" if branched else "task": text}, indent=2),
                                           encoding="utf-8")
        # python.exe, not pythonw.exe: the agents' console children then share one hidden console
        python = pathlib.Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and (python.parent / "python.exe").exists():
            python = python.parent / "python.exe"
        args = [str(python), "-m", "cav", "run", "--spec", str(run_dir / "spec.json"), "--run-dir", str(run_dir)]
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with open(run_dir / "run-output.log", "ab") as log:
            process = subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
                                       creationflags=flags, start_new_session=os.name != "nt")
        (run_dir / "run.json").write_text(json.dumps({"pid": process.pid, "repo": ", ".join(item["repo"] for item in repos)}),
                                          encoding="utf-8")
        return run_id

    def stop(self, run_id):
        """End a run's process and everything it started; the agents' own apps keep their sessions."""
        run_dir = self.run_path(run_id)
        pid = read_json(run_dir / "run.json").get("pid") if run_dir else None
        if not pid or not alive(pid):
            raise ValueError("that run is not running")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        state = read_json(run_dir / "state.json")
        state.update(phase="stopped", outcome="stopped", detail="stopped from the dashboard")
        (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    # --------------------------------------------------------------- routes

    def get(self, handler, parts, query):
        if not parts:
            return handler.send(200, (WEB / "home.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if parts == ["api", "runs"]:
            return handler.send(200, json.dumps(self.runs()))
        run_dir = parts[0] == "run" and len(parts) > 1 and self.run_path(parts[1])
        if not run_dir:
            return handler.send(404, "not found", "text/plain")
        rest = parts[2:]
        if not rest:
            return handler.send(200, (WEB / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if rest == ["api", "state"]:
            state = read_json(run_dir / "state.json") or {"phase": "starting"}
            return handler.send(200, json.dumps(dict(state, input=next(
                (p.read_text(encoding="utf-8", errors="replace") for p in (run_dir / "input.md", run_dir / "prompt.md")
                 if p.exists()), ""))))
        if rest == ["api", "prompt"]:
            record = run_dir / "rounds" / f"r{int(query.get('round', 0))}" / f"{query.get('agent', '')}-context.md"
            ok = RUN_ID.match(query.get("agent", "")) and record.is_file()
            return handler.send(200, json.dumps({"prompt": record.read_text(encoding="utf-8") if ok else ""}))
        if rest == ["events"]:
            return self.stream(handler, run_dir, int(query.get("after", 0)))
        handler.send(404, "not found", "text/plain")

    def post(self, handler, parts, query):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)  # read first: answering early aborts the client's send
        # a JSON body cannot come from a plain cross-site form post
        if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return handler.send(415, json.dumps({"error": "send JSON"}))
        body = json.loads(raw or b"{}")
        try:
            if parts == ["api", "start"]:
                run_id = self.start(body.get("repos"), body.get("text"))
                return handler.send(200, json.dumps({"id": run_id, "url": f"/{self.token}/run/{run_id}/"}))
            if parts == ["api", "stop"]:
                self.stop(str(body.get("id") or ""))
                return handler.send(200, "{}")
        except ValueError as error:
            return handler.send(400, json.dumps({"error": str(error)}))
        handler.send(404, "not found", "text/plain")

    @staticmethod
    def stream(handler, run_dir, after):
        """Server-sent events: everything after `after`, then new events as they land."""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        deadline, offset = time.time() + 1800, 0
        while time.time() < deadline:
            found, offset = events_mod.tail(run_dir / "events.jsonl", offset)
            for event in found:
                if event.get("seq", 0) > after:
                    after = event["seq"]
                    handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            handler.wfile.flush()
            time.sleep(0.7)
