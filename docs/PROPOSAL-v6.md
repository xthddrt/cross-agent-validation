# CAV v6 — final specification

Status: **final specification for review — nothing implemented**

Supersedes `PROPOSAL-v2.md`, `PROPOSAL-v2.1.md`, `PROPOSAL-v3.md` and both v4/v5 drafts. Built on Kimi's v5 — the more complete of the two — with six amendments. This is the last new document: further changes are edits to this file.

## 0. Changes in this version

Four are concessions to Kimi's v5; two are corrections to it.

1. **Blank-task assurance is now explicit invariants, not inferred base behaviour** (conceded, §9.3). My v5 said approval falls back to "base commit behaviour plus checks". That treats the base as an invariant, which turns intentional changes into regressions. Kimi's version — an explicit `base_behavior_invariants` list, or a stated reduced basis — is correct.
2. **Cosmetic `MUST_FIX` stays valid and non-blocking** (conceded, §8). My v5 invalidated such reviews and forced a clarification retry. Splitting `must_fix_total` from `blocking_must_fix` preserves the finding and its severity at no extra round.
3. **Carry-forward items require a disposition** (conceded, §7.1). My v5 only raised a signal. A required disposition is a mechanism; a signal is a hope. Made practical below.
4. **`class_claim_mismatch` is a first-class event** (conceded, §5). I had it as a concept in v4 and dropped it from v5.
5. **Approval may only be withheld by a validated blocking finding** (new, §9.4). Neither v5 draft closes this. Both require mutual approval, and neither stops an agent answering `false` with no citation — which is the original deadlock wearing a new gate.
6. **`waive_comments` is removed, and `strict_paths` defaults to empty** (corrections). Under `functional` a comment difference never needs waiving: the canonical tree is taken whole and remaining differences are judged, not eliminated. A tool also should not assume a project's layout — the shipped `msmon` case names its own sensitive globs.

Dropped rather than merged: the `churn: "revert"` enforcement mode. The unexplained-change signal is always on and never reverts; reverting risks discarding work nobody asked to discard.

## 1. Goals

1. Make runs transparent: current phase, agent activity, changes per round, agreements, disagreements, checks, and exact finalization blockers.
2. Stop wasting rounds on cosmetic or behaviorally equivalent differences without treating textual classification as semantic proof.
3. Give both agents the immutable original task, both previous attempts and reviews, check results, and the current divergence state.
4. Prevent the non-canonical agent's work from disappearing silently when a shared baseline is selected.
5. Support a single-prompt workflow: independent implementation, reconciliation, final validation, and optional publication.

## 2. Final design decisions

| Area | Decision |
|---|---|
| History | One orchestrator-owned append-only `events.jsonl` with ordered sequence numbers |
| Agent capture | Instrument OpenCode and Kimi; add the missing Kimi raw transcript |
| Divergence | Mechanical textual classification from Git trees, never semantic proof by itself |
| Baseline | One coherent previous candidate for both agents; no per-file synthetic baseline |
| Other attempt | Full Git ref, patch, review, checks, and carry-forward ledger remain available |
| Cosmetic ties | Configured canonical agent wins; it has no authority over behavior |
| File locking | None; use observation-only unexplained-change reporting |
| Acceptance | `strict` or `functional` |
| Review integrity | Structured validation and bounded retry; never silently repair or demote findings |
| Cosmetic findings | Visible and counted separately; they do not gate functional acceptance but do gate strict acceptance |
| Carry-forward work | Track unretained changes mechanically; the canonical agent disposes of each; do not assume every change is an improvement |
| Blank task | Allow functional acceptance on an explicitly stated basis; never invent an original requirement or treat the base as an invariant |
| Comments | Never automatically waived; a remaining comment difference is judged under `functional`, not eliminated |
| Approval refusal | Withholding approval requires a validated blocking finding (§9.4) |
| Finalization | Exact final commit, clean-worktree checks, and two read-only confirmations |
| Publication | Explicit `--push` only, with the existing fast-forward guard |
| Dashboard | Local, read-only, standard library only, SSE with polling fallback |
| Single-prompt harness | Independent implementation from one base followed by reconciliation and resume support |

## 3. Current problems

### 3.1 Limited visibility

