"""One CAV run: two agents, one result both agree has no blockers (see README)."""

import itertools
import json
import os
import pathlib
import random
import re
import secrets
import shutil
import subprocess
import threading
import time

from . import agents as agent_mod
from . import prompts
from .events import EventWriter, atomic_write
from .gitops import GITHUB, IDENTITY, NO_WINDOW, git, git_ok, resolve_ref, resolve_repo

LABELS = ("X", "Y")
BASE_LINE = re.compile(r"(?im)^\W*base\W*:\W*(?:candidate\s+)?([XY])\b")
ANSWER_SECONDS = 5400  # one agent answer
CHECK_SECONDS = 3600  # one check command
POLL_SECONDS = 2
BLIND_ROOT = pathlib.Path(os.environ.get("CAV_BLIND_ROOT")
                          or pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / "cav-blind")


def export_tree(repo, sha, dest):
    """A commit's files as a plain folder: no .git, so no branch name, author or message. Not `git archive`:
    it drops export-ignore paths and expands export-subst, which a build's snapshot would then commit."""
    if dest.exists():  # left by an interrupted step of a resumed run: start clean
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    git_dir = git("rev-parse", "--absolute-git-dir", cwd=repo)
    index = dest.parent / f"{dest.name}.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    where = ("--git-dir", git_dir, "--work-tree", str(dest))
    try:
        git(*where, "read-tree", sha, cwd=dest, env=env)
        git(*where, "checkout-index", "-a", "-f", cwd=dest, env=env)
    finally:
        index.unlink(missing_ok=True)
    return dest


def plan_base(text):
    match = BASE_LINE.search(text or "")
    return match.group(1).upper() if match else None


def words(text):
    return len((text or "").split())


