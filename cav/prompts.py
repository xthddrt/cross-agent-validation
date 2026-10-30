"""Prompt text for a round: the task, both candidates on equal terms, and the review contract.

Four variants: a plan or code, and an independent first round or a review round. The wording
is kept neutral: candidates are named by agent, judged against the task only, and a tie
between two equivalent versions goes the same way for both agents (less code), so it cannot
keep the two proposals apart.
"""

import sys

PLAN_FILE = "CAV-PLAN.md"
PLAN_CONTENT = "what changes, where (files and functions), how it will be verified, and what is out of scope"
RECONCILE_CONTENT = ('a first line "Base: Candidate X" or "Base: Candidate Y" (the branch to build on); then, for '
                     "each part of the task, keep X, keep Y, or new, with the reason; what to port onto the base; "
                     "how it will be verified")


def task_block(case):
    prompt = (case.task_prompt or "").strip()
    lines = ["TASK", prompt or "(no task text was configured)"]
    for title, items in (("Acceptance", case.task_acceptance),
                         ("Must not regress", case.base_invariants),
                         ("Read first", (case.task or {}).get("context_files") or [])):
        if items:
            lines += [f"{title}:"] + [f"- {item}" for item in items]
    return lines + [""]


def sources_block(case):
    """Two branches being reconciled, shown blind: neutral labels, plain folders."""
    sources = (case.task or {}).get("sources") or {}
    if not sources:
        return []
    return (["BRANCHES TO RECONCILE (read-only folders; neither is yours)"]
            + [f"- Candidate {label}: {path}" for label, path in sorted(sources.items())] + [""])


def plan_block(agent, other, case):
    """The code stage of a pipeline: which plan each agent implements."""
    plans = (case.task or {}).get("plans") or {}
    files = plans.get("files") or {}
    if not files:
        return []
    if plans.get("agreed"):
        start = (case.task or {}).get("plan_base")
        title = ("PLAN (chosen in the plan stage; implement it, including the defects listed at its end)"
                 if plans.get("forced") else "PLAN (both agents agreed on it; implement it)")
        return [title, f"- {files[agent]}",
                *([f"Your working tree starts from Candidate {start}, the plan's base."] if start else []),
                "Judge candidates against the task and this plan. A departure from the plan is a MUST_FIX",
                "unless the candidate's notes explain why the plan was wrong.", ""]
    return ["PLANS (no agreed plan; each agent implements its own)",
            f"- {agent}: {files[agent]}", f"- {other}: {files[other]}", ""]


def review_block(agent, other, round_no, review_json, reviewing, planning):
    names = sorted((agent, other))
    if not reviewing:
        return [f"REVIEW: write this JSON to {review_json}",
                f'{{"round": {round_no}, "agent": "{agent}", "notes": "<your approach, and anything you did not do>"}}']
    approve = ", ".join(f'"{name}": true|false' for name in names)
    candidate = "|".join(names)
    defect = ("something that would make the code wrong or leave part of the task undone" if planning else
              "wrong behaviour, an unmet part of the task, a security hole or data loss")
    return [
        f"REVIEW: write this JSON to {review_json}",
        f'{{"round": {round_no}, "agent": "{agent}", "approve": {{{approve}}},',
        f' "findings": [{{"id": "F-01", "candidate": "{candidate}", "severity": "MUST_FIX|SHOULD_FIX|NIT",',
        '               "path": "<file in that candidate\'s checkout>", "line": <n>, "summary": "...", "fix": "..."}],',
        ' "notes": "<what you took from which candidate, and why>"}',
        "- Approve each candidate you started this round from, unless it has a defect: " + defect + ".",
        "  Each defect is a MUST_FIX on that candidate, with a real path and line in its checkout.",
        "- Style and wording are SHOULD_FIX or NIT. They never block.",
    ]


