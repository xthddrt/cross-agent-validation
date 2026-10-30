"""Local dashboards: stdlib HTTP servers over run directories.

Dashboard  one run, read-only: what `cav run --dashboard` and `cav web` serve.
Home       every run under runs/, each run's dashboard, and a form that starts a pipeline.
"""

import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import compare_runs
from . import events as events_mod
from . import flow as flow_mod
from .gitops import git

WEB = pathlib.Path(__file__).resolve().parent / "web"
INDEX = WEB / "index.html"
HOME = WEB / "home.html"
COMPARE = WEB / "compare.html"
ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
RUN_ID = re.compile(r"^[\w.-]+$")
STAGES = ("plan", "code")
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


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
    """Routes every request to `app.get` / `app.post`; keeps the server alive on a bad request."""

    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def send(self, status, body, content_type="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method):
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
        self._dispatch(self.app.get)

    def do_POST(self):
        self._dispatch(self.app.post)


class Server:
    host, port, open_browser, token = "127.0.0.1", 0, False, ""

    def get(self, handler, parts, query):
        handler.send(404, "not found", "text/plain")

    def post(self, handler, parts, query):
        handler.send(404, "not found", "text/plain")

    def make_server(self):
        server = ThreadingHTTPServer((self.host, self.port), type("BoundHandler", (Handler,), {"app": self}))
        server.daemon_threads = True
        return server

    def url(self, server):
        return f"http://{self.host}:{server.server_address[1]}/{self.token}/"

    def started(self, server):
        pass

    def serve_forever(self):
        server = self.make_server()
        url = self.url(server)
        self.started(server)
        print(f"dashboard {url}", flush=True)
        if self.open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


class Dashboard(Server):
    def __init__(self, run_dir, port=7717, host="127.0.0.1", open_browser=False, token=None):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.port = port
        self.host = host
        self.open_browser = open_browser
        self.token = token or secrets.token_urlsafe(9)

    def artifact(self, *parts):
        path = self.run_dir.joinpath(*parts)
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    def state(self):
        try:
            return json.loads(self.artifact("state.json") or '{"phase": "no state.json yet"}')
        except json.JSONDecodeError:
            return {"phase": "state.json is being written"}

    def diff(self, round_no, file_path):
        """One file's diff between the two candidates of a round."""
        item = next((entry for entry in self.state().get("history") or []
                     if entry.get("round") == round_no and entry.get("results")), None)
        if not item:
            return "(no two candidates for that round)"
        left, right = (result["commit"] for result in item["results"].values())
        return git("--git-dir", str(self.run_dir / "exchange.git"), "diff", "--no-color", left, right,
                   "--", file_path, check=False)[:400000]

    def get(self, handler, parts, query):
        self.serve_run(handler, parts, query)

    def serve_run(self, handler, parts, query):
        """The run's page, its event stream and its read-only API, below wherever it is mounted."""
        route = parts[0] if parts else ""
        if route == "":
            handler.send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif route == "events":
            self.stream(handler, int(query.get("after", 0)))
        elif route == "api" and len(parts) > 1:
            handler.send(*self.api(parts[1], query))
        else:
            handler.send(404, "not found", "text/plain")

    def api(self, name, query):
        round_no = int(query.get("round", 0))
        agent, path = query.get("agent", ""), query.get("path", "")
        if name == "state":
            return 200, json.dumps(self.state())
        if name == "divergence":
            return 200, self.artifact("reviews", f"r{round_no}", "divergence.json") or "{}"
        if name == "review":
            body = self.artifact("reviews", f"r{round_no}", f"{agent}-review.json")
            return (200, body) if body else (404, "{}")
        if name == "checks":
            return 200, self.artifact("checks", f"r{round_no}", f"{agent}.json") or "{}"
        if name == "prompt":
            prompt = self.artifact("rounds", f"r{round_no}", f"{agent}-context.md") or ""
            return 200, json.dumps({"prompt": prompt})
        if name == "summary":
            return 200, json.dumps({"summary": self.artifact("SUMMARY.md") or ""})
        if name == "diff":
            if ".." in path or path.startswith("-"):
                return 400, '{"error": "bad path"}'
            return 200, json.dumps({"diff": self.diff(round_no, path)})
        return 404, "{}"

    def stream(self, handler, after):
        """Server-sent events: everything after `after`, then new events as they land."""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        deadline = time.time() + 1800
        offset = 0
        while time.time() < deadline:
            found, offset = events_mod.tail(self.run_dir / "events.jsonl", offset)
            for event in found:
                if event.get("seq", 0) > after:
                    after = event["seq"]
                    handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            handler.wfile.flush()
            time.sleep(0.7)


class Home(Server):
    """A fixed link (token kept in runs/serve.json) to every run, and the form that starts one."""

    def __init__(self, runs_dir=None, port=7700, host="127.0.0.1", open_browser=False):
        self.runs_dir = pathlib.Path(runs_dir or RUNS).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.host = host
        self.open_browser = open_browser
        self.settings = self.runs_dir / "serve.json"
        self.token = read_json(self.settings).get("token") or secrets.token_urlsafe(12)

    def started(self, server):
        self.settings.write_text(json.dumps({"token": self.token, "url": self.url(server)}, indent=2),
                                 encoding="utf-8")

    # ------------------------------------------------------------------ runs

    def run_path(self, run_id):
        path = self.runs_dir / run_id
        return path if RUN_ID.match(run_id) and not run_id.startswith("_") and path.is_dir() else None

    @staticmethod
    def current_stage(run_dir):
        return "code" if (run_dir / "code" / "state.json").exists() else "plan"

    def pipeline_info(self, run_dir):
        info = read_json(run_dir / "pipeline.json")
        if info.get("stage") not in ("done", "failed") and info.get("pid") and not alive(info["pid"]):
            info["stopped"] = True  # the pipeline process ended without recording an outcome
        prompt = run_dir / "prompt.md"
        info["prompt"] = prompt.read_text(encoding="utf-8", errors="replace") if prompt.exists() else ""
        info["stages"] = {}
        for stage in STAGES:
            state = read_json(run_dir / stage / "state.json")
            if state:
                info["stages"][stage] = {"phase": state.get("phase"), "round": state.get("round"),
                                         "agreed": bool(state.get("final"))}
        return info

    def runs(self):
        out = []
        for path in self.runs_dir.iterdir():
            if not path.is_dir() or not self.run_path(path.name):
                continue
            files = [path / "pipeline.json", path / "state.json"] + [path / stage / "state.json" for stage in STAGES]
            stamps = [item.stat().st_mtime for item in files if item.exists()]
            if not stamps:
                continue
            if (path / "pipeline.json").exists():
                info = self.pipeline_info(path)
                item = {"kind": "pipeline", "repo": info.get("repo") or info.get("repo_input"),
                        "prompt": info["prompt"][:400], **{key: info.get(key) for key in
                        ("stage", "outcome", "detail", "stopped", "stages", "branch", "labels")}}
            else:
                state = read_json(path / "state.json")
                item = {"kind": "run", "case": state.get("case"), "phase": state.get("phase"),
                        "round": state.get("round"), "agreed": bool(state.get("final"))}
            out.append(dict(item, id=path.name, updated=max(stamps)))
        return sorted(out, key=lambda item: item["updated"], reverse=True)

    def start(self, repo, prompt, branches=()):
        """Start a pipeline as its own process, so it outlives this server."""
        repo, prompt = (repo or "").strip(), (prompt or "").strip()
        branches = [item.strip() for item in branches or () if item and item.strip()]
        if len(branches) not in (0, 2):
            raise ValueError("give both branches to reconcile, or neither")
        if len(set(branches)) != len(branches):
            raise ValueError("the two branches are the same")
        if not repo:
            raise ValueError("give a local repo path or a GitHub URL")
        if not prompt:
            raise ValueError("the prompt is empty")
        if not flow_mod.is_github(repo):
            local = flow_mod.resolve_repo(repo)  # a bad path is refused here; a clone happens in the run
            for ref in branches:
                flow_mod.resolve_ref(local, ref)
        slug = re.sub(r"[^\w.-]+", "-", flow_mod.repo_name(repo)).strip("-.") or "repo"
        run_id = f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        while (self.runs_dir / run_id).exists():
            run_id += "x"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
        (run_dir / "pipeline.json").write_text(json.dumps(
            {"id": run_id, "repo_input": repo, "stage": "starting",
             "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2), encoding="utf-8")
        # python.exe, not pythonw.exe: the agents' console children then share one hidden console
        python = pathlib.Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and (python.parent / "python.exe").exists():
            python = python.parent / "python.exe"
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with open(run_dir / "pipeline.log", "ab") as log:
            subprocess.Popen([str(python), "-m", "cav", "run", "--repo", repo,
                              "--prompt-file", str(run_dir / "prompt.md"), "--run-dir", str(run_dir),
                              *[arg for ref in branches for arg in ("--branch", ref)]],
                             cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
                             creationflags=flags, start_new_session=os.name != "nt")
        return run_id

    # --------------------------------------------------------------- routes

    def get(self, handler, parts, query):
        if not parts:
            return handler.send(200, HOME.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if parts == ["api", "runs"]:
            return handler.send(200, json.dumps(self.runs()))
        if parts == ["compare"]:
            return handler.send(200, COMPARE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if parts == ["api", "compare"]:
            ids = [item for item in (query.get("runs") or "").split(",") if item]
            if len(ids) != 2 or not all(self.run_path(item) for item in ids):
                return handler.send(400, json.dumps({"error": "give two run ids: ?runs=a,b"}))
            return handler.send(200, json.dumps(compare_runs.compare(self.runs_dir, ids, alive)))
        if parts[0] != "run" or len(parts) < 2 or not self.run_path(parts[1]):
            return handler.send(404, "not found", "text/plain")
        run_dir, rest = self.run_path(parts[1]), parts[2:]
        if not (run_dir / "pipeline.json").exists():
            if rest == ["api", "pipeline"]:
                return handler.send(200, "{}")  # a plain run: the page shows only the link home
            return Dashboard(run_dir, token=self.token).serve_run(handler, rest, query)
        if rest[:1] and rest[0] in STAGES:
            return Dashboard(run_dir / rest[0], token=self.token).serve_run(handler, rest[1:], query)
        if not rest:
            return handler.redirect(f"/{self.token}/run/{parts[1]}/{self.current_stage(run_dir)}/")
        if rest == ["api", "pipeline"]:
            return handler.send(200, json.dumps(self.pipeline_info(run_dir)))
        handler.send(404, "not found", "text/plain")

    def post(self, handler, parts, query):
        if parts != ["api", "start"]:
            return handler.send(404, "not found", "text/plain")
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)  # read first: answering early aborts the client's send
        # a JSON body cannot come from a plain cross-site form post
        if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return handler.send(415, json.dumps({"error": "send JSON"}))
        body = json.loads(raw or b"{}")
        try:
            run_id = self.start(body.get("repo"), body.get("prompt"), body.get("branches") or ())
        except ValueError as error:
            return handler.send(400, json.dumps({"error": str(error)}))
        handler.send(200, json.dumps({"id": run_id, "url": f"/{self.token}/run/{run_id}/"}))
