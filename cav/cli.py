"""Command line: `cav run` for one run, `cav serve` for the dashboard."""

import argparse
import pathlib
import sys
import time

from .flow import Flow
from .web import Home


def read(path):
    return pathlib.Path(path).read_text(encoding="utf-8") if path else None


def cmd_run(args):
    run_dir = args.run_dir or pathlib.Path("runs") / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    flow = Flow(args.repo, run_dir, task=read(args.task_file), plan=read(args.plan_file), branches=args.branch)
    print(f"run {flow.run_dir}")
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
    run = sub.add_parser("run", help="a task, or a plan and two branches, to one result both agents agree on")
    run.add_argument("--repo", required=True, help="a local git repo or a GitHub URL")
    run.add_argument("--task-file", help="the task (with no branches)")
    run.add_argument("--plan-file", help="the plan the two branches implement")
    run.add_argument("--branch", action="append", default=[], help="a branch; give two, with --plan-file")
    run.add_argument("--run-dir")
    run.set_defaults(func=cmd_run)
    serve = sub.add_parser("serve", help="the dashboard: every run, and a form to start one")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--runs", help="the runs folder (default: runs/ in this checkout)")
    serve.set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)
