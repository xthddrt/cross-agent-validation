"""Command line entry points."""

import argparse
import pathlib
import sys
import threading
import time

from . import agents as agent_mod
from . import config
from .gitops import git, git_ok
from .orchestrator import Orchestrator
from .pipeline import Pipeline
from .web import Dashboard, Home

DEFAULT_MODELS = config.DEFAULT_MODELS


def _execute(orchestrator, dashboard, port):
    """Run to the end and report the outcome: 0 agreed, 1 failed, 2 no agreement."""
    if dashboard:
        server = Dashboard(orchestrator.run_dir, port=port, open_browser=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state = orchestrator.run()
    except RuntimeError as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        print(f"state: {orchestrator.run_dir / 'state.json'}")
        return 1
    final = state.get("final")
    if not final:
        print(f"\nNo agreement. See {orchestrator.run_dir / 'state.json'}")
        return 2
    print(f"\nAccepted: {final['commit']} ({final['published_from']['agent']}'s candidate)")
    print(f"Report:   {orchestrator.run_dir / 'REPORT.md'}")
    return 0


def cmd_run(args):
    case = config.load(args.case)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else (
        pathlib.Path("runs") / f"{case.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    orchestrator = Orchestrator(case, run_dir, push=args.push, thinking=args.thinking, resume=args.resume)
    print(f"case        {case.name}")
    print(f"host        {case.host_repo}")
    print(f"run         {orchestrator.run_dir}")
    print(f"push        {'yes' if args.push else 'no (use --push)'}")
    return _execute(orchestrator, args.dashboard, args.port)


def cmd_solve(args):
    repo = pathlib.Path(args.repo).resolve()
    if not repo.exists():
        print(f"repo not found: {repo}", file=sys.stderr)
        return 1
    commit = args.base or git("rev-parse", "HEAD", cwd=repo, check=False)
    if not commit:
        print("repo has no commits; use --base", file=sys.stderr)
        return 1
    specs = {}
    for name in (item.strip() for item in args.agents.split(",")):
        kind, model = DEFAULT_MODELS.get(name, ("opencode", None))
        specs[name] = config.AgentSpec(name=name, kind=kind, model=args.model.get(name, model),
                                       thinking="high" if kind == "kimi" else None)
    if len(specs) != 2:
        print("--agents needs exactly two names", file=sys.stderr)
        return 1
    case = config.Case(
        name=args.name or f"solve-{repo.name}", host_repo=repo, base=commit, agents=specs,
        checks=args.checks, bootstrap="base", max_rounds=args.max_rounds,
        round_seconds=args.round_seconds,
        task={"prompt": pathlib.Path(args.prompt).read_text(encoding="utf-8")},
    )
    orchestrator = Orchestrator(case, pathlib.Path("runs") / f"{case.name}-{time.strftime('%Y%m%d-%H%M%S')}",
                                push=args.push)
    print(f"solve       {case.name}")
    print(f"repo        {repo} @ {commit[:12]}")
    print(f"agents      {', '.join(specs)}")
    return _execute(orchestrator, args.dashboard, args.port)


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
    else:
        refs = [("base commit", case.base)] + [(f"{name}: bootstrap_ref", spec.bootstrap_ref)
                                               for name, spec in case.agents.items()]
        problems += [f"{label} not resolvable: {ref}" for label, ref in refs
                     if ref and not git_ok("rev-parse", "--verify", "--quiet", ref, cwd=case.host_repo)]
    if problems:
        print("\n".join(f"PROBLEM: {item}" for item in problems), file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_web(args):
    Dashboard(pathlib.Path(args.run), port=args.port, open_browser=args.open).serve_forever()
    return 0


def cmd_pipeline(args):
    prompt = pathlib.Path(args.prompt_file).read_text(encoding="utf-8")
    run_dir = args.run_dir or pathlib.Path("runs") / f"pipeline-{time.strftime('%Y%m%d-%H%M%S')}"
    return Pipeline(args.repo, prompt, run_dir, resume=args.resume, branches=args.branch, checks=args.check).run()


def cmd_serve(args):
    home = Home(port=args.port, open_browser=args.open)
    if sys.stdout is None:  # pythonw at login has no console: keep a log instead of losing output
        sys.stdout = sys.stderr = open(home.runs_dir / "serve.log", "a", buffering=1, encoding="utf-8")
    home.serve_forever()
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
    parser = argparse.ArgumentParser(prog="cav", description="Cross-agent validation driver")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="bootstrap candidates and drive rounds to agreement")
    run.add_argument("--case", required=True)
    run.add_argument("--run-dir", default=None)
    run.add_argument("--push", action="store_true", help="push the agreed commit to origin after convergence")
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
    solve.add_argument("--max-rounds", type=int, default=3)
    solve.add_argument("--round-seconds", type=int, default=5400)
    solve.add_argument("--push", action="store_true")
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

    pipeline = sub.add_parser("pipeline", help="plan, agree on the plan, implement, agree on code, merge and push")
    pipeline.add_argument("--repo", required=True, help="local repo path or GitHub URL")
    pipeline.add_argument("--prompt-file", required=True)
    pipeline.add_argument("--run-dir", default=None)
    pipeline.add_argument("--resume", action="store_true", help="continue the pipeline in --run-dir")
    pipeline.add_argument("--branch", action="append", default=[],
                          help="a branch to reconcile; give two, or none to plan from scratch")
    pipeline.add_argument("--check", action="append", default=[], help="a check command for the code stage")
    pipeline.set_defaults(func=cmd_pipeline)

    serve = sub.add_parser("serve", help="the home dashboard: every run, and a form to start a pipeline")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    smoke = sub.add_parser("smoke", help="check both agents answer a trivial prompt")
    smoke.add_argument("--case", required=True)
    smoke.add_argument("--dir", default=None)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    if hasattr(args, "model"):
        args.model = dict(item.split("=", 1) for item in args.model if "=" in item)
    return args.func(args)
