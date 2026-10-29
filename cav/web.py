"""Read-only local dashboard: one stdlib HTTP server over a run directory."""

import json
import pathlib
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events as events_mod
from .gitops import git

INDEX = pathlib.Path(__file__).resolve().parent / "web" / "index.html"


class Dashboard:
    def __init__(self, run_dir, port=7717, host="127.0.0.1", open_browser=False):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.port = port
        self.host = host
        self.open_browser = open_browser
        self.token = secrets.token_urlsafe(9)

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
                if not parts or parts[0] != dashboard.token:
                    return self._send(404, "not found", "text/plain")
                query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
                route = parts[1] if len(parts) > 1 else ""
                try:
                    if route == "":
                        self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                    elif route == "events":
                        self._stream(int(query.get("after", 0)))
                    elif route == "api" and len(parts) > 2:
                        self._send(*self._api(parts[2], query))
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
                round_no = int(query.get("round", 0))
                agent, path = query.get("agent", ""), query.get("path", "")
                if name == "state":
                    return 200, json.dumps(dashboard.state())
                if name == "divergence":
                    return 200, dashboard.artifact("reviews", f"r{round_no}", "divergence.json") or "{}"
                if name == "review":
                    body = dashboard.artifact("reviews", f"r{round_no}", f"{agent}-review.json")
                    return (200, body) if body else (404, "{}")
                if name == "checks":
                    return 200, dashboard.artifact("checks", f"r{round_no}", f"{agent}.json") or "{}"
                if name == "prompt":
                    prompt = dashboard.artifact("rounds", f"r{round_no}", f"{agent}-context.md") or ""
                    return 200, json.dumps({"prompt": prompt})
                if name == "diff":
                    if ".." in path or path.startswith("-"):
                        return 400, '{"error": "bad path"}'
                    return 200, json.dumps({"diff": dashboard.diff(round_no, path)})
                return 404, "{}"

            def _stream(self, after):
                """Server-sent events: everything after `after`, then new events as they land."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                deadline = time.time() + 1800
                offset = 0
                while time.time() < deadline:
                    found, offset = events_mod.tail(dashboard.run_dir / "events.jsonl", offset)
                    for event in found:
                        if event.get("seq", 0) > after:
                            after = event["seq"]
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
