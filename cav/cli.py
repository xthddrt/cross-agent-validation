"""Command line entry points."""

import argparse
import json
import pathlib
import sys
import time

from . import agents as agent_mod
from . import config
from .orchestrator import Orchestrator, clock


def cmd_run(args):
    case = config.load(args.case)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else (
        pathlib.Path("runs") / f"{case.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    orchestrator = Orchestrator(case, run_dir, push=args.push)
    print(f"case     {case.name}")
    print(f"host     {case.host_repo}")
    print(f"exchange {orchestrator.exchange.path}")
    print(f"push     {'yes' if args.push else 'no (use --push)'}")
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
    print(f"\nConverged: {final['commit']}")
    print(f"Tree:      {final['tree']}")
    print(f"Report:    {orchestrator.run_dir / 'REPORT.md'}")
    return 0


def cmd_status(args):
    run_dir = pathlib.Path(args.run)
    state_path = run_dir / "state.json"
    if not state_path.exists():
        print(f"no state.json in {run_dir}", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"case   {state.get('case')}")
    print(f"phase  {state.get('phase')}  round {state.get('round')}  updated {state.get('updated')}")
    for name, info in (state.get("agents") or {}).items():
        elapsed = clock(info.get("elapsed", 0))
        idle = clock(info.get("idle", 0))
        print(f"  {name:<9} {info.get('state', '?'):<8} {elapsed:>6}  idle {idle:>5}  "
              f"msgs {info.get('messages', 0):<3} {info.get('detail', '')}")
    for item in state.get("history", []):
        if "results" in item:
            print(f"  round {item['round']}: converged={item['converged']} "
                  f"trees_equal={item['trees_equal']} must_fix="
                  f"{ {k: v['must_fix'] for k, v in item['results'].items()} }")
        elif "failed" in item:
            print(f"  round {item['round']}: failed {item['failed']}")
    final = state.get("final")
    if final:
        print(f"final  {final['commit']} tree {final['tree']} pushed={final['pushed']}")
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
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show a run's progress")
    status.add_argument("--run", required=True)
    status.set_defaults(func=cmd_status)

    smoke = sub.add_parser("smoke", help="check both agents answer a trivial prompt")
    smoke.add_argument("--case", required=True)
    smoke.add_argument("--dir", default=None)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    return args.func(args)