- `state.json` is an overwritten snapshot, not an activity history.
- `RunState.detail` exposes only broad states such as `writing` and `tool:read`.
- OpenCode has a raw transcript; Kimi does not currently write `self.log_path`.
- The orchestrator computes tree equality but not file-level or hunk-level divergence.
- Review findings and area decisions are ignored except for one `MUST_FIX` integer.

### 3.2 Unnecessary non-convergence

Current convergence requires identical Git trees, valid zero-`MUST_FIX` reviews, and passing checks. Byte equality conflates cosmetic, editorial, equivalent-syntax, and behavioral differences.

The prompt's symmetric “keep theirs” rule lets each agent adopt the other's version and swap trees indefinitely. Different parents and merge orders add more divergence before either agent starts editing.

### 3.3 Missing context

- No immutable original task is included in every round.
- `other_review_path` names a current-round review being written concurrently rather than the completed previous review.
- The other attempt's reasoning, patch, checks, and unretained work are not explicit context.

## 4. Event history and agent capture

### 4.1 Append-only event stream

Create `runs/<id>/events.jsonl`:

```json
{
  "seq": 142,
  "ts": "2026-10-28T18:41:02.113Z",
  "round": 2,
  "agent": "kimi",
  "type": "agent.tool.end",
  "data": {
    "tool": "Bash",
    "input": {"command": "build.cmd"},
    "exit": 0,
    "ms": 812,
    "output_tail": "Build succeeded"
  }
}
```

One orchestrator-owned writer assigns monotonic sequence numbers and serializes appends. Adapters, checks, Git operations, and finalization all emit through it.

Initial event groups:

| Group | Events |
|---|---|
| Run | `run.start`, `run.resume`, `run.end` |
| Round | `round.start`, `round.prepare`, `round.context`, `round.finish` |
| Agent | `agent.launch`, `agent.step`, `agent.text`, `agent.tool.start`, `agent.tool.end`, `agent.idle`, `agent.retry`, `agent.done`, `agent.error`, `agent.blocked`, `agent.unexplained_change` |
| Review | `review.written`, `review.validated`, `review.rejected` |
| Git | `candidate.commit`, `baseline.selected`, `settle.commit`, `push.result` |
| Analysis | `divergence.computed`, `carry_forward.computed`, `class_claim_mismatch`, `oscillation.detected`, `convergence.evaluated` |
| Checks | `check.start`, `check.result` |
| Finalization | `final.start`, `final.checks`, `final.confirmation`, `final.complete` |

`state.json` remains a compatible current-state snapshot, updated through the same emission path. `events.jsonl` is the authoritative activity history. Reports, the CLI, and the dashboard render from the same structured data.

Payload strings are capped and truncated with a marker. Credentials, authorization values, environment dumps, and credential-like tool inputs must be redacted before writing. Raw thinking is off by default; concise activity and assistant summaries are the normal view.

### 4.2 OpenCode capture

Map CLI JSON events into normalized step, text, tool-start, tool-end, and completion events. Capture tool name, relevant file or command, output tail, exit status, and timestamps. Preserve the raw JSONL transcript.

### 4.3 Kimi capture

Track message-part IDs with a per-session high-water mark:

- Emit only newly observed parts.
- Map text, tool calls, and tool results into the shared event schema.
- Pair results with calls using `tool_call_id`.
- Write the raw transcript to the currently unused `self.log_path`.

Both agents therefore expose comparable activity and diagnostics.

## 5. Round artifacts and divergence

After both candidates are committed, write:

- `reviews/rN/divergence.json`
- `reviews/rN/divergence.md`
- `reviews/rN/carry-forward.json`
- `rounds/rN/<agent>-changes.md`
- `rounds/rN/<agent>.patch`
- `checks/rN/<agent>.json`

Per-path textual classes:

1. `identical` — blobs are equal.
2. `formatting_candidate` — equal under a whitespace-insensitive comparison.
3. `comments_candidate` — equal under a supported language-aware comment comparison.
4. `substantive` — all other textual differences.
5. `binary` — non-text content requiring separate handling.

These are textual classes only:

- Whitespace can be semantic in Python, YAML, macros, and multiline content.
- Comments can contain compiler, type, lint, build, or documentation directives.
- Textually substantive code can be behaviorally equivalent.
- Identical files can contain the same defect in both candidates.

