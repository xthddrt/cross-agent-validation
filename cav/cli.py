"""Command line: `cav run` for one run, `cav serve` for the dashboard."""

import argparse
import json
import pathlib
import sys
import time

from .flow import Flow
from .web import Home


def cmd_run(args):
    spec = json.loads(pathlib.Path(args.spec).read_text(encoding="utf-8"))
    run_dir = args.run_dir or pathlib.Path("runs") / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    flow = Flow(spec["repos"], run_dir, task=spec.get("task"), plan=spec.get("plan"), checks=spec.get("checks") or ())
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
    run = sub.add_parser("run", help="a task, or a plan and two sets of branches, to one result both agents agree on")
    run.add_argument("--spec", required=True,
                     help='a JSON file: {"repos": [{"repo": path or GitHub URL, "branches": [set 1, set 2]}], '
                          '"task": ...} or, with branches, "plan": ..., and optionally "checks": [commands]')
    run.add_argument("--run-dir")
    run.set_defaults(func=cmd_run)
    serve = sub.add_parser("serve", help="the dashboard: every run, and a form to start one")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--runs", help="the runs folder (default: runs/ in this checkout)")
    serve.set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)
