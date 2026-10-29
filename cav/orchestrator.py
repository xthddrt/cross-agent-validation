"""Cross-agent validation: two candidates, mutual evidence-backed review, checks, publication."""

import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

from . import agents as agent_mod
from . import compare
from . import review as review_mod
from .events import EventWriter
from .gitops import IDENTITY, Exchange, Repo, git, git_ok
from .prompts import retry_note, round_prompt

MAX_PATCH_BYTES = 2_000_000


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Orchestrator:
    def __init__(self, case, run_dir, push=False, thinking=False, resume=False):
        self.case = case
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.push = push
        self.thinking = thinking
        self.resume = resume
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = Exchange(self.run_dir / "exchange.git").ensure()
        self.repo = Repo(case.host_repo).attach(self.exchange, self.run_dir.name)
        self.log_path = self.run_dir / "run.log"
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_dir.name)
        self.state = {"case": case.name, "run_id": self.run_dir.name, "phase": "init",
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
            outcome = self.round(round_no)
            if outcome == "accepted":
                return self.state
            if outcome == "stop":
                break
        else:
            self.log(f"no agreement after {self.case.max_rounds} rounds", "ERROR")
        self.state["phase"] = "disagreement"
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

        Each agent starts from its own previous candidate, receives the other candidate's full
        diff, and must decide on every path where the two differ. Both sides are built by the
        same code; nothing depends on agent order.
        """
        case, names = self.case, list(self.case.agents)
        self.state.update(phase=f"r{round_no}-prepare", round=round_no)
        self.save()
        self.repo.fetch()
        previous = self.previous_results()
        phase = "implement" if (case.bootstrap == "base" and round_no == 1) else "reconcile"
        starts = {name: previous[name]["commit"] for name in names}
        # One comparison serves both agents: the differing paths are the same set from either side.
        diff = compare.compare_trees(self.exchange.path, previous[names[0]]["tree"],
                                     previous[names[1]]["tree"])
        ledger = compare.ledger(diff) if phase == "reconcile" else []
        agreed = diff["identical"] if phase == "reconcile" else []
        self.emit("round.prepared", round_no, data={"starts": starts, "phase": phase, "ledger": len(ledger)})
        self.log(f"r{round_no}: each agent starts from its own candidate "
                 f"({', '.join(f'{name} {sha[:12]}' for name, sha in starts.items())}); "
                 f"{len(ledger)} differing paths to decide")

        reviews_dir = self.run_dir / "reviews" / f"r{round_no}"
        context_dir = self.run_dir / "rounds" / f"r{round_no}"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        context_dir.mkdir(parents=True, exist_ok=True)
        worktrees, reviews, prompts, runs = {}, {}, {}, {}
        for name, spec in case.agents.items():
            other = case.other(name)
            worktrees[name] = self.prepare_worktree(self.run_dir / "wt" / f"r{round_no}" / name, starts[name])
            reviews[name] = reviews_dir / f"{name}-review.json"
            reviews[name].unlink(missing_ok=True)  # a restarted round must not accept an old review
            patch = (self.write_patch(context_dir / f"{name}-other.patch", starts[name], starts[other])
                     if ledger else None)
            # Line counts read from this agent's side: what taking the other's version would change.
            own_ledger = ledger if name == names[0] else [
                dict(item, added=item["removed"], removed=item["added"]) for item in ledger]
            prompts[name] = round_prompt(name, other, round_no, case, worktrees[name], reviews[name], {
                "phase": phase, "own_sha": starts[name], "other_sha": starts[other],
                "other_patch": patch, "other_review": self.previous_review(round_no, other),
                "own_review": self.previous_review(round_no, name), "ledger": own_ledger,
                "agreed": agreed, "history": self.history_line()})
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
        ids = [item["id"] for item in ledger]
        self.drive(round_no, runs, prompts, worktrees, reviews, ids)

        results, failed = {}, []
        for name in names:
            rev = review_mod.load(reviews[name], name, round_no, worktrees[name], ids)
            if rev is None or not rev.valid:
                reason = "no review written" if rev is None else f"invalid review: {rev.errors[0]}"
                failed.append(f"{name}: {reason}")
                self.emit("review.rejected", round_no, name, {"reason": reason})
                continue
            self.emit("review.validated", round_no, name, {"summary": rev.summary()})
            sha = self.repo.commit_all(worktrees[name], f"Reconcile round {round_no}: {name} recommendation"
                                                        f"\n\nStarted from: {starts[name]}")
            self.repo.push(sha, f"candidate/{name}/r{round_no}")
            changed = set(git("--git-dir", str(self.exchange.path), "diff", "--name-only",
                              starts[name], sha, check=False).splitlines())
            declared = {item.get("path") for item in rev.data.get("changes") or []}
            unexplained = sorted((changed & set(agreed)) - declared)
            if unexplained:
                self.emit("agent.unexplained_change", round_no, name, {"paths": unexplained[:40]})
                self.log(f"r{round_no} {name}: {len(unexplained)} undeclared change(s) to agreed "
                         f"files: {', '.join(unexplained[:4])}", "WARN")
            results[name] = {"commit": sha, "tree": self.repo.tree(sha), "blocking": rev.blocking,
                             "counts": rev.counts, "approved": rev.behavior_approved,
                             "other_approved": rev.other_approved,
                             "open_behavioural": rev.open_behavioural, "unexplained": unexplained}
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
        blockers = self.blockers(results, checks)
        self.state["history"].append({
            "round": round_no, "phase": phase, "starts": starts,
            "ledger": [{"id": item["id"], "path": item["path"]} for item in ledger],
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
            f"r{item['round']}: {item['divergence']['differing']} differing paths"
            for item in self.state.get("history", []))

    # --------------------------------------------------------------- drive/monitor

    def drive(self, round_no, runs, prompts, worktrees, reviews, required):
        """Run both agents in parallel; retry one that leaves no valid review."""

        def problem(name):
            if runs[name].state.error:
                return runs[name].state.error
            if not runs[name].state.finished:
                return "did not finish"
            rev = review_mod.load(reviews[name], name, round_no, worktrees[name], required)
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

    def run_checks(self, round_no, results, worktrees):
        """The case's checks on both candidates at once; an identical tree is checked once."""
        if not self.case.checks:
            return {name: {"ok": True, "commands": []} for name in results}
        self.state["phase"] = f"r{round_no}-checks"
        self.save()
        out_dir = self.run_dir / "checks" / f"r{round_no}"
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
                                  text=True, encoding="utf-8", errors="replace")
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

    def blockers(self, results, checks):
        """The acceptance rule: every check passes, no behavioural MUST_FIX, both agents approve
        both candidates, and no behavioural disagreement is left open."""
        names = list(self.case.agents)
        item = {
            "checks": all(check["ok"] for check in checks.values()),
            "blocking_must_fix": sum(results[name]["blocking"] for name in names),
            "must_fix_total": sum(results[name]["counts"].get("must_fix_total", 0) for name in names),
            "behaviour_approved": [results[name]["approved"] for name in names],
            "other_approved": [results[name]["other_approved"] for name in names],
            "open_behavioural": any(results[name]["open_behavioural"] for name in names),
            "unexplained_changes": sum(len(results[name]["unexplained"]) for name in names),
            "trees_equal": results[names[0]]["tree"] == results[names[1]]["tree"],
        }
        item["eligible"] = (item["checks"] and not item["blocking_must_fix"]
                            and all(item["behaviour_approved"]) and all(item["other_approved"])
                            and not item["open_behavioural"])
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
        reason = (f"fewest changes from its original branch: {changes[chosen]['lines_changed']} lines "
                  f"in {changes[chosen]['files_changed']} files (vs {others})")
        return chosen, {"reason": reason, "changes": changes}

    def finalize(self, round_no):
        """Publish exactly one of the two candidates both agents approved and whose checks passed.
        Nothing is rewritten after agreement, so nothing needs re-checking or re-approval."""
        entry = next(item for item in self.state["history"] if item["round"] == round_no)
        results = entry["results"]
        chosen, publication = self.choose_publication(results)
        commit, tree = results[chosen]["commit"], results[chosen]["tree"]
        self.emit("publication.selected", round_no, chosen, publication)
        self.state["phase"] = "converged"
        self.state["final"] = {"round": round_no, "commit": commit, "tree": tree, "pushed": False,
                               "published_from": {"agent": chosen, **publication},
                               "checks": entry["checks"][chosen]["commands"]}
        self.save()
        self.write_report(round_no)
        self.log(f"ACCEPTED in round {round_no}: {chosen}'s candidate {commit} ({publication['reason']})")
        self.emit("final.complete", round_no, data={"commit": commit, "tree": tree})
        if self.push:
            self.push_origin(commit)
            self.state["final"]["pushed"] = True
            self.save()
            self.write_report(round_no)
            self.emit("push.result", round_no, data={"ok": True, "branch": self.case.branch})
        else:
            self.log("not pushed to origin; rerun with --push to publish")
        self.emit("run.end", round_no, data={"phase": "converged"})
        return "accepted"

    def push_origin(self, commit):
        branch = self.case.branch
        if not git_ok("fetch", "origin", cwd=self.repo.path):
            raise RuntimeError("cannot fetch origin")
        if not git_ok("merge-base", "--is-ancestor", f"origin/{branch}", commit, cwd=self.repo.path):
            raise RuntimeError(f"{commit} is not a fast-forward of origin/{branch}; refusing to push")
        git("push", "origin", f"{commit}:refs/heads/{branch}", cwd=self.repo.path)
        self.log(f"pushed {commit[:12]} to origin/{branch}")

    def write_report(self, round_no):
        case, final = self.case, self.state.get("final") or {}
        entry = next(item for item in self.state["history"] if item["round"] == round_no)
        published = final.get("published_from") or {}
        folder = self.run_dir / "reviews" / f"r{round_no}"
        lines = [
            f"# Reconciliation report: {case.name}",
            "",
            f"- Run `{self.run_dir.name}`, accepted in round {round_no} of at most {case.max_rounds}; "
            f"both candidates weighed equally",
            f"- Published: {published.get('agent')}'s candidate, {published.get('reason')}",
            f"- Commit `{final.get('commit')}`, tree `{final.get('tree')}`",
            f"- Pushed to origin: {'yes' if final.get('pushed') else 'no'}",
            f"- Basis: {'task + ' if case.task_prompt.strip() else ''}checks + mutual review",
            "",
            "## Rounds",
            "",
            "| Round | Verdict | Blocking MUST_FIX | Checks | Paths differing |",
            "|---|---|---|---|---|",
        ]
        for item in self.state["history"]:
            gate = item.get("blockers") or {}
            lines.append(f"| {item['round']} | {item.get('verdict', 'failed')} "
                         f"| {gate.get('blocking_must_fix', '-')} "
                         f"| {('pass' if gate['checks'] else 'fail') if gate else '-'} "
                         f"| {(item.get('divergence') or {}).get('differing', '-')} |")
        differing = self.read_json(folder / "divergence.json").get("differing") or []
        lines += ["", "## Differences both agents accepted", ""]
        lines += [f"- `{path}`" for path in differing[:200]] or ["None: the two candidates are identical."]
        paths = {item["id"]: item["path"] for item in entry.get("ledger") or []}
        for name in case.agents:
            declined = [item for item in self.read_json(folder / f"{name}-review.json")
                        .get("carry_forward_dispositions") or []
                        if isinstance(item, dict) and item.get("status") != "retained"]
            if declined:
                lines += ["", f"## {case.other(name)}'s changes {name} declined", ""]
                lines += [f"- `{paths.get(item.get('item_id'), item.get('item_id'))}`: {item.get('why', '')}"
                          for item in declined[:200]]
        lines += ["", "## Checks on the published commit", ""]
        lines += [f"- `{c['command']}` exit {c['exit']} in {c['seconds']}s"
                  for c in final.get("checks") or []] or ["- none configured"]
        lines += ["", "Agreement and passing checks are mechanical evidence, not human approval; a green",
                  "check matrix means it builds, not that it works on real hardware.", ""]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
