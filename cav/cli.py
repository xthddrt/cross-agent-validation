"""Command line entry points."""

import argparse
import json
import pathlib
import sys
import threading
import time

from . import agents as agent_mod
from . import config
from .gitops import git, git_ok
from .orchestrator import Orchestrator, clock
from .web import Dashboard

DEFAULT_MODELS = {
    "deepseek": ("opencode", "deepseek/deepseek-flash#high"),
    "kimi": ("kimi", "kimi-code/k3.1-pro"),
}


def _start_dashboard(run_dir, port, open_browser=False):
    dashboard = Dashboard(run_dir, port=port, open_browser=open_browser)
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    return dashboard


def cmd_run(args):
    case = config.load(args.case)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else (
        pathlib.Path("runs") / f"{case.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    orchestrator = Orchestrator(case, run_dir, push=args.push, thinking=args.thinking,
                                strict=True if args.strict else None, resume=args.resume)
    print(f"case        {case.name} (schema v{case.version})")
    print(f"host        {case.host_repo}")
    print(f"exchange    {orchestrator.exchange.path}")
    print(f"convergence {case.convergence}   (both candidates weighed equally)")
    print(f"push        {'yes' if args.push else 'no (use --push)'}")
    if args.dashboard:
        _start_dashboard(orchestrator.run_dir, args.port, open_browser=True)
    try:
        state = orchestrator.run()
    except RuntimeError as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        print(f"state: {orchestrator.run_dir / 'state.json'}")
        return 1
    final = state.get("final")
    if not final:
        print("\nNo agreement. See state.json and REPORT.md is not written.")
        return 2
    print(f"\nAccepted ({final.get('verdict')}): {final['commit']}")
    print(f"Tree:      {final['tree']}")
    print(f"Report:    {orchestrator.run_dir / 'REPORT.md'}")
    return 0


def cmd_solve(args):
    repo = pathlib.Path(args.repo).resolve()
    if not repo.exists():
        print(f"repo not found: {repo}", file=sys.stderr)
        return 1
    commit = args.base or git("rev-parse", "HEAD", cwd=repo, check=False)
    if not commit:
        print("repo has no commits; use --base", file=sys.stderr)
        return 1
    prompt = pathlib.Path(args.prompt).read_text(encoding="utf-8")
    specs = {}
    for item in args.agents.split(","):
        name = item.strip()
        kind, model = DEFAULT_MODELS.get(name, ("opencode", None))
        if name in (args.model or {}):
            model = args.model[name]
        specs[name] = config.AgentSpec(name=name, kind=kind, model=model,
                                       thinking="high" if kind == "kimi" else None)
    if len(specs) != 2:
        print("--agents needs exactly two names", file=sys.stderr)
        return 1
    case = config.Case(
        name=args.name or f"solve-{repo.name}",
        host_repo=repo, base=commit, agents=specs, checks=args.checks,
        version=2, convergence=args.convergence,
        bootstrap="base", max_rounds=args.max_rounds,
        round_seconds=args.round_seconds, strict_paths=args.strict_path or [],
        task={"prompt": prompt, "acceptance": [], "context_files": [],
              "base_behavior_invariants": []},
    )
    orchestrator = Orchestrator(case, pathlib.Path("runs") / f"{case.name}-{time.strftime('%Y%m%d-%H%M%S')}",
                                push=args.push, strict=True if args.strict else None)
    print(f"solve       {case.name}")
    print(f"repo        {repo} @ {commit[:12]}")
    print(f"agents      {', '.join(specs)}   convergence {case.convergence}")
    if args.dashboard:
        _start_dashboard(orchestrator.run_dir, args.port, open_browser=True)
    try:
        state = orchestrator.run()
    except RuntimeError as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1
    final = state.get("final")
    print(f"\nAccepted: {final['commit']}" if final else "\nNo agreement.")
    return 0 if final else 2


