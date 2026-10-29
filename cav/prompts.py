"""Prompt text for reconciliation, implementation and confirmation rounds."""

import sys

REVIEW_SCHEMA = """{
  "round": <round number>,
  "agent": "<your name>",
  "counts": {"must_fix_total": <MUST_FIX findings>, "blocking_must_fix": <MUST_FIX findings with kind behavioral>, "should_fix": <n>, "nit": <n>},
  "behavior_approved": <true|false>,
  "other_behavior_approved": <true|false>,
  "findings": [
    {"id": "F-01", "severity": "MUST_FIX|SHOULD_FIX|NIT", "kind": "behavioral|cosmetic|editorial",
     "path": "<file relative to the worktree>", "line": <line number>,
     "summary": "<what is wrong>", "resolution": "<what to do>"}
  ],
  "areas": [{"area": "<name>", "status": "agreed|open", "kept": "<agent>", "why": "<reason>"}],
  "changes": [{"path": "<file you changed>", "why": "<why>"}],
  "incorporated_from_other": ["<file>"],
  "carry_forward_dispositions": [{"item_id": "CF-01", "status": "retained|intentionally_dropped", "why": "<why>"}],
  "remaining_disagreements": [{"area": "<name>", "kind": "behavioral|cosmetic|editorial|equivalent", "note": "<why>"}]
}"""


def task_block(case):
    prompt = (case.task_prompt or "").strip()
    lines = ["ORIGINAL OBJECTIVE", prompt or "(blank - no original prompt was configured)", ""]
    if case.task_acceptance:
        lines.append("ACCEPTANCE CRITERIA")
        lines += [f"- {item}" for item in case.task_acceptance]
        lines.append("")
    if case.base_invariants:
        lines.append("BASE BEHAVIOUR INVARIANTS (must not regress)")
        lines += [f"- {item}" for item in case.base_invariants]
        lines.append("")
    return "\n".join(lines)


def _check_list(checks):
    return "\n".join(f"  - {c.replace('{python}', sys.executable)}" for c in checks) or "  (none configured)"


def _ledger_block(ledger):
    items = (ledger or {}).get("items") or []
    if not items:
        return ""
    lines = ["THE OTHER CANDIDATE'S CHANGES YOUR TREE LACKS",
             "Each path below differs in the other candidate. Neither version is the default: judge",
             "each on its merits, take it (\"retained\") or keep yours (\"intentionally_dropped\"),",
             "and give the reason in carry_forward_dispositions. Every item needs a decision.", ""]
    for item in items:
        lines.append(f"- {item['id']}  {item['path']}  (+{item['added']}/-{item['removed']})")
    if (ledger or {}).get("omitted"):
        lines.append(f"- ... and {ledger['omitted']} more, rolled up (not required individually)")
    lines.append("")
    return "\n".join(lines)


def review_rules(agent, review_md, review_json, template_path, ledger_dispositions):
    dispositions = ""
    if ledger_dispositions:
        ids = ", ".join(f'"{item}"' for item in ledger_dispositions[:20])
        dispositions = (f"\n- Your review must dispose of every carry-forward item: {ids}"
                        f"{' ...' if len(ledger_dispositions) > 20 else ''}")
    return f"""REVIEW
- Write your review to this exact path: {review_md}
- Write the structured review (authoritative) to: {review_json}
  Schema:
{REVIEW_SCHEMA}
- The legacy template is here for reference: {template_path}
- MUST_FIX counts only defects: correctness, the original objective, security, data integrity,
  or required validation. Formatting, wording and taste are NIT or SHOULD_FIX, never MUST_FIX.
- Counts are derived from your findings and recomputed by the orchestrator; they are for your
  own summary, not trusted. `blocking_must_fix` is the number of MUST_FIX findings of kind
  "behavioral".
- If you approve (behavior_approved true), remaining_disagreements must contain no "behavioral"
  entry. A difference you accept as behaviourally equivalent is kind "equivalent"; "behavioral"
  means unresolved and blocks convergence. Work you still want goes in `areas` with status
  "open", or as a SHOULD_FIX finding.
- You may set behavior_approved or other_behavior_approved to false only by citing a MUST_FIX
  finding of kind "behavioral" with a real path and line. A refusal without one is invalid.{dispositions}
"""


