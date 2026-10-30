"""Cross-agent validation: two candidates, mutual evidence-backed review, checks, publication."""

import concurrent.futures
import json
import os
import pathlib
import random
import subprocess
import sys
import threading
import time

from . import agents as agent_mod
from . import compare
from . import review as review_mod
from .events import EventWriter
from .gitops import GIT, IDENTITY, NO_WINDOW, Exchange, Repo, git, git_ok
from .prompts import PLAN_FILE, decide_prompt, retry_note, round_prompt

MAX_PATCH_BYTES = 2_000_000


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Orchestrator:
    def __init__(self, case, run_dir, push=False, thinking=False, resume=False, merge=False, run_id=None):
        self.case = case
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.push = push
        self.merge = merge  # on push: merge into origin's branch when it moved, instead of refusing
        self.thinking = thinking
        self.resume = resume
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = Exchange(self.run_dir / "exchange.git").ensure()
        # the id names this run's refs in the host repo, so two runs on one repo must not share it
        self.run_id = run_id or self.run_dir.name
        self.repo = Repo(case.host_repo).attach(self.exchange, self.run_id)
        self.log_path = self.run_dir / "run.log"
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_id)
        self.state = {"case": case.name, "run_id": self.run_id, "phase": "init",
                      "round": 0, "agents": {}, "history": []}

    # ---------------------------------------------------------------- plumbing

    def log(self, message, level="INFO"):
        line = f"{time.strftime('%H:%M:%S')} {level:<5} {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save(self):
        self.state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (self.run_dir / "state.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def emit(self, type_, round_no=None, agent=None, data=None):
        return self.events.emit(type_, round_no, agent, data)

    @staticmethod
    def read_json(path):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def prepare_worktree(self, path, rev):
        if path.exists():
            self.repo.worktree_remove(path)
        return self.repo.worktree_add(path, rev)

    # --------------------------------------------------------------- bootstrap

    def bootstrap(self):
        """Publish each agent's starting candidate (r0) to the exchange."""
        self.state["phase"] = "bootstrap"
        self.save()
        if self.case.base and not git_ok("rev-parse", "--verify", "--quiet", self.case.base,
                                         cwd=self.repo.path):
            raise RuntimeError(f"base commit {self.case.base} not found in {self.repo.path}")
        for name, spec in self.case.agents.items():
            branch = f"candidate/{name}/r0"
            if self.exchange.sha(branch):
                self.log(f"{name}: r0 already published at {self.exchange.sha(branch)[:12]}")
                continue
            if self.case.bootstrap == "base" or spec.bootstrap_ref:
                ref = self.case.base if self.case.bootstrap == "base" else spec.bootstrap_ref
                sha = self.repo.push(self.repo.sha(ref), branch)
                self.log(f"{name}: r0 = {sha[:12]} ({ref})")
                continue
            if not self.case.secondary_repo:
                raise RuntimeError(f"{name}: no bootstrap_ref and no secondary repo to snapshot")
            secondary = Repo(self.case.secondary_repo).attach(self.exchange, self.run_dir.name)
            sha, problem = self.snapshot(secondary, spec)
            if problem:
                raise RuntimeError(f"{name}: cannot snapshot {secondary.path}: {problem}")
            secondary.push(sha, branch)
            self.log(f"{name}: r0 = {sha[:12]} snapshotted from {secondary.path}")
        self.repo.fetch()
        self.emit("run.start", 0, data={"case": self.case.name,
                                        "task_present": bool(self.case.task_prompt.strip())})

    def snapshot(self, repo, spec):
        """Commit a dirty working tree without touching its index, branch or checkout.

        Works on a repo with no commits yet: the snapshot becomes a root commit.
        """
        excludes = [f":(exclude){pattern}" for pattern in spec.bootstrap_exclude]
        index = self.run_dir / f"snapshot-{spec.name}.index"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        has_head = git_ok("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo.path)
        try:
            git("read-tree", "HEAD" if has_head else "--empty", cwd=repo.path, env=env)
            git("add", "-A", "--", ".", *excludes, cwd=repo.path, env=env)
            staged = (git("diff", "--cached", "--name-only", "HEAD", cwd=repo.path, env=env) if has_head
                      else git("ls-files", "--cached", cwd=repo.path, env=env)).splitlines()
            if not staged:
                return None, "the working tree has no changes to snapshot"
            tree = git("write-tree", cwd=repo.path, env=env)
            parents = ["-p", "HEAD"] if has_head else []
            sha = git(*IDENTITY, "commit-tree", tree, *parents, "-m",
                      f"Snapshot {spec.name} candidate r0", cwd=repo.path)
            self.log(f"{spec.name}: snapshotted {len(staged)} files: "
                     f"{', '.join(staged[:6])}{' ...' if len(staged) > 6 else ''}")
            return sha, None
        finally:
            index.unlink(missing_ok=True)

    # ------------------------------------------------------------------- run

    def run(self):
        start, accepted = 1, None
        if self.resume and (self.run_dir / "state.json").exists():
            self.state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
            if self.state.get("final"):
                return self.state
            history = self.state.get("history") or []
            start = max((item["round"] for item in history), default=0) + 1
            last = next((item for item in reversed(history) if item.get("results")), None)
            accepted = last if last and last.get("converged") else None
            self.log(f"resuming at round {start}")
            self.emit("run.resume", start, data={"history": len(history)})
        self.bootstrap()
        if accepted:
            # Accepted but not yet published when the run stopped: publish it, do not start over.
            self.finalize(accepted["round"])
            return self.state
        for round_no in range(start, self.case.max_rounds + 1):
            if self.deciding(round_no):
                return self.decide(round_no)
            outcome = self.round(round_no)
            if outcome == "accepted":
                return self.state
            if outcome == "stop":
                if self.case.mode == "plan":
                    return self.decide(round_no + 1)  # a repeating plan pair goes straight to the choice
                break
        else:
            self.log(f"no agreement after {self.case.max_rounds} rounds", "ERROR")
        self.state["phase"] = "disagreement"
        self.state["summary"] = str(self.write_summary())
        self.save()
        self.emit("run.end", self.case.max_rounds, data={"phase": "disagreement"})
        return self.state

    # ------------------------------------------------------------------- round

    def previous_results(self):
        """Candidates from the last round that produced results, else the r0 candidates.

        A failed round leaves a history entry without results; resuming must not stop there.
        """
        for item in reversed(self.state.get("history") or []):
            if item.get("results"):
                return item["results"]
        out = {}
        for name in self.case.agents:
            sha = self.exchange.sha(f"candidate/{name}/r0")
            if not sha:
                raise RuntimeError(f"missing candidate/{name}/r0")
            out[name] = {"commit": sha, "tree": self.exchange.tree_of(sha)}
        return out

    def round(self, round_no):
        """Both candidates get equal standing: neither is a default the other must argue against.

        Each agent reviews both starting candidates and turns its own worktree into a new
        proposal. When both approve both starting candidates, one of those reviewed candidates
        is published; otherwise the two new proposals start the next round. An independent
        first round (from the base) reviews nothing and cannot be accepted.
        """
        case, names = self.case, list(self.case.agents)
        self.state.update(phase=f"r{round_no}-prepare", round=round_no)
        self.save()
        self.repo.fetch()
        previous = self.previous_results()
        phase = "implement" if (case.bootstrap == "base" and round_no == 1) else "reconcile"
        starts = {name: previous[name]["commit"] for name in names}
        differing = (len(compare.compare_trees(self.exchange.path, previous[names[0]]["tree"],
                                               previous[names[1]]["tree"])["differing"])
                     if phase == "reconcile" else 0)
        self.emit("round.prepared", round_no, data={"starts": starts, "phase": phase, "differing": differing})
        self.log(f"r{round_no}: each agent starts from its own candidate "
                 f"({', '.join(f'{name} {sha[:12]}' for name, sha in starts.items())}); "
                 f"{differing} paths differ")

        reviews_dir = self.run_dir / "reviews" / f"r{round_no}"
        context_dir = self.run_dir / "rounds" / f"r{round_no}"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        context_dir.mkdir(parents=True, exist_ok=True)
        worktrees, reviews, prompts, runs = {}, {}, {}, {}
        for name in names:
            worktrees[name] = self.prepare_worktree(self.run_dir / "wt" / f"r{round_no}" / name, starts[name])
        # Each candidate as it stands at the start of the round, for the other agent to read. Its
        # working tree changes while it works, and a copy inside an agent's own tree would be
        # committed as part of that agent's proposal.
        candidates = ({name: self.prepare_worktree(context_dir / "candidates" / name, starts[name])
                       for name in names} if phase == "reconcile" else {})
        start_checks = self.start_checks(round_no, starts, candidates) if candidates else {}
        for name, spec in case.agents.items():
            other = case.other(name)
            scratch = self.run_dir / "scratch" / f"r{round_no}" / name
            scratch.mkdir(parents=True, exist_ok=True)
            reviews[name] = reviews_dir / f"{name}-review.json"
            reviews[name].unlink(missing_ok=True)  # a restarted round must not accept an old review
            patch = (self.write_patch(context_dir / f"{name}-other.patch", starts[name], starts[other])
                     if differing else None)
            prompts[name] = round_prompt(name, other, round_no, case, worktrees[name], reviews[name], {
                "phase": phase, "own_sha": starts[name], "other_sha": starts[other],
                "other_patch": patch, "other_review": self.previous_review(round_no, other),
                "other_tree": str(candidates[other]) if candidates else None,
                "own_tree": str(candidates[name]) if candidates else None,
                "other_worktree": str(worktrees[other]), "scratch": str(scratch),
                "own_review": self.previous_review(round_no, name), "history": self.history_line()})
            (context_dir / f"{name}-context.md").write_text(prompts[name], encoding="utf-8")
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"r{round_no}" / f"{name}.log",
                                    title_prefix=f"cav {case.name} r{round_no}")
            agent.emit = lambda type_, agent_name, data, _r=round_no: self.emit(type_, _r, agent_name, data)
            agent.thinking = self.thinking
            agent.session_id = None
            runs[name] = agent

        self.state["phase"] = f"r{round_no}-agents"
        self.save()
        self.emit("round.start", round_no, data={"phase": phase})
        trees = candidates or None
        self.drive(round_no, runs, prompts, worktrees, reviews, trees)

        results, failed = {}, []
        for name in names:
            rev = review_mod.load(reviews[name], name, round_no, trees)
            if rev is None or not rev.valid:
                reason = "no review written" if rev is None else f"invalid review: {rev.errors[0]}"
                failed.append(f"{name}: {reason}")
                self.emit("review.rejected", round_no, name, {"reason": reason})
                continue
            self.emit("review.validated", round_no, name, {"summary": rev.summary()})
            sha = self.repo.commit_all(worktrees[name], f"Reconcile round {round_no}: {name} recommendation"
                                                        f"\n\nStarted from: {starts[name]}")
            self.repo.push(sha, f"candidate/{name}/r{round_no}")
            results[name] = {"commit": sha, "tree": self.repo.tree(sha), "blocking": rev.blocking,
                             "counts": rev.counts, "approves": rev.approvals}
            self.emit("candidate.commit", round_no, name, {"commit": sha})
        if failed:
            self.state["phase"] = f"r{round_no}-failed"
            self.state["history"].append({"round": round_no, "failed": failed})
            self.save()
            raise RuntimeError(f"round {round_no} failed: " + "; ".join(failed))

        checks = self.run_checks(round_no, results, worktrees)
        divergence = compare.compare_trees(self.exchange.path, results[names[0]]["tree"],
                                           results[names[1]]["tree"])
        (reviews_dir / "divergence.json").write_text(json.dumps(
            {"round": round_no, "commits": {name: results[name]["commit"] for name in names},
             **divergence}, indent=2), encoding="utf-8")
        self.emit("divergence.computed", round_no,
                  data={"identical": len(divergence["identical"]), "differing": len(divergence["differing"]),
                        "files": divergence["differing"][:20]})
        blockers = self.blockers(results, start_checks, phase)
        self.state["history"].append({
            "round": round_no, "phase": phase, "starts": starts, "start_checks": start_checks,
            "results": results, "checks": checks,
            "divergence": {"identical": len(divergence["identical"]),
                           "differing": len(divergence["differing"])},
            "blockers": blockers, "verdict": blockers["verdict"], "converged": blockers["eligible"],
        })
        self.state["phase"] = f"r{round_no}-review"
        self.save()
        self.emit("convergence.evaluated", round_no, data={"verdict": blockers["verdict"], "blockers": blockers})
        self.log(f"r{round_no}: {blockers['verdict']} blockers={blockers}")

        if blockers["eligible"]:
            return self.finalize(round_no)
        if self.oscillating(round_no):
            self.emit("oscillation.detected", round_no)
            self.log(f"r{round_no}: candidate pair repeated; stopping as oscillation", "ERROR")
            return "stop"
        return "next"

    def write_patch(self, target, start, other_commit):
        """The other candidate as a diff against the tree an agent starts from."""
        text = git("--git-dir", str(self.exchange.path), "diff", start, other_commit, check=False)
        target.write_text(text[:MAX_PATCH_BYTES], encoding="utf-8")
        return str(target)

    def previous_review(self, round_no, name):
        path = self.run_dir / "reviews" / f"r{round_no - 1}" / f"{name}-review.json"
        return str(path) if round_no > 1 and path.exists() else None

    def history_line(self):
        return " -> ".join(
            f"r{item['round']}: failed" if item.get("failed") else
            f"r{item['round']}: forced choice" if item.get("verdict") == "forced" else
            f"r{item['round']}: {item['divergence']['differing']} differing paths"
            for item in self.state.get("history", []))

    # --------------------------------------------------------------- drive/monitor

    def drive(self, round_no, runs, prompts, worktrees, reviews, trees=None, check=None):
        """Run both agents in parallel; retry one that leaves no valid review."""

        def problem(name):
            if runs[name].state.error:
                return runs[name].state.error
            if not runs[name].state.finished:
                return "did not finish"
            if check:
                return check(name)
            rev = review_mod.load(reviews[name], name, round_no, trees)
            if rev is None:
                return "no review file"
            return None if rev.valid else f"invalid review: {rev.errors[0]}"

        for attempt in range(self.case.retries + 1):
            pending = [name for name in runs if problem(name)]
            if not pending:
                return
            if attempt:
                self.log(f"r{round_no} retry {attempt} for: {', '.join(pending)}", "WARN")
                self.emit("agent.retry", round_no, None, {"agents": pending, "attempt": attempt})
            threads = []
            for name in pending:
                note = retry_note(attempt, problem(name)) if attempt else ""
                runs[name].reset()
                thread = threading.Thread(target=self._worker, daemon=True, name=f"agent-{name}",
                                          args=(runs[name], prompts[name] + note, worktrees[name]))
                thread.start()
                threads.append((name, thread))
            self.monitor(round_no, runs, pending)
            for name, thread in threads:
                thread.join(timeout=30)
                if thread.is_alive():
                    # Never start a second agent on a worktree another one is still editing.
                    self.log(f"r{round_no} {name}: worker still exiting; not retrying it", "ERROR")
                    runs[name].state.error = runs[name].state.error or "worker did not exit"
                elif problem(name):
                    runs[name].state.error = runs[name].state.error or problem(name)

    def _worker(self, agent, prompt, worktree):
        try:
            agent.run(prompt, worktree)
        except Exception as error:  # surface adapter errors as a failed run, not a crash
            agent.state.error = str(error)
            agent.state.finished = True
            agent.state.touch("failed")

    def monitor(self, round_no, runs, pending):
        """Publish live progress; abort an agent blocked on a human or past the round timeout."""
        case = self.case
        deadline = time.time() + case.round_seconds
        reported, warned, blocked = set(), set(), set()
        while True:
            for name in pending:
                state = runs[name].state
                self.state["agents"][name] = {
                    "state": "done" if state.finished else "running", "detail": state.detail,
                    "elapsed": int(state.elapsed), "idle": int(state.idle),
                    "messages": state.messages, "error": state.error}
                if state.finished:
                    if name not in reported:
                        reported.add(name)
                        self.log(f"r{round_no} {name}: {'failed: ' + state.error if state.error else 'finished'}"
                                 f" after {clock(state.elapsed)}", "ERROR" if state.error else "INFO")
                    continue
                if state.detail.startswith("waiting:") and name not in blocked:
                    blocked.add(name)  # once: the agent thread can take a poll or two to notice
                    self.log(f"r{round_no} {name}: blocked on a human interaction ({state.detail}); aborting",
                             "ERROR")
                    state.error = f"blocked: {state.detail}"
                    self.emit("agent.blocked", round_no, name, {"detail": state.detail})
                    runs[name].abort()
                if state.idle > case.stall_seconds and name not in warned:
                    warned.add(name)
                    self.log(f"r{round_no} {name}: no activity for {clock(state.idle)}", "WARN")
            self.save()
            if len(reported) == len(pending):
                return
            if time.time() > deadline:
                self.log(f"r{round_no}: round timeout {clock(case.round_seconds)} reached", "ERROR")
                for name in pending:
                    if not runs[name].state.finished:
                        runs[name].abort()
                        runs[name].state.error = "round timeout"
                        runs[name].state.finished = True
            time.sleep(2)

    # ------------------------------------------------------------------- checks

    def start_checks(self, round_no, starts, candidates):
        """Checks of the candidates under review: from the round that produced them, else run now
        (the original branches a run starts from were never checked)."""
        previous = next((item for item in reversed(self.state["history"]) if item.get("results")), None)
        if previous and all(previous["results"][name]["commit"] == starts[name] for name in starts):
            return previous["checks"]
        trees = {name: {"tree": self.exchange.tree_of(sha)} for name, sha in starts.items()}
        return self.run_checks(round_no, trees, candidates, folder=f"r{round_no}-start")

    def run_checks(self, round_no, results, worktrees, folder=None):
        """The case's checks on both candidates at once; an identical tree is checked once."""
        if not self.case.checks:
            return {name: {"ok": True, "commands": []} for name in results}
        self.state["phase"] = f"r{round_no}-checks"
        self.save()
        out_dir = self.run_dir / "checks" / (folder or f"r{round_no}")
        out_dir.mkdir(parents=True, exist_ok=True)
        runners = {}
        for name, result in results.items():
            runners.setdefault(result["tree"], name)
        with concurrent.futures.ThreadPoolExecutor(len(runners)) as pool:
            futures = {tree: pool.submit(self.execute_checks, round_no, name, worktrees[name])
                       for tree, name in runners.items()}
        report = {}
        for name, result in results.items():
            report[name] = dict(futures[result["tree"]].result(), agent=name)
            (out_dir / f"{name}.json").write_text(json.dumps(report[name], indent=2)[:200000],
                                                  encoding="utf-8")
        return report

    def execute_checks(self, round_no, name, worktree):
        commands = []
        for command in self.case.checks:
            # {python} is quoted for paths with spaces, and the whole line goes through the shell:
            # passing a pre-quoted string as a list argument to cmd /c mangles the quotes.
            rendered = command.replace("{python}", f'"{sys.executable}"')
            started = time.time()
            self.emit("check.start", round_no, name, {"command": command})
            proc = subprocess.run(rendered, shell=True, cwd=str(worktree), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            seconds = int(time.time() - started)
            commands.append({"command": command, "exit": proc.returncode, "seconds": seconds,
                             "tail": (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]})
            self.emit("check.result", round_no, name,
                      {"command": command, "exit": proc.returncode, "seconds": seconds})
            self.log(f"checks {name}: {command} -> exit {proc.returncode} in {seconds}s",
                     "ERROR" if proc.returncode else "INFO")
            if proc.returncode:
                break
        ok = len(commands) == len(self.case.checks) and all(c["exit"] == 0 for c in commands)
        return {"agent": name, "ok": ok, "commands": commands}

    # ----------------------------------------------------------------- verdict

    def blockers(self, results, start_checks, phase):
        """The acceptance rule: both agents approve both starting candidates (approval is withheld
        only with a MUST_FIX) and those candidates passed the checks. An independent round has
        reviewed nothing, so it is never accepted."""
        names = list(self.case.agents)
        item = {"blocking_must_fix": sum(results[name]["blocking"] for name in names),
                "approvals": {name: results[name]["approves"] for name in names}}
        if phase == "implement":
            return dict(item, checks=None, eligible=False, verdict="independent")
        item["checks"] = all(check["ok"] for check in start_checks.values())
        if self.case.mode == "plan":
            # one plan both agents approve is enough: approving the other's plan ends the stage
            item["agreed_on"] = [name for name in names if all(results[r]["approves"].get(name) for r in names)]
            item["eligible"] = item["checks"] and bool(item["agreed_on"])
        else:
            item["eligible"] = item["checks"] and all(all(results[name]["approves"].values()) for name in names)
        item["verdict"] = "accepted" if item["eligible"] else "diverged"
        return item

    def oscillating(self, round_no):
        """The pair repeated after at least one intervening round: A/B/A/B, not A/B/A."""
        seen = {}
        for item in self.state["history"]:
            results = item.get("results") or {}
            if len(results) == len(self.case.agents):
                pair = tuple(results[name]["tree"] for name in self.case.agents)
                seen.setdefault(pair, []).append(item["round"])
        current = self.state["history"][-1]["results"]
        pair = tuple(current[name]["tree"] for name in self.case.agents)
        return any(item <= round_no - 2 for item in seen.get(pair, []))

    # -------------------------------------------------------------- publication

    def choose_publication(self, results):
        """Both agents approved both candidates; publish the one that moved least from its own
        original branch (r0), measured in changed lines, then changed files.

        Neither agent is a default. The last tie-break is the tree hash: arbitrary, but it does
        not depend on agent order, and when the trees are identical the choice does not matter.
        """
        changes = {}
        for name, result in results.items():
            original = self.exchange.sha(f"candidate/{name}/r0")
            out = git("--git-dir", str(self.exchange.path), "diff", "--numstat", "--no-renames", "-z",
                      original, result["commit"], check=False)
            lines = files = 0
            for record in out.split("\0"):
                parts = record.split("\t")
                if len(parts) >= 3:
                    files += 1
                    lines += sum(int(part) for part in parts[:2] if part.isdigit())
            changes[name] = {"lines_changed": lines, "files_changed": files}
        chosen = min(results, key=lambda name: (changes[name]["lines_changed"],
                                                changes[name]["files_changed"], results[name]["tree"]))
        others = ", ".join(f"{name} {item['lines_changed']}" for name, item in changes.items()
                           if name != chosen)
        if not others:
            return chosen, {"reason": "the only candidate both agents approved", "changes": changes}
        reason = (f"fewest changes from its original branch: {changes[chosen]['lines_changed']} lines "
                  f"in {changes[chosen]['files_changed']} files (vs {others})")
        return chosen, {"reason": reason, "changes": changes}

    def finalize(self, round_no):
        """Publish one of the two starting candidates both agents approved and whose checks passed:
        reviewed code, not a proposal written after the reviews. Nothing is rewritten after
        agreement, so nothing needs re-checking or re-approval."""
        entry = next(item for item in self.state["history"] if item["round"] == round_no)
        agreed = (entry.get("blockers") or {}).get("agreed_on") or list(entry["starts"])
        approved = {name: {"commit": sha, "tree": self.exchange.tree_of(sha)}
                    for name, sha in entry["starts"].items() if name in agreed}
        chosen, publication = self.choose_publication(approved)
        commit, tree = approved[chosen]["commit"], approved[chosen]["tree"]
        self.emit("publication.selected", round_no, chosen, publication)
        self.state["phase"] = "converged"
        self.state["final"] = {"round": round_no, "commit": commit, "tree": tree, "pushed": False,
                               "published_from": {"agent": chosen, **publication},
                               "checks": entry["start_checks"][chosen]["commands"]}
        self.save()
        self.write_report(round_no)
        self.log(f"ACCEPTED in round {round_no}: {chosen}'s candidate {commit} ({publication['reason']})")
        self.emit("final.complete", round_no, data={"commit": commit, "tree": tree})
        if self.push:
            pushed = self.push_origin(commit)
            self.state["final"]["pushed"] = True
            if pushed != commit:
                self.state["final"]["merge_commit"] = pushed
            self.save()
            self.write_report(round_no)
            self.emit("push.result", round_no, data={"ok": True, "branch": self.case.branch})
        else:
            self.log("not pushed to origin; rerun with --push to publish")
        self.emit("run.end", round_no, data={"phase": "converged"})
        return "accepted"

    # ------------------------------------------------------------ forced choice

    def deciding(self, round_no):
        """A plan stage's last round decides instead of reviewing again, so it always ends in one plan."""
        return self.case.mode == "plan" and round_no == self.case.max_rounds and round_no > 1

    def must_fix_against(self, round_no, candidate):
        """The MUST_FIX findings against a candidate in a round's reviews."""
        found = []
        for reviewer in self.case.agents:
            data = self.read_json(self.run_dir / "reviews" / f"r{round_no}" / f"{reviewer}-review.json")
            found += [{"summary": item.get("summary", ""), "fix": item.get("fix", ""), "from": reviewer,
                       "where": f"{item.get('path')}:{item.get('line')}"}
                      for item in data.get("findings") or []
                      if isinstance(item, dict) and item.get("severity") == "MUST_FIX"
                      and item.get("candidate") == candidate]
        return found

    def decide(self, round_no):
        """No plan has both approvals: each agent, in a fresh session that sees only the task and the
        two plans (Plan 1 and 2, random order, no authors), must choose one. The same choice wins; a
        split goes to the plan with fewer open MUST_FIX findings in the last reviews; a tie to a
        recorded coin flip. The defects listed against the winner travel with it."""
        case, names = self.case, list(self.case.agents)
        self.state.update(phase=f"r{round_no}-prepare", round=round_no)
        self.save()
        previous = self.previous_results()
        starts = {name: previous[name]["commit"] for name in names}
        order = random.sample(names, len(names))  # order[0] is Plan 1
        context_dir = self.run_dir / "rounds" / f"r{round_no}"
        reviews_dir = self.run_dir / "reviews" / f"r{round_no}"
        context_dir.mkdir(parents=True, exist_ok=True)
        reviews_dir.mkdir(parents=True, exist_ok=True)
        plans = {}
        for number, name in enumerate(order, 1):
            text = git("--git-dir", str(self.exchange.path), "show", f"{starts[name]}:{PLAN_FILE}", check=False)
            path = context_dir / f"plan-{number}.md"
            path.write_text(text + "\n", encoding="utf-8")
            plans[number] = str(path)
        self.emit("round.prepared", round_no, data={"starts": starts, "phase": "decide", "differing": 0})
        self.log(f"r{round_no}: no plan has both approvals; each agent chooses between them blind")
        runs, prompts, places, decisions = {}, {}, {}, {}
        for name, spec in case.agents.items():
            places[name] = self.run_dir / "scratch" / f"r{round_no}" / name
            places[name].mkdir(parents=True, exist_ok=True)
            decisions[name] = reviews_dir / f"{name}-review.json"
            decisions[name].unlink(missing_ok=True)
            prompts[name] = decide_prompt(name, round_no, case, plans, decisions[name])
            (context_dir / f"{name}-context.md").write_text(prompts[name], encoding="utf-8")
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"r{round_no}" / f"{name}.log",
                                    title_prefix=f"cav {case.name} r{round_no}")
            agent.emit = lambda type_, agent_name, data, _r=round_no: self.emit(type_, _r, agent_name, data)
            agent.thinking = self.thinking
            agent.session_id = None
            runs[name] = agent
        self.state["phase"] = f"r{round_no}-agents"
        self.save()
        self.emit("round.start", round_no, data={"phase": "decide"})
        self.drive(round_no, runs, prompts, places, decisions,
                   check=lambda name: review_mod.load_decision(decisions[name], name, round_no)[1])

        picks, failed = {}, []
        for name in names:
            data, problem = review_mod.load_decision(decisions[name], name, round_no)
            if problem:
                failed.append(f"{name}: {problem}")
                self.emit("review.rejected", round_no, name, {"reason": problem})
            else:
                picks[name] = data
                self.emit("review.validated", round_no, name, {"summary": f"chose Plan {data['choice']}"})
        if failed:
            self.state["phase"] = f"r{round_no}-failed"
            self.state["history"].append({"round": round_no, "failed": failed})
            self.save()
            raise RuntimeError(f"round {round_no} failed: " + "; ".join(failed))

        chosen = {name: order[picks[name]["choice"] - 1] for name in names}
        if len(set(chosen.values())) == 1:
            winner, method = chosen[names[0]], "both chose it"
        else:
            open_fix = {candidate: len(self.must_fix_against(round_no - 1, candidate)) for candidate in names}
            fewer = sorted(names, key=lambda candidate: open_fix[candidate])
            if open_fix[fewer[0]] != open_fix[fewer[1]]:
                winner = fewer[0]
                method = f"split; fewer open MUST_FIX ({open_fix[fewer[0]]} vs {open_fix[fewer[1]]})"
            else:
                winner = random.choice(names)
                method = f"split, tied on open MUST_FIX ({open_fix[fewer[0]]} each); coin flip"
        defects = [dict(item, **{"from": name}) for name in names if chosen[name] == winner
                   for item in picks[name].get("defects") or []]
        defects += self.must_fix_against(round_no - 1, winner)
        decision = {"plans": {str(number): name for number, name in enumerate(order, 1)},
                    "choices": {name: picks[name]["choice"] for name in names},
                    "winner": winner, "method": method, "defects": defects}
        blockers = {"verdict": "forced", "eligible": True, "checks": None, "blocking_must_fix": 0,
                    "approvals": {}, "decision": decision}
        self.state["history"].append({"round": round_no, "phase": "decide", "starts": starts,
                                      "decision": decision, "blockers": blockers, "verdict": "forced",
                                      "converged": True})
        self.emit("convergence.evaluated", round_no, data={"verdict": "forced", "blockers": blockers})
        self.log(f"r{round_no}: forced choice: {winner}'s plan ({method}); {len(defects)} defects carried")
        commit = starts[winner]
        self.state["phase"] = "converged"
        self.state["final"] = {"round": round_no, "commit": commit, "tree": self.exchange.tree_of(commit),
                               "pushed": False, "published_from": {"agent": winner, "reason": f"forced choice: {method}"},
                               "checks": [], "forced": decision}
        self.save()
        self.write_report(round_no)
        self.emit("publication.selected", round_no, winner, {"reason": f"forced choice: {method}"})
        self.emit("final.complete", round_no, data={"commit": commit, "tree": self.state["final"]["tree"]})
        self.emit("run.end", round_no, data={"phase": "converged"})
        return self.state

    def push_origin(self, commit):
        branch = self.case.branch
        if not git_ok("fetch", "origin", cwd=self.repo.path):
            raise RuntimeError("cannot fetch origin")
        target = commit
        if not git_ok("merge-base", "--is-ancestor", f"origin/{branch}", commit, cwd=self.repo.path):
            if not self.merge:
                raise RuntimeError(f"{commit} is not a fast-forward of origin/{branch}; refusing to push")
            target = self.merge_commit(commit, f"origin/{branch}")
        git("push", "origin", f"{target}:refs/heads/{branch}", cwd=self.repo.path)
        self.log(f"pushed {target[:12]} to origin/{branch}")
        return target

    def merge_commit(self, commit, upstream):
        """origin moved during the run: merge the agreed commit into it, or refuse on a conflict."""
        proc = subprocess.run([GIT, "merge-tree", "--write-tree", "--name-only", upstream, commit],
                              cwd=str(self.repo.path), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        lines = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not lines:
            conflicts = ", ".join(line for line in lines[1:] if line.strip())[:500]
            raise RuntimeError(f"merging into {upstream} conflicts ({conflicts or proc.stderr.strip()}); not pushed")
        sha = git(*IDENTITY, "commit-tree", lines[0], "-p", self.repo.sha(upstream), "-p", commit,
                  "-m", f"Merge CAV run {self.run_id}", cwd=self.repo.path)
        self.log(f"{upstream} moved during the run; merged as {sha[:12]}")
        return sha

    def write_report(self, round_no):
        case, final = self.case, self.state.get("final") or {}
        published = final.get("published_from") or {}
        entry = next((item for item in self.state["history"] if item["round"] == round_no), {})
        starts = entry.get("starts") or {}
        lines = [
            f"# Reconciliation report: {case.name}",
            "",
            f"- Run `{self.run_dir.name}`, accepted in round {round_no} of at most {case.max_rounds}: "
            f"both agents approved both candidates the round started from",
            f"- Published: {published.get('agent')}'s candidate, {published.get('reason')}",
            f"- Commit `{final.get('commit')}`, tree `{final.get('tree')}`",
            f"- Pushed to origin: {'yes' if final.get('pushed') else 'no'}",
            "",
            "## Rounds",
            "",
            "| Round | Verdict | MUST_FIX | Start candidates' checks | Paths differing after |",
            "|---|---|---|---|---|",
        ]
        for item in self.state["history"]:
            gate = item.get("blockers") or {}
            checks = gate.get("checks")
            lines.append(f"| {item['round']} | {item.get('verdict', 'failed')} "
                         f"| {gate.get('blocking_must_fix', '-')} "
                         f"| {'-' if checks is None else 'pass' if checks else 'fail'} "
                         f"| {(item.get('divergence') or {}).get('differing', '-')} |")
        if len(starts) == 2:
            names = list(starts)
            differing = compare.compare_trees(self.exchange.path, self.exchange.tree_of(starts[names[0]]),
                                              self.exchange.tree_of(starts[names[1]]))["differing"]
            lines += ["", "## Where the two approved candidates differ", ""]
            lines += [f"- `{path}`" for path in differing[:200]] or ["None: they are identical."]
        folder = self.run_dir / "reviews" / f"r{round_no}"
        for name in case.agents:
            notes = self.read_json(folder / f"{name}-review.json").get("notes")
            if notes:
                lines += ["", f"## {name}'s notes", "", str(notes)]
        lines += ["", "## Checks on the published commit", ""]
        lines += [f"- `{c['command']}` exit {c['exit']} in {c['seconds']}s"
                  for c in final.get("checks") or []] or ["- none configured"]
        lines += ["", "Agreement and passing checks are mechanical evidence, not human approval; a green",
                  "check matrix means it builds, not that it works on real hardware.", ""]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    def write_summary(self):
        """No agreement: how far apart the two candidates ended, from the reviews and the trees."""
        case, names = self.case, list(self.case.agents)
        history = [item for item in self.state["history"] if item.get("results")]
        lines = [f"# No agreement: {case.name}", ""]
        if not history:
            lines.append("No round produced two candidates.")
        else:
            last = history[-1]
            folder = self.run_dir / "reviews" / f"r{last['round']}"
            mark = lambda value: "yes" if value else "no"
            lines += [f"- Stopped after round {last['round']} of at most {case.max_rounds}",
                      f"- Last proposals: " + ", ".join(f"{name} `{last['results'][name]['commit'][:12]}`"
                                                        for name in names), "",
                      "## Trend", "",
                      "Approvals are of the candidates each round started from.", "",
                      "| Round | Paths differing after | MUST_FIX | " + " | ".join(
                          f"{name} approves {' / '.join(names)}" for name in names) + " |",
                      "|---|---|---|" + "---|" * len(names)]
            for item in history:
                gate = item.get("blockers") or {}
                approvals = item.get("results") or {}
                cells = " | ".join(" / ".join(mark((approvals[reviewer].get("approves") or {}).get(name))
                                              if approvals[reviewer].get("approves") else "-" for name in names)
                                   for reviewer in names)
                lines.append(f"| {item['round']} | {item['divergence']['differing']} "
                             f"| {gate.get('blocking_must_fix', '-')} | {cells} |")
            divergence = self.read_json(folder / "divergence.json")
            stats = divergence.get("stats") or {}
            lines += ["", "## Files where the last two proposals differ", ""]
            lines += [f"- `{path}` +{stats.get(path, {}).get('added', 0)} -{stats.get(path, {}).get('removed', 0)}"
                      for path in (divergence.get("differing") or [])[:200]] or ["None: the trees are identical."]
            for name in names:
                data = self.read_json(folder / f"{name}-review.json")
                blocking = [item for item in data.get("findings") or []
                            if isinstance(item, dict) and item.get("severity") == "MUST_FIX"]
                lines += ["", f"## {name}'s last review", ""]
                lines += [f"- MUST_FIX on {item.get('candidate')}'s candidate [{item.get('id')}] "
                          f"`{item.get('path')}:{item.get('line')}` {item.get('summary', '')} "
                          f"(fix: {item.get('fix', '')})" for item in blocking]
                if data.get("notes"):
                    lines.append(f"- Notes: {data['notes']}")
                if not blocking and not data.get("notes"):
                    lines.append("- Nothing blocking was recorded.")
        path = self.run_dir / "SUMMARY.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.emit("summary.written", self.state.get("round"), data={"path": str(path)})
        return path
