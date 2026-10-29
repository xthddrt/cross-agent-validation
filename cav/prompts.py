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
  "areas": [{"area": "<part of the task>", "status": "agreed|open", "kept": "<agent, both, or new>", "why": "<reason>"}],
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


def review_rules(review_json):
    return f"""REVIEW
- Write your review as JSON to this exact path: {review_json}
{REVIEW_SCHEMA}
- MUST_FIX counts only defects: correctness, the original objective, security, data integrity,
  or required validation. Formatting, wording and taste are NIT or SHOULD_FIX, never MUST_FIX.
- A MUST_FIX finding of kind "behavioral" blocks agreement. You may set behavior_approved or
  other_behavior_approved to false only by citing one, with a real path and line.
- If you approve (behavior_approved true), remaining_disagreements must contain no "behavioral"
  entry. A difference you accept as behaviourally equivalent is kind "equivalent". Work you
  still want goes in `areas` with status "open", or as a SHOULD_FIX finding.
- `areas` is your judgement per part of the task, not per file: what you kept from which
  candidate and why.
"""


def round_prompt(agent, other, round_no, case, worktree, review_json, context):
    phase = context.get("phase", "reconcile")
    if phase == "implement":
        objective = ("IMPLEMENT THE TASK in your working tree. The other agent is implementing the "
                     "same task independently; you will not see their attempt this round.")
    else:
        objective = ("WRITE ONE NEW PROPOSAL. Read both candidates below in full and judge what you "
                     "agree with in each. Then make your working tree a single proposal that combines "
                     "the best of both and meets the original objective. Work at the level of the "
                     "design and its parts; you do not have to account for individual files.")
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
            f"{other} receives the same material about your candidate and writes its own proposal.",
            "Review both candidates with the same scrutiny. Next round you each review the other's",
            "proposal, until both of you approve.",
            "",
        ]
    if context.get("history"):
        lines += ["ROUND HISTORY", context["history"], ""]
    lines += [
        "WORK",
        "1. Read the code of both candidates, not only the diff. Findings need a file and a line.",
        "2. Build your proposal in your working tree: keep what you agree with from either",
        "   candidate and fix what is wrong.",
        "3. Where two alternatives are behaviourally equivalent, keep the form already in your tree",
        "   and record it as kind \"equivalent\". Never rewrite equivalent code just to match.",
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
        review_rules(review_json),
    ]
    return "\n".join(lines)


def retry_note(attempt, problem):
    return f"""

IMPORTANT: your previous attempt did not produce a usable result ({problem}).
Complete the task now and make sure the review file exists and is valid.
Do not start over; work from the current state of your working tree.
"""
