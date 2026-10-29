# cross-agent-validation

Drives two AI agents — one in **OpenCode**, one in **Kimi Code** — through a review/recommendation loop until they agree on the same commit, then stops. Nothing is pushed until both agents' results are identical and every check passes.

First case: reconciling the two independent five-phase logging/coverage implementations of `msc-outside-monitor`.

```
candidate/deepseek/r0 ─┐
                       ├─ round 1 ─ r1 ─ round 2 ─ r2 ─ converged
candidate/kimi/r0 ─────┘
```

## What changed in this build (v2)

The loop now converges on **reviewed behavioural agreement**, not only byte-identical trees.

- **Observability** — every run writes `events.jsonl` (append-only, redacted, size-capped). `cav web --run <dir>` serves a read-only local dashboard: what each agent is doing right now, what changed this round, what is already agreed, what still differs, and the exact gates blocking finalization.
- **Convergence** — `convergence: "strict"` keeps the old rule. `"functional"` accepts when both structured reviews are valid, there are zero *blocking* `MUST_FIX` findings, both agents approve **both** candidates, no behavioural disagreement is open, and every check passes. Refusing approval requires a `MUST_FIX` finding of kind `behavioral` with a real path and line — taste does not block.
- **Shared baseline** — both agents start each round from one whole candidate (the only checks-passing one, otherwise `canonical_agent`), instead of merging each other's divergent histories.
- **Structured reviews** — `reviews/rN/<agent>-review.json` is authoritative and the markdown is rendered from it; the schema is in the prompt. Legacy markdown-only reviews still work, under `strict` only.
- **Carry-forward ledger** — changes from the non-baseline candidate that are not in the baseline are listed, and the baseline owner must dispose of each one, so work is not dropped silently.
- **Finalization** — canonical tree → safe normalisation (line endings and trailing whitespace only, never `strict_paths`) → full checks on a clean worktree of that exact commit → both agents confirm that exact diff → signoffs, `WAIVED.md`, `REPORT.md`. Publishing still requires `--push`.
- **Before spending agent time** — `cav check --case <file>` validates the case; `cav diff --run <dir>` shows what the two candidates disagree about from a plain terminal.

```
py -m cav check   --case cases\msmon-five-phase.local.json
py -m cav run     --case cases\msmon-five-phase.local.json [--push] [--dashboard] [--resume] [--strict]
py -m cav solve   --repo <path> --prompt task.md [--checks C]... [--convergence functional]
py -m cav web     --run runs\<id> [--open]
py -m cav status  --run runs\<id> [--watch]
py -m cav diff    --run runs\<id> [--round N] [--path P]
```

On Windows use `py` rather than `python`: the bare `python` on PATH is the Microsoft Store stub.

## What it actually does

1. **Bootstrap** — snapshot both starting candidates into a local bare repo (`exchange.git`). Nothing touches GitHub.
2. **Prepare** — for each agent, create a git worktree from *its own* previous candidate and merge the other candidate into it. Conflicts are handed to the agent to resolve.
3. **Drive** — send the same round prompt to both agents in parallel. Each reviews both implementations, edits its worktree to keep the best of each, and writes a review file with a `MUST_FIX: <n>` line.
4. **Monitor** — poll both agents every few seconds: elapsed time, idle time, message count, current tool. Warn on a stalled agent, report when one finishes and the other lags, abort on a human-blocking interaction or the round timeout.
5. **Collect** — the orchestrator alone commits each worktree and pushes `candidate/<agent>/rN` to the exchange. Agents never run `git commit` or `git push`.
6. **Check** — run the case's check matrix against each candidate (identical trees are checked once).
7. **Converge** — a round is agreed only when trees are identical, every review says `MUST_FIX: 0`, and all checks pass. Otherwise the next round uses both recommendations as inputs.
8. **Report** — write `signoff/<agent>.json`, `REPORT.md`, `state.json`, per-agent logs and check output.

Agents edit files; the orchestrator owns git. That is the main reliability decision: an agent cannot push the wrong branch, and a re-run cannot silently fork history.

## Requirements

- Windows with Git installed (`C:\Program Files\Git\cmd\git.exe` is used if `git` is not on `PATH`).
- OpenCode desktop installed (its bundled `opencode-cli.exe` is used).
- Kimi Code desktop **running** (the tool talks to its local HTTP API using `~/.kimi-code/server.token`).
- Python 3.10+ (standard library only).

## Usage

```bat
python -m cav smoke   --case cases\msmon-five-phase.json
python -m cav run     --case cases\msmon-five-phase.json
python -m cav status  --run runs\msmon-five-phase-20261028-165500
python -m cav run     --case cases\msmon-five-phase.json --run-dir runs\retry --push
```

Two cases ship:

| Case | What it reconciles |
|---|---|
| `msmon-five-phase` | the two five-phase logging/coverage implementations of `msc-outside-monitor` |
| `cross-agent-validation` | two independent implementations of this tool itself (unrelated histories, no shared base) |

