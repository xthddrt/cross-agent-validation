"""Two runs side by side: how long each round, stage and run took, how each ended, and how close
their results are, measured against the original branches both runs started from."""

import datetime
import json
import pathlib
import time

from .gitops import git

STAGES = ("plan", "implement", "reconcile", "code")


def _read(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _events(run_dir):
    out = []
    try:
        lines = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _seconds(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def stage_rounds(stage_dir, stage, live):
    """Each round of one orchestrator run: wall time, each agent's time, verdict and gap."""
    state, now = _read(stage_dir / "state.json"), time.time()
    history = {item["round"]: item for item in state.get("history") or []}
    rounds = {}
    for event in _events(stage_dir):
        number = event.get("round")
        if not number or not event.get("ts"):
            continue
        at = _seconds(event["ts"])
        item = rounds.setdefault(number, {"start": at, "end": at, "agents": {}})
        item["start"], item["end"] = min(item["start"], at), max(item["end"], at)
        if event.get("agent") and event["type"] == "agent.launch":
            item["agents"].setdefault(event["agent"], {}).setdefault("start", at)
        if event.get("agent") and event["type"] == "agent.done":
            item["agents"].setdefault(event["agent"], {})["end"] = at
    out = []
    for number in sorted(rounds):
        item, entry = rounds[number], history.get(number, {})
        running = live and not entry and number == state.get("round")
        end = now if running else item["end"]
        gate = entry.get("blockers") or {}
        out.append({
            "stage": stage, "round": number, "start": item["start"], "seconds": round(end - item["start"]),
            "verdict": "failed" if entry.get("failed") else entry.get("verdict") or ("running" if running else "?"),
            "differing": (entry.get("divergence") or {}).get("differing"),
            "must_fix": gate.get("blocking_must_fix"),
            "method": (gate.get("decision") or {}).get("method"),
            "agents": {name: round(times.get("end", now if running else item["end"]) - times["start"])
                       for name, times in item["agents"].items() if "start" in times},
        })
    return out


def _host(run_dir, pipe):
    if pipe.get("repo"):
        return pipe["repo"]
    for gitfile in run_dir.glob("wt/r*/*/.git"):
        text = gitfile.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            return text.split(":", 1)[1].strip().split("/.git/worktrees/")[0]
    return None


def _branch_name(host, sha):
    refs = git("for-each-ref", "--points-at", sha, "--format=%(refname:short)", "refs/remotes", "refs/heads",
               cwd=host, check=False).split()
    names = [ref.split("/", 1)[1] if ref.startswith("origin/") else ref for ref in refs if ref != "origin/HEAD"]
    return names[0] if names else sha[:8]


def run_summary(run_dir, alive=lambda pid: False):
    """Timings, outcome and result commits for one run (a pipeline or a plain run)."""
    pipe = _read(run_dir / "pipeline.json")
    host = _host(run_dir, pipe)
    if pipe:
        stages = [stage for stage in STAGES if (run_dir / stage / "state.json").exists()]
        live = pipe.get("stage") not in ("done", "failed") and alive(pipe.get("pid"))
        dirs = {stage: run_dir / stage for stage in stages}
        originals = {item["commit"]: item["ref"] for item in (pipe.get("labels") or {}).values()}
        last_stage = _read(run_dir / stages[-1] / "state.json") if stages else {}
        outcome = pipe.get("outcome") or (f"running · {pipe.get('stage')}" if live else pipe.get("stage") or "?")
        plan = {"outcome": (pipe.get("plan") or {}).get("outcome"), "rounds": (pipe.get("plan") or {}).get("rounds")}
        if pipe.get("plan_base"):
            plan["base"] = (pipe.get("labels") or {}).get(pipe["plan_base"], {}).get("ref")
        # pipeline.json keeps local time; events keep UTC
        start = time.mktime(time.strptime(pipe["started"], "%Y-%m-%dT%H:%M:%S")) if pipe.get("started") else None
    else:
        state = _read(run_dir / "state.json")
        stages = [""]
        live = not (state.get("final") or state.get("phase") == "disagreement"
                    or str(state.get("phase", "")).endswith("failed"))
        dirs = {"": run_dir}
        first = (state.get("history") or [{}])[0].get("starts") or {}
        originals = {sha: _branch_name(host, sha) if host else sha[:8] for sha in first.values()}
        last_stage = state
        outcome = ("agreed" if state.get("final") else "no agreement" if state.get("phase") == "disagreement"
                   else "running" if live else state.get("phase"))
        plan = None
        start = None
    rounds = [item for stage in stages for item in stage_rounds(dirs[stage], stage, live)]
    events = [event for stage in stages for event in _events(dirs[stage]) if event.get("ts")]
    if start is None and events:
        start = min(_seconds(event["ts"]) for event in events)
    end = time.time() if live else max((_seconds(event["ts"]) for event in events), default=start)
    final = (last_stage.get("final") or {}).get("commit")
    last = next((item["results"] for item in reversed(last_stage.get("history") or []) if item.get("results")), {})
    results = ({"published": final} if final else
               {f"{name}'s last proposal": result["commit"] for name, result in last.items()})
    return {"id": run_dir.name, "kind": "pipeline" if pipe else "run", "live": live, "outcome": outcome,
            "plan": plan, "host": host, "originals": originals, "results": results,
            "start": start, "seconds": round(end - start) if start else None, "rounds": rounds,
            "stages": {stage or "run": sum(item["seconds"] for item in rounds if item["stage"] == stage)
                       for stage in stages}}


def _distance(host, a, b):
    """Changed lines and files between two commits."""
    out = git("diff", "--numstat", "--no-renames", a, b, cwd=host, check=False)
    lines = files = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) >= 3:
            files += 1
            lines += sum(int(part) for part in parts[:2] if part.isdigit())
    return {"lines": lines, "files": files}


def compare(runs_dir, ids, alive=lambda pid: False):
    summaries = [run_summary(runs_dir / run_id, alive) for run_id in ids]
    host = next((item["host"] for item in summaries if item["host"]), None)
    originals = {}
    for item in summaries:
        originals.update(item["originals"])
    closeness = {}
    if host:
        for item in summaries:
            item["to_originals"] = {label: {originals[sha]: _distance(host, sha, commit) for sha in originals}
                                    for label, commit in item["results"].items()}
        if len(summaries) == 2:
            closeness = {f"{summaries[0]['id']}: {a} vs {summaries[1]['id']}: {b}": _distance(host, ca, cb)
                         for a, ca in summaries[0]["results"].items() for b, cb in summaries[1]["results"].items()}
    return {"runs": summaries, "originals": sorted(set(originals.values())), "closeness": closeness}
