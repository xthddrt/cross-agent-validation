"""Append-only event log: one file, monotonic seq, redacted and capped payloads."""

import json
import pathlib
import re
import threading
import time

MAX_FIELD = 4096
ROTATE_BYTES = 64 * 1024 * 1024
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
_SECRET = re.compile(
    r"(?i)((?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|secret)\s*[:=]\s*)([^\s\"',;}]+)"
)
_SECRET_KEY = re.compile(r"(?i)(?:authorization|token|secret|password|credential|cookie|api[_-]?key)")


def redact(text):
    text = _BEARER.sub(r"\1[redacted]", text)
    return _SECRET.sub(r"\1[redacted]", text)


def clip(value, limit=MAX_FIELD):
    if isinstance(value, str):
        value = redact(value)
        if len(value) <= limit:
            return value
        return value[:limit] + f"...[truncated {len(value) - limit} chars]"
    if isinstance(value, dict):
        # Keys like "token" or "cookie" carry secrets whose value has no recognisable shape.
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else clip(v, limit)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clip(v, limit) for v in value]
    return value


def read(path, after=0):
    """Events with seq > after. Tolerates a partially written last line."""
    path = pathlib.Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("seq", 0) > after:
                out.append(event)
    return out


class EventWriter:
    def __init__(self, path, run_id):
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self.seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for event in read(self.path):
            self.seq = max(self.seq, event.get("seq", 0))

    def _rotate(self):
        if self.path.exists() and self.path.stat().st_size > ROTATE_BYTES:
            self.path.replace(self.path.with_suffix(".jsonl.1"))

    def emit(self, type_, round=None, agent=None, data=None):
        with self._lock:
            self.seq += 1
            self._rotate()
            now = time.time()
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            event = {
                "seq": self.seq,
                "ts": f"{stamp}.{int((now % 1) * 1000):03d}Z",
                "run": self.run_id,
                "round": round,
                "agent": agent,
                "type": type_,
                "data": clip(data or {}),
            }
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=False) + "\n")
        return event
