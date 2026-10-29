"""Round loop: baseline, context, agents, checks, divergence, verdict, finalization."""

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

from . import agents as agent_mod
from . import compare
from . import review as review_mod
from .events import EventWriter
from .gitops import IDENTITY, Exchange, Repo, git, git_ok
from .prompts import confirm_prompt, retry_note, round_prompt

MUST_FIX_RE = review_mod.MUST_FIX_RE
parse_must_fix = review_mod.parse_must_fix
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "REVIEW_TEMPLATE.md"
MAX_PATCH_BYTES = 2_000_000


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Orchestrator:
    def __init__(self, case, run_dir, push=False, thinking=False, strict=None, resume=False):
        self.case = case
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.push = push
        self.thinking = thinking
        self.strict = (case.convergence == "strict") if strict is None else bool(strict)
        self.resume = resume
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = Exchange(self.run_dir / "exchange.git").ensure()
        self.repo = Repo(case.host_repo).attach(self.exchange, self.run_dir.name)
        self.log_path = self.run_dir / "run.log"
        self.events = EventWriter(self.run_dir / "events.jsonl", self.run_dir.name)
        self.state = {
            "case": case.name,
            "run_id": self.run_dir.name,
            "phase": "init",
            "round": 0,
            "agents": {},
            "history": [],
        }
        self._pairs = {}
        self._divergence_files = {}
        self.agents = {}

    # ---------------------------------------------------------------- plumbing

    def log(self, message, level="INFO", echo=True):
        line = f"{time.strftime('%H:%M:%S')} {level:<5} {message}"
        if echo:
            print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save(self):
        self.state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (self.run_dir / "state.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    @staticmethod
    def read_json(path):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def rel(self, path):
        try:
            return str(pathlib.Path(path).resolve().relative_to(self.run_dir))
        except ValueError:
            return str(path)

    def emit(self, type_, round_no=None, agent=None, data=None):
        return self.events.emit(type_, round_no, agent, data)

    def prepare_worktree(self, path, rev):
        path = pathlib.Path(path)
        if path.exists():
            self.repo.worktree_remove(path)
        self.repo.worktree_add(path, rev)
        return path

    # --------------------------------------------------------------- bootstrap

    def write_task(self):
        """Immutable task snapshot: written once, never rewritten by a later round or edit."""
        target = self.run_dir / "TASK.md"
        if target.exists():
            return target
        case = self.case
        lines = ["# Task", "", "## Original objective", "",
                 (case.task_prompt or "").strip() or "(blank - no original prompt was configured)", ""]
        if case.task_acceptance:
            lines += ["## Acceptance criteria", ""]
            lines += [f"- {item}" for item in case.task_acceptance]
            lines.append("")
        if case.base_invariants:
            lines += ["## Base behaviour invariants", ""]
            lines += [f"- {item}" for item in case.base_invariants]
            lines.append("")
        for path in (case.task or {}).get("context_files") or []:
            try:
                text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines += [f"## Context: {pathlib.Path(path).name}", "", text[:20000], ""]
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    def bootstrap(self):
        self.state["phase"] = "bootstrap"
        self.save()
        self.write_task()
        if self.case.base and not git_ok("rev-parse", "--verify", "--quiet", self.case.base,
                                         cwd=self.repo.path):
            raise RuntimeError(f"base commit {self.case.base} not found in {self.repo.path}")

        for name, spec in self.case.agents.items():
            branch = f"candidate/{name}/r0"
            if self.exchange.sha(branch):
                self.log(f"{name}: r0 already published at {self.exchange.sha(branch)[:12]}")
                continue
            if self.case.bootstrap == "base":
                sha = self.repo.sha(self.case.base)
                self.repo.push(sha, branch)
                self.log(f"{name}: r0 = {sha[:12]} (base commit)")
                self.emit("candidate.commit", 0, name, {"commit": sha, "branch": branch})
                continue
            if spec.bootstrap_ref:
                sha = self.repo.sha(spec.bootstrap_ref)
                self.repo.push(sha, branch)
                self.log(f"{name}: r0 = {sha[:12]} ({spec.bootstrap_ref})")
                self.emit("candidate.commit", 0, name, {"commit": sha, "branch": branch})
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
        self.emit("run.start", 0, data={"case": self.case.name, "convergence": self.case.convergence,
                                        "canonical": self.case.canonical(),
                                        "task_present": bool(self.case.task_prompt.strip())})

    def snapshot(self, repo, spec):
        """Commit a dirty working tree without touching its index, branch or checkout.

        Works on a repo with no commits yet: the snapshot becomes a root commit.
        """
        excludes = [f":(exclude){pattern}" for pattern in spec.bootstrap_exclude]
        index = self.run_dir / f"snapshot-{spec.name}.index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index)
        has_head = git_ok("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo.path)
        try:
            if has_head:
                git("read-tree", "HEAD", cwd=repo.path, env=env)
            else:
                git("read-tree", "--empty", cwd=repo.path, env=env)
            git("add", "-A", "--", ".", *excludes, cwd=repo.path, env=env)
            if has_head:
                staged = git("diff", "--cached", "--name-only", "HEAD", cwd=repo.path, env=env)
            else:
                staged = git("ls-files", "--cached", cwd=repo.path, env=env)
            if not staged:
                return None, "the working tree has no changes to snapshot"
            tree = git("write-tree", cwd=repo.path, env=env)
            parents = ["-p", "HEAD"] if has_head else []
            sha = git(*IDENTITY, "commit-tree", tree, *parents, "-m",
                      f"Snapshot {spec.name} candidate r0", cwd=repo.path)
            self.log(f"{spec.name}: snapshotted {len(staged.splitlines())} files: "
                     f"{', '.join(staged.splitlines()[:6])}{' ...' if len(staged.splitlines()) > 6 else ''}")
            return sha, None
        finally:
            index.unlink(missing_ok=True)

    # ------------------------------------------------------------------- run

    def run(self):
        start = 1
        accepted = None
        if self.resume and (self.run_dir / "state.json").exists():
            self.state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
            if self.state.get("final"):
                return self.state
            rounds = [item["round"] for item in self.state.get("history", [])]
            start = (max(rounds) + 1) if rounds else 1
            last = next((item for item in reversed(self.state.get("history") or [])
                         if item.get("results")), None)
            if last and last.get("converged") and not last.get("finalization"):
                accepted = last
            self.log(f"resuming at round {start}")
            self.emit("run.resume", start, data={"history": len(rounds)})
        self.bootstrap()
        if accepted:
            # The round was accepted but its finalization did not finish: finish it here instead
            # of advancing past it and letting the next round turn an accepted result into a
            # disagreement.
            names = list(self.case.agents)
            results = accepted["results"]
            self._divergence_files = compare.compare_trees(
                self.exchange.path, results[names[0]]["tree"], results[names[1]]["tree"],
                self.case.strict_paths)["files"]
            if self.finalize(accepted["round"], accepted["verdict"]) == "accepted":
                return self.state
        for round_no in range(start, self.case.max_rounds + 1):
            outcome = self.round(round_no)
            if outcome == "accepted":
                return self.state
            if outcome == "stop":
                break
        if self.state.get("phase") != "converged":
            self.log(f"rounds exhausted without agreement after {self.case.max_rounds}", "ERROR")
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
                return json.loads(json.dumps(item["results"]))
        out = {}
        for name in self.case.agents:
            sha = self.exchange.sha(f"candidate/{name}/r0")
            if not sha:
                raise RuntimeError(f"missing candidate/{name}/r0")
            out[name] = {"commit": sha, "tree": self.exchange.tree_of(sha), "checks_ok": None}
        return out

    def choose_baseline(self, previous):
        """One whole candidate, never a per-file mixture: cross-file consistency depends on it.

        `checks_ok` is only known from round 2 on, so round 1 uses the canonical candidate.
        """
        names = list(self.case.agents)
        passing = [name for name in names if previous.get(name, {}).get("checks_ok") is True]
        if len(passing) == 1:
            return previous[passing[0]]["commit"], f"only {passing[0]} passed every check", passing[0]
        owner = self.case.canonical()
        return previous[owner]["commit"], f"canonical candidate {owner}", owner

    def round(self, round_no):
        case = self.case
        self.state["phase"] = f"r{round_no}-prepare"
        self.state["round"] = round_no
        self.save()
        self.repo.fetch()
        previous = self.previous_results()
        baseline, reason, owner = self.choose_baseline(previous)
        phase = "implement" if (case.bootstrap == "base" and round_no == 1) else "reconcile"
        self.emit("baseline.selected", round_no,
                  data={"commit": baseline, "reason": reason, "owner": owner})
        self.log(f"r{round_no}: baseline {baseline[:12]} ({reason})")

        worktrees = {}
        for name in case.agents:
            path = self.prepare_worktree(self.run_dir / "wt" / f"r{round_no}" / name, baseline)
            worktrees[name] = {"path": path}

        ledger = {"items": [], "total": 0, "omitted": 0}
        at_risk = next((name for name in case.agents if name != owner), None)
        if phase == "reconcile" and case.carry_forward_limit > 0 and at_risk:
            ledger = compare.carry_forward(self.exchange.path, previous[at_risk]["tree"],
                                           self.repo.tree(baseline), case.strict_paths,
                                           case.carry_forward_limit)
        ledger_ids = [item["id"] for item in ledger["items"]]
        self.emit("carry_forward.computed", round_no,
                  data={"items": len(ledger_ids), "total": ledger.get("total", 0),
                        "omitted": ledger.get("omitted", 0), "owner": owner})

        reviews_dir = self.run_dir / "reviews" / f"r{round_no}"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        context_dir = self.run_dir / "rounds" / f"r{round_no}"
        context_dir.mkdir(parents=True, exist_ok=True)
        (reviews_dir / "carry-forward.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        paths = {name: {"md": reviews_dir / f"{name}-review.md",
                        "json": reviews_dir / f"{name}-review.json"} for name in case.agents}
        self.clear_reviews(paths)

        agreed = self.agreed_paths(previous)
        non_baseline = next((name for name in case.agents if name != owner), None) if owner else None
        delta_patch = None
        if phase == "reconcile" and non_baseline and non_baseline in previous:
            delta_patch = self.write_patch(round_no, non_baseline,
                                           previous[non_baseline]["commit"], baseline)
        prompts = {}
        for name in case.agents:
            other = case.other(name)
            context = {
                "phase": phase,
                "baseline": baseline,
                "baseline_reason": reason,
                "own_sha": previous.get(name, {}).get("commit", "?"),
                "other_sha": previous.get(other, {}).get("commit", "?"),
                "delta_agent": non_baseline or "(none)",
                "delta_patch": str(delta_patch) if delta_patch else "(none)",
                "delta_review": (self.previous_review_path(round_no, non_baseline)
                                 if non_baseline else "(none)"),
                "own_review": self.previous_review_path(round_no, name),
                "ledger": ledger if name == owner else None,
                "ledger_ids": ledger_ids if name == owner else [],
                "agreed": agreed,
                "history": self.history_line(),
            }
            prompts[name] = round_prompt(name, other, round_no, case, worktrees[name]["path"],
                                         paths[name]["md"], paths[name]["json"], TEMPLATE,
                                         case.checks, context)
            (context_dir / f"{name}-context.md").write_text(prompts[name], encoding="utf-8")
            self.emit("round.context", round_no, name,
                      {"prompt_chars": len(prompts[name]), "worktree": str(worktrees[name]["path"])})

        runs = {}
        for name, spec in case.agents.items():
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"r{round_no}" / f"{name}.log",
                                    title_prefix=f"cav {case.name} r{round_no}")
            agent.emit = lambda type_, agent_name, data, _r=round_no: self.emit(type_, _r, agent_name, data)
            agent.thinking = self.thinking
            agent.session_id = None
            runs[name] = agent
        self.agents = runs

        self.state["phase"] = f"r{round_no}-agents"
        self.save()
        self.emit("round.start", round_no, data={"phase": phase, "baseline": baseline})
        dispositions = {name: (tuple(ledger_ids) if (name == owner and ledger_ids) else ())
                        for name in case.agents}
        self.drive(round_no, runs, prompts, worktrees, paths, dispositions)

        results = {}
        failed = []
        for name in case.agents:
            required = ledger_ids if (name == owner and ledger_ids) else ()
            rev = review_mod.load(paths[name]["json"], paths[name]["md"], name, round_no,
                                  worktrees[name]["path"], required)
            if rev is None:
                failed.append(f"{name}: no usable review")
                self.emit("review.rejected", round_no, name, {"reason": "missing"})
                continue
            if not rev.valid:
                failed.append(f"{name}: invalid review: {rev.errors[0]}")
                self.emit("review.rejected", round_no, name, {"errors": rev.errors[:5]})
                continue
            if paths[name]["json"].exists():
                review_mod.write_markdown(paths[name]["json"], rev.data)
            self.emit("review.validated", round_no, name,
                      {"summary": rev.summary(), "fallback": rev.fallback})
            sha, problem = self.repo.commit_all(
                worktrees[name]["path"],
                f"Reconcile round {round_no}: {name} recommendation\n\n"
                f"Baseline: {baseline}\nBase: {case.base}")
            if problem:
                failed.append(f"{name}: {problem}")
                continue
            self.repo.push(sha, f"candidate/{name}/r{round_no}")
            diffstat = git("--git-dir", str(self.exchange.path), "diff", "--stat", baseline, sha,
                           check=False)
            (context_dir / f"{name}-changes.md").write_text(
                f"# {name} changes in round {round_no}\n\n```\n{diffstat}\n```\n", encoding="utf-8")
            (context_dir / f"{name}.patch").write_text(
                git("--git-dir", str(self.exchange.path), "diff", baseline, sha,
                    check=False)[:MAX_PATCH_BYTES], encoding="utf-8")
            changed_paths = set(git("--git-dir", str(self.exchange.path), "diff", "--name-only",
                                    baseline, sha, check=False).splitlines())
            declared = {item.get("path") for item in (rev.data.get("changes") or [])}
            unexplained = sorted((changed_paths & set(agreed)) - declared)
            if unexplained:
                self.emit("agent.unexplained_change", round_no, name, {"paths": unexplained[:40]})
                self.log(f"r{round_no} {name}: {len(unexplained)} unexplained change(s) to agreed "
                         f"files: {', '.join(unexplained[:4])}", "WARN")
            results[name] = {
                "commit": sha,
                "tree": self.repo.tree(sha),
                "review": self.rel(rev.path),
                "valid": rev.valid,
                "fallback": rev.fallback,
                "blocking": rev.blocking,
                "counts": rev.counts,
                "approved": rev.behavior_approved,
                "other_approved": rev.other_approved,
                "open_behavioural": rev.open_behavioural,
                "unexplained": unexplained,
            }
            self.emit("candidate.commit", round_no, name,
                      {"commit": sha, "tree": results[name]["tree"]})

        if failed:
            self.state["phase"] = f"r{round_no}-failed"
            self.state["history"].append({"round": round_no, "failed": failed})
            self.state["agents"] = {}
            self.save()
            raise RuntimeError(f"round {round_no} failed: " + "; ".join(failed))

        checks = self.run_checks(round_no, results, worktrees)
        for name in case.agents:
            results[name]["checks_ok"] = checks[name]["ok"]

        names = list(case.agents)
        divergence = compare.compare_trees(self.exchange.path, results[names[0]]["tree"],
                                           results[names[1]]["tree"], case.strict_paths)
        self.write_divergence(round_no, results, divergence)
        self._divergence_files = divergence["files"]
        self.emit("divergence.computed", round_no,
                  data={"totals": divergence["totals"],
                        "substantive": divergence["files"]["substantive"][:20]})
        if divergence["totals"]["substantive"] and self.evaluate(results, checks, divergence) != "diverged":
            # Accepted as behaviourally equivalent: a fact worth surfacing, not an error.
            self.emit("class_claim_mismatch", round_no,
                      data={"substantive": divergence["files"]["substantive"][:20],
                            "note": "accepted as behaviourally equivalent by both reviews"})

        verdict = self.evaluate(results, checks, divergence)
        blockers = self.blockers(results, checks, divergence, verdict)
        self.state["history"].append({
            "round": round_no,
            "phase": phase,
            "baseline": baseline,
            "baseline_owner": owner,
            "results": results,
            "checks": checks,
            "divergence": divergence["totals"],
            "carry_forward": {"items": len(ledger_ids), "total": ledger.get("total", 0)},
            "verdict": verdict,
            "blockers": blockers,
            "trees_equal": results[names[0]]["tree"] == results[names[1]]["tree"],
            "reviews_clean": all(results[name]["valid"] and results[name]["blocking"] == 0
                                 for name in names),
            "checks_ok": all(item["ok"] for item in checks.values()),
            "converged": verdict in ("strict", "functional"),
        })
        self.state["agents"] = {name: {"commit": results[name]["commit"],
                                       "tree": results[name]["tree"],
                                       "blocking": results[name]["blocking"],
                                       "approved": results[name]["approved"]}
                                for name in case.agents}
        self.state["blockers"] = blockers
        self.state["phase"] = f"r{round_no}-review"
        self.save()
        self.emit("convergence.evaluated", round_no,
                  data={"verdict": verdict, "blockers": blockers})
        self.log(f"r{round_no}: verdict={verdict} blockers={blockers}")

        if verdict in ("strict", "functional"):
            return self.finalize(round_no, verdict)
        if self.oscillating(round_no):
            self.emit("oscillation.detected", round_no)
            self.log(f"r{round_no}: candidate pair repeated; stopping as oscillation", "ERROR")
            self.state["phase"] = "disagreement"
            self.save()
            return "stop"
        return "next"

    def clear_reviews(self, paths):
        """A run re-started into the same run directory must not accept last run's review for
        an agent that this time writes none: the review file is how an agent is judged done."""
        for files in paths.values():
            for path in files.values():
                path.unlink(missing_ok=True)

    def write_patch(self, round_no, other, commit, baseline):
        target = self.run_dir / "rounds" / f"r{round_no}" / f"other-{other}.patch"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = git("--git-dir", str(self.exchange.path), "diff", baseline, commit, check=False)
        target.write_text(text[:MAX_PATCH_BYTES], encoding="utf-8")
        return target

    def previous_review_path(self, round_no, name):
        if round_no <= 1:
            return "(none)"
        folder = self.run_dir / "reviews" / f"r{round_no - 1}"
        for suffix in ("-review.json", "-review.md"):
            path = folder / f"{name}{suffix}"
            if path.exists():
                return str(path)
        return "(none)"

    def agreed_paths(self, previous):
        """Paths already identical in both candidates: free, and the prompt asks agents to leave them alone."""
        names = list(self.case.agents)
        if len(previous) < 2:
            return []
        try:
            diff = compare.compare_trees(self.exchange.path, previous[names[0]]["tree"],
                                         previous[names[1]]["tree"], self.case.strict_paths)
        except RuntimeError:
            return []
        return diff["files"]["identical"][:400]

    def history_line(self):
        parts = []
        for item in self.state.get("history", []):
            totals = item.get("divergence") or {}
            if totals:
                parts.append(f"r{item['round']}: {sum(totals.values()) - totals.get('identical', 0)} "
                             f"differing / {totals.get('substantive', 0)} substantive")
            elif item.get("failed"):
                parts.append(f"r{item['round']}: failed")
        return " -> ".join(parts)

    def write_divergence(self, round_no, results, divergence):
        names = list(self.case.agents)
        payload = {
            "round": round_no,
            names[0]: results[names[0]]["commit"],
            names[1]: results[names[1]]["commit"],
            "trees": divergence["trees"],
            "totals": divergence["totals"],
            "files": divergence["files"],
            "stats": divergence["stats"],
        }
        folder = self.run_dir / "reviews" / f"r{round_no}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "divergence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        lines = [f"# Divergence round {round_no}", "",
                 f"- {names[0]}: `{results[names[0]]['commit'][:12]}`",
                 f"- {names[1]}: `{results[names[1]]['commit'][:12]}`", "",
                 "| Class | Files |", "|---|---|"]
        lines += [f"| {key} | {value} |" for key, value in divergence["totals"].items()]
        for key in ("substantive", "comments_candidate", "formatting_candidate", "binary"):
            paths = divergence["files"][key]
            if not paths:
                continue
            lines += ["", f"## {key}", ""]
            lines += [f"- `{path}`" for path in paths[:200]]
        (folder / "divergence.md").write_text("\n".join(lines), encoding="utf-8")

    # --------------------------------------------------------------- drive/monitor

    def drive(self, round_no, runs, prompts, worktrees, reviews, required=None):
        case = self.case
        required = required or {}

        def artifact_problem(name):
            worktree = worktrees[name]["path"]
            rev = review_mod.load(reviews[name]["json"], reviews[name]["md"], name, round_no,
                                  worktree, required.get(name) or ())
            if runs[name].state.error:
                return runs[name].state.error
            if not runs[name].state.finished:
                return "did not finish"
            if rev is None:
                return "no review file"
            if not rev.valid:
                return f"invalid review: {rev.errors[0]}"
            return None

        def artifact_ok(name):
            return artifact_problem(name) is None

        for attempt in range(case.retries + 1):
            pending = [name for name in runs if not artifact_ok(name)]
            if not pending:
                return
            if attempt:
                self.log(f"r{round_no} retry {attempt} for: {', '.join(pending)}", "WARN")
                self.emit("agent.retry", round_no, None, {"agents": pending, "attempt": attempt})
            threads = []
            for name in pending:
                agent = runs[name]
                previous_error = artifact_problem(name)
                agent.reset()
                prompt = prompts[name] + (retry_note(attempt, previous_error) if attempt else "")
                thread = threading.Thread(target=self._worker, args=(agent, prompt, worktrees[name]["path"]),
                                          daemon=True, name=f"agent-{name}")
                thread.start()
                threads.append((name, thread))
            self.monitor(round_no, runs, pending)
            for _, thread in threads:
                thread.join(timeout=30)
            alive = {name for name, thread in threads if thread.is_alive()}
            for name in pending:
                if name in alive:
                    # Never start a second agent on a worktree another one is still editing.
                    self.log(f"r{round_no} {name}: worker still exiting; not retrying it", "ERROR")
                    continue
                problem = artifact_problem(name)
                if problem:
                    runs[name].state.error = runs[name].state.error or problem
        if all(artifact_ok(name) for name in runs):
            return
        self.log(f"r{round_no}: agents did not produce usable artifacts", "ERROR")

    def _worker(self, agent, prompt, worktree):
        try:
            agent.result = agent.run(prompt, worktree)
        except Exception as error:  # surface adapter errors as a failed run, not a crash
            agent.state.error = str(error)
            agent.state.finished = True
            agent.state.touch("failed")
            agent.result = {"ok": False, "reason": str(error)}

    def monitor(self, round_no, runs, pending):
        case = self.case
        deadline = time.time() + case.round_seconds
        warned = set()
        blocked = set()
        lag_reported = False
        last_print = 0.0
        while any(not runs[name].state.finished for name in pending):
            now = time.time()
            for name in pending:
                state = runs[name].state
                self.state["agents"][name] = {
                    "state": "done" if state.finished else "running",
                    "detail": state.detail,
                    "elapsed": int(state.elapsed),
                    "idle": int(state.idle),
                    "messages": state.messages,
                    "session": state.session,
                    "error": state.error,
                }
            self.save()

            if now - last_print >= case.poll_seconds:
                last_print = now
                self.print_progress(round_no, runs, pending)

            for name in pending:
                state = runs[name].state
                if state.finished:
                    continue
                # once: the agent thread can take a poll or two to notice the abort
                if state.detail.startswith("waiting:") and name not in blocked:
                    blocked.add(name)
                    self.log(f"r{round_no} {name}: blocked on a human interaction ({state.detail}); aborting", "ERROR")
                    state.error = f"blocked: {state.detail}"
                    self.emit("agent.blocked", round_no, name, {"detail": state.detail})
                    runs[name].abort()
                if state.idle > case.stall_seconds and name not in warned:
                    warned.add(name)
                    self.log(f"r{round_no} {name}: no activity for {clock(state.idle)} "
                             f"(stall threshold {clock(case.stall_seconds)})", "WARN")

            if not lag_reported and any(runs[n].state.finished for n in pending) \
                    and any(not runs[n].state.finished for n in pending):
                done = [n for n in pending if runs[n].state.finished]
                slow = [n for n in pending if not runs[n].state.finished]
                self.log(f"r{round_no}: {', '.join(done)} finished; waiting on "
                         f"{', '.join(slow)} (still working)")
                lag_reported = True

            if now > deadline:
                self.log(f"r{round_no}: round timeout {clock(case.round_seconds)} reached; aborting "
                         f"{', '.join(n for n in pending if not runs[n].state.finished)}", "ERROR")
                for name in pending:
                    if not runs[name].state.finished:
                        runs[name].abort()
                        runs[name].state.error = "round timeout"
                        runs[name].state.finished = True
            time.sleep(2)

        self.print_progress(round_no, runs, pending)

    def print_progress(self, round_no, runs, pending):
        for name in pending:
            state = runs[name].state
            status = "DONE" if state.finished else "WORK"
            error = f" ERROR: {state.error}" if state.error else ""
            line = (f"r{round_no} {name:<9} {status} {clock(state.elapsed):>6}  "
                    f"idle {clock(state.idle):>5}  msgs {state.messages:<3} {state.detail}{error}")
            print(f"  {line}", flush=True)
            self.log(line, "PROG", echo=False)

    # ------------------------------------------------------------------- checks

    def run_checks(self, round_no, results, worktrees):
        case = self.case
        if not case.checks:
            return {name: {"ok": True, "commands": []} for name in case.agents}
        self.state["phase"] = f"r{round_no}-checks"
        self.save()
        out_dir = self.run_dir / "checks" / f"r{round_no}"
        out_dir.mkdir(parents=True, exist_ok=True)
        cache = {}
        report = {}
        for name in case.agents:
            tree = results[name]["tree"]
            if tree in cache:
                result = dict(cache[tree], agent=name)
                self.log(f"checks {name}: identical tree to another agent, reusing result")
            else:
                result = self.execute_checks(name, worktrees[name]["path"], out_dir / f"{name}.json")
                cache[tree] = result
            self.write_check(out_dir / f"{name}.json", result)
            report[name] = result
        return report

    def execute_checks(self, name, worktree, out_path):
        case = self.case
        commands = []
        for command in case.checks:
            # {python} is quoted for paths with spaces, and the whole line goes through the shell:
            # passing a pre-quoted string as a list argument to cmd /c mangles the quotes.
            rendered = command.replace("{python}", f'"{sys.executable}"')
            started = time.time()
            self.log(f"checks {name}: {command}")
            self.emit("check.start", self.state.get("round"), name, {"command": command})
            proc = subprocess.run(rendered, shell=True, cwd=str(worktree),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            entry = {
                "command": command,
                "exit": proc.returncode,
                "seconds": int(time.time() - started),
                "tail": (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:],
            }
            commands.append(entry)
            self.emit("check.result", self.state.get("round"), name,
                      {"command": command, "exit": proc.returncode, "seconds": entry["seconds"]})
            if proc.returncode != 0:
                self.log(f"checks {name}: FAILED ({proc.returncode}) {command}", "ERROR")
                break
            self.log(f"checks {name}: ok in {entry['seconds']}s")
        result = {"agent": name, "ok": all(c["exit"] == 0 for c in commands) and len(commands) == len(case.checks),
                  "commands": commands}
        self.write_check(out_path, result)
        return result

    def write_check(self, path, result):
        pathlib.Path(path).write_text(json.dumps(result, indent=2)[:200000], encoding="utf-8")

    # ----------------------------------------------------------------- verdict

    def evaluate(self, results, checks, divergence):
        names = list(self.case.agents)
        equal = results[names[0]]["tree"] == results[names[1]]["tree"]
        checks_ok = all(item["ok"] for item in checks.values())
        valid = all(results[name]["valid"] for name in names)
        blocking = sum(results[name]["blocking"] for name in names)
        if self.strict:
            # Strict keeps the documented old rule: identical trees and every review says MUST_FIX: 0,
            # so a nonblocking MUST_FIX cannot slip through.
            must_fix = sum(int(results[name].get("counts", {}).get("must_fix_total") or 0)
                           for name in names)
            ok = equal and checks_ok and valid and must_fix == 0
            return "strict" if ok else "diverged"
        approved = all(results[name]["approved"] and results[name]["other_approved"] for name in names)
        open_behaviour = any(results[name]["open_behavioural"] for name in names)
        # Structured reviews are required for functional acceptance: a markdown-only verdict
        # says nothing about behaviour and would make the policy agent-asserted again.
        fallback = any(results[name]["fallback"] for name in names)
        ok = checks_ok and valid and blocking == 0 and approved and not open_behaviour and not fallback
        return "functional" if ok else "diverged"

    def blockers(self, results, checks, divergence, verdict):
        names = list(self.case.agents)
        return {
            "checks": all(item["ok"] for item in checks.values()),
            "blocking_must_fix": sum(results[name]["blocking"] for name in names),
            "must_fix_total": sum(int(results[name].get("counts", {}).get("must_fix_total") or 0)
                                  for name in names),
            "behaviour_approved": [results[name]["approved"] for name in names],
            "other_approved": [results[name]["other_approved"] for name in names],
            "open_behavioural": any(results[name]["open_behavioural"] for name in names),
            "unexplained_changes": sum(len(results[name].get("unexplained") or []) for name in names),
            "reviews_valid": all(results[name]["valid"] for name in names),
            "markdown_fallback": any(results[name]["fallback"] for name in names),
            "trees_equal": results[names[0]]["tree"] == results[names[1]]["tree"],
            "divergence": divergence["totals"],
            "verdict": verdict,
            "eligible": verdict in ("strict", "functional"),
        }

    def oscillating(self, round_no):
        """The pair repeated after at least one intervening round: A/B/A/B, not A/B/A."""
        seen = {}
        for item in self.state["history"]:
            results = item.get("results") or {}
            if len(results) != len(self.case.agents):
                continue
            pair = tuple(results[name]["tree"] for name in self.case.agents)
            seen.setdefault(pair, []).append(item["round"])
        current = self.state["history"][-1]["results"]
        pair = tuple(current[name]["tree"] for name in self.case.agents)
        return any(item <= round_no - 2 for item in seen.get(pair, []))

    # ------------------------------------------------------------- finalization

    def finalize(self, round_no, verdict):
        case = self.case
        names = list(case.agents)
        history = next(item for item in self.state["history"] if item["round"] == round_no)
        results = history["results"]
        canonical = case.canonical()
        commit = results[canonical]["commit"]

        final_wt = self.run_dir / "final"
        self.prepare_worktree(final_wt, commit)
        cosmetic = self._divergence_files.get("formatting_candidate") or []
        changed = compare.apply_safe_normalization(final_wt, case.strict_paths, cosmetic)
        if case.formatter:
            self.log(f"format: {case.formatter}")
            formatted = subprocess.run(case.formatter, shell=True, cwd=str(final_wt),
                                       capture_output=True, text=True)
            if formatted.returncode:
                tail = ((formatted.stdout or "") + (formatted.stderr or ""))[-1000:]
                self.log(f"formatter exited {formatted.returncode}: {tail}", "ERROR")
                return self.refuse(round_no, f"formatter exited {formatted.returncode}",
                                   {"ok": False, "commands": []})
        self.emit("settle.commit", round_no,
                  data={"commit": commit, "normalised": changed[:50], "count": len(changed)})
        if self.repo.dirty(final_wt):
            settled, problem = self.repo.commit_all(final_wt, f"Settlement round {round_no}")
            if problem:
                raise RuntimeError(f"settlement failed: {problem}")
            commit = settled
        final_tree = self.repo.tree(commit)
        self.log(f"r{round_no}: final commit {commit[:12]} tree {final_tree[:12]} "
                 f"({len(changed)} normalised files)")

        self.emit("final.start", round_no, data={"commit": commit})
        self.state["phase"] = f"r{round_no}-final"
        self.save()
        # The canonical candidate is one of the two trees both agents just approved. Re-running
        # the matrix and re-asking for approval on byte-identical content is pure overhead.
        unchanged = commit == results[canonical]["commit"]
        if unchanged:
            final_checks = dict(history["checks"][canonical])
            self.write_check(self.run_dir / "final" / "checks.json", final_checks)
            self.emit("final.checks", round_no, data={"ok": final_checks["ok"], "reused": True})
        else:
            final_checks = self.execute_checks("final", final_wt, self.run_dir / "final" / "checks.json")
            self.emit("final.checks", round_no, data={"ok": final_checks["ok"], "reused": False})
        if not final_checks["ok"]:
            return self.refuse(round_no, "final checks failed", final_checks)

        if unchanged:
            confirmed = True
            self.log(f"r{round_no}: final commit is the approved candidate; confirmation skipped")
            self.emit("final.confirmation", round_no, data={"ok": True, "skipped": "unchanged tree"})
        else:
            confirmed = self.confirm(round_no, commit, history)
        if not confirmed:
            return self.refuse(round_no, "final confirmation refused", final_checks)

        waived = self.waived_list(history)
        confirmation = "skipped (approved tree)" if unchanged else "confirmed"
        for name in case.agents:
            entry = self.state["agents"].setdefault(name, {})
            entry.update({"state": "done", "detail": "finished", "error": None})
        self.write_signoffs(round_no, commit, final_tree, verdict, waived, confirmation)
        self.write_waived(round_no, history, waived)
        self.state["phase"] = "converged"
        self.state["final"] = {"round": round_no, "commit": commit, "tree": final_tree,
                               "verdict": verdict, "pushed": False,
                               "accepted": waived["accepted"][:200],
                               "checks": final_checks["commands"],
                               "confirmation": confirmation,
                               "basis": self.assurance_basis()}
        self.save()
        self.write_report(round_no)
        self.log(f"ACCEPTED in round {round_no} ({verdict}): {commit} tree {final_tree}")
        self.emit("final.complete", round_no,
                  data={"commit": commit, "tree": final_tree, "verdict": verdict})

        if self.push:
            self.push_origin(commit)
            self.state["final"]["pushed"] = True
            self.save()
            self.write_report(round_no)
            self.emit("push.result", round_no, data={"ok": True, "branch": case.branch})
        else:
            self.log("not pushed to origin; rerun with --push to publish")
        self.emit("run.end", round_no, data={"phase": "converged", "verdict": verdict})
        return "accepted"

    def refuse(self, round_no, reason, final_checks):
        self.log(f"r{round_no}: {reason}; not publishing", "ERROR")
        self.emit("final.confirmation", round_no, data={"ok": False, "reason": reason})
        if self.state.get("history"):
            self.state["history"][-1]["finalization"] = {"ok": False, "reason": reason,
                                                         "checks": final_checks.get("ok")}
        self.state["phase"] = f"r{round_no}-diverged"
        self.save()
        if round_no >= self.case.max_rounds:
            self.state["phase"] = "disagreement"
            self.save()
            return "stop"
        return "next"

    def confirm(self, round_no, commit, history):
        case = self.case
        names = list(case.agents)
        folder = self.run_dir / "reviews" / f"r{round_no}-confirm"
        folder.mkdir(parents=True, exist_ok=True)
        results = history["results"]
        final_diff = self.run_dir / "final" / "diff-other.patch"
        final_diff.write_text(git("--git-dir", str(self.exchange.path), "diff",
                                  results[names[0]]["commit"], commit, check=False)[:MAX_PATCH_BYTES],
                              encoding="utf-8")
        worktrees, runs, prompts, reviews = {}, {}, {}, {}
        for name in names:
            worktrees[name] = {"path": self.prepare_worktree(
                self.run_dir / "wt" / f"r{round_no}-confirm" / name, commit)}
            agent = agent_mod.build(case.agents[name],
                                    self.run_dir / "logs" / f"r{round_no}-confirm" / f"{name}.log",
                                    title_prefix=f"cav {case.name} confirm")
            agent.emit = lambda type_, agent_name, data, _r=round_no: self.emit(type_, _r, agent_name, data)
            agent.thinking = self.thinking
            runs[name] = agent
            reviews[name] = {"md": folder / f"{name}-review.md",
                             "json": folder / f"{name}-review.json"}
            own_diff = self.run_dir / "final" / f"diff-{name}.patch"
            own_diff.write_text(git("--git-dir", str(self.exchange.path), "diff",
                                    results[name]["commit"], commit, check=False)[:MAX_PATCH_BYTES],
                                encoding="utf-8")
            waived = self.waived_list(history)["accepted"]
            prompts[name] = confirm_prompt(
                name, round_no, case, worktrees[name]["path"],
                reviews[name]["md"], reviews[name]["json"], TEMPLATE,
                {"final_commit": commit, "final_diff": str(final_diff), "own_diff": str(own_diff),
                 "waived": ", ".join(waived[:40]) or "(none)",
                 "checks": "pass" if (self.run_dir / "final" / "checks.json").exists() else "unknown",
                 "ledger": None, "ledger_ids": []})

        self.state["phase"] = f"r{round_no}-confirm"
        self.save()
        self.clear_reviews(reviews)
        self.drive(round_no, runs, prompts, worktrees, reviews)

        expected = round_no
        ok = True
        for name in names:
            rev = review_mod.load(reviews[name]["json"], reviews[name]["md"], name, expected,
                                  worktrees[name]["path"])
            if rev is None or not rev.valid or rev.blocking or not rev.behavior_approved:
                ok = False
                reason = "missing" if rev is None else (rev.errors[0] if rev.errors else "refused")
                self.log(f"r{round_no} {name}: confirmation not given ({reason})", "ERROR")
                self.emit("final.confirmation", round_no, name, {"ok": False, "reason": reason})
            else:
                self.emit("final.confirmation", round_no, name, {"ok": True})
        return ok

    def waived_list(self, history):
        """Candidate-to-candidate differences that remained at acceptance."""
        files = self._divergence_files or {}
        accepted = [path for key in ("formatting_candidate", "comments_candidate",
                                     "substantive", "binary")
                    for path in (files.get(key) or [])]
        return {"accepted": accepted, "totals": history.get("divergence") or {}}

    def write_signoffs(self, round_no, commit, tree, verdict, waived, confirmation):
        path = self.run_dir / "signoff"
        path.mkdir(exist_ok=True)
        for name in self.case.agents:
            signoff = {
                "agent": name,
                "decision": "approve",
                "round": round_no,
                "verdict": verdict,
                "commit": commit,
                "tree": tree,
                "checks": (f"checks/r{round_no}/{name}.json" if self.case.checks
                           else "final/checks.json"),
                "final_checks": "final/checks.json",
                "accepted_differences": waived["accepted"],
                "confirmation": confirmation,
                "basis": self.assurance_basis(),
                "derived_from": ("mechanical evidence: valid reviews, zero blocking findings, "
                                 "passing checks, final confirmation on this commit"),
                "signed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (path / f"{name}.json").write_text(json.dumps(signoff, indent=2), encoding="utf-8")

    def assurance_basis(self):
        if self.case.task_prompt.strip():
            return "task + acceptance criteria + checks + mutual review"
        if self.case.base_invariants:
            return "checks + declared base-behaviour invariants + mutual review"
        return "checks + mutual review (no task prompt)"

    def write_waived(self, round_no, history, waived):
        lines = [f"# Accepted differences: round {round_no}", "",
                 f"- Verdict: `{history['verdict']}`",
                 f"- Basis: {self.assurance_basis()}", ""]
        if waived["accepted"]:
            lines += ["## Candidate differences accepted", ""]
            lines += [f"- `{path}`" for path in waived["accepted"][:200]]
        else:
            lines += ["No candidate-to-candidate differences remained at acceptance."]
        # The ledger is what the baseline lacked when the round started; the owner's review says
        # what became of each item. Only items it did not keep were dropped. Read from disk so a
        # resumed run reports the same thing.
        folder = self.run_dir / "reviews" / f"r{round_no}"
        ledger = self.read_json(folder / "carry-forward.json")
        owner = history.get("baseline_owner")
        owner_review = self.read_json(folder / f"{owner}-review.json") if owner else {}
        status = {entry.get("item_id"): entry.get("status")
                  for entry in (owner_review.get("carry_forward_dispositions") or [])
                  if isinstance(entry, dict)}
        dropped = [item for item in (ledger.get("items") or [])
                   if status.get(item["id"]) not in ("retained", "reoffered")]
        if dropped:
            lines += ["", "## Carry-forward items not retained", ""]
            lines += [f"- `{item['path']}` (+{item['added']}/-{item['removed']}): "
                      f"{status.get(item['id']) or 'no disposition'}" for item in dropped[:200]]
        (self.run_dir / "WAIVED.md").write_text("\n".join(lines), encoding="utf-8")

    def push_origin(self, commit):
        branch = self.case.branch
        if not git_ok("fetch", "origin", cwd=self.repo.path):
            raise RuntimeError("cannot fetch origin")
        if not git_ok("merge-base", "--is-ancestor", f"origin/{branch}", commit, cwd=self.repo.path):
            raise RuntimeError(f"{commit} is not a fast-forward of origin/{branch}; refusing to push")
        git("push", "origin", f"{commit}:refs/heads/{branch}", cwd=self.repo.path)
        self.log(f"pushed {commit[:12]} to origin/{branch}")

    def write_report(self, round_no):
        case = self.case
        final = self.state.get("final") or {}
        lines = [
            f"# Reconciliation report: {case.name}",
            "",
            f"- Run: `{self.run_dir.name}`",
            f"- Base: `{case.base or 'none'}`",
            f"- Convergence: `{case.convergence}`   canonical agent: `{case.canonical()}`",
            f"- Acceptance basis: {self.assurance_basis()}",
            f"- Accepted in round {round_no} of at most {case.max_rounds}",
            f"- Final commit: `{final.get('commit', '?')}`",
            f"- Final tree: `{final.get('tree', '?')}`",
            f"- Pushed to origin: {'yes' if final.get('pushed') else 'no'}",
            f"- Confirmation: {final.get('confirmation', '?')}",
            "",
            "## Rounds",
            "",
            "| Round | Verdict | Trees equal | Blocking | Checks | Divergence |",
            "|---|---|---|---|---|---|",
        ]
        for item in self.state["history"]:
            totals = item.get("divergence") or {}
            lines.append(f"| {item['round']} | {item.get('verdict', 'failed')} "
                         f"| {item.get('trees_equal')} "
                         f"| {item.get('blockers', {}).get('blocking_must_fix', '?')} "
                         f"| {'pass' if item.get('blockers', {}).get('checks') else 'fail'} "
                         f"| {totals.get('substantive', '?')} substantive |")
        lines += ["", "## Final round", ""]
        last = next((item for item in self.state["history"] if item["round"] == round_no), {})
        for name in case.agents:
            result = (last.get("results") or {}).get(name) or {}
            lines.append(f"- {name}: `{str(result.get('commit', '?'))[:12]}` "
                         f"MUST_FIX={result.get('blocking', '?')} "
                         f"approved={result.get('approved')}/{result.get('other_approved')}")
        lines += ["", "## Checks (final commit)", ""]
        for command in (final.get("checks") or []):
            lines.append(f"- `{command['command']}` exit {command['exit']} in {command['seconds']}s")
        lines += ["", "## Signoff", "",
                  "Signoffs are mechanical evidence of agent agreement and passing checks on the exact",
                  "final commit. They are not independent human approval, and a green check matrix",
                  "means it builds, not that it works on hardware.", ""]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
