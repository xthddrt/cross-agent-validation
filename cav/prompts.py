"""Prompt text for a round: the task, both candidates on equal terms, and the review contract."""

import sys

REVIEW_SCHEMA = """{
  "round": <round number>,
  "agent": "<your name>",
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
    for title, items in (("ACCEPTANCE CRITERIA", case.task_acceptance),
                         ("BASE BEHAVIOUR INVARIANTS (must not regress)", case.base_invariants),
                         ("CONTEXT FILES (read them)", (case.task or {}).get("context_files") or [])):
        if items:
            lines += [title] + [f"- {item}" for item in items] + [""]
    return "\n".join(lines)


def _ledger_block(ledger):
    if not ledger:
        return ""
    lines = ["THE OTHER CANDIDATE'S CHANGES YOUR TREE LACKS",
             "Each path below differs in the other candidate. Neither version is the default: judge",
             "each on its merits, take it (\"retained\") or keep yours (\"intentionally_dropped\"),",
             "and give the reason in carry_forward_dispositions. Every item needs a decision.", ""]
    lines += [f"- {item['id']}  {item['path']}  (+{item['added']}/-{item['removed']})" for item in ledger]
    return "\n".join(lines + [""])


def review_rules(review_json, ledger_ids):
    dispositions = ""
    if ledger_ids:
        dispositions = (f"\n- Decide on every carry-forward item: {', '.join(ledger_ids[:20])}"
                        f"{' ...' if len(ledger_ids) > 20 else ''}")
    return f"""REVIEW
- Write your review as JSON to this exact path: {review_json}
{REVIEW_SCHEMA}
- MUST_FIX counts only defects: correctness, the original objective, security, data integrity,
  or required validation. Formatting, wording and taste are NIT or SHOULD_FIX, never MUST_FIX.
- A MUST_FIX finding of kind "behavioral" blocks agreement. You may set behavior_approved or
  other_behavior_approved to false only by citing one, with a real path and line.
- If you approve (behavior_approved true), remaining_disagreements must contain no "behavioral"
  entry. A difference you accept as behaviourally equivalent is kind "equivalent". Work you
  still want goes in `areas` with status "open", or as a SHOULD_FIX finding.{dispositions}
"""


def round_prompt(agent, other, round_no, case, worktree, review_json, context):
    phase = context.get("phase", "reconcile")
    if phase == "implement":
        objective = ("IMPLEMENT THE TASK in your working tree. The other agent is implementing the "
                     "same task independently; you will not see their attempt this round.")
    else:
        objective = ("RECONCILE the two candidates below into the strongest result, preserving the "
                     "original objective.")
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
    if phase == "reconcile":
        lines += [
            "THE TWO CANDIDATES (equal standing: neither is the default)",
            f"- yours, which your working tree starts from: {context['own_sha']}",
            f"- {other}'s: {context['other_sha']}",
            f"- {other}'s candidate as a diff against your tree: {context.get('other_patch') or '(none)'}",
            f"- {other}'s previous review: {context.get('other_review') or '(none)'}",
            f"- your previous review: {context.get('own_review') or '(none)'}",
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
        lines += [
            "ALREADY IDENTICAL IN BOTH CANDIDATES",
            ", ".join(agreed[:40]) + (" ..." if len(agreed) > 40 else ""),
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
        "- Do not write anywhere under the run directory except your review path.",
        "- Never claim a test you did not run.",
        "- Finish with the review written. That file is how you are judged done.",
        "",
        "CHECKS THE ORCHESTRATOR RUNS AFTERWARDS",
        "\n".join(f"  - {c.replace('{python}', sys.executable)}" for c in case.checks) or "  (none configured)",
        "",
        review_rules(review_json, [item["id"] for item in context.get("ledger") or []]),
    ]
    return "\n".join(lines)


def retry_note(attempt, problem):
    return f"""

IMPORTANT: your previous attempt did not produce a usable result ({problem}).
Complete the task now and make sure the review file exists and is valid.
Do not start over; work from the current state of your working tree.
"""
