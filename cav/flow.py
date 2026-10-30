"""One CAV run: both agents agree on one plan, both build it, both judge the two builds.

    audit     the run's first judge: both agents, blind, list every defect in each candidate, tagged
              blocker or minor, then look again for more in the same session
    triage    each rates the other's blockers blocker, minor or not real; one rated lower goes back
              to its author once with the reason. Both say blocker: it joins the ledger. Still split:
              disputed, listed, not blocking. A candidate with no open ledger blocker, and passing
              checks, is the result.
    plan      both plan the next build: every open blocker, minors only where small, no new command,
              tool, mode or state machine (a blocker needing one is deferred, and listed). (A task
              alone starts here, with a plan for the task.)
    agree     both approve or reject each plan, shown as the builders will get it (the blockers and
              minors in its base attached). A plan both approve is the plan. Otherwise both revise
              from the rejection reasons and vote again, up to 3 rounds, then a coin flip.
    build     both implement the agreed plan from the same base, separately; the checks run
    verify    later judges: each ledger blocker, fixed or not in each build (fixed only if both say
              so), then what each build's diff broke, and the rest only for blockers; new blockers go
              through triage. Up to 3 builds; after the last: the one passing its checks, else fewer
              open blockers, else the one both would keep, else a coin flip.
    publish   pushed to origin's default branch unless --no-push; failing checks publish nothing.
              What the result may still have (minors, disputed or deferred blockers) is listed.

No prompt names an agent or says whose anything is: candidates are X and Y (labelled at random,
read from plain folders without .git), plans and reviews are numbered at random, candidates are
listed in random order. Each agent keeps one session through a cycle (audit or verify, triage and
plan, or plan; plan rounds; build) and starts a fresh one at the next cycle, so no agent judges builds
knowing which is its own.
Everything an agent reads or writes lives in a blind area outside every git repo and away from
the run's bookkeeping; builds are plain copies that CAV commits itself, through a separate index.
"""

import concurrent.futures
import io
import json
import os
import pathlib
import random
import re
import secrets
import subprocess
import sys
import tarfile
import threading
import time

from . import agents as agent_mod
from . import config, prompts
from .events import EventWriter
from .gitops import GIT, IDENTITY, NO_WINDOW, git, git_ok

PLAN_WORDS, WHY_WORDS, REAL_WORDS = prompts.PLAN_WORDS, prompts.WHY_WORDS, prompts.REAL_WORDS
LABELS = ("X", "Y")
MAX_PLAN_ROUNDS, MAX_BUILDS = 3, 3
SEVERITIES = ("blocker", "minor")
RATINGS = ("blocker", "minor", "not real")
OUTCOMES = ("ledger", "minor", "disputed", "dropped")
ATTACHED = "## Blockers to fix"
DEFERRED = re.compile(r"(?im)^\W*deferred\W*:(.*)$")
BASE_LINE = re.compile(r"(?im)^\W*base\W*:\W*(?:candidate\s+)?([XY])\b")
CLONE_ROOT = pathlib.Path(os.environ.get("CAV_CLONE_ROOT") or pathlib.Path.home() / "Documents" / "Default Project")
GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
STEP_SECONDS = {"judge": 2700, "triage": 1800, "plan": 1800, "revise": 1800, "agree": 1800, "build": 5400}
# The agents' side of a run: outside every git repo, so no git command there finds any history.
BLIND_ROOT = pathlib.Path(os.environ.get("CAV_BLIND_ROOT")
                          or pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / "cav-blind")


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


def hits(defect, label):
    return defect.get("candidate") in (label, "both")


def defect_line(item, label=True):
    """One defect as the agents read it: its ledger number if it has one, no author, no ratings."""
    return ((f"#{item['id']} " if item.get("id") else "") + (f"[{item['candidate']}] " if label else "")
            + f"{item.get('file', '')}:{item.get('line', '')} " + item["problem"]
            + (f" (fix: {item['fix']})" if item.get("fix") else ""))


def status(item):
    """What a defect the result may still have is: the report's and the dashboard's words for it."""
    if item.get("status"):
        return item["status"]
    if item.get("outcome") == "disputed":
        return "disputed blocker: " + (item.get("defense") or "")
    if item.get("outcome") == "minor":
        return "blocker rated minor (fixed only if small)"
    return "minor (fixed only if small)"


def follow_up(item):
    return f"{item.get('file', '')}:{item.get('line', '')} {item['problem']} ({status(item)})"


