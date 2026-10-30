"""One CAV run: both agents agree on one base and one plan, both build it, both judge the results.

    judge    both agents, blind and separately: approve or reject each of the two candidates (a
             rejection needs a defect), name the one they would keep, and write a plan (at most
             500 words) whose first line names the candidate to build on. A candidate both
             approve, with passing checks, is the result.
    plan     a task with no candidates yet: both write a plan, separately
    agree    both read the two plans (no authors) and approve or reject each (a rejection needs
             a missed acceptance criterion or a reason it would not work). A plan both approve
             is the plan; otherwise the plan both would keep, or a coin flip.
    build    both implement the agreed plan from the same base, separately; the checks run, and
             the two results are the next judge's candidates
    publish  a candidate both approve; after the last build, the one both would keep (a split
             goes to a coin flip). Pushed to origin's default branch unless --no-push.

Two branches start at judge, since they may already be acceptable. No prompt names an agent or
says whose anything is: candidates are Candidate X and Y (relabelled at random each round, read
from plain folders without .git), plans and reviews are numbered at random, candidates are listed
in random order, and every step runs in a fresh session, so no agent remembers what it wrote.

Everything an agent reads or writes lives in a blind area outside every git repo and away from
the run's bookkeeping: plain folders only, no .git anywhere above them, no state.json. Builds
happen in plain copies too; CAV commits them into the repo itself, through a separate index.
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

PLAN_WORDS, WHY_WORDS, MAX_DEFECTS = prompts.PLAN_WORDS, prompts.WHY_WORDS, prompts.MAX_DEFECTS
LABELS = ("X", "Y")
MAX_BUILDS = 3
BASE_LINE = re.compile(r"(?im)^\W*base\W*:\W*candidate\s+([XY])\b")
CLONE_ROOT = pathlib.Path(os.environ.get("CAV_CLONE_ROOT") or pathlib.Path.home() / "Documents" / "Default Project")
GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
STEP_SECONDS = {"judge": 2700, "plan": 1800, "agree": 1800, "build": 5400}
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


def _numbered(names):
    """Each agent's file number for a step, at random, so no file name shows whose it is."""
    return dict(zip(random.sample(list(names), len(names)), range(1, len(names) + 1)))


# ------------------------------------------------------------------------------- flow

