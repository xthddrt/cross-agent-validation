"""What each agent is told. Each agent keeps one session through a cycle (judge or plan, plan rounds,
build), so the cycle's first prompt carries the task and the candidates and every later prompt only
what is new. The same for both agents, and nothing names an agent or says whose anything is."""

import sys

PLAN_WORDS, WHY_WORDS = 1000, 250
PLAN = "the fewest changes that deliver most of the result, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'
DEFECT = "a real defect: wrong behaviour, a missed requirement, a security hole or data loss (not style)"


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks)


def plan(task, path):
    """A cycle's first prompt when there is nothing to judge yet."""
    return f"TASK\n{task}\n\nWrite a plan to {path} (at most {PLAN_WORDS} words): {PLAN}.\n"


def judge(task, candidates, reply, plan_path=None):
    """A cycle's first prompt. `candidates`: (label, folder, checks) in the order to list them. With
    `plan_path` the judge also plans the next build; without, it names the candidate it would keep."""
    keep = ', "prefer": "X|Y"' if plan_path is None else ""
    lines = ["TASK", task, "", "CANDIDATES"]
    lines += [f"- {label}: {folder} (checks: {checks})" for label, folder, checks in candidates] + [""]
    lines += [
        f"Approve each candidate unless it has {DEFECT}. Write to {reply}:",
        '{"approve": {"X": true|false, "Y": true|false}, "defects": [{"candidate": "X|Y", "file": "...", '
        f'"line": 1, "problem": "...", "fix": "..."}}]{keep}}}',
        "Reject with at least one defect, approve with none." + (' "prefer" is the one you would keep.' if keep else ""),
    ]
    if plan_path is not None:
        lines.append(f"Then write a plan to {plan_path} (at most {PLAN_WORDS} words): {PLAN}, fixing the defects "
                     f"and taking what the other candidate does better.{BASE}")
    return "\n".join(lines) + "\n"


def agree(plans, reply, reviews=(), attached=False):
    """A vote. `plans`: (number, file); `reviews`: the judges' reviews, listed in the first round only;
    `attached`: the plans end with the defects the reviews found in their base."""
    lines = []
    if reviews:
        lines += ["REVIEWS"] + [f"- {path}" for path in sorted(map(str, reviews))] + [""]
    lines += ["PLANS" + (" (each ends with the defects found in its base, as the builders get it)" if attached else "")]
    lines += [f"- {number}: {path}" for number, path in plans] + [""]
    lines += [
        "Approve each plan unless it misses the task or would not work; liking one more is not a reason to reject "
        f"the other. Write to {reply}:",
        f'{{"approve": {{"1": true|false, "2": true|false}}, "why": {{"<rejected plan>": "<at most {WHY_WORDS} words>"}}}}',
    ]
    return "\n".join(lines) + "\n"


def revise(objections, path, base=False):
    """After a vote with no plan approved by both. `objections`: plan number -> the reasons."""
    lines = ["OBJECTIONS"] + [f"- Plan {number}: {text}" for number, texts in sorted(objections.items())
                              for text in texts] + [""]
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"(at most {PLAN_WORDS} words): {PLAN}.{BASE if base else ''}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    lines = [f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base_label}." if base_label else ".")]
    if checks:
        lines.append(f"Run the checks there: {_checks(checks)}.")
    lines.append(f"Write a short note of what you did to {note}.")
    return "\n".join(lines) + "\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
