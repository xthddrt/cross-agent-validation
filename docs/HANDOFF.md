# Handoff — current state of CAV

Written at commit `0f9de54` for whoever debugs this next. Everything below is what the code
does today, not what it was designed to do.

## What is actually proven

- **65 unit tests pass** (`py -m unittest discover -s tests -t .`), including a two-round loop
  with fake agents, commit guards, review validation, functional acceptance, settlement,
  carry-forward, oscillation and the dashboard endpoints.
- **Two live runs completed with real agents:**
  - `runs/cav-selftest2-20261028-204240` — `cav solve` on a scratch repo, 1 round, converged
    `functional`, confirmation round ran, signoffs written.
  - `runs/cav-self-2` — the tool reconciling two implementations of itself, 4 rounds,
    accepted (`e1b1d55`), merged into `main` as `93ab30b`.

## What is NOT proven — start here

1. **No reconciliation of the monitor repo has ever completed a round.** Two attempts
   (`runs/msmon-1`, `runs/msmon-2`) both died in round 1 on review-schema validation. The
   schema bugs are fixed (see below), but nothing tells us whether two agents can actually
   converge on that codebase. This is the biggest unknown.
2. **`--resume` is untested end-to-end.** It reads `state.json`, resumes at `max(round)+1`,
   rebuilds worktrees. Only inspected by reading, never exercised by a real interrupted run.
3. **`functional` acceptance on a large, genuinely divergent tree is untested.** The self-run
   ended with 2 substantive files; the monitor case starts with ~63.
4. **Carry-forward dispositions.** The baseline owner must dispose of every ledger item
   (capped by `carry_forward_limit`, 20 in the monitor case). If an agent skips any, the review
   is invalid and the round retries. This has never been through a real round successfully.
5. **Kimi adapter polling assumptions.** It reads `pending_interaction`, `busy`,
   `main_turn_active`, `last_turn_reason` from `/api/v1/sessions/{id}` every 3 s. Only `approval`
   was ever observed. States like `question` or `plan_approval` are guesses.
6. **`compare.strip_comments`** uses `tokenize` for `.py` and a hand-written scanner for the C
   family. The C scanner has no tests for macro continuations, raw strings or unterminated
   comments, and `strict_paths` is the only thing protecting driver headers.

## Bugs already fixed — do not reintroduce

| Symptom | Cause | Fix |
|---|---|---|
| Commits landed in another run's exchange | `git remote cav-exchange` is repo-global and shared between runs | push/fetch by path; the named remote is removed on attach (`cav/gitops.py`) |
| An agent was told both candidates were identical | the patch handed over was `diff(baseline, other)`, empty when the other candidate *is* the baseline | both agents get the attempt **not used as the baseline** (`other-<agent>.patch`) |
| Kimi hung for an hour "waiting for approval" | `pending_interaction: approval` arrives while `busy: true`; the busy branch was checked first | pending is checked first; permission mode is verified after the profile is set (`cav/agents.py`) |
| Review rejected over arithmetic | counts were trusted input | counts are recomputed from `findings` (`cav/review.py`) |
| Review rejected for approving while listing a difference | no way to say "differs but equivalent" | `kind: "equivalent"` added; only `behavioral` blocks (`cav/review.py`) |
| Every duplicate check run + confirmation after agreement | tail was unconditional | skipped when the final commit is the approved candidate (`cav/orchestrator.py`) |
| Dashboard logged a traceback per browser disconnect | `ConnectionAbortedError` not caught | caught (`cav/web.py`) |

## Layout

```
cav/config.py        case loading and validation; schema v1 and v2
cav/agents.py        OpenCode CLI + Kimi HTTP adapters, progress state, event emission
cav/events.py        append-only events.jsonl, monotonic seq, redaction, per-type caps
cav/compare.py       tree comparison, cosmetic classes, settlement normalisation, carry-forward
cav/review.py        structured review schema, validation, markdown rendering
cav/prompts.py       reconcile / implement / confirm prompts and the review schema text
cav/orchestrator.py  bootstrap, baseline choice, rounds, checks, verdict, finalization
cav/web.py           read-only dashboard server (cav/web/index.html)
cav/cli.py           run / solve / check / web / status / diff / smoke
```

## Running it

```
py -m cav check   --case cases\msmon-five-phase.local.json
py -m cav run     --case cases\msmon-five-phase.local.json --run-dir runs\msmon-3
py -m cav web     --run runs\msmon-3 --open
py -m cav diff    --run runs\msmon-3
py -m unittest discover -s tests -t .
```

`py`, not `python` — the bare `python` on PATH is the Microsoft Store stub.

## Evidence on disk (not committed; `runs/` is gitignored)

- `runs/cav-self-2` — the accepted 4-round run. `REPORT.md`, `WAIVED.md`, per-round reviews,
  divergence artifacts, confirmation reviews, `events.jsonl`.
- `runs/msmon-1` — round 1 rejected. Contains the most useful real-world output so far:
  kimi's 4 blocking findings and deepseek's 6 should-fix findings against the merged monitor
  code, in `reviews/r1/`.
- `runs/msmon-2` — killed after ~2 minutes.

## Environment gotchas

- `core.autocrlf=true` on this machine: blobs are LF, working trees CRLF. Settlement therefore
  rewrites checked-out files without changing blobs — expected, not a bug.
- Kimi Code must be running; the adapter talks to `~/.kimi-code/server/instances/*.json`.
- The OpenCode CLI must be the desktop bundle (`@opencodedesktop\resources\opencode-cli.exe`);
  the winget CLI on PATH is v1 and shares a database it cannot read.
- Killing `cav run` does not stop agent work: OpenCode is a child process (kill it too) and a
  Kimi turn continues server-side.
