"""Structured reviews: each agent's evidence-backed verdict on both candidates."""

import json
import pathlib

SEVERITIES = ("MUST_FIX", "SHOULD_FIX", "NIT")
FINDING_KINDS = ("behavioral", "cosmetic", "editorial")
# "equivalent" lets an agent record a difference it accepts; only "behavioral" blocks agreement.
DISAGREEMENT_KINDS = FINDING_KINDS + ("equivalent",)
DISPOSITIONS = ("retained", "intentionally_dropped")


class Review:
    """One agent's validated result for one round."""

    def __init__(self, agent, round_no, path, data, errors):
        self.agent = agent
        self.round = round_no
        self.path = pathlib.Path(path)
        self.data = data if isinstance(data, dict) else {}
        self.errors = list(errors)

    @property
    def valid(self):
        return not self.errors

    @property
    def counts(self):
        return self.data.get("counts") or {}

    @property
    def blocking(self):
        return int(self.counts.get("blocking_must_fix") or 0)

    @property
    def behavior_approved(self):
        return self.data.get("behavior_approved") is True

    @property
    def other_approved(self):
        return self.data.get("other_behavior_approved") is True

    @property
    def open_behavioural(self):
        return any((item or {}).get("kind") == "behavioral"
                   for item in (self.data.get("remaining_disagreements") or []))

    def summary(self):
        counts = self.counts
        return (f"MUST_FIX={counts.get('must_fix_total', '?')} "
                f"blocking={counts.get('blocking_must_fix', '?')} "
                f"approved={self.behavior_approved}/{self.other_approved}")


def _count(findings, severity, kind=None):
    return sum(1 for finding in findings
               if finding.get("severity") == severity and kind in (None, finding.get("kind")))


def _line_count(path):
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines()) or 1
    except OSError:
        return None


def _shape_errors(data):
    """Reviews are agent-written: every field the orchestrator reads must have the right shape."""
    errors = []
    for key in ("areas", "remaining_disagreements", "carry_forward_dispositions"):
        value = data.get(key)
        if value is not None and (not isinstance(value, list)
                                  or any(not isinstance(item, dict) for item in value)):
            errors.append(f"{key} must be a list of objects")
    changes = data.get("changes")
    if changes is not None and (not isinstance(changes, list)
                                or any(not isinstance(item, dict) or not item.get("path")
                                       for item in changes)):
        errors.append("changes must be a list of objects with a path")
    incorporated = data.get("incorporated_from_other")
    if incorporated is not None and not isinstance(incorporated, list):
        errors.append("incorporated_from_other must be a list")
    return errors


def validate(data, agent, round_no, worktree, required_dispositions=()):
    """Errors in an agent's review; empty means valid. Fills in `counts` from the findings."""
    if not isinstance(data, dict):
        return ["review is not an object"]
    errors = []
    worktree = pathlib.Path(worktree)
    if data.get("agent") != agent:
        errors.append(f"agent is {data.get('agent')!r}, expected {agent!r}")
    try:
        if int(data.get("round")) != int(round_no):
            errors.append(f"round is {data.get('round')!r}, expected {round_no}")
    except (TypeError, ValueError):
        errors.append("round missing or not an integer")

    findings = data.get("findings") or []
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        return errors + ["findings must be a list of objects"]
    shaped = _shape_errors(data)
    if shaped:
        return errors + shaped
    ids = [finding.get("id") for finding in findings]
    if any(not isinstance(item, str) or not item for item in ids):
        errors.append("finding ids must be non-empty strings")
    elif len(ids) != len(set(ids)):
        errors.append("duplicate finding ids")
    line_counts = {}
    for finding in findings:
        label = finding.get("id") or "?"
        if finding.get("severity") not in SEVERITIES:
            errors.append(f"{label}: severity {finding.get('severity')!r} invalid")
        if finding.get("kind") not in FINDING_KINDS:
            errors.append(f"{label}: kind {finding.get('kind')!r} invalid")
        # Every finding is evidence: it must point at a real line the other agent can open.
        path = finding.get("path")
        if not isinstance(path, str) or not path:
            errors.append(f"{label}: no valid path")
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
        try:
            line = int(finding.get("line"))
        except (TypeError, ValueError):
            errors.append(f"{label}: line missing or not an integer")
            continue
        if target not in line_counts:
            line_counts[target] = _line_count(target)
        count = line_counts[target]
        if count is not None and not 1 <= line <= count:
            errors.append(f"{label}: line {line} outside {path} (1..{count})")

    # Counts are derived from the findings, never trusted from the agent.
    blocking = _count(findings, "MUST_FIX", "behavioral")
    data["counts"] = {"must_fix_total": _count(findings, "MUST_FIX"), "blocking_must_fix": blocking,
                      "should_fix": _count(findings, "SHOULD_FIX"), "nit": _count(findings, "NIT")}

    for key in ("behavior_approved", "other_behavior_approved"):
        if not isinstance(data.get(key), bool):
            errors.append(f"{key} must be boolean")
        elif data[key] is False and blocking == 0:
            errors.append(f"{key} is false without a blocking finding")

    for item in data.get("remaining_disagreements") or []:
        kind = item.get("kind")
        if kind not in DISAGREEMENT_KINDS:
            errors.append(f"remaining disagreement {item.get('area')!r}: kind {kind!r} invalid")
        if kind == "behavioral" and data.get("behavior_approved"):
            errors.append(
                f"remaining_disagreements lists {item.get('area')!r} as behavioural while "
                f"behavior_approved is true; mark it equivalent, move it to areas, or withhold approval")

    dispositions = data.get("carry_forward_dispositions") or []
    disposed = {entry.get("item_id") for entry in dispositions}
    missing = [item for item in required_dispositions if item not in disposed]
    if missing:
        errors.append(f"carry-forward items without a disposition: {', '.join(sorted(missing)[:8])}")
    for entry in dispositions:
        if entry.get("status") not in DISPOSITIONS:
            errors.append(f"{entry.get('item_id')}: invalid disposition {entry.get('status')!r}")
        elif not isinstance(entry.get("why"), str) or not entry["why"].strip():
            errors.append(f"{entry.get('item_id')}: disposition needs a why")
    return errors


def load(path, agent, round_no, worktree, required_dispositions=()):
    """The agent's review, validated; None when it wrote none."""
    path = pathlib.Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        return Review(agent, round_no, path, {}, [f"invalid JSON: {error}"])
    return Review(agent, round_no, path, data,
                  validate(data, agent, round_no, worktree, required_dispositions))
