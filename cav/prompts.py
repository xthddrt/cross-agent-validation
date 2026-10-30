"""What each agent is told, step by step. Short, and the same for both agents: nothing names an
agent or says whose anything is, and a rejection counts only with a defect or a missed criterion."""

import sys

PLAN_WORDS, WHY_WORDS, MAX_DEFECTS = 500, 100, 5
HEAD = "You are one of two agents working on the task below."
PARETO = ("the few changes that give most of the result (the 20% of the work that delivers 80% of the value), "
          "each with what and where; what you leave out on purpose; how it is checked. Meet the task's "
          "acceptance criteria and nothing beyond them.")


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks) or "none configured"


def _task(task):
    return [HEAD, "", "TASK", task]


def plan(task, path):
    return "\n".join([
        *_task(task), "",
        f"Write a plan to {path}, at most {PLAN_WORDS} words: {PARETO}",
        "Don't edit code. Don't ask questions.",
    ]) + "\n"


def judge(task, candidates, reply, plan_path):
    """`candidates`: (label, folder, checks summary), in the order to list them."""
    return "\n".join([
        *_task(task), "", "CANDIDATES (read-only folders)",
        *[f"- Candidate {label}: {folder} (checks: {checks})" for label, folder, checks in candidates], "",
        "1. Judge each candidate against the task only. Approve it unless it has a defect: wrong behaviour, an "
        "unmet acceptance criterion, a security hole or data loss. Style and wording never block.",
        f"2. Write JSON to {reply}:",
        '{"approve": {"X": true|false, "Y": true|false}, "defects": [{"candidate": "X|Y", "file": "<path in that '
        'candidate>", "line": 1, "problem": "...", "fix": "..."}], "prefer": "X|Y"}',
        f"A rejected candidate has 1 to {MAX_DEFECTS} defects; an approved one has none. \"prefer\" is the one you "
        "would keep if only one could be kept.",
        f"3. Write a plan to {plan_path}, at most {PLAN_WORDS} words: first line \"Base: Candidate X\" or "
        f"\"Base: Candidate Y\" (the candidate to build on); then what makes it meet the task, fixing its defects "
        f"and taking what the other does better: {PARETO}",
        "Don't edit code. Don't ask questions.",
    ]) + "\n"


def _context(task, candidates, reviews, plans):
    """The task, the candidates and their reviews, and two plans. `candidates`: (label, folder);
    `reviews`: review files; `plans`: (number, path)."""
    lines = _task(task)
    if candidates:
        lines += ["", "CANDIDATES (read-only folders)"] + [f"- Candidate {label}: {folder}" for label, folder in candidates]
    if reviews:
        lines += ["", "REVIEWS OF THE CANDIDATES (authors not shown)"] + [f"- {path}" for path in sorted(map(str, reviews))]
    return lines + ["", "PLANS (authors not shown)"] + [f"- Plan {number}: {path}" for number, path in plans]


def revise(task, candidates, reviews, plans, path):
    base = '[first line "Base: Candidate X" or "Base: Candidate Y"]; ' if candidates else ""
    cover = "cover the defects the reviews found in the base you choose, " if reviews else ""
    return "\n".join([
        *_context(task, candidates, reviews, plans), "",
        f"Read both plans{' and the reviews' if reviews else ''}. Write a revised plan to {path}, at most "
        f"{PLAN_WORDS} words: {base}the plan you expect both agents to approve. Keep what is right in either plan, "
        f"fix what misses an acceptance criterion or would not work, {cover}and keep it to {PARETO}",
        "Don't edit code. Don't ask questions.",
    ]) + "\n"


def agree(task, candidates, reviews, plans, reply):
    lines = _context(task, candidates, reviews, plans)
    if reviews:
        lines += ["", "Each plan ends with the defects the reviews found in its base; the builders get it exactly as written."]
    lines += [
        "", "Approve each plan unless it misses an acceptance criterion or would not work. A plan you would have "
        "written differently is still approved; preferring the other plan is not a reason to reject one.",
        f"Write JSON to {reply}:",
        '{"approve": {"1": true|false, "2": true|false}, "why": {"<rejected plan number>": "<the criterion it '
        f'misses or why it would not work, at most {WHY_WORDS} words>"}}, "prefer": 1|2}}',
        '"prefer" is the plan you would keep if only one could be kept. Don\'t edit files. Don\'t ask questions.',
    ]
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    start = f" It starts from Candidate {base_label}, the plan's base." if base_label else ""
    return "\n".join([
        f"Implement this plan: {plan_path}, in {work}.{start} Write the least code that does it.",
        f"Run the checks: {_checks(checks)}.",
        f"Write what you did, in a few lines, to {note}. Change only files in {work}. No git commands. "
        "Don't ask questions.",
    ]) + "\n"


def retry_note(problem):
    return f"\nYour last answer was not usable: {problem}. Fix that and answer again.\n"
