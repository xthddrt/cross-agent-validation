"""Repo + prompt to a pushed result: both agents plan, agree on a plan, implement it, agree on code.

    plan stage   each agent writes CAV-PLAN.md independently, then up to 3 review rounds
    code stage   each implements the agreed plan (or, without agreement, its own last plan),
                 then up to 3 review rounds
    agreement    the agreed commit is merged into origin's default branch and pushed
    otherwise    SUMMARY.md in the stage folder says how far apart the two candidates ended

Each stage is an ordinary run directory (plan/, code/) so the per-run dashboard works on it.
"""

import json
import os
import pathlib
import re
import time
import traceback

from . import config
from .gitops import git, git_ok
from .orchestrator import Orchestrator
from .prompts import PLAN_FILE

REVIEW_ROUNDS = 3  # after each stage's independent first round
CLONE_ROOT = pathlib.Path(os.environ.get("CAV_CLONE_ROOT") or pathlib.Path.home() / "Documents" / "Default Project")
GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)


def is_github(spec):
    return bool(GITHUB.match(spec.strip()))


def _remote_key(url):
    match = GITHUB.match((url or "").strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else (url or "").strip().lower()


def repo_name(spec):
    match = GITHUB.match(spec.strip())
    return match.group(2) if match else pathlib.Path(spec.strip().strip('"')).name


def resolve_repo(spec, clone_root=None):
    """A local git repo as given, or a GitHub repo cloned to (or reused from) the clone root."""
    text = spec.strip().strip('"')
    match = GITHUB.match(text)
    if not match:
        path = pathlib.Path(text).expanduser()
        if not path.is_dir() or not git_ok("rev-parse", "--git-dir", cwd=path):
            raise ValueError(f"not a git repository: {path}")
        return path.resolve()
    url = f"https://github.com/{match.group(1)}/{match.group(2)}.git"
    target = pathlib.Path(clone_root or CLONE_ROOT) / match.group(2)
    if target.exists():
        origin = git("remote", "get-url", "origin", cwd=target, check=False)
        if _remote_key(origin) != _remote_key(url):
            raise ValueError(f"{target} already exists with origin {origin or '(none)'}, not {url}")
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", url, str(target))
    return target.resolve()


def base_of(repo):
    """The commit both agents start from, the branch a result goes to, and whether it can be pushed."""
    if git("remote", "get-url", "origin", cwd=repo, check=False):
        git("fetch", "-q", "origin", cwd=repo)
        head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
        if not head:
            git("remote", "set-head", "origin", "--auto", cwd=repo, check=False)
            head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
        branch = head.split("/", 1)[1] if head else git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        return git("rev-parse", f"origin/{branch}", cwd=repo), branch, True
    return git("rev-parse", "HEAD", cwd=repo), git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo), False


class Pipeline:
    def __init__(self, repo_spec, prompt, run_dir, resume=False):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.prompt = prompt
        self.resume = resume
        self.path = self.run_dir / "pipeline.json"
        previous = Orchestrator.read_json(self.path)
        self.state = dict(previous, id=self.run_dir.name, repo_input=repo_spec, pid=os.getpid())
        self.state.setdefault("started", time.strftime("%Y-%m-%dT%H:%M:%S"))
        if not (self.run_dir / "prompt.md").exists():
            (self.run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    def save(self, **changes):
        self.state.update(changes, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def run(self):
        try:
            return self._run()
        except Exception as error:  # the dashboard shows why; the traceback goes to pipeline.log
            traceback.print_exc()
            self.save(stage="failed", outcome="failed", detail=str(error))
            return 1

    def _run(self):
        self.save(stage="starting", outcome=None, detail=None)
        repo = resolve_repo(self.state["repo_input"])
        if not (self.resume and self.state.get("base")):
            base, branch, pushable = base_of(repo)
            self.save(repo=str(repo), base=base, branch=branch, pushable=pushable)
        name = re.sub(r"[^\w.-]+", "-", repo.name)

        self.save(stage="plan")
        plan = Orchestrator(self.stage_case(f"{name}-plan", repo, "plan", {"prompt": self.prompt}),
                            self.run_dir / "plan", resume=self.resume, run_id=f"{self.state['id']}-plan")
        plan_state = plan.run()
        plans = self.write_plans(plan, plan_state)
        self.save(plan={"outcome": "agreed" if plans["agreed"] else "disagreement",
                        "rounds": len(plan_state.get("history") or []), "summary": plan_state.get("summary")})

        self.save(stage="code")
        code = Orchestrator(self.stage_case(f"{name}-code", repo, "code", {"prompt": self.prompt, "plans": plans}),
                            self.run_dir / "code", push=self.state["pushable"], merge=True,
                            resume=self.resume, run_id=f"{self.state['id']}-code")
        push_error = None
        try:
            code_state = code.run()
        except RuntimeError as error:
            code_state = code.state
            if not code_state.get("final"):
                raise
            push_error = str(error)  # agreed, but the push or merge failed
        final = code_state.get("final")
        self.save(code={"outcome": "agreed" if final else "disagreement",
                        "rounds": len(code_state.get("history") or []), "summary": code_state.get("summary")})
        branch = self.state["branch"]
        if not final:
            outcome, detail = "no agreement", "see code/SUMMARY.md for how far apart the candidates ended"
        elif push_error:
            outcome, detail = "agreed, not pushed", push_error
        elif not self.state["pushable"]:
            outcome, detail = "agreed, not pushed", f"the repo has no origin remote; agreed commit {final['commit']}"
        else:
            pushed = final.get("merge_commit") or final["commit"]
            outcome = "merged and pushed"
            detail = f"{pushed} on origin/{branch}" + (" (merge commit)" if final.get("merge_commit") else "")
        self.save(stage="done", outcome=outcome, detail=detail, final=final)
        print(f"pipeline {self.state['id']}: {outcome}: {detail}", flush=True)
        return 0 if final else 2

    def stage_case(self, name, repo, mode, task):
        agents = {agent: config.AgentSpec(name=agent, kind=kind, model=model,
                                          thinking="high" if kind == "kimi" else None)
                  for agent, (kind, model) in config.DEFAULT_MODELS.items()}
        return config.Case(name=name, host_repo=repo, base=self.state["base"], branch=self.state["branch"],
                           agents=agents, checks=[], bootstrap="base", mode=mode, task=task,
                           max_rounds=1 + REVIEW_ROUNDS, round_seconds=5400, stall_seconds=1200, retries=1)

    def write_plans(self, plan, state):
        """The agreed plan for both agents, or each agent's own last plan."""
        folder = self.run_dir / "plans"
        folder.mkdir(exist_ok=True)

        def text(commit):
            body = git("--git-dir", str(plan.exchange.path), "show", f"{commit}:{PLAN_FILE}", check=False)
            if not body.strip():
                raise RuntimeError(f"plan candidate {commit[:12]} has no {PLAN_FILE}")
            return body + "\n"

        if state.get("final"):
            path = folder / "agreed.md"
            path.write_text(text(state["final"]["commit"]), encoding="utf-8")
            return {"agreed": True, "files": {agent: str(path) for agent in config.DEFAULT_MODELS}}
        last = next((item for item in reversed(state.get("history") or []) if item.get("results")), None)
        if not last:
            raise RuntimeError("the plan stage produced no plans")
        files = {}
        for agent, result in last["results"].items():
            path = folder / f"{agent}.md"
            path.write_text(text(result["commit"]), encoding="utf-8")
            files[agent] = str(path)
        return {"agreed": False, "files": files}
