"""Append-only event log: one file, monotonic seq, redacted and capped payloads."""

import json
import pathlib
import re
import threading
import time

MAX_FIELD = 4096
MAX_TEXT_FIELD = 200_000  # agent messages are read whole by a human; do not truncate them
TEXT_TYPES = ("agent.text", "agent.thinking")
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


def tail(path, offset=0):
    """Events appended after byte `offset`, and the offset to continue from.

    A follower reads only what is new instead of re-parsing the whole log on every poll. A
    partially written last line is left for the next call; a file smaller than `offset` was
    replaced, so reading starts over.
    """
    path = pathlib.Path(path)
    try:
        if path.stat().st_size < offset:
            offset = 0
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n") + 1
    out = []
    for line in chunk[:end].splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, offset + end


class EventWriter:
    def __init__(self, path, run_id):
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A resumed run continues the sequence instead of restarting it.
        self.seq = max((event.get("seq", 0) for event in tail(self.path)[0]), default=0)

    def emit(self, type_, round=None, agent=None, data=None):
        with self._lock:
            self.seq += 1
            now = time.time()
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            event = {
                "seq": self.seq,
                "ts": f"{stamp}.{int((now % 1) * 1000):03d}Z",
                "run": self.run_id,
                "round": round,
                "agent": agent,
                "type": type_,
                "data": clip(data or {}, MAX_TEXT_FIELD if type_ in TEXT_TYPES else MAX_FIELD),
            }
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=False) + "\n")
        return event