class Flow:
    def __init__(self, repo_spec, task, run_dir, branches=(), checks=(), push=True, models=None,
                 max_builds=MAX_BUILDS):
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.repo_spec, self.task = repo_spec, task.strip()
        self.branches, self.checks = list(branches), list(checks)
        if self.branches and len(self.branches) != 2:
            raise ValueError("give two branches to reconcile, or none")
        self.push, self.max_builds = push, max_builds
        models = models or config.DEFAULT_MODELS
        self.names = list(models)
        self.run_id = self.run_dir.name
        self.blind = BLIND_ROOT / secrets.token_hex(4)  # a random name: nothing in the path says what it is
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_id)
        self.state = {"case": f"{repo_name(repo_spec)}", "run_id": self.run_id, "flow": True, "phase": "starting",
                      "round": 0, "agents": {}, "history": [], "names": self.names, "authors": {},
                      "blind": str(self.blind)}
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

    def begin(self, kind, cycle, **data):
        """A new step: its number is the dashboard's round."""
        self.step += 1
        entry = {"round": self.step, "step": kind, "cycle": cycle, "started": time.time(), **data}
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
                agent.reset()
                agent.session_id = None  # a fresh session: nothing remembered from an earlier step
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
        for cycle in range(1, self.max_builds + 2):
            if candidates:
                last = cycle > self.max_builds
                result, plans, reviews = self.judge(cycle, candidates, last)
                if result:
                    return self.publish(branch, candidates[result], result)
            else:
                plans, reviews = self.plan(cycle), []
            plan_path, label = self.agree(cycle, plans, candidates, reviews)
            start = candidates[label]["commit"] if candidates else base
            candidates = self.build(cycle, plan_path, start, label)
        raise RuntimeError("the last judge must decide")  # unreachable: the last judge always picks

    def judge(self, cycle, candidates, last):
        """Both agents judge both candidates blind. A candidate both approve, with passing checks, is
        the result; after the last build, the one both would keep (a split: a coin flip)."""
        folder = self.blind / "judge" / f"c{cycle}"
        folder.mkdir(parents=True, exist_ok=True)
        numbers = _numbered(self.names)
        replies = {name: folder / f"review-{numbers[name]}.json" for name in self.names}
        plans = {name: folder / f"plan-{numbers[name]}.md" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name
                                      for files in (replies, plans) for name, path in files.items()})
        entry = self.begin("judge", cycle, candidates={label: item["commit"] for label, item in candidates.items()})
        jobs = {}
        for name in self.names:
            listed = [(label, candidates[label]["folder"], self.checks_summary(candidates[label]))
                      for label in random.sample(LABELS, 2)]  # no fixed first place
            jobs[name] = (prompts.judge(self.task, listed, replies[name], plans[name]), self.blind)
        self.ask(jobs, "judge", lambda name: self.judge_problem(replies[name], plans[name], candidates))
        verdicts = {name: _json(replies[name]) for name in self.names}
        approvals = {name: verdicts[name]["approve"] for name in self.names}
        both = [label for label in LABELS
                if all(approvals[name][label] for name in self.names) and candidates[label]["checks_ok"]]
        defects = [item for name in self.names for item in verdicts[name]["defects"]]
        record = {"approvals": approvals, "prefer": {name: verdicts[name]["prefer"] for name in self.names},
                  "defects": defects}
        if both:
            result = both[0] if len(both) == 1 else self.smaller(candidates)
            how = (f"both approved Candidate {result}" if len(both) == 1 else
                   f"both approved both; Candidate {result} changes less")
        elif last:
            result, how = self.keep(candidates, record["prefer"])
        else:
            self.end(entry, "rejected", **record)
            return None, {name: plans[name] for name in self.names}, list(replies.values())
        self.end(entry, "accepted" if both else "forced", how=how, **record)
        self.save(decided=how, open_defects=[item for item in defects if item["candidate"] == result])
        return result, None, None

    def plan(self, cycle):
        """No candidates yet: both write a plan for the task, separately."""
        folder = self.blind / "plans"
        folder.mkdir(exist_ok=True)
        numbers = _numbered(self.names)
        paths = {name: folder / f"c{cycle}-plan-{numbers[name]}.md" for name in self.names}
        self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in paths.items()})
        entry = self.begin("plan", cycle)
        self.ask({name: (prompts.plan(self.task, paths[name]), self.blind) for name in self.names}, "plan",
                 lambda name: self.plan_problem(paths[name].read_text(encoding="utf-8")
                                                if paths[name].exists() else None, None))
        self.end(entry, "planned", words={name: words(paths[name].read_text(encoding="utf-8")) for name in self.names})
        return paths

    def agree(self, cycle, plans, candidates, reviews):
        """Both approve or reject each plan. A plan both approve is the plan (both: the shorter, then
        a coin flip); none: the plan both would keep, or a coin flip. The rejections and the
        reviews' defects against its base go with it to the builders."""
        texts = {name: plans[name].read_text(encoding="utf-8") for name in self.names}
        numbers = {int(re.search(r"plan-(\d)", plans[name].name).group(1)): name for name in self.names}
        folder = self.blind / "agree"
        folder.mkdir(exist_ok=True)
        entry = self.begin("agree", cycle)
        if len(set(text.strip() for text in texts.values())) == 1:
            chosen, how, objections, approvals = numbers[1], "the two plans are the same", [], {}
        else:
            replies = {name: folder / f"c{cycle}-reply-{n}.json" for name, n in _numbered(self.names).items()}
            self.state["authors"].update({str(path.relative_to(self.blind)): name for name, path in replies.items()})
            listed = [(n, plans[numbers[n]]) for n in (1, 2)]
            sources = ([(label, candidates[label]["folder"]) for label in random.sample(LABELS, 2)]
                       if candidates else [])
            self.ask({name: (prompts.agree(self.task, sources, reviews, listed, replies[name]), self.blind)
                      for name in self.names}, "agree", lambda name: self.agree_problem(replies[name]))
            data = {name: _json(replies[name]) for name in self.names}
            approvals = {name: data[name]["approve"] for name in self.names}
            both = [n for n in (1, 2) if all(approvals[name][str(n)] for name in self.names)]
            if len(both) == 1:
                chosen, how = numbers[both[0]], f"both approved plan {both[0]}"
            elif both:
                shorter = sorted((1, 2), key=lambda n: words(texts[numbers[n]]))
                if words(texts[numbers[1]]) == words(texts[numbers[2]]):
                    shorter = random.sample(shorter, 2)
                chosen, how = numbers[shorter[0]], f"both approved both; plan {shorter[0]} is shorter"
            else:
                kept = {data[name]["prefer"] for name in self.names}
                pick = kept.pop() if len(kept) == 1 else random.choice((1, 2))
                chosen = numbers[pick]
                how = (f"no plan had both approvals; both would keep plan {pick}" if not kept else
                       f"no plan had both approvals and they would keep different plans; coin flip chose plan {pick}")
            number = next(n for n, name in numbers.items() if name == chosen)
            objections = [data[name]["why"][str(number)] for name in self.names
                          if not approvals[name][str(number)]]
        body = texts[chosen].rstrip() + "\n"
        label = plan_base(body) if candidates else None
        found = [item for path in reviews for item in (_json(path) or {}).get("defects") or []
                 if item.get("candidate") == label]
        if found:
            body += ("\n## Defects found in the base (fix these too)\n\n"
                     + "\n".join(f"- {item.get('file', '')}:{item.get('line', '')} {item['problem']}"
                                 + (f" (fix: {item['fix']})" if item.get("fix") else "") for item in found) + "\n")
        if objections:
            body += "\n## Objections to this plan (address them where they are right)\n\n" + "\n".join(
                f"- {text}" for text in objections) + "\n"
        path = self.blind / "plans" / f"c{cycle}-agreed.md"
        for copy in (path, self.run_dir / "plans" / path.name):  # the builders' copy, and the run's record
            copy.parent.mkdir(exist_ok=True)
            copy.write_text(body, encoding="utf-8")
        self.end(entry, "agreed", how=how, approvals=approvals, plan=str(path), base=label)
        self.save(plan={"path": str(path), "how": how, "words": words(texts[chosen]), "base": label})
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
        entry = self.begin("build", cycle, start=start)
        self.ask({name: (prompts.build(plan_path, works[name], self.checks, notes[name], base_label), works[name])
                  for name in self.names}, "build", lambda name: None if notes[name].exists() else "no note written")
        commits = {name: self.snapshot(works[name], start, f"cycle {cycle} build {numbers[name]}") for name in self.names}
        labels = dict(zip(random.sample(self.names, 2), LABELS))
        candidates = self.candidates(cycle + 1, {labels[name]: (commits[name], works[name]) for name in self.names})
        self.state["authors"].update({f"c{cycle + 1} Candidate {labels[name]}": name for name in self.names})
        self.end(entry, "built", commits=commits,
                 checks={name: {"ok": candidates[labels[name]]["checks_ok"],
                                "commands": candidates[labels[name]]["checks"]} for name in self.names})
        return candidates

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
                         + (f"; {len(defects)} open defect(s)" if defects else ""))
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
        trees = {label: (item["commit"], export_tree(repo, item["commit"], self.run_dir / "checks" / f"c1-{label}"))
                 for label, item in labels.items()}
        return self.candidates(1, trees)

    def candidates(self, cycle, trees):
        """label -> commit, a clean plain folder to read, and its checks (run in the given folder, both at once)."""
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            checks = {label: pool.submit(self.run_checks, work) for label, (_, work) in trees.items()}
        out = {}
        for label, (commit, _) in trees.items():
            ok, results = checks[label].result()
            folder = export_tree(self.repo, commit, self.blind / "candidates" / f"c{cycle}" / f"candidate-{label}")
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

    def keep(self, candidates, prefer):
        """The last judge without a candidate both approve: the passing one if only one passes its
        checks, else the one both would keep, else a coin flip."""
        passing = [label for label in LABELS if candidates[label]["checks_ok"]]
        if len(passing) == 1:
            return passing[0], f"no candidate had both approvals; only Candidate {passing[0]} passes its checks"
        kept = set(prefer.values())
        if len(kept) == 1:
            label = kept.pop()
            return label, f"no candidate had both approvals; both would keep Candidate {label}"
        label = random.choice(LABELS)
        return label, f"no candidate had both approvals and they would keep different ones; coin flip chose Candidate {label}"

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
            return 'the plan must start with "Base: Candidate X" or "Base: Candidate Y"'
        return None

    def judge_problem(self, reply, plan, candidates):
        data = _json(reply)
        approve = (data or {}).get("approve")
        if not isinstance(approve, dict) or set(approve) != set(LABELS) \
                or any(not isinstance(value, bool) for value in approve.values()):
            return 'write JSON with "approve": {"X": true|false, "Y": true|false}'
        if data.get("prefer") not in LABELS:
            return '"prefer" must be "X" or "Y"'
        defects = data.get("defects") or []
        if not isinstance(defects, list) or any(not isinstance(item, dict) or not item.get("problem")
                                                or item.get("candidate") not in LABELS for item in defects):
            return '"defects" must be a list of objects with "candidate" ("X" or "Y") and "problem"'
        for label in LABELS:
            mine = [item for item in defects if item["candidate"] == label]
            if not approve[label] and not mine:
                return f"Candidate {label} is rejected without a defect"
            if approve[label] and mine:
                return f"Candidate {label} is approved but has defects; approve it or reject it"
            if len(mine) > MAX_DEFECTS:
                return f"list at most {MAX_DEFECTS} defects for Candidate {label}"
            root = pathlib.Path(candidates[label]["folder"]).resolve()
            for item in mine:
                target = (root / str(item.get("file") or "")).resolve()
                if not item.get("file") or root not in target.parents or not target.is_file():
                    return f"defect file {item.get('file')!r} is not a file in Candidate {label}"
        data["defects"] = defects
        reply.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.plan_problem(plan.read_text(encoding="utf-8") if plan.exists() else None, candidates)

    @staticmethod
    def agree_problem(path):
        data = _json(path)
        approve = (data or {}).get("approve")
        if not isinstance(approve, dict) or set(approve) != {"1", "2"} \
                or any(not isinstance(value, bool) for value in approve.values()):
            return 'write JSON with "approve": {"1": true|false, "2": true|false}'
        if data.get("prefer") not in (1, 2):
            return '"prefer" must be 1 or 2'
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
            lines += ["- Branches: " + ", ".join(f"Candidate {label} = {item['ref']}"
                                                 for label, item in self.state["labels"].items()), ""]
        lines += ["| Step | | Time | Result |", "|---|---|---|---|"]
        lines += [f"| {item['round']} | {item['step']} (cycle {item['cycle']}) "
                  f"| {item.get('seconds', 0) // 60}m {item.get('seconds', 0) % 60:02d}s | {item.get('verdict')} |"
                  for item in self.state["history"]]
        defects = self.state.get("open_defects") or []
        if defects:
            lines += ["", "## Open defects in the result", ""] + [
                f"- {item.get('file', '')}:{item.get('line', '')} {item['problem']}" for item in defects]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


POLL_SECONDS = 2
