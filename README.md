# cross-agent-validation

Two AI agents (DeepSeek in **OpenCode**, Kimi in **Kimi Code**) turn one task into one reviewed result, fast:

```
plan    both write a plan (≤400 words), independently, in parallel
agree   take turns: accept the other plan as written, or answer with a revised plan and why (≤100 words)
        one accept ends it; after 4 turns a coin flip picks between the two latest plans
build   DeepSeek implements the agreed plan; the checks run
review  Kimi approves or lists ≤5 defects; DeepSeek fixes them; at most 2 fixes
publish committed, and pushed to origin's default branch (merged if it moved) unless --no-push
```

Each agent keeps **one session** for the whole run, so nothing is read twice. Plans are Pareto: the few changes that deliver most of the result, meeting the acceptance criteria and nothing beyond. Prompts never name an agent; a plan is rejected only when it misses the task or would not work.

**Two branches to reconcile** (`--branch A --branch B`, or the two fields on the dashboard form): both agents read the branches as plain folders labelled **Candidate X** and **Candidate Y** (random per run, exported without `.git`, so no branch name, author or commit message), and the plan's first line is `Base: Candidate X` or `Base: Candidate Y`. The build starts from that branch. Everything else is the same.

## Use

```
py -m cav serve                     # the home dashboard (link kept in runs\serve.json); starts runs from a form
py -m cav run --repo <path or GitHub URL> --prompt-file task.md [--branch A --branch B] [--check C]... [--no-push] [--dashboard]
py -m cav run --case cases\kernel-filter-containment.json [--no-push]
py -m cav web --run runs\<id>       # the dashboard for one run
py -m cav smoke                     # both agents answer a trivial prompt
```

`--builder kimi` swaps the roles (the other agent reviews). On Windows use `py`, not `python` (the Store stub).

A run directory holds `state.json` (steps, verdicts, times), `events.jsonl` (every agent action), `plans/` (numbered plans; `agreed.md`), `agree/` (each turn's reply), `review/` (diffs and reviews), `work/` (the built tree), `REPORT.md`, and the result as `refs/cav/<id>/result` in the repo. `/compare?runs=a,b` on the home dashboard puts two runs side by side.

## Requirements

- Windows with Git; OpenCode desktop (its `opencode-cli.exe`); Kimi Code desktop **running** (its local API via `~/.kimi-code/server.token`); Python 3.10+, standard library only.
- [Ponytail](https://github.com/DietrichGebert/ponytail) in both agents, run at `ultra` (`PONYTAIL_LEVEL` in `cav/agents.py`).
- Unattended: Kimi's approval prompts are answered and its questions dismissed for CAV's own sessions only; every process CAV starts has no window.

## Layout

```
cav/flow.py      the run: plan, agree, build, review, publish; repo, branch and push helpers
cav/prompts.py   what each agent is told, step by step
cav/agents.py    OpenCode and Kimi adapters: persistent sessions, approvals, ponytail
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
