# cross-agent-validation

Two AI agents (DeepSeek in **OpenCode**, Kimi in **Kimi Code**) turn one task, or two branches, into one result both approve:

```
audit   the run's first judge (two branches, or the first two builds): both, blind, list every defect
        in each candidate, tagged blocker (a security hole, wrong results on a path the task needs, a
        crash, data loss, or a clear departure from the task or plan) or minor; then look again in the
        same session for anything missed.
triage  each rates the other's blockers blocker, minor or not real; one rated lower goes back to its
        author once, with the reason. Both say blocker: it joins the LEDGER, numbered. Still split:
        disputed, listed for you, not blocking. A candidate with no open ledger blocker, and passing
        checks, is the result.
plan    both write a plan (≤1000 words, first line "Base: X|Y") fixing every open blocker in its base,
        minors only where small, with no new command, tool, mode or state machine; a blocker that
        would need one is deferred on a "Deferred: #n" line and listed. (A task alone starts here.)
agree   both approve or reject each plan, shown as the builders will get it, rejecting one that misses
        the task, would not work, leaves a blocker unfixed without deferring it, defers one a small
        change could fix, fixes a defect that is not real, or adds a new feature, with a reason (≤250
        words). A plan both approve is the plan; otherwise both revise and vote again: up to 3
        rounds, then a coin flip.
build   both implement the agreed plan from the same base, separately; the checks run
verify  later judges: is each ledger blocker fixed in each build (fixed only if both say so)? Then
        what each build's diff broke, and the rest of the code only for blockers; new blockers go
        through triage, so a late one blocks only if both call it a blocker. Up to 3 builds; after
        the 3rd: the one passing its checks, else fewer open blockers, else the one both would keep,
        else a coin flip.
publish pushed to origin's default branch (merged if it moved) unless --no-push; what the result may
        still have (minors, disputed or deferred blockers, open ones if forced) is listed as
        follow-ups in REPORT.md and on the dashboard
```

The ledger is how a run converges: agents agree issue by issue rather than on a whole build, the list of blockers only shrinks except for blockers both agree a change caused or found, and each check is a yes/no question. Every defect is still found and listed; what is not fixed is reported, not hidden.

**No bias toward either side.** No prompt names an agent or says whose anything is. Candidates are **X** and **Y**, labelled at random (the builds afresh) and read from plain folders exported without `.git` (so no branch name, author or commit message); plans and reviews are numbered at random; candidates are listed in random order; and each agent keeps **one session per cycle** (audit or verify, triage and plan, or plan; plan rounds; build), starting fresh at the next cycle, so no agent judges builds knowing which is its own. Nobody keeps a tree of their own: both builds start from the one agreed base. Everything an agent reads or writes sits in a **blind area** outside every git repo (`%LOCALAPPDATA%\cav-blind\<random>`, or `CAV_BLIND_ROOT`): plain folders only, so `git log` there finds nothing, and none of the run's bookkeeping (`state.json`, which records who wrote what). Builds are plain copies too; CAV commits them itself. Plans are the smallest changes that complete the task and fix every open ledger blocker. Prompts are as short as they can be.

**Two branches to reconcile** (`--branch A --branch B`, or the two fields on the dashboard form) start at judge, since one may already be acceptable to both.

## Use

```
py -m cav serve                     # the home dashboard (link kept in runs\serve.json); starts runs from a form
py -m cav run --repo <path or GitHub URL> --prompt-file task.md [--branch A --branch B] [--check C]... [--no-push] [--dashboard]
py -m cav run --case cases\kernel-filter-containment.json [--no-push]
py -m cav web --run runs\<id>       # the dashboard for one run
py -m cav smoke                     # both agents answer a trivial prompt
```

On Windows use `py`, not `python` (the Store stub).

A run directory holds `state.json` (steps, verdicts, times, and `authors`: which agent wrote each numbered file), `events.jsonl` (every agent action), `judge/` (reviews and plans per cycle), `plans/` (`c<N>-agreed.md`), `agree/` (replies), `candidates/` (what each judge read), `wt/` (the builds), `REPORT.md`, and the result as `refs/cav/<id>/result` in the repo. `/compare?runs=a,b` on the home dashboard puts two runs side by side.

## Requirements

- Windows with Git; OpenCode desktop (its `opencode-cli.exe`); Kimi Code desktop **running** (its local API via `~/.kimi-code/server.token`); Python 3.10+, standard library only.
- [Ponytail](https://github.com/DietrichGebert/ponytail) in both agents, run at `ultra` (`PONYTAIL_LEVEL` in `cav/agents.py`).
- Unattended: Kimi's approval prompts are answered and its questions dismissed for CAV's own sessions only; every process CAV starts has no window.

## Layout

```
cav/flow.py      the run: judge or plan, agree, build, publish; repo, branch and push helpers
cav/prompts.py   what each agent is told, step by step
cav/agents.py    OpenCode and Kimi adapters: sessions, approvals, ponytail
cav/gitops.py    git helpers
cav/events.py    the event log
cav/web.py, web/ the run dashboard, the home server, the compare page
cav/config.py    case files
cav/cli.py       run / serve / web / check / smoke
```

## Tests

```bat
py -m unittest discover -s tests -t .
```