When agents declare a difference equivalent but its mechanical class is `substantive`, emit `class_claim_mismatch`. This is a prominent warning and dashboard fact, not an automatic veto.

Each comparison records changed files, hunks, diffstats, candidate commits, review-backed agreements, remaining differences, and the trend across rounds.

## 6. Reconciliation rounds

### 6.1 Coherent shared baseline

After initial candidates exist:

1. If only one previous candidate passes all checks, select that candidate as the baseline.
2. Otherwise select the configured canonical agent's candidate.
3. Start both agents from that exact commit in separate worktrees.
4. Give both agents the other candidate's complete Git ref, patch, review, checks, and carry-forward ledger.
5. Record the reason in `baseline.selected`.

A complete candidate preserves cross-file consistency. The non-selected implementation remains first-class input and must be reviewed, but the orchestrator does not invent a synthetic per-file merge of substantive alternatives.

There is no automatic merge of divergent histories during reconciliation and no agreed-file locking.

### 6.2 Canonical cosmetic authority

`canonical_agent` defaults to the first case agent. Both agents receive:

> When alternatives are behaviorally equivalent and differ only cosmetically, retain the canonical candidate's form. Do not exchange equivalent implementations or reopen settled formatting choices.

Canonical authority applies only to cosmetic ties, never to behavior, failed checks, the original task, or validated findings.

### 6.3 Round sequence

1. Select and materialize the shared baseline.
2. Compute the carry-forward ledger.
3. Write each agent's context bundle.
4. Run both agents in parallel.
5. Validate their structured reviews.
6. Commit both worktrees and publish candidate refs to the local exchange.
7. Run checks on both candidates.
8. Compute divergence, agreements, unexplained changes, and carry-forward disposition.
9. Evaluate `STRICT`, `FUNCTIONAL`, or `DIVERGED`.
10. Finalize when accepted; otherwise select the next baseline or stop with a diagnosis.

## 7. Carry-forward ledger and churn reporting

### 7.1 Carry-forward ledger

Selecting one baseline can omit work from the other candidate. The orchestrator therefore computes a deterministic ledger of prior-candidate changes not present in the current baseline.

Each ledger item is **file-level**, not hunk-level — hunk identifiers are attached as attributes, not as separate items — and contains:

- Source agent and candidate commit.
- Path, plus the hunk identifiers and patch reference within it.
- Whether the same textual change is present in the baseline.
- Disposition: `retained`, `reoffered`, `intentionally_dropped`, or `unaddressed`.
- Rationale for intentional removal.

The ledger calls these **unretained changes**, not automatically "improvements." Only review evidence and checks can support a quality judgment.

`substantive` paths only; `identical`, `formatting_candidate` and `comments_candidate` paths never enter the ledger. The list is capped at `carry_forward_limit` (default 50) with an overflow rollup line naming the remainder, so round 2 cannot turn a 63-file candidate into a 63-item prompt.

Disposition is required from the **canonical agent**, which owns the baseline and therefore made the choice. The other agent reviews the disposition list and may object to any item with a blocking finding; it does not re-dispose every item, which would double the cost for the agent that lost the work. An `unaddressed` item from the canonical agent makes its review incomplete and receives the normal bounded retry. An `intentionally_dropped` item does not block acceptance when both agents approve the result, but it is listed in the final report and in `WAIVED.md`.

### 7.2 Unexplained-change signal

If a file identical at the baseline is modified without a matching entry in the agent's review `changes` list, emit `agent.unexplained_change`.

This is visible in the dashboard and report but causes no revert and creates no lock. Review completeness validation may request the missing explanation through the normal bounded retry. There is no enforcement mode in the initial design.

## 8. Structured reviews and integrity

For schema-v2 cases, `review.json` is authoritative and the orchestrator renders Markdown from it.