def round_prompt(agent, other, round_no, case, worktree, review_md, review_json,
                 template_path, checks, context):
    phase = context.get("phase", "reconcile")
    if phase == "implement":
        objective = ("IMPLEMENT THE TASK in your working tree. The other agent is implementing the "
                     "same task independently; you will not see their attempt this round.")
    elif phase == "reconcile":
        objective = ("RECONCILE the two attempts below into the strongest result, preserving the "
                     "original objective.")
    else:
        objective = "CONFIRM the final result."

    lines = [
        f"You are {agent}, one of two independent agents working on the same task.",
        "",
        f"Round {round_no} of at most {case.max_rounds}.",
        f"Repository: {case.host_repo}",
        f"Your working tree: {worktree}",
        f"Phase: {phase}",
        "",
        task_block(case),
        "CURRENT ROUND OBJECTIVE",
        objective,
        "",
    ]
    if context.get("phase") == "reconcile":
        lines += [
            "THE TWO CANDIDATES (equal standing: neither is the default)",
            f"- yours, which your working tree starts from: {context.get('own_sha', '?')}",
            f"- {other}'s: {context.get('other_sha', '?')}",
            f"- {other}'s candidate as a diff against your tree: {context.get('other_patch', '(none)')}",
            f"- {other}'s previous review: {context.get('other_review', '(none)')}",
            f"- your previous review: {context.get('own_review', '(none)')}",
            "",
            f"{other} receives the same material about your candidate. Review both candidates with",
            "the same scrutiny, take the stronger version of each part into your tree, and say",
            "which you took and why.",
            "",
        ]
    ledger = _ledger_block(context.get("ledger"))
    if ledger:
        lines += [ledger]
    agreed = context.get("agreed") or []
    if agreed:
        shown = ", ".join(agreed[:40]) + (" ..." if len(agreed) > 40 else "")
        lines += [
            "ALREADY IDENTICAL IN BOTH CANDIDATES",
            shown,
            "",
            "Do not rewrite these for style. If one genuinely must change to support a fix",
            "elsewhere, say why in `changes` and cite the reason in your review.",
            "",
        ]
    if context.get("history"):
        lines += ["ROUND HISTORY", context["history"], ""]
    lines += [
        "WORK",
        "1. Read the code, not only the diff. Findings need a file and a line.",
        "2. Edit files in your working tree so the result is the strongest version of the task.",
        "3. Where two alternatives are behaviourally equivalent and differ only cosmetically, keep",
        "   the form already in your tree and record it as kind \"equivalent\". Never rewrite",
        "   equivalent code just to match the other candidate.",
        "4. Build or test if you change code.",
        "",
        "RULES",
        "- Edit only files inside your working tree.",
        "- Do NOT run git commit, add, push, checkout, switch, reset, merge, rebase, clean or stash.",
        "  Read-only git (status, diff, show, log) is allowed and encouraged. The orchestrator",
        "  commits and exchanges your result.",
        "- Do not write anywhere under the run directory except your two review paths.",
        "- Never claim a test you did not run.",
        "- Finish with the review written. That file is how you are judged done.",
        "",
        "CHECKS THE ORCHESTRATOR RUNS AFTERWARDS",
        _check_list(checks),
        "",
        review_rules(agent, review_md, review_json, template_path,
                     context.get("ledger_ids") or []),
    ]
    return "\n".join(lines)


def confirm_prompt(agent, round_no, case, worktree, review_md, review_json, template_path,
                   context):
    lines = [
        f"You are {agent}. Round {round_no} is being finalised.",
        "",
        f"Final commit: {context.get('final_commit')}",
        f"Your working tree: {worktree} (read-only inspection; your edits are discarded)",
        "",
        task_block(case),
        "FINAL RESULT",
        f"- diff against the other candidate: {context.get('final_diff', '(none)')}",
        f"- diff against your own last candidate: {context.get('own_diff', '(none)')}",
        f"- accepted differences: {context.get('waived', '(none)')}",
        f"- final checks: {context.get('checks', '(none)')}",
        "",
    ]
    ledger = _ledger_block(context.get("ledger"))
    if ledger:
        lines += [ledger]
    lines += [
        "Decide whether this exact final tree may be published.",
        "Approve only if it is acceptable as the result of the task above.",
        "If you refuse, you must cite a MUST_FIX behavioural finding with a real path and line.",
        "",
        review_rules(agent, review_md, review_json, template_path,
                     context.get("ledger_ids") or []),
    ]
    return "\n".join(lines)


def retry_note(attempt, problem):
    return f"""

IMPORTANT: your previous attempt did not produce a usable result ({problem}).
Complete the task now and make sure the review files exist and are valid.
Do not start over; work from the current state of your working tree.
"""