`smoke` asks each agent for `CAV_OK`; run it first — it proves the adapters and authentication work before spending real time.

`--push` publishes the agreed commit to `origin/<branch>` after convergence, and refuses unless it is a fast-forward.

While a run is going, use either:

```bat
python -m cav status --run runs\<id> --watch
```

or read the transcript, which now includes a progress line per agent on every poll:

```bat
powershell -Command "Get-Content runs\<id>\run.log -Wait -Tail 20"
```

Each `PROG` line shows the round, agent, state (`WORK`/`DONE`), elapsed time, idle time, message count, and what the agent is currently doing (`tool:shell`, `writing`, `step`). Things worth watching for: `idle` climbing while `detail` stays put (a stall), and one agent `DONE` while the other keeps working (the lag line names it).

## Tests

```bat
python -m unittest discover -s tests -t .
```

The suite drives a full two-round loop with fake agents and covers the commit guards: unrelated-history merges, conflict markers, snapshotting a repo with no commits or no git identity, the check runner, and the agent lifecycle (retry after abort, process kill).

## A round, concretely

| Agent | Worktree | Starts from | Prompt outcome |
|---|---|---|---|
| deepseek | `runs/<id>/wt/r1/deepseek` | `candidate/deepseek/r0` + merge of `candidate/kimi/r0` | edits + `reviews/r1/deepseek-review.md` |
| kimi | `runs/<id>/wt/r1/kimi` | `candidate/kimi/r0` + merge of `candidate/deepseek/r0` | edits + `reviews/r1/kimi-review.md` |

Then the orchestrator commits both, pushes `candidate/deepseek/r1` and `candidate/kimi/r1`, diffs the trees, and decides.

## Failure handling

| Situation | Behaviour |
|---|---|
| Agent fails or exits non-zero | one retry, then the round fails with the reason |
| Review file missing or without exactly one valid `MUST_FIX` line | treated as no result; retried once |
| Unresolved merge conflicts or leftover `<<<<<<<` markers | commit refused, round fails with the file list |
| Agent waits for a permission prompt | aborted immediately with `blocked: ...` |
| No activity for `stall_seconds` | warning logged, run continues |
| Round exceeds `round_seconds` | unfinished agents aborted, round fails |
| Trees differ | another round, up to `max_rounds` |
| Rounds exhausted | `phase: disagreement`, nothing pushed |

## Case file

`cases/msmon-five-phase.json` is the whole configuration:

```json
{
  "repo":   { "host": "...", "secondary": "...", "base": "816163d", "branch": "main" },
  "agents": { "deepseek": { "kind": "opencode",
                            "model": "deepseek/deepseek-flash#high",
                            "bootstrap_ref": "798405f" },
              "kimi":     { "kind": "kimi", "model": "kimi-code/k3.1-pro", "thinking": "high",
                            "bootstrap_exclude": ["reconciliation"] } },
  "checks": ["build.cmd", "build.cmd --with-compat", "build.cmd --with-fixtures",
             "{python} tools\\check_sensor_map.py"],
  "max_rounds": 3, "round_seconds": 5400, "stall_seconds": 1200
}
```

- `bootstrap_ref` — an existing commit to publish as that agent's r0.
- `base` — omit it when the two implementations have unrelated histories (the self case). Merges then run with `--allow-unrelated-histories` and the prompt says there is no shared base.
- `model` — **always set it.** OpenCode's CLI otherwise falls back to its own default model (observed: `opencode/flint-40b-free`). The suffix `#high` is the reasoning variant.
- `thinking` — Kimi's thinking level (`low`, `high`, `max`). Kimi's model and thinking level are checked after each run and a mismatch fails the round.
- No `bootstrap_ref` — snapshot that agent's dirty working tree from `secondary` into r0 (the working tree, branch and index are left untouched).
- `{python}` in a check command is replaced with the interpreter running the tool.
- `checks` may be omitted; convergence then rests on identical trees and clean reviews only.

## Limits

- Kimi Code must be running; the adapter uses its local HTTP API and fails with "server not running" when there is no instance file.
- A timed-out Kimi turn is abandoned, not cancelled: the server keeps working and the run stops waiting for it.
- Signoffs are derived mechanically from each agent's own review, the shared tree hash, and the check results. They are not independent human approvals.
- Checks are whatever the case lists. This tool does not verify driver runtime behaviour — a green check matrix means it builds, not that it works on hardware.
- Worktrees stay under `runs/<id>/wt` for inspection. Deleting a run directory needs `git worktree prune` in the host repo afterwards.

## Layout

```
cav/
  config.py        case loading and validation
  gitops.py        exchange, worktrees, commits, conflict guards
  agents.py        OpenCode CLI and Kimi HTTP adapters + progress state
  prompts.py       the round prompt
  orchestrator.py  bootstrap, rounds, monitoring, checks, convergence, report
  cli.py           run / status / smoke
cases/             one JSON per reconciliation
docs/              review template the agents must follow
tests/             loop test with fake agents, plus commit guards
```