```json
{
  "round": 2,
  "agent": "kimi",
  "counts": {
    "must_fix_total": 0,
    "blocking_must_fix": 0,
    "should_fix": 1,
    "nit": 2
  },
  "behavior_approved": true,
  "other_behavior_approved": true,
  "findings": [
    {
      "id": "F-01",
      "severity": "SHOULD_FIX",
      "kind": "behavioral",
      "path": "driver/guard.c",
      "line": 42,
      "difference_id": null,
      "evidence": "…",
      "resolution": "…"
    }
  ],
  "areas": [
    {"area": "IRP_MJ_CREATE ordering", "status": "agreed", "kept": "kimi", "why": "…"}
  ],
  "changes": [
    {"path": "driver/trace.c", "why": "…"}
  ],
  "incorporated_from_other": ["agent/classify.cpp"],
  "carry_forward_dispositions": [
    {"item_id": "CF-01", "status": "reoffered", "why": "…"}
  ],
  "remaining_disagreements": []
}
```

Finding `kind` is `behavioral`, `cosmetic`, or `editorial`.

Validation covers:

- Schema and required fields.
- Unique finding IDs.
- Count consistency with findings.
- Path containment and cited line existence.
- Approval/disagreement consistency.
- Actual changed files versus the review's change list.
- Complete carry-forward dispositions.
- `blocking_must_fix` derived from behavioral `MUST_FIX` findings.
- A `false` approval flag accompanied by no blocking finding (§9.4).

Invalid or incomplete reviews receive the existing bounded retry. Findings are never silently repaired or demoted.

Cosmetic and editorial findings remain visible with their original severity. They count toward `must_fix_total` but not `blocking_must_fix`. They do not gate `functional` acceptance; all `MUST_FIX` findings gate `strict` acceptance. A behavioral finding remains blocking even when its file is identical or cosmetically different between candidates.

For legacy Markdown-only reviews, retain the existing parser, record `review_format: "markdown-fallback"`, and permit only `strict` acceptance. Replace the template's prefilled `MUST_FIX: 0` with `MUST_FIX: <number>`.

## 9. Acceptance policies

### 9.1 `strict`

Required conditions:

- Candidate trees are byte-identical.
- Both reviews are valid with zero total `MUST_FIX`.
- All checks pass for both candidates.

Existing v1 cases remain `strict` by default.

### 9.2 `functional`

Required conditions:

- No unresolved conflicts.
- All checks pass for both candidates.
- Both reviews are valid with zero behavioral blocking `MUST_FIX`.
- Both agents approve their own and the other candidate's behavior.
- Neither review lists an open behavioral disagreement.
- Both agents classify every remaining candidate-to-candidate difference as cosmetic, editorial, or behaviorally equivalent syntax.
- Every carry-forward item carries a disposition from the canonical agent; the other agent raised no blocking objection.
- Every withheld approval cites a blocking finding (§9.4).

Exact tree equality is unnecessary. Mechanical classes inform the decision but do not independently prove or disprove behavioral equivalence.

### 9.3 Blank-task assurance basis

An empty task prompt is allowed and must not cause the orchestrator to invent a requirement.

The report records the approval basis explicitly:

- With a task prompt: `task + acceptance criteria + checks + mutual review`.
- Without a task prompt: `checks + explicit base-behavior invariants + mutual review`.
- If no base-behavior invariants are configured: `checks + mutual review (no task prompt)`.

Base behavior is an invariant only when the case explicitly declares it. Otherwise, intentional changes from the base are not treated as regressions merely because they differ from the base.

A blank-task signoff therefore carries reduced requirement assurance and says so in the report.

### 9.4 Approval may only be withheld by a validated finding

The rule that makes `functional` reachable, and the one gap both v5 drafts left open.

An agent may set `behavior_approved` or `other_behavior_approved` to `false` **only** by citing a finding that passes validation and has all of:

- severity `MUST_FIX`,
- `kind: behavioral`,
- a `path` and `line` that resolve in the relevant candidate,
- a stated consequence.

Style, preference, "I would have written it differently", and unease without a citation are recorded in `areas` or `remaining_disagreements` and do **not** block. A `false` flag with no qualifying finding makes the review invalid and receives the normal bounded retry.

The orchestrator distinguishes the two cases mechanically and reports the difference, so "agent B objected" and "agent B preferred its own design" are never conflated in the dashboard or the report.

Without this rule, `functional` fails exactly the way byte-equality failed: two opinionated agents hold the run at `DIVERGED` over taste and burn every remaining round proving it.

## 10. Settlement and finalization

### 10.1 Conservative settlement

When functional acceptance is reached:

1. Start from the canonical candidate's complete tree.
2. Apply only explicitly configured formatting, plus the safe normalization set: line endings and trailing whitespace. Never collapse blank lines, never strip leading whitespace, never touch a `strict_paths` file.
3. Never assume all whitespace normalization is safe.
4. Honor `strict_paths`, which are excluded from automatic formatting and cosmetic settlement.
5. Never automatically compose substantive hunks.
6. Record accepted differences, both blob hashes, the selected side, and review rationale in `WAIVED.md`.

Formatting is off by default. The default for `strict_paths` is empty; project cases configure their own sensitive trees — the shipped `msmon` case sets `["driver/**", "compat/**", "**/*.h"]`, where `#if` indentation and macro continuations are not cosmetic.

### 10.2 Exact final-commit validation

For functional acceptance:

1. Create the final candidate or settlement commit.
2. Materialize that exact commit in a clean worktree.
3. Run the complete check matrix there.
4. Give both agents a bounded, read-only confirmation prompt containing:
   - Immutable `TASK.md` and the explicit assurance basis.
   - Exact final commit and diff.
   - Diff against their own last candidate.
   - Remaining candidate differences and class-claim warnings.
   - Carry-forward ledger and dispositions.
   - Final check results.
5. Require two valid confirmations with `behavior_approved: true` and zero behavioral blocking `MUST_FIX`.
6. Write signoffs, `WAIVED.md`, `REPORT.md`, and final state.
7. Push only with explicit `--push` and a successful fast-forward guard.

If final checks fail or either agent refuses, continue with those findings when rounds remain; otherwise end as disagreement naming the blocker. Never publish an unchecked or unconfirmed tree.

Strict acceptance needs no extra confirmation when the identical checked candidates already are the final commit.

### 10.3 Oscillation detection

Track the pair of candidate tree hashes across rounds.

If a previous pair repeats:

- Finalize if the active acceptance policy is satisfied.
- Otherwise stop as `disagreement: oscillation`, identifying flipped files and both versions.

`max_rounds` remains the fallback stop.

## 11. Original task and context exchange

### 11.1 Immutable task snapshot

Add:

```json
"task": {
  "prompt": "",
  "prompt_file": null,
  "acceptance": [],
  "context_files": [],
  "base_behavior_invariants": []
}
```

Rules:

- An absent task block means an empty prompt and no declared invariants.
- `prompt` and `prompt_file` are mutually exclusive; specifying both is a configuration error.
- Relative paths resolve against the case file.
- At bootstrap, write `runs/<id>/TASK.md` once with the verbatim prompt, acceptance criteria, invariants, and context snapshots.
- Later source-file edits do not change a running task.

Every implementation, reconciliation, retry, and confirmation prompt includes the same snapshot. Prose acceptance criteria remain separate from mechanical checks.

### 11.2 Complete context bundle

Write `rounds/rN/<agent>-context.md` containing:

- Immutable `TASK.md` and assurance basis.
- Original base commit, when configured.
- Shared baseline and selection reason.
- Own previous commit, patch, review, and checks.
- Other agent's previous commit, patch, review, and checks.
- Previous divergence report and agreement/disagreement summary.
- Carry-forward ledger and required dispositions.
- Round history trend.
- Canonical cosmetic rule.
- Current phase: `implement`, `reconcile`, or `confirm`.
- Required review schema and output path.

Large patches are referenced by workspace path rather than inlined. The other candidate's complete Git ref remains available for inspection.

## 12. Dashboard and CLI

### 12.1 Read-only local dashboard

Add:

```bash
py -m cav web --run <dir> [--port 7717] [--open]
py -m cav run --case <file> --dashboard
```

Use `ThreadingHTTPServer` and one self-contained HTML/CSS/JavaScript page. Bind to `127.0.0.1`, use a random per-run path token, and expose no write endpoints.

Transport is Server-Sent Events with `?after=<seq>` replay and polling fallback.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | Run, phase, agents, verdict, assurance basis, and blockers |
| `GET /api/divergence?round=N` | Mechanical comparison and class-claim warnings |
| `GET /api/diff?round=N&path=P` | Candidate diff for one path |
| `GET /api/file?rev=<sha>&path=P` | Side-by-side source content |
| `GET /api/review?round=N&agent=A` | Structured review and validation |
| `GET /api/checks?round=N&agent=A` | Check results |
| `GET /api/carry-forward?round=N` | Unretained changes and dispositions |

