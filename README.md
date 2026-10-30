# cross-agent-validation

Two AI agents — one in **OpenCode**, one in **Kimi Code** — each produce a candidate, review each other's with evidence, and iterate until both approve. Nothing is published until both agents approve both candidates and every check passes.

```
candidate/deepseek/r0 ─┐                  ┌─ both approve ─ publish
                       ├─ round 1 ─ r1 ─ … ┤
candidate/kimi/r0 ─────┘                  └─ otherwise next round
```

```
py -m cav check  --case cases\msmon-five-phase.local.json
py -m cav smoke  --case cases\msmon-five-phase.local.json
py -m cav run    --case cases\msmon-five-phase.local.json [--run-dir runs\<id>] [--resume] [--push] [--dashboard]
py -m cav solve  --repo <path> --prompt task.md [--checks C]...
py -m cav web    --run runs\<id> [--open]
py -m cav serve  [--port 7700] [--open]
py -m cav pipeline --repo <path or GitHub URL> --prompt-file task.md [--run-dir runs\<id>] [--resume]
```

On Windows use `py` rather than `python`: the bare `python` on PATH is the Microsoft Store stub.

## Starting a run from the dashboard

`py -m cav serve` is the home dashboard: every run under `runs\`, and a **New run** form that takes a repo (a local path, or a GitHub URL cloned to `Documents\Default Project\<repo>` or reused if that folder already has the same origin) and a prompt. Its link is fixed: the token is kept in `runs\serve.json`, which also records the URL. A Startup shortcut (`CAV dashboard.lnk`, running `pyw -m cav serve` in this folder) starts it at login; its output goes to `runs\serve.log`.

The form starts `cav pipeline` as its own process, so a run outlives the server:

1. **Plan** — each agent writes `CAV-PLAN.md` independently, then up to 3 review rounds of the usual loop over the two plans.
2. **Code** — both agents implement the agreed plan from the base commit, or, without agreement, each implements its own last plan. Then up to 3 review rounds over the two branches.
3. **Agreement** — the agreed commit is pushed to origin's default branch; if that branch moved during the run it is merged first, and a conflicting merge is refused, not pushed.
4. **No agreement** — the stage's `SUMMARY.md` (also the dashboard's *How far apart* button) lists, from the reviews and trees, the round-by-round trend, the files that still differ and each agent's blocking findings and open disagreements.

Each stage is an ordinary run directory (`runs\<id>\plan`, `runs\<id>\code`) with its own dashboard; the pipeline's state is `runs\<id>\pipeline.json` and its output `pipeline.log`. Defaults for now: the two agents and models below, no check commands, 90 minutes per round.

## What a run does

1. **Bootstrap** — publish both starting candidates (r0) into a local bare repo (`exchange.git`), from a ref, the base commit (`solve`), or a snapshot of an uncommitted working tree. Nothing touches GitHub.
2. **Prepare** — neither candidate is a default. Each agent gets a worktree of *its own* previous candidate and the other candidate's full diff against it.
3. **Propose** — both agents run in parallel. Each reads both candidates in full, judges what it agrees with in each, and turns its worktree into **one new proposal** combining the best of both. It writes a JSON review: approval of each candidate, findings with a real path and line, and its judgement per area of the task (not per file). An agent that writes no valid review is retried once. Next round, each agent receives the two new proposals and does the same.
4. **Monitor** — live progress in `state.json` and `events.jsonl`; an agent blocked on a human prompt is aborted, a stall is reported, and the round has a timeout.
5. **Collect** — the orchestrator alone commits each worktree and pushes `candidate/<agent>/rN`. Agents never run git commands that change history.
6. **Check** — the case's checks run on both candidates at once (an identical tree once).
7. **Decide** — accepted when every check passes, no review has a `MUST_FIX` finding of kind `behavioral`, both agents approve both candidates, and no behavioural disagreement is left open. Otherwise the next round starts from the new candidates; a pair that repeats an earlier round stops the run.
8. **Publish** — the approved candidate that changed least from its own original branch is published as it is: nothing is rewritten after agreement, so nothing is re-checked or re-approved. `REPORT.md` records the rounds, the differences both agents accepted, and each agent's judgement by area. `--push` fast-forwards `origin/<branch>`.

Refusing approval requires a behavioural `MUST_FIX` finding with a real path and line, so taste does not block. Counts are derived from the findings, never trusted from the agent.

## Watching a run

`--dashboard` (or `cav web --run <dir>`) serves a read-only local page: the round's steps, each agent's live feed, the acceptance gates, and the files the candidates still differ on with per-file diffs. The terminal and `run.log` show round, agent, check and verdict lines.

## Requirements

- Windows with Git installed (`C:\Program Files\Git\cmd\git.exe` is used if `git` is not on `PATH`).
- OpenCode desktop installed (its bundled `opencode-cli.exe` is used).
- Kimi Code desktop **running** (the tool talks to its local HTTP API using `~/.kimi-code/server.token`). Sessions run in yolo mode, and CAV approves the prompts Kimi's dangerous-command guard still raises (e.g. `rm -rf`) for its own sessions only. A question to the user is dismissed, so the agent decides for itself; the round prompt also tells both agents never to ask.
- [Ponytail](https://github.com/DietrichGebert/ponytail) in both agents: CAV runs them at level `ultra` (`PONYTAIL_LEVEL` in `cav/agents.py`). OpenCode needs the plugin in its config and gets the level through `PONYTAIL_DEFAULT_MODE`; each Kimi session activates the `ponytail:ponytail` skill with it, and a Kimi without the plugin fails the run instead of running without it.
- Python 3.10+ (standard library only).

## Case file

```json
{
  "name": "msmon-five-phase",
  "repo":   { "host": "...", "base": "816163d", "branch": "main" },
  "agents": { "deepseek": { "kind": "opencode", "model": "deepseek/deepseek-flash#high",
                            "bootstrap_ref": "origin/candidate/deepseek-phase-1-5" },
              "kimi":     { "kind": "kimi", "model": "kimi-code/k3.1-pro", "thinking": "high",
                            "bootstrap_ref": "origin/candidate/kimi-phase-1-5" } },
  "checks": [".\\build.cmd", "{python} tools\\check_sensor_map.py"],
  "task":   { "prompt": "...", "acceptance": ["..."], "context_files": ["..."] },
  "max_rounds": 3, "round_seconds": 5400, "stall_seconds": 1200, "retries": 1
}
```

- `bootstrap_ref` — the commit to use as that agent's r0. Without one, the agent's uncommitted working tree in `repo.secondary` is snapshotted (`bootstrap_exclude` skips paths); the source repo is left untouched.
- `base` — omit it when the two implementations have unrelated histories.
- `model` — **always set it.** OpenCode's CLI otherwise falls back to its own default model.
- `thinking` — Kimi's thinking level; the model and level are verified after each run.
- `checks` — shell commands run in each candidate's worktree; `{python}` is the interpreter running the tool. Use `.\build.cmd`, not `build.cmd`: Windows may refuse to run scripts from the current directory by bare name.
- `task` — the objective, acceptance criteria, base-behaviour invariants and context files, all given to both agents.
- `canonical_agent`, `carry_forward_limit`, `convergence`, `strict_paths` and `formatter` no longer exist; a case that sets one is rejected.

## Failure handling

| Situation | Behaviour |
|---|---|
| Agent fails, exits non-zero, or leaves no valid review | one retry with the reason, then the round fails |
| Agent waits for a permission prompt | aborted immediately with `blocked: ...` |
| No activity for `stall_seconds` | warning logged, run continues |
| Round exceeds `round_seconds` | unfinished agents aborted |
| A check fails | that round is not accepted; the next round sees the result |
| Candidate pair repeats an earlier round | stopped as oscillation |
| Rounds exhausted | `phase: disagreement`, nothing published |
| Interrupted run | `--resume` continues after the last finished round, or publishes an accepted one |

## Limits

- Kimi Code must be running; a timed-out Kimi turn is abandoned, not cancelled.
- Agreement and checks are mechanical evidence, not human approval. A green check matrix means it builds, not that it works on hardware.
- Worktrees stay under `runs/<id>/wt` for inspection. Deleting a run directory needs `git worktree prune` in the host repo afterwards.

## Layout

```
cav/
  config.py        case loading and validation
  gitops.py        exchange repo, worktrees, commits
  agents.py        OpenCode CLI and Kimi HTTP adapters + progress state
  prompts.py       the round prompt and review contract
  review.py        review validation
  compare.py       which paths the two candidates share or differ on
  orchestrator.py  bootstrap, rounds, checks, acceptance, publication, report
  events.py        append-only, redacted event log
  pipeline.py      repo + prompt: plan stage, code stage, merge and push
  web.py, web/     run dashboard, and the home server that lists runs and starts pipelines
  cli.py           run / solve / pipeline / serve / check / web / smoke
cases/             one JSON per reconciliation
tests/             loop tests with fake agents, plus unit tests
```

## Tests

```bat
py -m unittest discover -s tests -t .
```
