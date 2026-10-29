"""Structured reviews: schema validation, markdown rendering, legacy fallback."""

import json
import pathlib
import re

MUST_FIX_RE = re.compile(r"(?m)^MUST_FIX\s*:\s*(\d+)\s*$")
SEVERITIES = ("MUST_FIX", "SHOULD_FIX", "NIT")
KINDS = ("behavioral", "cosmetic", "editorial")


def parse_must_fix(path):
    path = pathlib.Path(path)
    if not path.exists():
        return None
    found = MUST_FIX_RE.findall(path.read_text(encoding="utf-8", errors="replace"))
    return int(found[0]) if len(found) == 1 else None


class Review:
    """One agent's validated result for one round."""

    def __init__(self, agent, round_no, path, data, errors, fallback):
        self.agent = agent
        self.round = round_no
        self.path = pathlib.Path(path)
        self.data = data
        self.errors = list(errors)
        self.fallback = fallback

    @property
    def valid(self):
        return not self.errors

    @property
    def counts(self):
        return self.data.get("counts") or {}

    @property
    def blocking(self):
        if self.fallback:
            return int(self.counts.get("blocking_must_fix") or 0)
        return int(self.counts.get("blocking_must_fix") or 0)

    @property
    def behavior_approved(self):
        return bool(self.data.get("behavior_approved", self.fallback))

    @property
    def other_approved(self):
        return bool(self.data.get("other_behavior_approved", self.fallback))

    @property
    def open_behavioural(self):
        return any((item or {}).get("kind") == "behavioral"
                   for item in (self.data.get("remaining_disagreements") or []))

    def summary(self):
        counts = self.counts
        return (f"MUST_FIX={counts.get('must_fix_total', '?')} "
                f"blocking={counts.get('blocking_must_fix', '?')} "
                f"approved={self.behavior_approved}/{self.other_approved}")


def _count(data, severity):
    return sum(1 for finding in (data.get("findings") or [])
               if finding.get("severity") == severity)


def _line_count(path):
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines()) or 1
    except OSError:
        return None


def validate(data, agent, round_no, worktree, required_dispositions=()):
    errors = []
    worktree = pathlib.Path(worktree)
    if not isinstance(data, dict):
        return ["review is not an object"]
    if data.get("agent") != agent:
        errors.append(f"agent is {data.get('agent')!r}, expected {agent!r}")
    try:
        if int(data.get("round")) != int(round_no):
            errors.append(f"round is {data.get('round')!r}, expected {round_no}")
    except (TypeError, ValueError):
        errors.append("round missing or not an integer")

    findings = data.get("findings") or []
    if not isinstance(findings, list):
        return errors + ["findings is not a list"]
    ids = [finding.get("id") for finding in findings]
    if len(ids) != len(set(ids)):
        errors.append("duplicate finding ids")
    for finding in findings:
        label = finding.get("id") or "?"
        if finding.get("severity") not in SEVERITIES:
            errors.append(f"{label}: severity {finding.get('severity')!r} invalid")
        if finding.get("kind") not in KINDS:
            errors.append(f"{label}: kind {finding.get('kind')!r} invalid")
        path = finding.get("path")
        if not path:
            errors.append(f"{label}: no path")
            continue
        target = (worktree / path).resolve()
        try:
            target.relative_to(worktree.resolve())
        except ValueError:
            errors.append(f"{label}: path escapes the worktree: {path}")
            continue
        if not target.exists():
            errors.append(f"{label}: path does not exist: {path}")
            continue
        line = finding.get("line")
        if line is not None:
            count = _line_count(target)
            try:
                line = int(line)
            except (TypeError, ValueError):
                errors.append(f"{label}: line is not an integer")
            else:
                if count is not None and (line < 1 or line > count):
                    errors.append(f"{label}: line {line} outside {path} (1..{count})")

    counts = data.get("counts") or {}
    expected = {
        "must_fix_total": _count(data, "MUST_FIX"),
        "should_fix": _count(data, "SHOULD_FIX"),
        "nit": _count(data, "NIT"),
    }
    blocking = sum(1 for finding in findings
                   if finding.get("severity") == "MUST_FIX" and finding.get("kind") == "behavioral")
    expected["blocking_must_fix"] = blocking
    for key, value in expected.items():
        if counts.get(key) != value:
            errors.append(f"counts.{key} is {counts.get(key)!r}, findings say {value}")

    if data.get("behavior_approved") is False and blocking == 0:
        errors.append("behavior_approved is false without a blocking finding")
    if data.get("other_behavior_approved") is False and blocking == 0:
        errors.append("other_behavior_approved is false without a blocking finding")

    for item in (data.get("remaining_disagreements") or []):
        if (item or {}).get("kind") == "behavioral" and data.get("behavior_approved"):
            errors.append(f"open behavioural disagreement {item.get('area')!r} contradicts approval")

    if required_dispositions:
        disposed = {entry.get("item_id") for entry in (data.get("carry_forward_dispositions") or [])}
        missing = [item for item in required_dispositions if item not in disposed]
        if missing:
            errors.append(f"carry-forward items without a disposition: {', '.join(sorted(missing)[:8])}")
        for entry in (data.get("carry_forward_dispositions") or []):
            if entry.get("status") not in ("retained", "reoffered", "intentionally_dropped", "unaddressed"):
                errors.append(f"{entry.get('item_id')}: invalid disposition {entry.get('status')!r}")
            if entry.get("status") == "unaddressed":
                errors.append(f"{entry.get('item_id')}: still unaddressed")
    return errors


