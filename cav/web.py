"""Read-only local dashboard: one stdlib HTTP server over a run directory."""

import collections
import json
import pathlib
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events as events_mod
from .gitops import git

HERE = pathlib.Path(__file__).resolve().parent
INDEX = HERE / "web" / "index.html"
REV_RE = re.compile(r"[0-9a-fA-F]{4,64}")


class Dashboard:
    def __init__(self, run_dir, port=7717, host="127.0.0.1", open_browser=False):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.port = port
        self.host = host
        self.open_browser = open_browser
        self.token = secrets.token_urlsafe(9)

    # ---------------------------------------------------------------- helpers

    def state(self):
        path = self.run_dir / "state.json"
        if not path.exists():
            return {"phase": "no state.json yet"}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"phase": "state.json is being written"}

    def candidate_commits(self, round_no):
        state = self.state()
        for item in reversed(state.get("history") or []):
            if item.get("round") == round_no and item.get("results"):
                names = list(item["results"])
                return [(name, item["results"][name]["commit"]) for name in names]
        return []

    def artifact(self, *parts):
        path = self.run_dir.joinpath(*parts)
        if not path.exists() or path.is_dir():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def diff(self, round_no, file_path):
        exchange = self.run_dir / "exchange.git"
        commits = self.candidate_commits(round_no)
        if len(commits) < 2 or not exchange.exists():
            return "(no two candidates for that round)"
        _, left = commits[0]
        _, right = commits[1]
        return git("--git-dir", str(exchange), "diff", "--no-color", left, right, "--", file_path,
                   check=False)[:400000]

    def file_at(self, rev, file_path):
        exchange = self.run_dir / "exchange.git"
        return git("--git-dir", str(exchange), "show", f"{rev}:{file_path}", check=False)[:400000]

    def messages(self, round_no, agent, limit=400):
        """One agent's round as a transcript: what it said, plus one line per tool call.

        Scanned from the event log rather than held in memory, so a dashboard can attach to a
        finished run without the orchestrator still being alive.
        """
        path = self.run_dir / "events.jsonl"
        out = collections.deque(maxlen=max(1, limit))
        if not path.exists():
            return []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"agent.text"' not in line and '"agent.tool.start"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("round") != round_no or event.get("agent") != agent:
                    continue
                kind = event.get("type")
                data = event.get("data") or {}
                if kind == "agent.text":
                    text = data.get("text") or ""
                    if text.strip():
                        out.append({"seq": event.get("seq", 0), "ts": event.get("ts", ""),
                                    "kind": "text", "text": text})
                elif kind == "agent.tool.start":
                    tool = data.get("tool") or "tool"
                    given = data.get("input") or {}
                    target = ""
                    if isinstance(given, dict):
                        for key in ("command", "filePath", "path", "pattern", "description", "query"):
                            if given.get(key):
                                target = str(given[key])
                                break
                    line_text = f"{tool} {target}".strip()
                    out.append({"seq": event.get("seq", 0), "ts": event.get("ts", ""),
                                "kind": "tool", "text": line_text[:160]})
        return list(out)

    def prompt(self, round_no, agent):
        return self.artifact("rounds", f"r{round_no}", f"{agent}-context.md") or ""

    # ----------------------------------------------------------------- server

    def make_handler(self):
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (ConnectionResetError, BrokenPipeError):
                    self.close_connection = True

            def _send(self, status, body, content_type="application/json"):
                payload = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                query = urllib.parse.parse_qs(parsed.query)
                if not parts or parts[0] != dashboard.token:
                    self._send(404, "not found", "text/plain")
                    return
                route = parts[1] if len(parts) > 1 else ""
                try:
                    if route == "":
                        self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                    elif route == "api" and len(parts) > 2:
                        self._api(parts[2], query)
                    elif route == "events":
                        self._stream(query)
                    else:
                        self._send(404, "not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                except Exception as error:  # keep the dashboard alive for a bad request
                    try:
                        self._send(500, json.dumps({"error": str(error)}))
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return

            def _api(self, name, query):
                round_no = int((query.get("round") or ["0"])[0])
                agent = (query.get("agent") or [""])[0]
                path = (query.get("path") or [""])[0]
                if name == "state":
                    return self._send(200, json.dumps(dashboard.state()))
                if name == "events":
                    after = int((query.get("after") or ["0"])[0])
                    return self._send(200, json.dumps(events_mod.read(dashboard.run_dir / "events.jsonl", after)))
                if name == "divergence":
                    body = dashboard.artifact("reviews", f"r{round_no}", "divergence.json")
                    return self._send(200, body or "{}")
                if name == "carry-forward":
                    body = dashboard.artifact("reviews", f"r{round_no}", "carry-forward.json")
                    return self._send(200, body or '{"items": []}')
                if name == "review":
                    for suffix in ("-review.json", "-review.md"):
                        body = dashboard.artifact("reviews", f"r{round_no}", f"{agent}{suffix}")
                        if body:
                            return self._send(200, body)
                    return self._send(404, "{}")
                if name == "checks":
                    body = dashboard.artifact("checks", f"r{round_no}", f"{agent}.json")
                    return self._send(200, body or "{}")
                if name == "messages":
                    limit = int((query.get("limit") or ["400"])[0])
                    return self._send(200, json.dumps(dashboard.messages(round_no, agent, limit)))
                if name == "prompt":
                    return self._send(200, json.dumps({"prompt": dashboard.prompt(round_no, agent)}))
                if name == "diff":
                    if ".." in path or path.startswith("-"):
                        return self._send(400, '{"error": "bad path"}')
                    return self._send(200, json.dumps({"diff": dashboard.diff(round_no, path)}))
                if name == "file":
                    rev = (query.get("rev") or [""])[0]
                    # `rev` leads the `git show` argument: a value like `--output=...` would be an option
                    if ".." in path or path.startswith("-") or not REV_RE.fullmatch(rev):
                        return self._send(400, '{"error": "bad request"}')
                    return self._send(200, json.dumps({"content": dashboard.file_at(rev, path)}))
                self._send(404, "{}")

            def _stream(self, query):
                after = int((query.get("after") or ["0"])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                deadline = time.time() + 1800
                last = after
                offset = 0
                while time.time() < deadline:
                    found, offset = events_mod.tail(dashboard.run_dir / "events.jsonl", offset)
                    for event in found:
                        if event.get("seq", 0) <= last:
                            continue
                        last = event["seq"]
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.7)

        return Handler

    def make_server(self):
        server = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        server.daemon_threads = True
        return server

    def serve_forever(self):
        server = self.make_server()
        url = f"http://{self.host}:{server.server_address[1]}/{self.token}/"
        print(f"dashboard {url}")
        print(f"watching  {self.run_dir}")
        if self.open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