Panels:

1. **Header and blockers** — original task, assurance basis, phase, round, policy, baseline, and exact finalization gates.
2. **Convergence history** — per-round divergence, blocking findings, checks, verdict, and oscillation status.
3. **Two agent cards** — current tool and target, recent actions, latest message, elapsed/idle, diffstat, findings, checks, retries, and transcript.
4. **Changed this round** — each agent's patch, adopted changes, and unexplained changes.
5. **Agreed** — identical files and review-backed behavioral agreements.
6. **Remaining differences** — textual class, hunks, mismatch warnings, and both explanations.
7. **Carry-forward work** — unretained changes, source candidate, and explicit dispositions.
8. **Review and check viewers** — findings, evidence, commands, exit codes, and output tails.
9. **Finalization** — final commit, clean-worktree checks, confirmations, waived differences, and push result.

The dashboard is read-only. Abort, force-accept, candidate selection, and push controls are out of scope.

### 12.2 Terminal commands

```bash
py -m cav run     --case <file> [--push] [--strict] [--dashboard] [--resume <dir>]
py -m cav solve   --repo <path> --prompt <file> [--checks C]... [--accept functional]
py -m cav web     --run <dir> [--port 7717] [--open]
py -m cav status  --run <dir> [--watch] [--json]
py -m cav diff    --run <dir> [--round N] [--path P]
py -m cav check   --case <file>
py -m cav smoke   --case <file>
```

`cav status --watch` renders a condensed view of the same structured state. No separate TUI is planned.

## 13. Single-prompt workflow and resume

`cav solve` creates a case internally and runs:

1. **Implement:** both agents start independently from the same `repo.base` or `HEAD`, receive `TASK.md`, and create initial candidates without merging.
2. **Reconcile:** use the shared-baseline loop until acceptance, disagreement, oscillation, or the round limit.
3. **Finalize and optionally publish:** use the same final validation and explicit push rules as `cav run`.

`--resume <run-dir>`:

- Loads existing run metadata and immutable task snapshot.
- Reuses committed candidate refs.
- Discards only partial orchestrator-created worktrees from the unfinished round.
- Resumes from the last complete committed round.

## 14. Case configuration

```json
{
  "name": "msmon-five-phase",
  "version": 2,
  "task": {
    "prompt_file": "docs/TASK.md",
    "acceptance": [
      "All configured builds pass",
      "The five-phase behavior matches the original requirement"
    ],
    "context_files": ["docs/LOG_SCHEMA.md"],
    "base_behavior_invariants": []
  },
  "repo": {
    "host": "C:\\...\\msc-outside-monitor",
    "base": "816163d",
    "branch": "main"
  },
  "agents": {
    "deepseek": {
      "kind": "opencode",
      "model": "deepseek/deepseek-flash#high",
      "bootstrap_ref": "origin/candidate/deepseek-phase-1-5"
    },
    "kimi": {
      "kind": "kimi",
      "model": "kimi-code/k3.1-pro",
      "thinking": "high",
      "bootstrap_ref": "origin/candidate/kimi-phase-1-5"
    }
  },
  "checks": [
    "build.cmd",
    "build.cmd --with-compat",
    "{python} tools\\check_sensor_map.py"
  ],
  "accept": "functional",
  "canonical_agent": "deepseek",
  "strict_paths": ["driver/**", "compat/**", "**/*.h"],
  "formatter": null,
  "carry_forward_limit": 50,
  "max_rounds": 3,
  "round_seconds": 5400,
  "stall_seconds": 1200,
  "poll_seconds": 15,
  "retries": 1
}
```

All new keys are optional. Existing v1 cases preserve strict convergence while gaining event history, transcripts, divergence artifacts, carry-forward reporting, and the dashboard.

`cav check --case` validates schema, paths, task inputs, models, and policy combinations before starting agent work.

## 15. Run artifacts