def load(json_path, md_path, agent, round_no, worktree, required_dispositions=()):
    """JSON review when present, otherwise the legacy markdown verdict."""
    json_path = pathlib.Path(json_path)
    md_path = pathlib.Path(md_path)
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as error:
            return Review(agent, round_no, json_path, {}, [f"invalid JSON: {error}"], False)
        return Review(agent, round_no, json_path, data,
                      validate(data, agent, round_no, worktree, required_dispositions), False)
    if md_path.exists():
        must_fix = parse_must_fix(md_path)
        if must_fix is None:
            return None
        data = {"agent": agent, "round": round_no, "behavior_approved": True,
                "other_behavior_approved": True,
                "counts": {"must_fix_total": must_fix, "blocking_must_fix": must_fix,
                           "should_fix": 0, "nit": 0},
                "findings": []}
        return Review(agent, round_no, md_path, data, [], True)
    return None


def render(data):
    """Markdown for humans, rendered from the structured review."""
    counts = data.get("counts") or {}
    lines = [
        f"# Round {data.get('round')} - {data.get('agent')} review and recommendation",
        "",
        f"- Behaviour approved (own): {'yes' if data.get('behavior_approved') else 'no'}",
        f"- Behaviour approved (other): {'yes' if data.get('other_behavior_approved') else 'no'}",
        f"- MUST_FIX: {counts.get('must_fix_total', 0)} "
        f"(blocking: {counts.get('blocking_must_fix', 0)})",
        f"- SHOULD_FIX: {counts.get('should_fix', 0)}   NIT: {counts.get('nit', 0)}",
        "",
        "## Findings",
        "",
        "| ID | Severity | Kind | File / line | Summary | Resolution |",
        "|---|---|---|---|---|---|",
    ]
    for finding in data.get("findings") or []:
        lines.append(f"| {finding.get('id', '?')} | {finding.get('severity', '?')} "
                     f"| {finding.get('kind', '?')} | `{finding.get('path', '?')}:{finding.get('line', '')}` "
                     f"| {finding.get('summary', '')} | {finding.get('resolution', '')} |")
    lines += ["", "## Areas", "", "| Area | Status | Kept | Why |", "|---|---|---|---|"]
    for area in data.get("areas") or []:
        lines.append(f"| {area.get('area', '')} | {area.get('status', '')} "
                     f"| {area.get('kept', '')} | {area.get('why', '')} |")
    if data.get("changes"):
        lines += ["", "## Changes from baseline", ""]
        lines += [f"- `{entry.get('path')}`: {entry.get('why', '')}" for entry in data["changes"]]
    if data.get("incorporated_from_other"):
        lines += ["", "## Incorporated from the other candidate", ""]
        lines += [f"- `{path}`" for path in data["incorporated_from_other"]]
    if data.get("remaining_disagreements"):
        lines += ["", "## Remaining disagreements", ""]
        lines += [f"- {item.get('area', '')} ({item.get('kind', '')}): {item.get('note', '')}"
                  for item in data["remaining_disagreements"]]
    lines += ["", f"MUST_FIX: {counts.get('must_fix_total', 0)}", ""]
    return "\n".join(lines)


def write_markdown(json_path):
    """Render the human-readable review next to the authoritative JSON."""
    json_path = pathlib.Path(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    md_path = json_path.with_name(json_path.name.replace("-review.json", "-review.md"))
    md_path.write_text(render(data), encoding="utf-8")
    return md_path
