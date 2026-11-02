"""Command line: `cav run` for one run, `cav resume` to continue a stopped one, `cav serve` for the dashboard."""

import argparse
import json
import os
import pathlib
import sys
import time

from .flow import Flow
from .web import Home, meta_alive, process_start


def cmd_run(args):
    spec = json.loads(pathlib.Path(args.spec).read_text(encoding="utf-8-sig"))
    run_dir = args.run_dir or pathlib.Path("runs") / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    flow = Flow(spec["repos"], run_dir, task=spec.get("task"), plan=spec.get("plan"), checks=spec.get("checks") or (),
                agents=spec.get("agents"))
    meta = flow.run_dir / "run.json"  # the dashboard follows this process: live, stoppable, not resumable while it runs
    data = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    meta.write_text(json.dumps(dict({"repo": flow.state["repo"], **data}, pid=os.getpid(),
                                    started=process_start(os.getpid()))), encoding="utf-8")
    print(f"run {flow.run_dir}")
    return flow.run()


def cmd_resume(args):
    meta = pathlib.Path(args.run_dir) / "run.json"  # the dashboard follows this process from now on
    data = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    # the dashboard records this process (or the launcher that started it) before it gets here
    if data.get("pid") not in (os.getpid(), os.getppid()) and meta_alive(meta):
        raise SystemExit("that run is still running")
    flow = Flow.resume(args.run_dir)
    meta.write_text(json.dumps(dict(data, pid=os.getpid(), started=process_start(os.getpid()))), encoding="utf-8")
    print(f"resume {flow.run_dir}")
    return flow.run()


def cmd_serve(args):
    home = Home(runs_dir=args.runs, port=args.port)
    if sys.stdout is None:  # pythonw at login has no console: keep a log instead of losing output
        sys.stdout = sys.stderr = open(home.runs_dir / "serve.log", "a", buffering=1, encoding="utf-8")
    home.serve_forever()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cav", description="Cross-agent validation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="a task, or a plan and two sets of branches, to one result both agents agree on")
    run.add_argument("--spec", required=True,
                     help='a JSON file: {"repos": [{"repo": path or GitHub URL, "branches": [set 1, set 2]}], '
                          '"task": ...} or, with branches, "plan": ..., and optionally "checks": [commands] and '
                          '"agents": [two of deepseek, kimi, claude]')
    run.add_argument("--run-dir")
    run.set_defaults(func=cmd_run)
    resume = sub.add_parser("resume", help="a run that stopped before its result, from the step it was in")
    resume.add_argument("--run-dir", required=True)
    resume.set_defaults(func=cmd_resume)
    serve = sub.add_parser("serve", help="the dashboard: every run, and a form to start one")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--runs", help="the runs folder (default: runs/ in this checkout)")
    serve.set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)
