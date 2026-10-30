"""One CAV run: two agents plan, agree on one plan, one builds it, the other reviews it.

    plan     both agents, in parallel and independently: a plan of at most 400 words
    agree    alternating turns in the same sessions: accept the other plan as written, or answer
             with a revised plan and why (at most 100 words); one accept ends it, and after 4
             turns a coin flip picks between the two latest plans
    build    the builder (deepseek by default) implements the agreed plan; the checks run
    review   the reviewer approves or lists up to 5 defects, the builder fixes them, at most twice
    publish  the result is committed and pushed to origin's default branch (merged if it moved),
             unless --no-push; failing checks publish nothing

Given two branches, both agents read them as plain folders labelled Candidate X and Y (random
labels, no .git, so no branch name, author or message) and the plan's first line names the one
to build on. Each agent keeps one session for the whole run, so nothing is read twice.
"""

import io
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import tarfile
import threading
import time

from . import agents as agent_mod
from . import config, prompts
from .events import EventWriter
from .gitops import GIT, IDENTITY, NO_WINDOW, git, git_ok

PLAN_WORDS, WHY_WORDS, MAX_DEFECTS = prompts.PLAN_WORDS, prompts.WHY_WORDS, prompts.MAX_DEFECTS
LABELS = ("X", "Y")
BASE_LINE = re.compile(r"(?im)^\W*base\W*:\W*candidate\s+([XY])\b")
CLONE_ROOT = pathlib.Path(os.environ.get("CAV_CLONE_ROOT") or pathlib.Path.home() / "Documents" / "Default Project")
GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
STEP_SECONDS = {"plan": 1800, "agree": 1200, "build": 5400, "review": 1800, "fix": 3600}


# ------------------------------------------------------------------------ repo helpers

def is_github(spec):
    return bool(GITHUB.match(spec.strip()))


def repo_name(spec):
    match = GITHUB.match(spec.strip())
    return match.group(2) if match else pathlib.Path(spec.strip().strip('"')).name


def _remote_key(url):
    match = GITHUB.match((url or "").strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else (url or "").strip().lower()


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
    """The commit work starts from, the branch a result goes to, and whether it can be pushed."""
    if git("remote", "get-url", "origin", cwd=repo, check=False):
        git("fetch", "-q", "origin", cwd=repo)
        head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
        if not head:
            git("remote", "set-head", "origin", "--auto", cwd=repo, check=False)
            head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
        branch = head.split("/", 1)[1] if head else git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        return git("rev-parse", f"origin/{branch}", cwd=repo), branch, True
    return git("rev-parse", "HEAD", cwd=repo), git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo), False


def resolve_ref(repo, ref):
    """A branch as given, or as fetched from origin."""
    for candidate in (ref, f"origin/{ref}"):
        sha = git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", cwd=repo, check=False)
        if sha:
            return sha
    raise ValueError(f"branch {ref!r} not found in {repo}")


def export_tree(repo, sha, dest):
    """A commit's files as a plain folder: no .git, so no branch name, author or message."""
    data = subprocess.run([GIT, "archive", "--format=tar", sha], cwd=str(repo), capture_output=True,
                          check=True, creationflags=NO_WINDOW).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(dest, filter="data")
    return dest


def plan_base(text):
    """The candidate a plan builds on, from its "Base: Candidate X" line."""
    match = BASE_LINE.search(text or "")
    return match.group(1).upper() if match else None


def words(text):
    return len((text or "").split())