def _numbered(names):
    """Each agent's file number for a step, at random, so no file name shows whose it is."""
    return dict(zip(random.sample(list(names), len(names)), range(1, len(names) + 1)))


# ------------------------------------------------------------------------------- flow

class Flow:
    def __init__(self, repo_spec, task, run_dir, branches=(), checks=(), push=True, models=None,
                 max_plan_rounds=MAX_PLAN_ROUNDS, max_builds=MAX_BUILDS):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.repo_spec, self.task = repo_spec, task.strip()
        self.branches, self.checks = list(branches), list(checks)
        if self.branches and len(self.branches) != 2:
            raise ValueError("give two branches to reconcile, or none")
        self.push, self.max_plan_rounds, self.max_builds = push, max_plan_rounds, max_builds
        models = models or config.DEFAULT_MODELS
        self.names = list(models)
        self.run_id = self.run_dir.name
        self.blind = BLIND_ROOT / secrets.token_hex(4)  # a random name: nothing in the path says what it is
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_id)
        self.state = {"case": f"{repo_name(repo_spec)}", "run_id": self.run_id, "flow": True, "phase": "starting",
                      "round": 0, "agents": {}, "history": [], "names": self.names, "authors": {},
                      "blind": str(self.blind)}
        self.step = 0
        self.judged = False  # the run's first judge audits everything
        self.ledger, self.next_id = [], 1  # the open blockers both agents agreed, in the base being built on
        self.follow_ups = []  # what the base built on may still have: minors, disputed and deferred blockers
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
        """Both agents at once, each with a prompt and a folder to work in, each in a fresh session;
        one retry for an agent whose answer fails `check`."""
        problems = {name: None for name in jobs}
        for attempt in range(2):
            pending = {name: job for name, job in jobs.items() if attempt == 0 or problems[name]}
            if not pending:
                break
            threads = []
            for name, (prompt, cwd) in pending.items():
                agent = self.agents[name]
                agent.reset()  # the session itself carries on until the next cycle
                note = prompts.retry_note(problems[name]) if attempt else ""
                record = self.run_dir / "rounds" / f"r{self.step}" / f"{name}-context.md"  # for the dashboard
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(prompt + note, encoding="utf-8")
                thread = threading.Thread(target=self._work, args=(agent, prompt + note, cwd), daemon=True)
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

    def _work(self, agent, prompt, cwd):
        try:
            agent.run(prompt, cwd)
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
        self.repo = repo = resolve_repo(self.repo_spec)
        base, branch, pushable = base_of(repo)
        self.save(repo=str(repo), base=base, branch=branch, pushable=pushable and self.push)
        self.blind.mkdir(parents=True, exist_ok=True)
        if git_ok("rev-parse", "--git-dir", cwd=self.blind):
            raise RuntimeError(f"the agents' folder {self.blind} is inside a git repo; set CAV_BLIND_ROOT elsewhere")
        candidates = self.branch_candidates(repo) if self.branches else None
        plan_path = None  # the plan the candidates implemented; none for two branches
        for cycle in range(1, self.max_builds + 2):
            for agent in self.agents.values():
                agent.session_id = None  # a fresh session per cycle: no agent judges builds knowing which is its own
            if candidates:
                result, plans, found = self.judge(cycle, candidates, plan_path, final=cycle > self.max_builds)
                if result:
                    return self.publish(branch, candidates[result], result)
            else:
                plans, found = self.plan(cycle, base), None
            plan_path, label = self.agree(cycle, plans, candidates, found)
            candidates = self.build(cycle, plan_path, candidates[label]["commit"] if candidates else base, label)
        raise RuntimeError("the final judge must decide")  # unreachable: the final judge always picks

    def judge(self, cycle, candidates, built_from, final):
        """Both agents judge both candidates blind, builds against the plan they implemented (`built_from`),
        branches against the task. The run's first judge audits everything, twice; a later one checks each
        ledger blocker, fixed or not, then what the changes broke. New blockers go through triage; those
        both agree on join the ledger. A candidate with no open ledger blocker, and passing checks, is the
        result. Otherwise both plan the next build from its open blockers; after the last build, the
        final judge keeps the passing one, else the one with fewer open blockers, else the one both
        would keep, else a coin flip."""
        folder = self.blind / "judge" / f"c{cycle}"
        folder.mkdir(parents=True, exist_ok=True)
        numbers = _numbered(self.names)
        replies = {name: folder / f"review-{numbers[name]}.json" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in replies.items()})
        entry = self.begin("judge", cycle=cycle,
                           candidates={label: item["commit"] for label, item in candidates.items()})
        first = not self.judged
        ledger_file, ids = None, []
        if not first and self.ledger:
            ledger_file = folder / "ledger.md"
            ledger_file.write_text("".join(f"{defect_line(item, label=False)}\n" for item in self.ledger),
                                   encoding="utf-8")
            ids = [str(item["id"]) for item in self.ledger]
        target = "the plan" if built_from is not None else "the task"
        jobs = {}
        for name in self.names:
            listed = [(label, candidates[label]["folder"], self.checks_summary(candidates[label]),
                       None if first else candidates[label].get("changes"))
                      for label in random.sample(LABELS, 2)]  # no fixed first place
            jobs[name] = (prompts.audit(self.task, listed, replies[name], final, built_from) if first else
                          prompts.verify(self.task, listed, replies[name], ledger_file, final, built_from), self.blind)
        self.ask(jobs, "judge", lambda name: self.judge_problem(replies[name], final, candidates, ids))
        reviews = {name: _json(replies[name]) for name in self.names}
        if first:  # a second look in the same session catches what the first pass skipped
            more = {name: folder / f"more-{numbers[name]}.json" for name in self.names}
            self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in more.items()})
            self.ask({name: (prompts.more(more[name]), self.blind) for name in self.names}, "judge",
                     lambda name: self.judge_problem(more[name], False, candidates, []))
            for name in self.names:
                reviews[name]["defects"] += _json(more[name])["defects"]
        self.judged = True
        for name in self.names:
            for item in reviews[name]["defects"]:
                item.update(by=name, cycle=cycle)  # bookkeeping only: the agents never see it
        record = {"defects": [item for name in self.names for item in reviews[name]["defects"]]}
        if ids:
            record["fixed"] = {name: reviews[name]["fixed"] for name in self.names}
        self.end(entry, "reviewed", **record)

        entry = self.begin("triage", cycle=cycle)
        blockers = {name: [item for item in reviews[name]["defects"] if item["severity"] == "blocker"]
                    for name in self.names}
        self.triage(folder, blockers)
        added = [item for name in self.names for item in blockers[name] if item["outcome"] == "ledger"]
        for item in added:
            item["id"], self.next_id = self.next_id, self.next_id + 1
        minors = [item for name in self.names for item in reviews[name]["defects"]
                  if item["severity"] == "minor" or item["outcome"] in ("minor", "disputed")]
        still = {label: [item for item in self.ledger if ids and not all(
                     reviews[name]["fixed"][label][str(item["id"])] for name in self.names)] for label in LABELS}
        open_ = {label: still[label] + [item for item in added if hits(item, label)] for label in LABELS}
        clear = [label for label in LABELS if not open_[label] and candidates[label]["checks_ok"]]
        outcomes = [item["outcome"] for name in self.names for item in blockers[name]]
        record = {"open": {label: [item["id"] for item in open_[label]] for label in LABELS},
                  "ledger": [{key: item.get(key) for key in ("id", "file", "line", "problem")}
                             for item in self.ledger + added],
                  "outcomes": {outcome: outcomes.count(outcome) for outcome in OUTCOMES if outcome in outcomes}}
        if final:
            record["prefer"] = {name: reviews[name]["prefer"] for name in self.names}
        if clear:
            result = clear[0] if len(clear) == 1 else self.smaller(candidates)
            how = (f"no open blocker in {result}" if len(clear) == 1
                   else f"no open blocker in either; {result} changes less")
        elif final:
            result, how = self.keep(candidates, record["prefer"], {label: len(open_[label]) for label in LABELS})
        else:
            self.end(entry, "rejected", **record)
            found = {label: {"blockers": open_[label], "minors": [item for item in minors if hits(item, label)]}
                     for label in LABELS}
            return None, self.fix_plans(cycle, folder, found), found
        self.end(entry, "accepted" if clear else "forced", how=how, **record)
        for item in open_[result]:
            item["status"] = "open blocker"
        left = self.follow_ups + [item for item in minors if hits(item, result)] + open_[result]
        self.save(decided=how, open_defects=[dict(item, status=status(item)) for item in left])
        return result, None, None

    def triage(self, folder, blockers):
        """Each agent, in the session where it just judged, rates each of the other's new blockers blocker,
        minor or not real; a blocker rated lower goes back to its author once, with the reason. Sets
        `outcome` on each: ledger (both say blocker), minor, dropped (both say not real) or disputed
        (still split: it does not block, and is listed)."""
        numbers = _numbered(self.names)
        other = dict(zip(self.names, reversed(self.names)))
        for name in self.names:
            for item in blockers[name]:
                item["outcome"] = "ledger"
        lists = {name: blockers[other[name]] for name in self.names if blockers[other[name]]}
        replies = self.rate(folder, "triage", numbers, lists, defect_line)
        for name, ratings in replies.items():
            for item, (rating, why) in zip(lists[name], ratings):
                item["rating"], item["challenge"] = rating, why
        challenged = {name: [item for item in blockers[name] if item.get("rating", "blocker") != "blocker"]
                      for name in self.names}
        challenged = {name: items for name, items in challenged.items() if items}
        answers = self.rate(folder, "challenged", numbers, challenged,
                            lambda item: f"{defect_line(item)}\n   Rated {item['rating']}: {item['challenge']}")
        for name, ratings in answers.items():
            for item, (rating, why) in zip(challenged[name], ratings):
                if rating == "blocker":
                    item["outcome"], item["defense"] = "disputed", why
                else:
                    item["outcome"] = "minor" if "minor" in (rating, item["rating"]) else "dropped"

    def rate(self, folder, kind, numbers, lists, line):
        """One rating round: each agent in `lists` rates its numbered items. Returns name -> [(rating, why)]."""
        files, replies = {}, {}
        for name, items in lists.items():
            files[name] = folder / f"{kind}-{numbers[name]}.md"
            files[name].write_text("".join(f"{n}. {line(item)}\n" for n, item in enumerate(items, 1)), encoding="utf-8")
            replies[name] = folder / f"{kind}-{numbers[name]}.json"
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in replies.items()})
        if not replies:
            return {}
        ask_why = "lower" if kind == "triage" else "blocker"
        make = prompts.triage if kind == "triage" else prompts.challenged
        self.ask({name: (make(files[name], replies[name]), self.blind) for name in replies}, "triage",
                 lambda name: self.rating_problem(replies[name], len(lists[name]), ask_why))
        out = {}
        for name in replies:
            data = _json(replies[name])
            out[name] = [(data["rating"][str(n)], (data.get("why") or {}).get(str(n)))
                         for n in range(1, len(lists[name]) + 1)]
        return out

    def fix_plans(self, cycle, folder, found):
        """Both agents, still in the cycle's session, plan the next build from the open blockers and the minors."""
        defects = folder / "defects.md"
        sections = []
        for label in LABELS:
            sections += [f"## {label}", "", "Blockers:"] + [f"- {defect_line(item, label=False)}"
                                                           for item in found[label]["blockers"]]
            sections += ["", "Minor:"] + [f"- {defect_line(item, label=False)}" for item in found[label]["minors"]] + [""]
        defects.write_text("\n".join(sections), encoding="utf-8")
        numbers = _numbered(self.names)
        paths = {name: folder / f"plan-{numbers[name]}.md" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in paths.items()})
        entry = self.begin("plan", cycle=cycle)
        self.ask({name: (prompts.fix_plan(defects, paths[name]), self.blind) for name in self.names}, "plan",
                 lambda name: self.plan_problem(paths[name].read_text(encoding="utf-8")
                                                if paths[name].exists() else None, dict.fromkeys(LABELS)))
        self.end(entry, "planned", words={name: words(paths[name].read_text(encoding="utf-8")) for name in self.names})
        return paths

    def plan(self, cycle, base):
        """No candidates yet: both read a plain copy of the base and write a plan for the task, separately."""
        code = export_tree(self.repo, base, self.blind / "code")
        folder = self.blind / "plans"
        folder.mkdir(exist_ok=True)
        numbers = _numbered(self.names)
        paths = {name: folder / f"c{cycle}-plan-{numbers[name]}.md" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in paths.items()})
        entry = self.begin("plan", cycle=cycle)
        self.ask({name: (prompts.plan(self.task, code, paths[name]), self.blind) for name in self.names}, "plan",
                 lambda name: self.plan_problem(paths[name].read_text(encoding="utf-8")
                                                if paths[name].exists() else None, None))
        self.end(entry, "planned", words={name: words(paths[name].read_text(encoding="utf-8")) for name in self.names})
        return paths

    def revise(self, cycle, number, objections, candidates):
        """Round `number`: each agent, still in the session where it just voted, turns the two plans and
        the reasons they were rejected into one revised plan."""
        folder = self.blind / "plans"
        numbers = _numbered(self.names)
        paths = {name: folder / f"c{cycle}-r{number}-revised-{numbers[name]}.md" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in paths.items()})
        entry = self.begin("revise", cycle=cycle, plan_round=number)
        self.ask({name: (prompts.revise(objections, paths[name], base=bool(candidates)), self.blind)
                  for name in self.names}, "revise",
                 lambda name: self.plan_problem(paths[name].read_text(encoding="utf-8")
                                                if paths[name].exists() else None, candidates))
        self.end(entry, "revised", words={name: words(paths[name].read_text(encoding="utf-8")) for name in self.names})
        return paths

    def agree(self, cycle, plans, candidates, found):
        """Up to `max_plan_rounds` rounds. Round 1 votes on the plans as written; each later round first
        revises them from the rejection reasons. A vote shows each plan as the builders will get it
        (the open blockers and the minors in its base attached, from `found`). A plan both approve
        is the plan (both: the shorter, then a coin flip); after the last round, a coin flip, with the
        rejections attached."""
        folder = self.blind / "agree"
        folder.mkdir(parents=True, exist_ok=True)
        (self.blind / "plans").mkdir(exist_ok=True)
        ballots, objections, how = [], {}, None
        for number in range(1, self.max_plan_rounds + 1):
            if number > 1:
                plans = self.revise(cycle, number, objections, candidates)
            texts = {name: plans[name].read_text(encoding="utf-8") for name in self.names}
            order = random.sample(self.names, 2)  # plan 1 and plan 2, at random
            ballots = []
            for n, name in enumerate(order, 1):
                path = self.blind / "plans" / f"c{cycle}-r{number}-ballot-{n}.md"
                path.write_text(self.ballot(texts[name], candidates, found), encoding="utf-8")
                ballots.append((n, path))
            if len({text.strip() for text in texts.values()}) == 1:
                chosen, how, objections = ballots[0][1], "the two plans are the same", {}
                break
            replies = {name: folder / f"c{cycle}-r{number}-reply-{n}.json" for name, n in _numbered(self.names).items()}
            self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in replies.items()})
            entry = self.begin("agree", cycle=cycle, plan_round=number)
            attached = any(ATTACHED in path.read_text(encoding="utf-8") for _, path in ballots)
            self.ask({name: (prompts.agree(ballots, replies[name], attached), self.blind)
                      for name in self.names}, "agree", lambda name: self.agree_problem(replies[name]))
            data = {name: _json(replies[name]) for name in self.names}
            approvals = {name: data[name]["approve"] for name in self.names}
            both = [n for n in (1, 2) if all(approvals[name][str(n)] for name in self.names)]
            objections = {n: [data[name]["why"][str(n)] for name in self.names if not approvals[name][str(n)]]
                          for n in (1, 2)}
            if both:
                pick = both[0]
                if len(both) == 2:
                    sizes = {n: words(texts[order[n - 1]]) for n in (1, 2)}
                    pick = min((1, 2), key=sizes.get) if sizes[1] != sizes[2] else random.choice((1, 2))
                chosen, objections = ballots[pick - 1][1], {}
                how = f"both approved plan {pick} in round {number}" + (" (the shorter of two)" if len(both) == 2 else "")
                self.end(entry, "agreed", how=how, approvals=approvals)
                break
            self.end(entry, "rejected", approvals=approvals, why=objections)
        else:
            pick = random.choice((1, 2))
            chosen = ballots[pick - 1][1]
            how = f"no plan had both approvals in {self.max_plan_rounds} rounds; coin flip chose plan {pick}"
            objections = {pick: objections.get(pick) or []}
        body = chosen.read_text(encoding="utf-8")
        label = plan_base(body) if candidates else None
        if found and label:
            deferred = {int(n) for line in DEFERRED.findall(body) for n in re.findall(r"#(\d+)", line)}
            blockers = found[label]["blockers"]
            for item in blockers:
                if item["id"] in deferred:
                    item["status"] = "deferred: needs a new command, tool, mode or state machine"
            self.ledger = [item for item in blockers if item["id"] not in deferred]
            self.follow_ups += [item for item in blockers if item["id"] in deferred] + found[label]["minors"]
            self.save(ledger=[{key: item.get(key) for key in ("id", "file", "line", "problem")} for item in self.ledger])
        rejected = [text for texts in objections.values() for text in texts]
        if rejected:
            body += "\n## Objections to this plan (address them where they are right)\n\n" + "\n".join(
                f"- {text}" for text in rejected) + "\n"
        path = self.blind / "plans" / f"c{cycle}-agreed.md"
        for copy in (path, self.run_dir / "plans" / path.name):  # the builders' copy, and the run's record
            copy.parent.mkdir(exist_ok=True)
            copy.write_text(body, encoding="utf-8")
        self.save(plan={"path": str(path), "how": how, "words": words(body), "base": label})
        self.log(f"plan: {how}")
        return path, label

    def build(self, cycle, plan_path, start, base_label):
        """Both implement the plan from the same commit, separately. The two results are relabelled
        at random for the next judge."""
        numbers = _numbered(self.names)
        works = {name: export_tree(self.repo, start, self.blind / "build" / f"c{cycle}-{numbers[name]}")
                 for name in self.names}  # plain copies: no git history to read
        notes = {name: self.blind / "notes" / f"c{cycle}-build-{numbers[name]}.md" for name in self.names}
        for name in self.names:
            notes[name].parent.mkdir(parents=True, exist_ok=True)
        entry = self.begin("build", cycle=cycle, start=start)
        # the cycle's session started in the blind area; the prompt names the build folder
        self.ask({name: (prompts.build(plan_path, works[name], self.checks, notes[name], base_label), self.blind)
                  for name in self.names}, "build", lambda name: None if notes[name].exists() else "no note written")
        commits = {name: self.snapshot(works[name], start, f"cycle {cycle} build {numbers[name]}") for name in self.names}
        labels = dict(zip(random.sample(self.names, 2), LABELS))
        builds = self.candidates(f"c{cycle + 1}", {labels[name]: (commits[name], works[name]) for name in self.names})
        for name in self.names:  # what each build changed, for a judge to start from
            changes = self.blind / "candidates" / f"c{cycle + 1}" / f"{labels[name]}.diff"
            changes.write_text(git("diff", start, commits[name], cwd=self.repo) + "\n", encoding="utf-8")
            builds[labels[name]]["changes"] = str(changes)
        self.state["authors"].update({f"c{cycle + 1} {labels[name]}": name for name in self.names})
        self.end(entry, "built", commits=commits,
                 checks={name: {"ok": builds[labels[name]]["checks_ok"],
                                "commands": builds[labels[name]]["checks"]} for name in self.names})
        return builds

    def publish(self, branch, candidate, label):
        commit = candidate["commit"]
        git("update-ref", f"refs/cav/{self.run_id}/result", commit, cwd=self.repo)  # keep the result reachable
        defects = self.state.get("open_defects") or []
        final = {"round": self.step, "commit": commit, "candidate": label, "how": self.state.get("decided"), "open_defects": defects}
        if not candidate["checks_ok"]:
            self.save(phase="failed", outcome="checks fail", final=final,
                      detail=f"result {commit[:12]} kept as refs/cav/{self.run_id}/result, not published")
            self.write_report()
            return 2
        if self.state["pushable"]:
            final["pushed"] = push_result(self.repo, branch, commit, self.run_id)
            outcome = f"merged and pushed to origin/{branch}"
        else:
            outcome = "done, kept local"
        self.save(phase="done", outcome=outcome, final=final,
                  detail=f"{commit[:12]}: {self.state.get('decided')}"
                         + (f"; {len(defects)} follow-up(s)" if defects else ""))
        self.events.emit("final.complete", self.step, data={"commit": commit})
        self.write_report()
        self.log(f"{outcome}: {commit}")
        return 0

    # ------------------------------------------------------------------- helpers

    def branch_candidates(self, repo):
        """The two branches as Candidate X and Y, at random."""
        shas = [resolve_ref(repo, ref) for ref in self.branches]
        order = random.sample(range(2), 2)
        labels = {label: {"ref": self.branches[i], "commit": shas[i]} for label, i in zip(LABELS, order)}
        self.save(labels=labels)
        # the checks run in CAV's own copies; the agents only ever see the blind exports
        trees = {label: (item["commit"], export_tree(repo, item["commit"], self.run_dir / "checks" / label))
                 for label, item in labels.items()}
        return self.candidates("c1", trees)

    def candidates(self, what, trees):
        """label -> commit, a clean plain folder to read, and its checks (run in the given folder, both at once)."""
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            checks = {label: pool.submit(self.run_checks, work) for label, (_, work) in trees.items()}
        out = {}
        for label, (commit, _) in trees.items():
            ok, results = checks[label].result()
            folder = export_tree(self.repo, commit, self.blind / "candidates" / what / label)
            out[label] = {"commit": commit, "folder": str(folder), "checks_ok": ok, "checks": results}
        return out

    def smaller(self, candidates):
        """Two candidates both approve: the one with fewer changed lines from their common base."""
        common = git("merge-base", *(candidates[label]["commit"] for label in LABELS), cwd=self.repo, check=False)
        sizes = {}
        for label in LABELS:
            out = git("diff", "--numstat", common or candidates[label]["commit"], candidates[label]["commit"],
                      cwd=self.repo, check=False)
            sizes[label] = sum(int(part) for line in out.splitlines() for part in line.split("\t")[:2] if part.isdigit())
        if sizes["X"] == sizes["Y"]:
            return random.choice(LABELS)
        return min(LABELS, key=sizes.get)

    def keep(self, candidates, prefer, sizes):
        """The last judge with no candidate clear: the passing one if only one passes its checks, else
        the one with fewer open blockers (`sizes`), else the one both would keep, else a coin flip."""
        passing = [label for label in LABELS if candidates[label]["checks_ok"]]
        if len(passing) == 1:
            return passing[0], f"no candidate was clear; only Candidate {passing[0]} passes its checks"
        if sizes["X"] != sizes["Y"]:
            label = min(LABELS, key=sizes.get)
            return label, f"no candidate was clear; Candidate {label} has fewer open blockers ({sizes[label]})"
        kept = set(prefer.values())
        if len(kept) == 1:
            label = kept.pop()
            return label, f"no candidate was clear; both would keep Candidate {label}"
        label = random.choice(LABELS)
        return label, f"no candidate was clear and they would keep different ones; coin flip chose Candidate {label}"


    @staticmethod
    def ballot(text, candidates, found):
        """A plan as the builders get it: the open blockers and the minors in the base it names attached."""
        body = text.rstrip() + "\n"
        label = plan_base(body) if candidates else None
        if found and label in found:
            for title, items in ((ATTACHED + " (both judges agreed; defer only what needs a new command, tool, mode "
                                  "or state machine)", found[label]["blockers"]),
                                 ("## Minor defects (fix only where the fix is small)", found[label]["minors"])):
                if items:
                    body += f"\n{title}\n\n" + "".join(f"- {defect_line(item, label=False)}\n" for item in items)
        return body

    def snapshot(self, folder, parent, what):
        """Commit a plain folder on top of `parent` through a separate index: the folder never gets a
        .git, and the repo's own index and checkout are untouched. Its .gitignore still applies."""
        git_dir = git("rev-parse", "--absolute-git-dir", cwd=self.repo)
        index = self.run_dir / f"index-{secrets.token_hex(4)}"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        where = ("--git-dir", git_dir, "--work-tree", str(folder))
        try:
            git(*where, "read-tree", parent, cwd=folder, env=env)
            git(*where, "add", "-A", cwd=folder, env=env)
            tree = git(*where, "write-tree", cwd=folder, env=env)
        finally:
            index.unlink(missing_ok=True)
        if tree == git("rev-parse", f"{parent}^{{tree}}", cwd=self.repo):
            return parent
        commit = git(*IDENTITY, "commit-tree", tree, "-p", parent, "-m", f"CAV {self.run_id}: {what}", cwd=self.repo)
        git("update-ref", f"refs/cav/{self.run_id}/{what.replace(' ', '-')}", commit, cwd=self.repo)  # keep it reachable
        return commit

    @staticmethod
    def checks_summary(candidate):
        results = candidate["checks"]
        if not results:
            return "no checks configured"
        return ("all pass" if candidate["checks_ok"] else "failing") + ": " + "; ".join(
            f"{item['command']} exit {item['exit']}" for item in results)

    @staticmethod
    def plan_problem(text, candidates):
        if text is None:
            return "no plan file written"
        if words(text) > PLAN_WORDS:
            return f"the plan has {words(text)} words; cut it to {PLAN_WORDS}"
        if candidates and plan_base(text) not in candidates:
            return 'the plan must start with "Base: X" or "Base: Y"'
        return None

    def judge_problem(self, reply, final, candidates, ids):
        """`ids`: the ledger blockers each candidate must be checked against, as strings."""
        data = _json(reply)
        if data is None:
            return 'write JSON with "defects": [...]'
        if final and data.get("prefer") not in LABELS:
            return '"prefer" must be "X" or "Y"'
        if ids:
            fixed = data.get("fixed")
            if not isinstance(fixed, dict) or set(fixed) != set(LABELS) or any(
                    not isinstance(fixed[label], dict) or set(fixed[label]) != set(ids)
                    or any(not isinstance(value, bool) for value in fixed[label].values()) for label in LABELS):
                return ('"fixed" must give, for X and for Y, true or false for each ledger blocker: '
                        + ", ".join(ids))
        defects = data.get("defects") or []
        if not isinstance(defects, list) or any(
                not isinstance(item, dict) or not item.get("problem") or item.get("candidate") not in (*LABELS, "both")
                or item.get("severity") not in SEVERITIES for item in defects):
            return ('"defects" must be a list of objects with "candidate" ("X", "Y" or "both"), '
                    '"severity" ("blocker" or "minor") and "problem"')
        for item in defects:
            labels = LABELS if item["candidate"] == "both" else (item["candidate"],)
            roots = [pathlib.Path(candidates[label]["folder"]).resolve() for label in labels]
            targets = [(root, (root / str(item.get("file") or "")).resolve()) for root in roots]
            if not item.get("file") or not any(root in target.parents and target.is_file() for root, target in targets):
                return f"defect file {item.get('file')!r} is not a file in {item['candidate']}"
        data["defects"] = defects
        reply.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return None

    @staticmethod
    def rating_problem(path, count, ask_why):
        """A triage answer: a rating for each of `count` defects, and a reason for each rated lower than
        blocker (`ask_why` "lower") or still a blocker ("blocker")."""
        data = _json(path)
        rating = (data or {}).get("rating")
        if not isinstance(rating, dict) or set(rating) != {str(n) for n in range(1, count + 1)} \
                or any(value not in RATINGS for value in rating.values()):
            return f'write JSON with "rating": "blocker", "minor" or "not real" for each of 1 to {count}'
        why = data.get("why") or {}
        if not isinstance(why, dict):
            return '"why" must map defect numbers to reasons'
        for number, value in rating.items():
            if (value == "blocker") == (ask_why == "blocker"):
                if not isinstance(why.get(number), str):
                    return f'defect {number} is rated {value} without a "why"'
                if words(why[number]) > REAL_WORDS:
                    return f'"why" for defect {number} has {words(why[number])} words; cut it to {REAL_WORDS}'
        data["why"] = why
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return None

    @staticmethod
    def agree_problem(path):
        data = _json(path)
        approve = (data or {}).get("approve")
        if not isinstance(approve, dict) or set(approve) != {"1", "2"} \
                or any(not isinstance(value, bool) for value in approve.values()):
            return 'write JSON with "approve": {"1": true|false, "2": true|false}'
        why = data.get("why") or {}
        if not isinstance(why, dict):
            return '"why" must map each rejected plan to the reason'
        for number, ok in approve.items():
            if not ok and not isinstance(why.get(number), str):
                return f'plan {number} is rejected without a "why"'
            if not ok and words(why[number]) > WHY_WORDS:
                return f'"why" for plan {number} has {words(why[number])} words; cut it to {WHY_WORDS}'
        data["why"] = why
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return None

    def write_report(self):
        lines = [f"# CAV run {self.run_id}", "", f"- Outcome: {self.state.get('outcome')}",
                 f"- Decided: {self.state.get('decided')}", ""]
        if self.state.get("labels"):
            lines += ["- Branches: " + ", ".join(f"{label} = {item['ref']}"
                                                 for label, item in self.state["labels"].items()), ""]
        lines += ["| Step | | Time | Result |", "|---|---|---|---|"]
        lines += [f"| {item['round']} | {item['step']}" + (f" {item['plan_round']}" if item.get("plan_round") else "") + " "
                  f"| {item.get('seconds', 0) // 60}m {item.get('seconds', 0) % 60:02d}s | {item.get('verdict')} |"
                  for item in self.state["history"]]
        defects = self.state.get("open_defects") or []
        if defects:
            lines += ["", "## Follow-ups: defects the result still has", ""] + [
                f"- {follow_up(item)}" for item in defects]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


POLL_SECONDS = 2
