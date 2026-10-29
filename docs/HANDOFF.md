# Handoff — current state of CAV

For whoever debugs this next. Everything below is what the code does today, not what it was
designed to do.

## What is actually proven

- **50 unit tests pass** (`py -m unittest discover -s tests -t .`), including a two-round loop
  with fake agents (round 1 blocked by a real finding, round 2 agreed), review validation, the
  acceptance rule, publication choice, resume, retries, oscillation, parallel checks and the
  dashboard endpoints.
- **Live runs completed with real agents** (under the earlier canonical-baseline rules):
  - `cav-self-2` — the tool reconciling two implementations of itself, 4 rounds, accepted.
  - `msmon-3` — the monitor case, 3 rounds, accepted (blocking findings 6 → 3 → 0), merged
    into msc-outside-monitor `main` as PR #1.

## What is NOT proven — start here

1. **The current round design has not run with real agents.** Each agent reads both
   candidates and writes one combined proposal from its own; there are no per-file decisions,
   and the approved candidate is published without a confirmation round. Fake-agent tests
   cover it; a real run does not yet.
2. **Nothing forces an agent to consider every difference.** Without per-file decisions, an
   agent may overlook part of the other candidate; the other agent's review of its proposal
   next round is what catches that.
3. **`--resume` with real agents.** Covered by tests (failed round, accepted-but-unpublished
   round); never exercised by a real interrupted run.
4. **Kimi adapter polling assumptions.** It reads `pending_interaction`, `busy`,
   `main_turn_active`, `last_turn_reason` from `/api/v1/sessions/{id}` every 3 s. Only `approval`
   was ever observed. States like `question` or `plan_approval` are guesses, and a turn that
   ends `failed` after producing output is treated as finished (the review check catches it).
5. **Known gaps, not fixed:** OpenCode receives the prompt on the command line (Windows limit
   ~32,000 characters); check commands have no timeout; a retry gets a fresh `round_seconds`.

## Bugs already fixed — do not reintroduce

| Symptom | Cause | Fix |
|---|---|---|
| Commits landed in another run's exchange | `git remote cav-exchange` is repo-global and shared between runs | push/fetch by path; the named remote is removed on attach (`cav/gitops.py`) |
| An agent was told both candidates were identical | the patch handed over was empty when the other candidate was the shared baseline | each agent gets the other candidate's diff against its own tree (`rounds/rN/<agent>-other.patch`) |
| Kimi hung for an hour "waiting for approval" | `pending_interaction: approval` arrives while `busy: true`; the busy branch was checked first | pending is checked first; permission mode is verified after the profile is set (`cav/agents.py`) |
| Review rejected over arithmetic | counts were trusted input | counts are derived from `findings` and no longer asked of agents (`cav/review.py`) |
| Review rejected for approving while listing a difference | no way to say "differs but equivalent" | `kind: "equivalent"`; only `behavioral` blocks (`cav/review.py`) |
| A resumed round accepted the previous attempt's review | old review files stayed on disk | a round deletes its review files before the agents start (`cav/orchestrator.py`) |
| One candidate was favoured in every round | a canonical agent supplied the baseline, the tie-break and the published tree | both candidates have equal standing; publication picks the least-changed approved one |
| Build checks failed only when run from Claude Code | `NoDefaultCurrentDirectoryInExePath` stops cmd finding `build.cmd` by bare name | checks use `.\build.cmd` |
| Dashboard logged a traceback per browser disconnect | `ConnectionAbortedError` not caught | caught (`cav/web.py`) |

## Layout

```
cav/config.py        case loading and validation
cav/gitops.py        exchange repo, worktrees, commits
cav/agents.py        OpenCode CLI + Kimi HTTP adapters, progress state, event emission
cav/prompts.py       round prompt and review contract
cav/review.py        review validation
cav/compare.py       identical vs differing paths
cav/orchestrator.py  bootstrap, symmetric rounds, parallel checks, acceptance, publication, report
cav/events.py        append-only events.jsonl, monotonic seq, redaction
cav/web.py           read-only dashboard server (cav/web/index.html)
cav/cli.py           run / solve / check / web / smoke
```

## Running it

```
py -m cav check   --case cases\msmon-five-phase.local.json
py -m cav run     --case cases\msmon-five-phase.local.json --run-dir runs\msmon-4 --dashboard
py -m cav web     --run runs\msmon-4 --open
py -m unittest discover -s tests -t .
```

`py`, not `python` — the bare `python` on PATH is the Microsoft Store stub.

## Environment gotchas

- `core.autocrlf=true` on this machine: blobs are LF, working trees CRLF.
- Kimi Code must be running; the adapter talks to `~/.kimi-code/server/instances/*.json`.
- The OpenCode CLI must be the desktop bundle (`@opencodedesktop\resources\opencode-cli.exe`);
  the winget CLI on PATH is v1 and shares a database it cannot read.
- Killing `cav run` does not stop agent work: OpenCode is a child process (kill it too) and a
  Kimi turn continues server-side.