def push_result(repo, branch, commit, run_id):
    """Push to origin's branch; merge first if it moved during the run; refuse a conflicting merge."""
    if not git_ok("fetch", "origin", cwd=repo):
        raise RuntimeError("cannot fetch origin")
    target = commit
    if not git_ok("merge-base", "--is-ancestor", f"origin/{branch}", commit, cwd=repo):
        proc = subprocess.run([GIT, "merge-tree", "--write-tree", "--name-only", f"origin/{branch}", commit],
                              cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=NO_WINDOW)
        lines = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not lines:
            conflicts = ", ".join(line for line in lines[1:] if line.strip())[:500]
            raise RuntimeError(f"merging into origin/{branch} conflicts ({conflicts or proc.stderr.strip()})")
        upstream = git("rev-parse", f"origin/{branch}", cwd=repo)
        target = git(*IDENTITY, "commit-tree", lines[0], "-p", upstream, "-p", commit,
                     "-m", f"Merge CAV run {run_id}", cwd=repo)
    git("push", "origin", f"{target}:refs/heads/{branch}", cwd=repo)
    return target


def _json(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------------------- flow

class Flow:
    def __init__(self, repo_spec, task, run_dir, branches=(), checks=(), push=True, builder="deepseek",
                 models=None, max_turns=4, fix_rounds=2):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.repo_spec, self.task = repo_spec, task.strip()
        self.branches, self.checks = list(branches), list(checks)
        if self.branches and len(self.branches) != 2:
            raise ValueError("give two branches to reconcile, or none")
        self.push, self.max_turns, self.fix_rounds = push, max_turns, fix_rounds
        models = models or config.DEFAULT_MODELS
        if builder not in models:
            raise ValueError(f"builder must be one of {sorted(models)}")
        self.builder, self.reviewer = builder, next(name for name in models if name != builder)
        self.run_id = self.run_dir.name
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_id)
        self.state = {"case": f"{repo_name(repo_spec)}", "run_id": self.run_id, "flow": True, "phase": "starting",
                      "round": 0, "agents": {}, "history": [], "builder": self.builder, "reviewer": self.reviewer}
        self.step = 0
        self.agents = {}
        for name, (kind, model) in models.items():
            spec = config.AgentSpec(name=name, kind=kind, model=model, thinking="high" if kind == "kimi" else None)
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"{name}.log", title_prefix=f"cav {self.run_id}")
            agent.emit = lambda type_, agent_name, data: self.events.emit(type_, self.step, agent_name, data)
            self.agents[name] = agent

    # ------------------------------------------------------------------ plumbing

    def log(self, message, level="INFO"):
        line = f"{time.strftime('%H:%M:%S')} {level:<5} {message}"
        print(line, flush=True)
        with open(self.run_dir / "run.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save(self, **changes):
        self.state.update(changes, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        (self.run_dir / "state.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def begin(self, kind, **data):
        """A new step: its number is the dashboard's round."""
        self.step += 1
        entry = {"round": self.step, "step": kind, "started": time.time(), **data}
        self.state["history"].append(entry)
        self.save(phase=kind, round=self.step)
        self.events.emit("round.start", self.step, data={"phase": kind})
        return entry

    def end(self, entry, verdict, **data):
        entry.update(data, verdict=verdict, seconds=round(time.time() - entry["started"]))
        self.save()
        self.events.emit("convergence.evaluated", entry["round"], data={"verdict": verdict})
        self.log(f"step {entry['round']} {entry['step']}: {verdict}")

    def ask(self, jobs, kind, check):
        """Run agents in parallel (one prompt each); one retry for an agent whose answer fails `check`."""
        problems = {name: None for name in jobs}
        for attempt in range(2):
            pending = {name: prompt for name, prompt in jobs.items() if attempt == 0 or problems[name]}
            if not pending:
                break
            threads = []
            for name, prompt in pending.items():
                agent = self.agents[name]
                agent.reset()
                note = prompts.retry_note(problems[name]) if attempt else ""
                record = self.run_dir / "rounds" / f"r{self.step}" / f"{name}-context.md"  # for the dashboard
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(prompt + note, encoding="utf-8")
                thread = threading.Thread(target=self._work, args=(agent, prompt + note), daemon=True)
                thread.start()
                threads.append((name, thread))
            deadline = time.time() + STEP_SECONDS[kind]
            while any(thread.is_alive() for _, thread in threads):
                self.state["agents"] = {name: {"state": "running" if thread.is_alive() else "done",
                                               "detail": self.agents[name].state.detail,
                                               "idle": int(self.agents[name].state.idle)}
                                        for name, thread in threads}
                self.save()
                if time.time() > deadline:
                    for name, thread in threads:
                        if thread.is_alive():
                            self.agents[name].abort()
                            self.agents[name].state.error = "step timeout"
                time.sleep(POLL_SECONDS)
            for name, _ in threads:
                agent = self.agents[name]
                problems[name] = agent.state.error or check(name)
                if problems[name]:
                    self.log(f"{name}: {problems[name]}" + ("; retrying" if attempt == 0 else ""), "WARN")
        failed = {name: problem for name, problem in problems.items() if problem}
        if failed:
            raise RuntimeError("; ".join(f"{name}: {problem}" for name, problem in failed.items()))

    def _work(self, agent, prompt):
        try:
            agent.run(prompt, self.run_dir)
        except Exception as error:  # an adapter error is a failed answer, not a crash
            agent.state.error = str(error)
            agent.state.finished = True

    def run_checks(self, worktree):
        results = []
        for command in self.checks:
            rendered = command.replace("{python}", f'"{sys.executable}"')
            started = time.time()
            proc = subprocess.run(rendered, shell=True, cwd=str(worktree), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            results.append({"command": command, "exit": proc.returncode, "seconds": int(time.time() - started),
                            "tail": ((proc.stdout or "") + (proc.stderr or ""))[-1500:]})
            self.log(f"check {command} -> exit {proc.returncode}", "ERROR" if proc.returncode else "INFO")
            if proc.returncode:
                break
        ok = len(results) == len(self.checks) and all(item["exit"] == 0 for item in results)
        return ok, results

    # --------------------------------------------------------------------- steps

    def run(self):
        try:
            return self._run()
        except Exception as error:
            self.log(f"failed: {error}", "ERROR")
            self.save(phase="failed", outcome="failed", detail=str(error))
            return 1

    def _run(self):
        repo = resolve_repo(self.repo_spec)
        base, branch, pushable = base_of(repo)
        sources = self.sources(repo) if self.branches else None
        self.save(repo=str(repo), base=base, branch=branch, pushable=pushable and self.push)
        names = list(self.agents)
        folder = self.run_dir / "plans"
        folder.mkdir(exist_ok=True)

        # plan: both, independently; files are numbered, not named, so no prompt shows whose plan it is
        entry = self.begin("plan")
        numbers = iter(range(1, 100))
        paths = {name: folder / f"plan-{next(numbers)}.md" for name in random.sample(names, 2)}
        self.save(plan_authors={path.name: name for name, path in paths.items()})
        self.ask({name: prompts.plan(self.task, sources, paths[name]) for name in names}, "plan",
                 lambda name: self.plan_problem(paths[name].read_text(encoding="utf-8") if paths[name].exists()
                                                else None, sources))
        self.end(entry, "planned", words={name: words(paths[name].read_text(encoding="utf-8")) for name in names})

        # agree: alternate until one accepts the other's plan as written
        latest, objection = dict(paths), {name: None for name in names}
        order = random.sample(names, 2)
        agreed, how = None, None
        for turn in range(1, self.max_turns + 1):
            speaker = order[(turn - 1) % 2]
            other = order[turn % 2]
            reply = self.run_dir / "agree" / f"turn-{turn}.json"
            reply.parent.mkdir(exist_ok=True)
            entry = self.begin("agree", speaker=speaker)
            self.ask({speaker: prompts.agree(latest[other], objection[speaker], reply, sources)}, "agree",
                     lambda name: self.reply_problem(reply, sources))
            data = _json(reply)
            if data["accept"]:
                agreed, how = latest[other], f"{speaker} accepted {other}'s plan in turn {turn}"
                self.end(entry, "accepted", plan=str(agreed))
                break
            latest[speaker] = folder / f"plan-{next(numbers)}.md"
            latest[speaker].write_text(data["plan"].strip() + "\n", encoding="utf-8")
            self.state["plan_authors"][latest[speaker].name] = speaker
            objection[other] = data["why"].strip()
            self.end(entry, "revised", why=objection[other], plan=str(latest[speaker]))
        if agreed is None:
            winner = random.choice(names)
            agreed, how = latest[winner], f"no accept in {self.max_turns} turns; coin flip chose {winner}'s plan"
        plan_path = folder / "agreed.md"
        plan_path.write_text(pathlib.Path(agreed).read_text(encoding="utf-8"), encoding="utf-8")
        self.save(plan={"path": str(plan_path), "how": how, "words": words(plan_path.read_text(encoding="utf-8"))})
        self.log(f"plan: {how}")

        # build from the plan's base
        start, base_label = base, None
        if sources:
            base_label = plan_base(plan_path.read_text(encoding="utf-8"))
            if base_label not in sources:
                raise RuntimeError('the agreed plan names no base; it must say "Base: Candidate X" or "Base: Candidate Y"')
            start = self.state["labels"][base_label]["commit"]
        work, note = self.run_dir / "work", self.run_dir / "note.md"
        git("worktree", "add", "--detach", str(work), start, cwd=repo)
        entry = self.begin("build", agent=self.builder)
        self.ask({self.builder: prompts.build(plan_path, work, self.checks, note, base_label)}, "build",
                 lambda name: None if note.exists() else "no note written")
        commit, (ok, results) = self.commit(repo, work, "build"), self.run_checks(work)
        self.end(entry, "built", commit=commit, checks_ok=ok, checks=results)

        # review: approve, or defects for the builder to fix
        defects = []
        for cycle in range(1, self.fix_rounds + 2):
            diff = self.run_dir / "review" / f"diff-{cycle}.patch"
            diff.parent.mkdir(exist_ok=True)
            diff.write_text(git("diff", start, commit, cwd=repo, check=False), encoding="utf-8")
            reply = self.run_dir / "review" / f"review-{cycle}.json"
            entry = self.begin("review", agent=self.reviewer)
            self.ask({self.reviewer: prompts.review(plan_path, diff, work, self.checks_summary(ok, results), reply)},
                     "review", lambda name: self.review_problem(reply))
            data = _json(reply)
            defects = [] if data["approve"] else data["defects"]
            if not ok:
                defects = defects + [{"problem": f"check fails: {item['command']} (exit {item['exit']})",
                                      "fix": "make it pass"} for item in results if item["exit"]][:1]
            self.end(entry, "approved" if not defects else "defects", defects=defects)
            if not defects or cycle > self.fix_rounds:
                break
            entry = self.begin("fix", agent=self.builder)
            self.ask({self.builder: prompts.fix(defects, work, self.checks, note)}, "fix", lambda name: None)
            commit, (ok, results) = self.commit(repo, work, f"fix {cycle}"), self.run_checks(work)
            self.end(entry, "fixed", commit=commit, checks_ok=ok, checks=results)
        return self.publish(repo, branch, commit, ok, defects)

    def publish(self, repo, branch, commit, ok, defects):
        git("update-ref", f"refs/cav/{self.run_id}/result", commit, cwd=repo)  # keep the result reachable
        if not ok:
            self.save(phase="failed", outcome="checks fail", detail=f"result {commit[:12]} kept, not published")
            self.write_report(None, defects)
            return 2
        final = {"commit": commit, "open_defects": defects}
        if self.state["pushable"]:
            final["pushed"] = push_result(repo, branch, commit, self.run_id)
            outcome = f"merged and pushed to origin/{branch}"
        else:
            outcome = "done, kept local"
        self.save(phase="done", outcome=outcome, final=final,
                  detail=f"{commit[:12]}" + (f"; {len(defects)} open defect(s)" if defects else ""))
        self.events.emit("final.complete", self.step, data={"commit": commit})
        self.write_report(final, defects)
        self.log(f"{outcome}: {commit}")
        return 0

    # ------------------------------------------------------------------- helpers

    def sources(self, repo):
        """The two branches as Candidate X and Y: random labels, plain folders."""
        shas = [resolve_ref(repo, ref) for ref in self.branches]
        order = random.sample(range(2), 2)
        labels = {label: {"ref": self.branches[i], "commit": shas[i]} for label, i in zip(LABELS, order)}
        self.save(labels=labels)
        return {label: str(export_tree(repo, item["commit"], self.run_dir / "sources" / f"candidate-{label}"))
                for label, item in labels.items()}

    def commit(self, repo, work, what):
        git("add", "-A", cwd=work)
        if git("status", "--porcelain", cwd=work):
            git(*IDENTITY, "commit", "-q", "-m", f"CAV {self.run_id}: {what}", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    @staticmethod
    def checks_summary(ok, results):
        if not results:
            return "no checks configured"
        return ("all pass" if ok else "failing") + ": " + "; ".join(f"{item['command']} exit {item['exit']}"
                                                                     for item in results)

    @staticmethod
    def plan_problem(text, sources):
        if text is None:
            return "no plan file written"
        if words(text) > PLAN_WORDS:
            return f"the plan has {words(text)} words; cut it to {PLAN_WORDS}"
        if sources and plan_base(text) not in sources:
            return 'the plan must start with "Base: Candidate X" or "Base: Candidate Y"'
        return None

    def reply_problem(self, path, sources):
        data = _json(path)
        if data is None or not isinstance(data.get("accept"), bool):
            return 'write JSON with "accept": true or false'
        if data["accept"]:
            return None
        if not isinstance(data.get("plan"), str) or not isinstance(data.get("why"), str):
            return 'a revision needs "plan" and "why"'
        if words(data["why"]) > WHY_WORDS:
            return f'"why" has {words(data["why"])} words; cut it to {WHY_WORDS}'
        return self.plan_problem(data["plan"], sources)

    @staticmethod
    def review_problem(path):
        data = _json(path)
        if data is None or not isinstance(data.get("approve"), bool):
            return 'write JSON with "approve": true or false'
        defects = data.get("defects") or []
        if not data["approve"] and (not isinstance(defects, list) or not defects
                                    or any(not isinstance(item, dict) or not item.get("problem") for item in defects)):
            return 'a rejection needs "defects": a list of objects with a "problem"'
        if len(defects) > MAX_DEFECTS:
            return f"list at most {MAX_DEFECTS} defects"
        data["defects"] = defects
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return None

    def write_report(self, final, defects):
        history, lines = self.state["history"], [f"# CAV run {self.run_id}", ""]
        lines += [f"- Outcome: {self.state.get('outcome')}", f"- Plan: {(self.state.get('plan') or {}).get('how')}",
                  f"- Builder {self.builder}, reviewer {self.reviewer}", ""]
        if self.state.get("labels"):
            lines += ["- " + ", ".join(f"Candidate {label} = {item['ref']}" for label, item in self.state["labels"].items()), ""]
        lines += ["| Step | | Time | Result |", "|---|---|---|---|"]
        lines += [f"| {item['round']} | {item['step']} {item.get('speaker') or item.get('agent') or ''} "
                  f"| {item.get('seconds', 0) // 60}m {item.get('seconds', 0) % 60:02d}s | {item.get('verdict')} |"
                  for item in history]
        if defects:
            lines += ["", "## Open defects", ""] + [f"- {item.get('file', '')}:{item.get('line', '')} {item['problem']}"
                                                   for item in defects]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


POLL_SECONDS = 2
