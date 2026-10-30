"""Command line entry points."""

import argparse
import pathlib
import sys
import threading
import time

from . import agents as agent_mod
from . import config
from .flow import Flow
from .gitops import git_ok
from .web import Dashboard, Home


def case_task(case):
    """A case file's task as one text: the prompt, then its acceptance criteria and invariants."""
    lines = [case.task_prompt.strip()]
    for title, items in (("Acceptance", case.task_acceptance), ("Must not regress", case.base_invariants)):
        if items:
            lines += ["", f"{title}:"] + [f"- {item}" for item in items]
    return "\n".join(lines)


def cmd_run(args):
    if args.case:
        case = config.load(args.case)
        repo, task, checks = str(case.host_repo), case_task(case), case.checks
        branches = [spec.bootstrap_ref for spec in case.agents.values() if spec.bootstrap_ref]
        branches = branches if len(set(branches)) == 2 else []
    else:
        if not (args.repo and args.prompt_file):
            print("give --case, or --repo and --prompt-file", file=sys.stderr)
            return 1
        repo, task, checks, branches = args.repo, pathlib.Path(args.prompt_file).read_text(encoding="utf-8"), [], []
    run_dir = args.run_dir or pathlib.Path("runs") / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    flow = Flow(repo, task, run_dir, branches=args.branch or branches, checks=args.check or checks,
                push=not args.no_push, builder=args.builder)
    if args.dashboard:
        server = Dashboard(flow.run_dir, port=args.port, open_browser=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"run {flow.run_dir}")
    return flow.run()


def cmd_check(args):
    try:
        case = config.load(args.case)
    except Exception as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    print(config.describe(case))
    refs = [spec.bootstrap_ref for spec in case.agents.values() if spec.bootstrap_ref]
    missing = [ref for ref in refs if not git_ok("rev-parse", "--verify", "--quiet", ref, cwd=case.host_repo)]
    if not case.host_repo.exists() or missing:
        print(f"PROBLEM: repo {case.host_repo} or refs {missing} not found", file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_web(args):
    Dashboard(pathlib.Path(args.run), port=args.port, open_browser=args.open).serve_forever()
    return 0


def cmd_serve(args):
    home = Home(port=args.port, open_browser=args.open)
    if sys.stdout is None:  # pythonw at login has no console: keep a log instead of losing output
        sys.stdout = sys.stderr = open(home.runs_dir / "serve.log", "a", buffering=1, encoding="utf-8")
    home.serve_forever()
    return 0


def cmd_smoke(args):
    work = pathlib.Path(args.dir or (pathlib.Path("runs") / "_smoke")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, (kind, model) in config.DEFAULT_MODELS.items():
        agent = agent_mod.build(config.AgentSpec(name=name, kind=kind, model=model), work / f"{name}.log",
                                title_prefix="cav smoke")
        print(f"{name} ({kind}) ...", end=" ", flush=True)
        try:
            result = agent.run("Reply with exactly: CAV_OK", work)
        except Exception as error:
            print(f"ERROR {error}")
            failed += 1
            continue
        ok = result.get("ok") and "CAV_OK" in (result.get("text") or "")
        print(f"{'OK' if ok else 'FAIL'}  text={(result.get('text') or '')[:60]!r}")
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cav", description="Cross-agent validation")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="plan, agree, build and review with two agents")
    run.add_argument("--case", help="a case file (repo, branches, checks, task)")
    run.add_argument("--repo", help="local repo path or GitHub URL")
    run.add_argument("--prompt-file", help="the task")
    run.add_argument("--branch", action="append", default=[], help="a branch to reconcile; give two, or none")
    run.add_argument("--check", action="append", default=[], help="a check command (build, tests)")
    run.add_argument("--builder", default="deepseek", help="the agent that implements; the other reviews")
    run.add_argument("--no-push", action="store_true", help="keep the result local instead of pushing")
    run.add_argument("--run-dir", default=None)
    run.add_argument("--dashboard", action="store_true")
    run.add_argument("--port", type=int, default=7717)
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="validate a case file")
    check.add_argument("--case", required=True)
    check.set_defaults(func=cmd_check)

    web = sub.add_parser("web", help="serve the dashboard for one run")
    web.add_argument("--run", required=True)
    web.add_argument("--port", type=int, default=7717)
    web.add_argument("--open", action="store_true")
    web.set_defaults(func=cmd_web)

    serve = sub.add_parser("serve", help="the home dashboard: every run, and a form to start one")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    smoke = sub.add_parser("smoke", help="check both agents answer a trivial prompt")
    smoke.add_argument("--dir", default=None)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    return args.func(args)