```text
runs/<id>/
  TASK.md
  events.jsonl
  state.json
  REPORT.md
  WAIVED.md
  exchange.git/
  logs/rN/<agent>.jsonl
  rounds/rN/<agent>-context.md
  rounds/rN/<agent>-changes.md
  rounds/rN/<agent>.patch
  reviews/rN/<agent>-review.json
  reviews/rN/<agent>-review.md
  reviews/rN/divergence.json
  reviews/rN/divergence.md
  reviews/rN/carry-forward.json
  checks/rN/<agent>.json
  final/checks.json
  signoff/<agent>.json
```

## 16. Implementation order

### Phase 0 — Structured observability

- Event writer, redaction, and `events.jsonl`.
- OpenCode mapping and Kimi transcript/message-part tracking.
- Candidate patches, changes, divergence artifacts, and carry-forward computation.
- Upgraded `cav status` and new `cav diff`.

### Phase 1 — Task and review integrity

- Immutable `TASK.md`, explicit assurance basis, and schema-v2 cases.
- Correct previous-attempt and previous-review context bundles.
- Structured reviews, validation, carry-forward dispositions, and Markdown rendering.
- `cav check --case` and strict-only legacy fallback.

### Phase 2 — Convergence and finalization

- Shared coherent baselines and canonical cosmetic authority.
- `strict` and `functional` policies.
- Conservative settlement, unexplained-change signals, oscillation detection, and `WAIVED.md`.
- Clean-worktree final checks and two-agent final confirmation.

### Phase 3 — Read-only dashboard

- Local server, SSE, polling fallback, and artifact APIs.
- Self-contained dashboard page and automatic browser opening.

### Phase 4 — Single-prompt operation

- `cav solve` implementation phase.
- Resume support and interrupted-run recovery.

## 17. Required tests

- Event sequence numbers remain ordered across concurrent activity.
- Sensitive fields are redacted and payload caps enforced.
- Kimi polling emits each part once and pairs tool calls correctly.
- Divergence artifacts report exact paths and hunks without claiming semantic proof.
- Class-claim mismatches are surfaced without becoming automatic vetoes.
- Original task and both previous attempts/reviews reach every round, retry, and confirmation.
- Blank-task runs report the correct reduced assurance basis and do not invent base-behavior invariants.
- Invalid reviews are retried, never silently repaired or demoted.
- Cosmetic findings do not gate functional acceptance but do gate strict acceptance.
- Behavioral findings block even in identical or cosmetically different files.
- A withheld approval with no qualifying blocking finding is rejected and retried; the same approval with one blocks finalization.
- Carry-forward items are file-level, capped, and the overflow rollup names the remainder.
- Both agents start reconciliation rounds from the same coherent baseline.
- Carry-forward items are computed mechanically and require explicit dispositions.
- Identical files remain editable; unexplained edits are reported and never automatically reverted.
- Equivalent alternatives stop swapping between agents.
- Formatting-only differences can finalize in functional mode but not strict mode.
- Behaviorally disputed code cannot finalize in functional mode.
- A repeated candidate pair produces an oscillation diagnosis.
- Settlement and formatting are followed by checks in a clean worktree on the exact final commit.
- Failed final checks or refused confirmations block publication and continue the run when rounds remain.
- Markdown fallback permits only strict acceptance.
- `--push` remains required and non-fast-forward pushes are refused.
- Dashboard endpoints work on live and recorded runs without modifying them.
- `cav solve` performs implement-then-reconcile, and resume continues from the last complete round.

## 18. Boundaries

- No agreed-file locking or automatic reversion of unexplained changes.
- No third-agent arbiter in the initial implementation.
- No comment waivers, and no classifier-only behavioral acceptance.
- No assumption that every unretained change is an improvement.
- No dashboard write controls or separate terminal TUI.
- No new runtime dependency; standard library only.
- No automatic remote push without `--push`.
- Agent signoff is evidence of agreement and passing checks, not independent human approval or proof of hardware runtime behavior.

---

## 19. Decisions left to you

1. **`formatter`** — leave `null`, or configure `clang-format` for non-`strict_paths` files so formatting is normalised rather than judged?
2. **`canonical_agent`** — fixed name (deterministic), or prefer whichever candidate passed checks this round? The spec already prefers the same one implicitly; this only affects the tie.
3. **`accept` for the existing `msmon` case** — staying on `strict` until phase 2 is proven, or switching to `functional` on day one?

Everything else above is decided.