def round_prompt(agent, other, round_no, case, worktree, review_json, context):
    phase = context.get("phase", "reconcile")
    planning = case.mode == "plan"
    reviewing = phase == "reconcile"
    work = "plan" if planning else "solution"
    lines = [f"You are {agent}. You and {other} each propose a {work} for the task below. Round {round_no}.",
             f"Repository: {case.host_repo}", f"Your working tree: {worktree}", "", *task_block(case),
             *sources_block(case), *plan_block(agent, other, case)]
    content = RECONCILE_CONTENT if (planning and (case.task or {}).get("sources")) else PLAN_CONTENT

    if reviewing:
        own, theirs = context.get("own_review"), context.get("other_review")
        lines += ["CANDIDATES",
                  f"- {agent}: {context['own_sha']} (your working tree starts here; checkout: {context.get('own_tree')})",
                  f"- {other}: {context['other_sha']} (checkout: {context.get('other_tree')}; "
                  f"diff from {agent}'s: {context.get('other_patch') or 'none, the trees are identical'})"]
        if own or theirs:
            lines.append(f"- Last reviews: {agent} {own or '(none)'}, {other} {theirs or '(none)'}")
        lines += ["", "DO", "1. Read both candidates. Judge each against the task only.",
                  f"2. Make your working tree the best {work}: take either candidate whole, parts of each, "
                  "or new work."]
        lines += ([f"3. Where two plans say the same thing, keep the shorter wording. A plan has {content}.",
                   f"4. Change no file but {PLAN_FILE}, then write your review."] if planning else
                  ["3. Where two versions do the same thing, keep the one with less code.",
                   "4. Build and test what you change, then write your review."])
    elif planning:
        lines += ["DO", "1. Read the code the task touches." if content is PLAN_CONTENT else
                  "1. Read both branches to reconcile. Judge each against the task only.",
                  f"2. Write your plan in {PLAN_FILE} at the root of your working tree: {content}.",
                  f"3. Change no other file, then write your review. {other} plans independently; "
                  "you will not see its plan this round."]
    else:
        lines += ["DO", "1. Read the code the task touches.", "2. Implement the task in your working tree.",
                  f"3. Build and test what you change, then write your review. {other} works independently; "
                  "you will not see its work this round."]

    lines += ["", "RULES",
              f"- Change only your working tree, and only with your {work}. Temporary files go in "
              f"{context.get('scratch')}.",
              f"- Don't open {other}'s working tree ({context.get('other_worktree')}); it is being edited.",
              "- No git commands that change history or the index (commit, add, checkout, reset, stash...). "
              "The orchestrator commits.",
              "- Don't ask questions; nobody is watching. Decide, and explain in the review.",
              "- Don't claim a test you didn't run.", ""]
    if case.checks:
        lines += ["The orchestrator then runs: " + "; ".join(c.replace("{python}", sys.executable) for c in case.checks), ""]
    lines += review_block(agent, other, round_no, review_json, reviewing, planning)
    return "\n".join(lines) + "\n"


def decide_prompt(agent, round_no, case, plans, decision_json):
    """The plan stage's forced choice: two plans, blind (random order, no authors), choose one."""
    lines = [f"You are {agent}. Two plans were written for the task below. Choose the one to implement. "
             f"Round {round_no}.", f"Repository: {case.host_repo}", "", *task_block(case), *sources_block(case),
             "PLANS (in random order; who wrote which is not shown)",
             f"- Plan 1: {plans[1]}", f"- Plan 2: {plans[2]}", "",
             "DO", "1. Read both plans and the code they refer to. Judge each against the task only.",
             "2. Choose one. You must choose: there is no third option and no merging.",
             "3. List the defects the plan you chose must still fix: anything that would make the code wrong "
             "or leave part of the task undone. Leave style alone.", "",
             "RULES", "- Change no files; only write the decision below.",
             "- Don't ask questions; nobody is watching.", "",
             f"DECISION: write this JSON to {decision_json}",
             f'{{"round": {round_no}, "agent": "{agent}", "choice": 1|2, '
             '"defects": [{"summary": "...", "fix": "..."}], "reason": "..."}']
    return "\n".join(lines) + "\n"


def retry_note(attempt, problem):
    return f"""
Your previous attempt left no usable review ({problem}). Continue from your working tree as it
is now, and make sure the review file exists and is valid.
"""
