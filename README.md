# cross-agent-validation

Two AI agents (DeepSeek in **OpenCode**, Kimi in **Kimi Code**) turn one task, or two branches, into one result both approve:

```
judge   (two branches) both, blind: approve or reject each branch (a rejection needs at least one defect) and
        write a plan (≤1000 words, first line "Base: X|Y"). A branch both approve, with passing
        checks, is the result.
plan    (a task alone) both write a plan (≤1000 words)
agree   both approve or reject each of the two plans, shown as the builders will get it (the reviews'
        defects in its base attached); a rejection needs a reason (≤250 words). A plan both approve
        is the plan. Otherwise both revise from the reasons and vote again: up to 10 rounds, then a
        coin flip.
build   both implement the agreed plan from the same base, once, separately; the checks run
judge   both judge the two builds blind. A build both approve, with passing checks, is the result;
        else the one passing its checks, else the one both would keep, else a coin flip.
publish pushed to origin's default branch (merged if it moved) unless --no-push
```

Approval moves things forward, not preference: a branch, plan or build both agents approve wins. A plan deadlock goes to more revise-and-vote rounds, not to preference; only the final pick between two builds asks which one each would keep.

**No bias toward either side.** No prompt names an agent or says whose anything is. Candidates are **X** and **Y**, labelled at random (the builds afresh) and read from plain folders exported without `.git` (so no branch name, author or commit message); plans and reviews are numbered at random; candidates are listed in random order; and every step runs in a **fresh session**, so no agent remembers what it wrote. Nobody keeps a tree of their own: both builds start from the one agreed base. Everything an agent reads or writes sits in a **blind area** outside every git repo (`%LOCALAPPDATA%\cav-blind\<random>`, or `CAV_BLIND_ROOT`): plain folders only, so `git log` there finds nothing, and none of the run's bookkeeping (`state.json`, which records who wrote what). Builds are plain copies too; CAV commits them itself. Plans are Pareto: the fewest changes that deliver most of the result. Prompts are as short as they can be.

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