def _json(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hits(blocker, label):
    return blocker.get("candidate") in (label, "both")


def blocker_line(item):
    """A blocker as the agents read it: its number once agreed, never who raised it."""
    return ((f"#{item['id']} " if item.get("id") else "") + f"[{item['candidate']}] {item.get('file', '')}:"
            f"{item.get('line', '')} {item['problem']}" + (f" (fix: {item['fix']})" if item.get("fix") else ""))


class Flow:
    def __init__(self, repos, run_dir, task=None, plan=None, checks=(), agents=None):
        """`repos`: [{"repo": a local path or a GitHub URL, "branches": [set 1, set 2]}]; a blank branch is
        that repo's main. Any branch at all means two sets to reconcile against `plan`; none means `task`.
        `agents`: the two agents by name (see agents.AGENTS); none means the defaults."""
        self.repos = [{"spec": item["repo"], "branches": (list(item.get("branches") or []) + ["", ""])[:2]}
                      for item in repos]
        self.branched = any(ref for item in self.repos for ref in item["branches"])
        if not self.repos or (self.branched and not plan) or (not self.branched and not task):
            raise ValueError("give repos and a task, or repos with two sets of branches and a plan")
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.task, self.plan = (task or "").strip(), (plan or "").strip()
        self.checks = [command.strip() for command in checks if command.strip()]  # run in each candidate's folder
        self.run_id = self.run_dir.name
        self.blind = BLIND_ROOT / secrets.token_hex(4)  # a random name: nothing in the path says what it is
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_id)
        models = agent_mod.models_for(agents)
        self.names = list(models)
        self.state = {"run_id": self.run_id, "repo": ", ".join(str(item["spec"]) for item in self.repos),
                      "names": self.names, "phase": "starting",
                      "round": 0, "agents": {}, "history": [], "authors": {}}
        self.step, self.cycle, self.next_id = 0, 0, 1
        self.resumed = False
        spec_file = self.run_dir / "spec.json"  # what `cav resume` starts the run from again
        if not spec_file.exists():
            spec_file.write_text(json.dumps({"repos": repos, "task": task, "plan": plan, "checks": self.checks,
                                             "agents": self.names}, indent=2), encoding="utf-8")
        self.agents = {}
        for name, (kind, model) in models.items():
            spec = agent_mod.AgentSpec(name, kind, model, "high" if kind in ("kimi", "claude") else None)
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"{name}.log", title_prefix=f"cav {self.run_id}")
            agent.emit = lambda type_, agent_name, data: self.events.emit(type_, self.step, agent_name, data)
            self.agents[name] = agent

    @classmethod
    def resume(cls, run_dir):
        """A run that stopped before its result, picked up from its last resume point: the step it was
        in starts over, everything before it is kept."""
        run_dir = pathlib.Path(run_dir).resolve()
        state, spec = _json(run_dir / "state.json") or {}, _json(run_dir / "spec.json")
        point = state.get("resume")
        if state.get("phase") == "done" or not point or not spec:
            raise ValueError(f"{run_dir.name} has no point to resume from")
        flow = cls(spec["repos"], run_dir, task=spec.get("task"), plan=spec.get("plan"),
                   checks=spec.get("checks") or (), agents=spec.get("agents"))
        for name, info in (state.get("agents") or {}).items():  # a stopped run's Kimi turn keeps writing into this run's files
            if name in flow.agents and isinstance(info, dict) and info.get("session"):
                flow.agents[name].session_id = info["session"]
                flow.agents[name].abort()
                flow.agents[name].session_id = None  # the restarted step opens a fresh session, as before
        for entry in state["history"]:
            entry.setdefault("verdict", "interrupted")
        for key in ("outcome", "detail"):
            state.pop(key, None)
        flow.state, flow.blind, flow.resumed = state, pathlib.Path(state["blind"]), True
        flow.repos = [dict(item, path=pathlib.Path(item["path"])) for item in point["repos"]]
        flow.step, flow.cycle, flow.next_id = state["round"], point["cycle"], point["next_id"]
        return flow

    # ------------------------------------------------------------------ plumbing

    def log(self, message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(self.run_dir / "run.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save(self, **changes):
        self.state.update(changes, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        atomic_write(self.run_dir / "state.json", json.dumps(self.state, indent=2))

    def begin(self, kind, **data):
        """A new step: the dashboard's unit."""
        self.step += 1
        entry = {"round": self.step, "step": kind, "cycle": self.cycle, "started": time.time(), **data}
        self.state["history"].append(entry)
        self.save(phase=kind, round=self.step)
        self.events.emit("round.start", self.step, data={"phase": kind})
        return entry

    def end(self, entry, verdict, **data):
        entry.update(data, verdict=verdict, seconds=round(time.time() - entry["started"]))
        self.save()
        self.log(f"step {entry['round']} {entry['step']}: {verdict}")

    def checkpoint(self, step, candidates=None, plans=(), agreed=None, **data):
        """Where a stopped run picks up again: everything `step` needs, saved before it starts."""
        point = {"next": step, "cycle": self.cycle, "next_id": self.next_id, "repos": self.repos,
                 "candidates": candidates, "plans": list(plans), "agreed": agreed, **data}
        self.save(resume=json.loads(json.dumps(point, default=str)))

    def mine(self, paths):
        """Record who wrote each numbered file: bookkeeping the agents never see. A file of that name
        left by an interrupted step must not pass for this step's answer."""
        for path in paths.values():
            pathlib.Path(path).unlink(missing_ok=True)
        self.state["authors"].update({self.rel(path): name for name, path in paths.items()})

    def rel(self, path):
        """A file in the blind area as the dashboard links to it."""
        return pathlib.Path(path).relative_to(self.blind).as_posix()

    def ask(self, jobs, check):
        """Each named agent at once, in its session for the round; one retry for an answer `check` refuses."""
        problems = {name: None for name in jobs}
        for attempt in range(2):
            pending = {name: prompt for name, prompt in jobs.items() if attempt == 0 or problems[name]}
            if not pending:
                break
            threads = []
            for name, prompt in pending.items():
                agent = self.agents[name]
                agent.reset()
                text = prompt + (prompts.retry_note(problems[name]) if attempt else "")
                record = self.run_dir / "rounds" / f"r{self.step}" / f"{name}-context.md"  # for the dashboard
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(text, encoding="utf-8")
                thread = threading.Thread(target=self._work, args=(agent, text), daemon=True)
                thread.start()
                threads.append((name, thread))
            deadline = time.time() + ANSWER_SECONDS
            # the session too: a resumed run aborts a turn this run leaves running (see resume)
            status = lambda: {name: {"state": "running" if thread.is_alive() else "done",
                                     "detail": self.agents[name].state.detail, "idle": int(self.agents[name].state.idle),
                                     "session": self.agents[name].session_id} for name, thread in threads}
            while any(thread.is_alive() for _, thread in threads):
                self.state["agents"] = status()
                self.save()
                if time.time() > deadline:
                    for name, thread in threads:
                        if thread.is_alive() and not self.agents[name].state.error:
                            self.agents[name].abort()
                            self.agents[name].state.error = "timed out"
                time.sleep(POLL_SECONDS)
            self.save(agents=status())
            for name, _ in threads:
                problems[name] = self.agents[name].state.error or check(name)
                if problems[name]:
                    self.log(f"{name}: {problems[name]}" + ("; retrying" if attempt == 0 else ""))
        failed = {name: problem for name, problem in problems.items() if problem}
        if failed:
            raise RuntimeError("; ".join(f"{name}: {problem}" for name, problem in failed.items()))

    def _work(self, agent, prompt):
        try:
            agent.run(prompt, self.blind)
        except Exception as error:  # an adapter error is a failed answer, not a crash
            agent.state.error = str(error)
            agent.state.finished = True

    def new_round(self):
        self.cycle += 1
        for agent in self.agents.values():
            agent.session_id = None  # a fresh session per round

    # --------------------------------------------------------------------- run

    def run(self):
        try:
            return self._run()
        except Exception as error:
            self.log(f"failed: {error}")
            self.save(phase="failed", outcome="failed", detail=str(error))
            return 1

    def _run(self):
        if self.resumed:
            candidates, plans, agreed = self.pick_up()
        else:
            candidates, plans, agreed = self.start()
        while True:
            self.checkpoint("review", candidates, plans, agreed)
            self.new_round()
            found = self.review(candidates, plans, agreed)
            clear = [label for label in LABELS if not found[label]]
            if clear:  # a fresh audit, told the review found nothing, before a candidate is the result
                self.new_round()
                audited = self.review(candidates, plans, agreed, audit=clear)
                found = {label: found[label] + audited[label] for label in LABELS}  # the review's blockers stand
                clear = [label for label in LABELS if not found[label]]
                if clear:
                    return self.finish(candidates, random.choice(clear))
            both = self.agreed_file("both", [item for label in LABELS for item in found[label]])
            fix = self.agree(self.write_plans(lambda path: prompts.fix_plan(both, plans, path)), fixes=True)
            plans.append(fix)
            label = plan_base(fix.read_text(encoding="utf-8"))
            agreed = self.agreed_file(label, found[label])
            self.save(base=label, agreed=[blocker_line(item) for item in found[label]])
            self.checkpoint("build", candidates, plans, agreed, base=label, fixing=len(found[label]))
            candidates = self.build(fix, candidates[label]["commits"], label, len(found[label]))

    def pick_up(self):
        """A resumed run's candidates, plans and agreed blockers, rebuilding first if it stopped in a build."""
        point = self.state["resume"]
        self.log(f"resumed: {point['next']} starts over")
        self.save(phase="resuming")
        candidates, plans = point["candidates"], [pathlib.Path(path) for path in point["plans"]]
        agreed = pathlib.Path(point["agreed"]) if point["agreed"] else None
        if point["next"] == "first-build":
            candidates = self.build(plans[0], {item["name"]: item["main"] for item in self.repos})
        elif point["next"] == "build":
            candidates = self.build(plans[-1], candidates[point["base"]]["commits"], point["base"], point["fixing"])
        return candidates, plans, agreed

    def start(self):
        """Resolve the repos, then the first candidates: the two branch sets, or two builds of the agreed plan."""
        for item in self.repos:  # each repo gets a folder name of its own inside every candidate
            item["path"] = resolve_repo(item["spec"])
            taken = {other.get("name") for other in self.repos}
            item["name"] = next(name for n in itertools.count(1)
                                for name in [item["path"].name + (f"-{n}" if n > 1 else "")] if name not in taken)
            # a GitHub repo's main is its default branch as fetched; a local one's, its checkout
            item["main"] = resolve_ref(item["path"], "origin/HEAD" if GITHUB.match(str(item["spec"]).strip()) else "HEAD")
        self.blind.mkdir(parents=True, exist_ok=True)
        self.save(blind=str(self.blind))
        if git_ok("rev-parse", "--git-dir", cwd=self.blind):
            raise RuntimeError(f"the agents' folder {self.blind} is inside a git repo; set CAV_BLIND_ROOT elsewhere")
        plan_path = self.blind / "plan.md"
        mains = {item["name"]: item["main"] for item in self.repos}
        if self.branched:
            plan_path.write_text(self.plan + "\n", encoding="utf-8")
            order = dict(zip(LABELS, random.sample((0, 1), 2)))  # which branch set is X, at random
            refs = {label: {item["name"]: item["branches"][order[label]] for item in self.repos} for label in LABELS}
            commits = {label: {item["name"]: resolve_ref(item["path"], refs[label][item["name"]],
                       prefer_origin=bool(GITHUB.match(str(item["spec"]).strip()))) if refs[label][item["name"]]
                               else item["main"] for item in self.repos} for label in LABELS}
            self.save(labels={label: {name: {"ref": refs[label][name] or "(main)", "commit": commits[label][name]}
                                      for name in mains} for label in LABELS})
            candidates = self.candidates(commits)
        else:
            self.new_round()
            code = self.export(mains, self.blind / "code")
            paths = self.write_plans(lambda path: prompts.plan(self.task, code, path), base_needed=False)
            plan_path.write_text(self.agree(paths, fixes=False).read_text(encoding="utf-8"), encoding="utf-8")
            self.checkpoint("first-build", plans=[plan_path])
            candidates = self.build(plan_path, mains)
        # every plan both agents approved, in order: the code follows all of them; no agreed blockers yet
        return candidates, [plan_path], None

    # ------------------------------------------------------------------- steps

    def write_plans(self, prompt, base_needed=True):
        paths = {name: self.blind / f"c{self.cycle}-plan-{n}.md" for name, n in self.numbered().items()}
        self.mine(paths)
        entry = self.begin("plan", files={name: self.rel(path) for name, path in paths.items()})
        self.ask({name: prompt(paths[name]) for name in self.names},
                 lambda name: self.plan_problem(paths[name], base_needed))
        self.end(entry, "planned", words={name: words(paths[name].read_text(encoding="utf-8")) for name in self.names})
        return paths

    def agree(self, paths, fixes):
        """Vote on the two plans until both approve one, revising from the objections between votes."""
        for number in itertools.count(1):
            if number > 1:
                revised = {name: self.blind / f"c{self.cycle}-revised-{number}-{n}.md" for name, n in self.numbered().items()}
                self.mine(revised)
                entry = self.begin("revise", vote=number, files={name: self.rel(path) for name, path in revised.items()})
                self.ask({name: prompts.revise(objections, revised[name], fixes) for name in self.names},
                         lambda name: self.plan_problem(revised[name], fixes))
                self.end(entry, "revised")
                paths = revised
            order = random.sample(self.names, 2)  # plan 1 and plan 2, at random
            # neutral copies: a voter is not shown which file it wrote
            ballots = [self.blind / f"c{self.cycle}-vote-{number}-plan-{n}.md" for n in (1, 2)]
            for ballot, name in zip(ballots, order):
                ballot.write_text(paths[name].read_text(encoding="utf-8"), encoding="utf-8")
            if ballots[0].read_text(encoding="utf-8").strip() == ballots[1].read_text(encoding="utf-8").strip():
                self.state["history"][-1]["agreed_plan"] = self.rel(ballots[0])  # the plan step that wrote them
                return self.agreed_plan(ballots[0], "the two plans are the same")
            replies = {name: self.blind / f"c{self.cycle}-vote-{number}-{n}.json" for name, n in self.numbered().items()}
            self.mine(replies)
            entry = self.begin("vote", vote=number, plans={str(n): name for n, name in enumerate(order, 1)},
                               ballots={str(n): self.rel(ballot) for n, ballot in enumerate(ballots, 1)})
            self.ask({name: prompts.vote(list(enumerate(ballots, 1)), replies[name], fixes)
                      for name in self.names}, lambda name: self.vote_problem(replies[name]))
            answers = {name: _json(replies[name]) for name in self.names}
            approvals = {name: answers[name]["approve"] for name in self.names}
            objections = {n: [answers[name]["why"][str(n)] for name in self.names if not approvals[name][str(n)]]
                          for n in (1, 2)}
            both = [n for n in (1, 2) if all(approvals[name][str(n)] for name in self.names)]
            if both:
                pick = random.choice(both)  # both approved both: neither is preferred
                self.end(entry, "agreed", approvals=approvals, how=f"both approved plan {pick}",
                         agreed_plan=self.rel(ballots[pick - 1]))
                return self.agreed_plan(ballots[pick - 1], f"both approved plan {pick} in vote {number}")
            self.end(entry, "rejected", approvals=approvals, why=objections)

    def agreed_plan(self, path, how):
        copy = self.run_dir / "plans" / f"c{self.cycle}-agreed.md"
        copy.parent.mkdir(exist_ok=True)
        copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.log(f"plan: {how}")
        return path

    def build(self, plan_path, start, base=None, fixing=None):
        """Both implement from the same commits (repo name -> commit), separately; the results are
        relabelled at random."""
        numbers = self.numbered()
        works = {name: self.export(start, self.blind / "build" / f"c{self.cycle}-{numbers[name]}") for name in self.names}
        notes = {name: self.blind / "notes" / f"c{self.cycle}-{numbers[name]}.md" for name in self.names}
        (self.blind / "notes").mkdir(exist_ok=True)
        for path in notes.values():  # a note left by an interrupted build is not this build's
            path.unlink(missing_ok=True)
        entry = self.begin("build", start=start, base=base, plan=self.rel(plan_path), fixing=fixing,
                           notes={name: self.rel(path) for name, path in notes.items()})
        self.ask({name: prompts.build(plan_path, works[name], notes[name], base, self.checks) for name in self.names},
                 lambda name: None if notes[name].exists() else "no note written")
        commits = {name: {item["name"]: self.snapshot(item["path"], works[name] / item["name"], start[item["name"]],
                                                      f"round {self.cycle} build {numbers[name]}") for item in self.repos}
                   for name in self.names}
        labels = dict(zip(random.sample(self.names, 2), LABELS))
        self.state["authors"].update({f"c{self.cycle + 1} {labels[name]}": name for name in self.names})
        self.end(entry, "built", commits=commits)
        return self.candidates({labels[name]: commits[name] for name in self.names})

    def review(self, candidates, plans, agreed, audit=()):
        """Both check every requirement and list the blockers in both candidates; each then votes on the
        other's, and a voted-down blocker goes back to its author once. Returns, per candidate, the
        blockers both agreed on, numbered, plus one for failing checks. `audit`: the candidates the
        last review found clear, which this one audits."""
        numbers = self.numbered()
        replies = {name: self.blind / f"c{self.cycle}-review-{numbers[name]}.json" for name in self.names}
        self.mine(replies)
        entry = self.begin("audit" if audit else "review",
                           candidates={label: item["commits"] for label, item in candidates.items()},
                           checks={label: item["checks"] for label, item in candidates.items()},
                           files={name: self.rel(path) for name, path in replies.items()})
        listed = lambda: [(label, candidates[label]["folder"], candidates[label]["checks"]) for label in random.sample(LABELS, 2)]
        self.ask({name: prompts.review(plans, listed(), replies[name], agreed, audit) for name in self.names},
                 lambda name: self.review_problem(replies[name], candidates))
        answers = {name: _json(replies[name]) for name in self.names}
        claims = {name: answers[name]["blockers"] for name in self.names}
        entry["requirements"] = {name: answers[name]["requirements"] for name in self.names}
        for name in self.names:
            for item in claims[name]:
                was = item.pop("id", None)  # numbers are CAV's to give; keep what a reviewer cited
                if was not in (None, "", "none"):
                    item["was"] = was
                item["by"] = name
        self.end(entry, "reviewed", blockers=[item for name in self.names for item in claims[name]])

        entry = self.begin("confirm")
        other = dict(zip(self.names, reversed(self.names)))
        lists, votes = {}, {}
        for name in self.names:
            if claims[other[name]]:
                lists[name] = self.blind / f"c{self.cycle}-blockers-{numbers[name]}.md"
                lists[name].write_text("".join(f"{n}. {blocker_line(item)}\n"
                                               for n, item in enumerate(claims[other[name]], 1)), encoding="utf-8")
                votes[name] = self.blind / f"c{self.cycle}-confirm-{numbers[name]}.json"
        self.mine(votes)
        if votes:
            self.ask({name: prompts.confirm(lists[name], votes[name]) for name in votes},
                     lambda name: self.confirm_problem(votes[name], len(claims[other[name]])))
        found = {label: [] for label in LABELS}
        for name in votes:
            answer = _json(votes[name])
            for n, item in enumerate(claims[other[name]], 1):
                item["agreed"] = answer["blocker"][str(n)]
                if not item["agreed"]:
                    item["why_not"] = answer["why"][str(n)]
                    continue
                item["id"], self.next_id = self.next_id, self.next_id + 1
                for label in LABELS:
                    if hits(item, label):
                        found[label].append(item)
        self.comeback(claims, numbers)
        for label in LABELS:  # failing checks block like an agreed blocker, without a vote
            if not candidates[label]["checks_ok"]:
                found[label].append({"candidate": label, "file": "(checks)", "problem": candidates[label]["checks"],
                                     "by": "checks", "agreed": True, "id": self.next_id})
                self.next_id += 1
        clear = [label for label in LABELS if not found[label]]
        disputed = [item for name in self.names for item in claims[name] if item.get("disputed")]
        self.end(entry, f"no blocker in {' and '.join(clear)}" if clear else "blocked",
                 agreed={label: [blocker_line(item) for item in found[label]] for label in LABELS},
                 disputed=[f"{blocker_line(item)} — kept by {item['by']}: {item['disputed']}" for item in disputed])
        if disputed:
            self.save(disputed=self.state.get("disputed", []) + entry["disputed"])
        return found

    def comeback(self, claims, numbers):
        """A blocker voted down goes back to its author once, with the reason: kept, it is disputed
        (listed for the user, not blocking); else withdrawn."""
        lists, replies = {}, {}
        for name in self.names:
            down = [item for item in claims[name] if item.get("agreed") is False]
            if down:
                lists[name] = self.blind / f"c{self.cycle}-voted-down-{numbers[name]}.md"
                lists[name].write_text("".join(f"{n}. {blocker_line(item)}\n   Not a blocker because: {item['why_not']}\n"
                                               for n, item in enumerate(down, 1)), encoding="utf-8")
                replies[name] = (self.blind / f"c{self.cycle}-comeback-{numbers[name]}.json", down)
        self.mine({name: path for name, (path, _) in replies.items()})
        if not replies:
            return
        self.ask({name: prompts.challenged(lists[name], path) for name, (path, _) in replies.items()},
                 lambda name: self.keep_problem(replies[name][0], len(replies[name][1])))
        for name, (path, down) in replies.items():
            answer = _json(path)
            for n, item in enumerate(down, 1):
                if answer["keep"][str(n)]:
                    item["disputed"] = answer["why"][str(n)]

    def finish(self, candidates, label):
        commits, branch = candidates[label]["commits"], f"cav/{self.run_id}"
        for item in self.repos:
            git("update-ref", f"refs/heads/{branch}", commits[item["name"]], cwd=item["path"])
        self.save(phase="done", outcome=f"converged: branch {branch}" + (" in each repo" if len(self.repos) > 1 else ""),
                  final={"commits": commits, "branch": branch, "candidate": label})
        self.log(f"converged: {branch} = {commits}")
        return 0

    # ------------------------------------------------------------------- helpers

    def numbered(self):
        """Each agent's file number for a step, at random, so no file name shows whose it is."""
        return dict(zip(random.sample(self.names, 2), (1, 2)))

    def agreed_file(self, what, items):
        path = self.blind / f"c{self.cycle}-agreed-{what}.md"
        unique = {item["id"]: item for item in items}.values()  # a blocker in both candidates is listed once
        path.write_text("".join(f"{blocker_line(item)}\n" for item in unique), encoding="utf-8")
        return path

    def export(self, commits, folder):
        """One plain folder per repo (repo name -> commit) inside `folder`."""
        for item in self.repos:
            export_tree(item["path"], commits[item["name"]], folder / item["name"])
        return folder

    def candidates(self, commits):
        """label -> its commits (repo name -> commit), a plain folder to read (one folder per repo), and
        how its checks went, run by CAV in a copy of its own."""
        folder = self.blind / "candidates" / f"c{self.cycle + 1}"
        out = {}
        for label in commits:
            ok, summary = self.run_checks(commits[label], f"c{self.cycle + 1}-{label}")
            out[label] = {"commits": commits[label], "folder": str(self.export(commits[label], folder / label)),
                          "checks_ok": ok, "checks": summary}
        return out

    def run_checks(self, commits, name):
        """Run each check command in a fresh copy, stopping at the first failure. The log goes to the
        blind area, where the reviewers can read it."""
        if not self.checks:
            return True, "none configured"
        work = self.export(commits, self.run_dir / "checks" / name)
        log = self.blind / f"{name}-checks.log"
        lines = []
        for command in self.checks:
            try:
                proc = subprocess.run(command, shell=True, cwd=str(work), capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=CHECK_SECONDS, creationflags=NO_WINDOW)
                code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                code, output = "timeout", f"no result after {CHECK_SECONDS} seconds"
            lines += [f"$ {command}", output[-6000:], f"exit {code}", ""]
            if code != 0:
                log.write_text("\n".join(lines), encoding="utf-8")
                self.log(f"checks {name}: {command} -> exit {code}")
                return False, f"FAILED: {command} exit {code}; log {log}"
        log.write_text("\n".join(lines), encoding="utf-8")
        return True, f"all pass ({len(self.checks)}); log {log}"

    def snapshot(self, repo, folder, parent, what):
        """Commit a plain folder on top of `parent` through a separate index: the folder never gets a .git."""
        git_dir = git("rev-parse", "--absolute-git-dir", cwd=repo)
        index = self.run_dir / f"index-{secrets.token_hex(4)}"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        where = ("--git-dir", git_dir, "--work-tree", str(folder))
        try:
            git(*where, "read-tree", parent, cwd=folder, env=env)
            git(*where, "add", "-A", cwd=folder, env=env)
            tree = git(*where, "write-tree", cwd=folder, env=env)
        finally:
            index.unlink(missing_ok=True)
        if tree == git("rev-parse", f"{parent}^{{tree}}", cwd=repo):
            return parent
        commit = git(*IDENTITY, "commit-tree", tree, "-p", parent, "-m", f"CAV {self.run_id}: {what}", cwd=repo)
        git("update-ref", f"refs/cav/{self.run_id}/{what.replace(' ', '-')}", commit, cwd=repo)  # keep it reachable
        return commit

    # ------------------------------------------------------------- answer checks

    @staticmethod
    def plan_problem(path, base_needed):
        if not path.exists():
            return "no plan file written"
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # e.g. written by Windows PowerShell or cp1252 Python: ask again, do not fail the run
            return "the plan is not UTF-8; write it as UTF-8"
        if words(text) > prompts.WORDS:
            return f"the plan has {words(text)} words; cut it to {prompts.WORDS}"
        if base_needed and plan_base(text) not in LABELS:
            return 'the plan must start with "Base: X" or "Base: Y"'
        return None

    @staticmethod
    def vote_problem(path):
        data = _json(path) or {}
        approve, why = data.get("approve"), data.get("why") or {}
        if not isinstance(approve, dict) or set(approve) != {"1", "2"} or not all(isinstance(v, bool) for v in approve.values()):
            return 'write JSON with "approve": {"1": true|false, "2": true|false}'
        for n, ok in approve.items():
            if not ok and not isinstance(why.get(n), str):
                return f'plan {n} is rejected without a "why"'
            if not ok and words(why[n]) > prompts.WHY_WORDS:
                return f'the "why" for plan {n} has {words(why[n])} words; cut it to {prompts.WHY_WORDS}'
        return None

    @staticmethod
    def review_problem(path, candidates):
        data = _json(path) or {}
        requirements = data.get("requirements")
        if not isinstance(requirements, list) or not requirements or any(
                not isinstance(item, dict) or not item.get("requirement") or not isinstance(item.get("met"), dict)
                or set(item["met"]) != set(LABELS) or not all(isinstance(v, bool) for v in item["met"].values())
                for item in requirements):
            return ('write "requirements": a list of {"requirement", "met": {"X": true|false, "Y": true|false}, '
                    '"evidence"}, one for each requirement in the agreed plans')
        blockers = data.get("blockers")
        if not isinstance(blockers, list) or any(not isinstance(item, dict) or not item.get("problem")
                                                 or item.get("candidate") not in (*LABELS, "both") for item in blockers):
            return 'write JSON with "blockers": a list of {"candidate": "X|Y|both", "file", "line", "problem", "fix"}'
        roots = [pathlib.Path(c["folder"]).resolve() for c in candidates.values()]
        for item in blockers:  # a missing file can be the blocker; a path outside the candidates cannot
            if not item.get("file") or not any(root in (root / str(item["file"])).resolve().parents for root in roots):
                return f"blocker file {item.get('file')!r} is not a path inside the candidate"
        return None

    @staticmethod
    def keep_problem(path, count):
        data = _json(path) or {}
        keep, why = data.get("keep"), data.get("why") or {}
        if not isinstance(keep, dict) or set(keep) != {str(n) for n in range(1, count + 1)} \
                or not all(isinstance(v, bool) for v in keep.values()):
            return f'write JSON with "keep": true or false for each of 1 to {count}'
        for n, kept in keep.items():
            if kept and not isinstance(why.get(n), str):
                return f'blocker {n} is kept without a "why"'
        return None

    @staticmethod
    def confirm_problem(path, count):
        data = _json(path) or {}
        votes, why = data.get("blocker"), data.get("why") or {}
        if not isinstance(votes, dict) or set(votes) != {str(n) for n in range(1, count + 1)} \
                or not all(isinstance(v, bool) for v in votes.values()):
            return f'write JSON with "blocker": true or false for each of 1 to {count}'
        for n, ok in votes.items():
            if not ok and not isinstance(why.get(n), str):
                return f'blocker {n} is voted down without a "why"'
            if not ok and words(why[n]) > prompts.WHY_WORDS:
                return f'the "why" for blocker {n} has {words(why[n])} words; cut it to {prompts.WHY_WORDS}'
        return None
