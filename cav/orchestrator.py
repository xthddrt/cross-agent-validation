"""Round loop: prepare worktrees, drive both agents, monitor, commit, check, converge."""

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

from . import agents as agent_mod
from .gitops import IDENTITY, Exchange, Repo, git, git_ok
from .prompts import retry_note, round_prompt

MUST_FIX_RE = re.compile(r"(?m)^MUST_FIX\s*:\s*(\d+)")
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "REVIEW_TEMPLATE.md"


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def parse_must_fix(path):
    path = pathlib.Path(path)
    if not path.exists():
        return None
    found = MUST_FIX_RE.findall(path.read_text(encoding="utf-8", errors="replace"))
    return int(found[-1]) if found else None


class Orchestrator:
    def __init__(self, case, run_dir, push=False):
        self.case = case
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.push = push
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = Exchange(self.run_dir / "exchange.git").ensure()
        self.repo = Repo(case.host_repo).attach(self.exchange)
        self.log_path = self.run_dir / "run.log"
        self.state = {
            "case": case.name,
            "run_id": self.run_dir.name,
            "phase": "init",
            "round": 0,
            "agents": {},
            "history": [],
        }

    # ---------------------------------------------------------------- plumbing

    def log(self, message, level="INFO"):
        line = f"{time.strftime('%H:%M:%S')} {level:<5} {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save(self):
        self.state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (self.run_dir / "state.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def rel(self, path):
        try:
            return str(pathlib.Path(path).resolve().relative_to(self.run_dir))
        except ValueError:
            return str(path)

    # --------------------------------------------------------------- bootstrap

    def bootstrap(self):
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
            if spec.bootstrap_ref:
                sha = self.repo.sha(spec.bootstrap_ref)
                self.repo.push(sha, branch)
                self.log(f"{name}: r0 = {sha[:12]} ({spec.bootstrap_ref})")
                continue
            if not self.case.secondary_repo:
                raise RuntimeError(f"{name}: no bootstrap_ref and no secondary repo to snapshot")
            secondary = Repo(self.case.secondary_repo).attach(self.exchange)
            sha, problem = self.snapshot(secondary, spec)
            if problem:
                raise RuntimeError(f"{name}: cannot snapshot {secondary.path}: {problem}")
            secondary.push(sha, branch)
            self.log(f"{name}: r0 = {sha[:12]} snapshotted from {secondary.path}")
        self.repo.fetch()

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

    # ------------------------------------------------------------------- round

    def run(self):
        self.bootstrap()
        self.log(f"exchange ready: {self.run_dir / 'exchange.git'}")
        for round_no in range(1, self.case.max_rounds + 1):
            converged = self.round(round_no)
            if converged:
                self.finalize(round_no)
                return self.state
        self.log(f"rounds exhausted without agreement after {self.case.max_rounds}", "ERROR")
        self.state["phase"] = "disagreement"
        self.save()
        return self.state

    def round(self, round_no):
        case = self.case
        previous = round_no - 1
        primary = next(iter(case.agents))
        own_refs = {name: f"candidate/{name}/r{previous}" for name in case.agents}
        for name, branch in own_refs.items():
            if not self.exchange.sha(branch):
                raise RuntimeError(f"missing input {branch}")

        self.state["phase"] = f"r{round_no}-prepare"
        self.state["round"] = round_no
        self.save()
        self.repo.fetch()

        worktrees = {}
        for name in case.agents:
            other = case.other(name)
            path = self.run_dir / "wt" / f"r{round_no}" / name
            self.repo.worktree_add(path, self.repo.ref(own_refs[name]))
            merged, conflicts, message = self.repo.merge(path, self.repo.ref(own_refs[other]))
            if not merged and not conflicts:
                raise RuntimeError(f"r{round_no} {name}: cannot merge {own_refs[other]} "
                                   f"into {own_refs[name]}: {message}")
            worktrees[name] = {"path": path, "merged": merged, "conflicts": conflicts}
            note = "clean" if merged else f"{len(conflicts)} conflicts"
            self.log(f"r{round_no} {name}: worktree {self.rel(path)} merged {other} ({note})")

        reviews = {name: self.run_dir / "reviews" / f"r{round_no}" / f"{name}-review.md"
                   for name in case.agents}
        reviews_dir = self.run_dir / "reviews" / f"r{round_no}"
        reviews_dir.mkdir(parents=True, exist_ok=True)

        runs = {}
        for name, spec in case.agents.items():
            agent = agent_mod.build(spec, self.run_dir / "logs" / f"r{round_no}" / f"{name}.log",
                                    title_prefix=f"cav {case.name} r{round_no}")
            agent.session_id = None
            runs[name] = agent
        self.agents = runs

        prompts = {}
        for name in case.agents:
            other = case.other(name)
            prompts[name] = round_prompt(
                agent=name, other=other, round_no=round_no, case=case,
                worktree=worktrees[name]["path"], review_path=reviews[name],
                other_review_path=reviews[other], template_path=TEMPLATE,
                own_ref=own_refs[name], own_sha=self.exchange.sha(own_refs[name]),
                other_ref=own_refs[other], other_sha=self.exchange.sha(own_refs[other]),
                conflicts=worktrees[name]["conflicts"], checks=case.checks,
            )

        self.drive(round_no, runs, prompts, worktrees, reviews)

        results = {}
        failed = []
        for name in case.agents:
            must_fix = parse_must_fix(reviews[name])
            review_ok = must_fix is not None
            agent_result = getattr(runs[name], "result", {"ok": False, "reason": "no result"})
            if not review_ok:
                failed.append(f"{name}: no usable review file at {self.rel(reviews[name])}")
                continue
            if not agent_result.get("ok"):
                failed.append(f"{name}: agent failed ({agent_result.get('reason')})")
                continue
            path = worktrees[name]["path"]
            sha, problem = self.repo.commit_all(
                path,
                f"Reconcile round {round_no}: {name} recommendation\n\n"
                f"Inputs: {own_refs[primary]} + {own_refs[case.other(primary)]}\n"
                f"Base: {case.base}")
            if problem:
                failed.append(f"{name}: {problem}")
                continue
            self.repo.push(sha, f"candidate/{name}/r{round_no}")
            results[name] = {
                "commit": sha,
                "tree": self.repo.tree(sha),
                "review": self.rel(reviews[name]),
                "must_fix": must_fix,
                "conflicts": worktrees[name]["conflicts"],
            }
            self.log(f"r{round_no} {name}: pushed candidate/{name}/r{round_no} {sha[:12]} "
                     f"tree {results[name]['tree'][:12]} MUST_FIX={must_fix}")

        if failed:
            self.state["phase"] = f"r{round_no}-failed"
            self.state["history"].append({"round": round_no, "failed": failed})
            self.state["agents"] = {}
            self.save()
            raise RuntimeError(f"round {round_no} failed: " + "; ".join(failed))

        checks = self.run_checks(round_no, results, worktrees)

        trees_equal = results[primary]["tree"] == results[case.other(primary)]["tree"]
        reviews_clean = all(results[name]["must_fix"] == 0 for name in case.agents)
        checks_ok = all(item["ok"] for item in checks.values())
        converged = trees_equal and reviews_clean and checks_ok

        self.state["history"].append({
            "round": round_no,
            "results": results,
            "checks": checks,
            "trees_equal": trees_equal,
            "reviews_clean": reviews_clean,
            "checks_ok": checks_ok,
            "converged": converged,
        })
        for name in case.agents:
            self.state["agents"][name] = {
                "commit": results[name]["commit"],
                "tree": results[name]["tree"],
                "must_fix": results[name]["must_fix"],
            }
        self.state["phase"] = f"r{round_no}-review"
        self.save()
        self.log(f"r{round_no}: trees_equal={trees_equal} reviews_clean={reviews_clean} "
                 f"checks_ok={checks_ok} -> {'CONVERGED' if converged else 'next round'}")
        return converged

    # --------------------------------------------------------------- drive/monitor

    def drive(self, round_no, runs, prompts, worktrees, reviews):
        case = self.case
        self.state["phase"] = f"r{round_no}-agents"
        self.save()

        for attempt in range(case.retries + 1):
            pending = [name for name in runs if not self.artifact_ok(runs[name], reviews[name])]
            if not pending:
                return
            if attempt:
                self.log(f"r{round_no} retry {attempt} for: {', '.join(pending)}", "WARN")
            threads = []
            for name in pending:
                agent = runs[name]
                previous_error = agent.state.error
                agent.state = agent_mod.RunState()
                prompt = prompts[name] + (retry_note(attempt, previous_error) if attempt else "")
                thread = threading.Thread(target=self._worker, args=(agent, prompt, worktrees[name]["path"]),
                                          daemon=True, name=f"agent-{name}")
                thread.start()
                threads.append(thread)
            self.monitor(round_no, runs, pending)
            for thread in threads:
                thread.join(timeout=30)
            for name in pending:
                agent = runs[name]
                if parse_must_fix(reviews[name]) is None:
                    agent.state.error = agent.state.error or "no review file"
            if all(self.artifact_ok(runs[name], reviews[name]) for name in runs):
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

    def artifact_ok(self, agent, review_path):
        return (not agent.state.error and agent.state.finished
                and parse_must_fix(review_path) is not None)

    def monitor(self, round_no, runs, pending):
        case = self.case
        deadline = time.time() + case.round_seconds
        warned = set()
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
                if state.detail.startswith("waiting:"):
                    self.log(f"r{round_no} {name}: blocked on a human interaction ({state.detail}); aborting", "ERROR")
                    runs[name].abort()
                    state.error = state.error or f"blocked: {state.detail}"
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
            print(f"  r{round_no} {name:<9} {status} {clock(state.elapsed):>6}  "
                  f"idle {clock(state.idle):>5}  msgs {state.messages:<3} {state.detail}{error}", flush=True)

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
                result = cache[tree]
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
            rendered = command.replace("{python}", f'"{sys.executable}"')
            started = time.time()
            self.log(f"checks {name}: {command}")
            if os.name == "nt":
                proc = subprocess.run(["cmd", "/c", rendered], cwd=str(worktree),
                                      capture_output=True, text=True, encoding="utf-8", errors="replace")
            else:
                proc = subprocess.run(["sh", "-c", rendered], cwd=str(worktree),
                                      capture_output=True, text=True, encoding="utf-8", errors="replace")
            entry = {
                "command": command,
                "exit": proc.returncode,
                "seconds": int(time.time() - started),
                "tail": (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:],
            }
            commands.append(entry)
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

    # --------------------------------------------------------------- finalize

    def finalize(self, round_no):
        case = self.case
        history = next(item for item in self.state["history"] if item["round"] == round_no)
        results = history["results"]
        primary = next(iter(case.agents))
        commit = results[primary]["commit"]
        tree = results[primary]["tree"]

        for name in case.agents:
            signoff = {
                "agent": name,
                "decision": "approve",
                "round": round_no,
                "commit": commit,
                "tree": tree,
                "review": results[name]["review"],
                "checks": f"checks/r{round_no}/{name}.json",
                "derived_from": "review MUST_FIX=0, identical trees, checks passed",
                "signed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            path = self.run_dir / "signoff"
            path.mkdir(exist_ok=True)
            (path / f"{name}.json").write_text(json.dumps(signoff, indent=2), encoding="utf-8")

        self.state["phase"] = "converged"
        self.state["final"] = {"round": round_no, "commit": commit, "tree": tree, "pushed": False}
        self.save()
        self.write_report(round_no)
        self.log(f"CONVERGED in round {round_no}: {commit} tree {tree}")

        if self.push:
            self.push_origin(commit)
            self.state["final"]["pushed"] = True
            self.save()
            self.write_report(round_no)
        else:
            self.log("not pushed to origin; rerun with --push to publish")

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
        history = next(item for item in self.state["history"] if item["round"] == round_no)
        results = history["results"]
        primary = next(iter(case.agents))
        lines = [
            f"# Reconciliation report: {case.name}",
            "",
            f"- Run: `{self.run_dir.name}`",
            f"- Base: `{case.base or 'none (independent implementations)'}`",
            f"- Converged in round {round_no} of at most {case.max_rounds}",
            f"- Final commit: `{results[primary]['commit']}`",
            f"- Final tree: `{results[primary]['tree']}`",
            f"- Pushed to origin: {'yes' if self.push else 'no'}",
            "",
            "## Rounds",
            "",
            "| Round | Trees equal | Reviews clean | Checks | Converged |",
            "|---|---|---|---|---|",
        ]
        for item in self.state["history"]:
            lines.append(f"| {item['round']} | {item['trees_equal']} | {item['reviews_clean']} "
                         f"| {item['checks_ok']} | {item['converged']} |")

        lines += ["", "## Agent results (final round)", "",
                  "| Agent | Commit | Tree | MUST_FIX | Review |", "|---|---|---|---|---|"]
        for name in case.agents:
            entry = results[name]
            lines.append(f"| {name} | `{entry['commit'][:12]}` | `{entry['tree'][:12]}` "
                         f"| {entry['must_fix']} | `{entry['review']}` |")

        lines += ["", "## Checks (final round)", ""]
        for name in case.agents:
            check = history["checks"][name]
            lines.append(f"### {name}: {'PASS' if check['ok'] else 'FAIL'}")
            lines.append("")
            for command in check["commands"]:
                lines.append(f"- `{command['command']}` exit {command['exit']} in {command['seconds']}s")
            lines.append("")

        conflicts = results[primary].get("conflicts") or []
        lines += ["## Merge conflicts", "",
                  "- none" if not conflicts else "\n".join(f"- `{c}`" for c in conflicts), ""]
        lines += ["## Signoff", "",
                  "Both signoffs are derived from each agent's own review file, the identical tree hash, "
                  "and the recorded check results. They are not independent human approvals.", ""]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