def cmd_check(args):
    try:
        case = config.load(args.case)
    except Exception as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    print(config.describe(case))
    problems = []
    if not case.host_repo.exists():
        problems.append(f"host repo missing: {case.host_repo}")
    elif case.base and not git_ok("rev-parse", "--verify", "--quiet", case.base, cwd=case.host_repo):
        problems.append(f"base commit not resolvable: {case.base}")
    if case.convergence == "functional" and case.version < 2:
        problems.append("functional convergence needs schema v2 (structured reviews)")
    for name, spec in case.agents.items():
        if spec.bootstrap_ref and case.host_repo.exists() and not git_ok(
                "rev-parse", "--verify", "--quiet", spec.bootstrap_ref, cwd=case.host_repo):
            problems.append(f"{name}: bootstrap_ref not resolvable: {spec.bootstrap_ref}")
    if problems:
        print("\n".join(f"PROBLEM: {item}" for item in problems), file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_web(args):
    Dashboard(pathlib.Path(args.run), port=args.port,
              open_browser=args.open).serve_forever()
    return 0


def cmd_status(args):
    run_dir = pathlib.Path(args.run)
    while True:
        state_path = run_dir / "state.json"
        if not state_path.exists():
            print(f"no state.json in {run_dir}", file=sys.stderr)
            return 1
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if args.json:
            print(json.dumps(state, indent=2))
            return 0
        if args.watch:
            import os
            os.system("cls" if os.name == "nt" else "clear")
            print(f"watching {run_dir}  (Ctrl+C to stop)")
        print(f"case   {state.get('case')}")
        print(f"phase  {state.get('phase')}  round {state.get('round')}  updated {state.get('updated')}")
        for name, info in (state.get("agents") or {}).items():
            print(f"  {name:<9} {info.get('state', '?'):<8} {clock(info.get('elapsed', 0)):>6}  "
                  f"idle {clock(info.get('idle', 0)):>5}  msgs {info.get('messages', 0):<3} "
                  f"{info.get('detail', '')}")
        for item in state.get("history", []):
            totals = item.get("divergence") or {}
            if item.get("results"):
                print(f"  round {item['round']}: {item.get('verdict')}  "
                      f"substantive={totals.get('substantive')}  "
                      f"blocking={item.get('blockers', {}).get('blocking_must_fix')}")
            elif item.get("failed"):
                print(f"  round {item['round']}: failed {item['failed']}")
        blockers = state.get("blockers") or {}
        if blockers:
            print(f"  gates  checks={blockers.get('checks')} "
                  f"blocking={blockers.get('blocking_must_fix')} "
                  f"trees_equal={blockers.get('trees_equal')} "
                  f"eligible={blockers.get('eligible')}")
        final = state.get("final")
        if final:
            print(f"final  {final['commit']} tree {final['tree']} "
                  f"verdict={final.get('verdict')} pushed={final['pushed']}")
        if not args.watch:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def cmd_diff(args):
    run_dir = pathlib.Path(args.run)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    history = [item for item in state.get("history", []) if item.get("results")]
    if not history:
        print("no round with two candidates yet", file=sys.stderr)
        return 1
    item = next((h for h in history if h["round"] == args.round), history[-1]) if args.round else history[-1]
    commits = [item["results"][name]["commit"] for name in item["results"]]
    exchange = run_dir / "exchange.git"
    if args.path:
        print(git("--git-dir", str(exchange), "diff", "--no-color", commits[0], commits[1],
                  "--", args.path, check=False))
    else:
        print(f"round {item['round']}  {commits[0][:12]} vs {commits[1][:12]}")
        print(git("--git-dir", str(exchange), "diff", "--stat", commits[0], commits[1], check=False))
    return 0


def cmd_smoke(args):
    case = config.load(args.case)
    work = pathlib.Path(args.dir or (pathlib.Path("runs") / "_smoke")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, spec in case.agents.items():
        agent = agent_mod.build(spec, work / f"{name}.log", title_prefix="cav smoke")
        print(f"{name} ({spec.kind}) ...", end=" ", flush=True)
        try:
            result = agent.run("Reply with exactly: CAV_OK", work)
        except Exception as error:
            print(f"ERROR {error}")
            failed += 1
            continue
        ok = result.get("ok") and "CAV_OK" in (result.get("text") or "")
        print(f"{'OK' if ok else 'FAIL'}  session={result.get('session')}  "
              f"text={(result.get('text') or '')[:60]!r}")
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cav", description="Cross-agent reconciliation driver")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="bootstrap candidates and drive rounds to agreement")
    run.add_argument("--case", required=True)
    run.add_argument("--run-dir", default=None)
    run.add_argument("--push", action="store_true", help="push the agreed commit to origin after convergence")
    run.add_argument("--strict", action="store_true", help="require byte-identical trees")
    run.add_argument("--dashboard", action="store_true", help="open the local dashboard for this run")
    run.add_argument("--port", type=int, default=7717)
    run.add_argument("--resume", action="store_true", help="continue the run in --run-dir")
    run.add_argument("--thinking", action="store_true", help="include raw thinking blocks in events")
    run.set_defaults(func=cmd_run)

    solve = sub.add_parser("solve", help="implement one prompt with two agents, then reconcile")
    solve.add_argument("--repo", required=True)
    solve.add_argument("--prompt", required=True, help="file containing the task")
    solve.add_argument("--checks", action="append", default=[])
    solve.add_argument("--agents", default="deepseek,kimi")
    solve.add_argument("--model", action="append", default=[], metavar="NAME=MODEL")
    solve.add_argument("--name", default=None)
    solve.add_argument("--base", default=None)
    solve.add_argument("--convergence", default="functional", choices=list(config.CONVERGENCE))
    solve.add_argument("--max-rounds", type=int, default=3)
    solve.add_argument("--round-seconds", type=int, default=5400)
    solve.add_argument("--strict-path", action="append", default=[])
    solve.add_argument("--push", action="store_true")
    solve.add_argument("--strict", action="store_true")
    solve.add_argument("--dashboard", action="store_true")
    solve.add_argument("--port", type=int, default=7717)
    solve.set_defaults(func=cmd_solve)

    check = sub.add_parser("check", help="validate a case file before spending agent time")
    check.add_argument("--case", required=True)
    check.set_defaults(func=cmd_check)

    web = sub.add_parser("web", help="serve the read-only dashboard for a run")
    web.add_argument("--run", required=True)
    web.add_argument("--port", type=int, default=7717)
    web.add_argument("--open", action="store_true")
    web.set_defaults(func=cmd_web)

    status = sub.add_parser("status", help="show a run's progress")
    status.add_argument("--run", required=True)
    status.add_argument("--watch", action="store_true", help="refresh until Ctrl+C")
    status.add_argument("--json", action="store_true")
    status.add_argument("--interval", type=float, default=5.0)
    status.set_defaults(func=cmd_status)

    diff = sub.add_parser("diff", help="show what the two candidates disagree on")
    diff.add_argument("--run", required=True)
    diff.add_argument("--round", type=int, default=None)
    diff.add_argument("--path", default=None)
    diff.set_defaults(func=cmd_diff)

    smoke = sub.add_parser("smoke", help="check both agents answer a trivial prompt")
    smoke.add_argument("--case", required=True)
    smoke.add_argument("--dir", default=None)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    if getattr(args, "model", None):
        args.model = dict(item.split("=", 1) for item in args.model if "=" in item)
    return args.func(args)
