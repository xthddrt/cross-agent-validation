"""Structured reviews: each agent's evidence-backed verdict on both candidates."""

import json
import pathlib

SEVERITIES = ("MUST_FIX", "SHOULD_FIX", "NIT")


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
        return int(self.counts.get("must_fix") or 0)

    @property
    def approvals(self):
        """Candidate name -> approved; empty for an independent round, which approves nothing."""
        approve = self.data.get("approve")
        return {name: value is True for name, value in approve.items()} if isinstance(approve, dict) else {}

    def summary(self):
        approvals = " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in self.approvals.items())
        return f"MUST_FIX={self.blocking} approves {approvals or '-'}"


def _line_count(path):
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines()) or 1
    except OSError:
        return None


def validate(data, agent, round_no, trees=None):
    """Errors in an agent's review; empty means valid. Fills in `counts` from the findings.

    `trees` maps each candidate under review to its checkout; None for an independent round,
    whose review is notes only. Every finding names its candidate and must point at a real line
    in that candidate's checkout, and approval is withheld only by a MUST_FIX on that candidate.
    """
    if not isinstance(data, dict):
        return ["review is not an object"]
    errors = []
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
    if trees is None:
        data["counts"] = {"must_fix": 0, "should_fix": 0, "nit": 0}
        return errors

    approve = data.get("approve")
    if not isinstance(approve, dict) or set(approve) != set(trees) \
            or any(not isinstance(value, bool) for value in approve.values()):
        return errors + [f"approve must map exactly {sorted(trees)} to true or false"]
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
        candidate = finding.get("candidate")
        if candidate not in trees:
            errors.append(f"{label}: candidate must be one of {sorted(trees)}")
            continue
        # Every finding is evidence: it must point at a real line the other agent can open.
        root = pathlib.Path(trees[candidate]).resolve()
        path = finding.get("path")
        if not isinstance(path, str) or not path:
            errors.append(f"{label}: no valid path")
            continue
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            errors.append(f"{label}: path escapes {candidate}'s candidate: {path}")
            continue
        if not target.is_file():
            errors.append(f"{label}: {path} is not a file in {candidate}'s candidate")
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
    must_fix = {name: sum(1 for f in findings if f.get("severity") == "MUST_FIX" and f.get("candidate") == name)
                for name in trees}
    data["counts"] = {"must_fix": sum(must_fix.values()),
                      "should_fix": sum(1 for f in findings if f.get("severity") == "SHOULD_FIX"),
                      "nit": sum(1 for f in findings if f.get("severity") == "NIT")}
    for name, approved in approve.items():
        if approved and must_fix[name]:
            errors.append(f"approves {name} but lists a MUST_FIX against it")
        if not approved and not must_fix[name]:
            errors.append(f"withholds approval from {name} without a MUST_FIX against it")
    return errors


def load(path, agent, round_no, trees=None):
    """The agent's review, validated; None when it wrote none."""
    path = pathlib.Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        return Review(agent, round_no, path, {}, [f"invalid JSON: {error}"])
    return Review(agent, round_no, path, data, validate(data, agent, round_no, trees))
