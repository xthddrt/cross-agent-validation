"""What each agent is told. Short, the same for both agents, and nothing names an agent or says whose
anything is."""

import sys

PLAN_WORDS, WHY_WORDS = 1000, 250
PLAN = "the fewest changes that deliver most of the result, each with what and where, and how to check them"
BASE = 'first line "Base: X" or "Base: Y" (the candidate to build on), then '
DEFECT = "a real defect: wrong behaviour, a missed requirement, a security hole or data loss (not style)"


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks)


def _head(task, candidates=(), reviews=(), plans=()):
    """`candidates`: (label, folder, checks or None); `reviews`: files; `plans`: (number, file)."""
    lines = ["TASK", task]
    if candidates:
        lines += ["", "CANDIDATES"] + [f"- {label}: {folder}" + (f" (checks: {checks})" if checks else "")
                                       for label, folder, checks in candidates]
    if reviews:
        lines += ["", "REVIEWS"] + [f"- {path}" for path in sorted(map(str, reviews))]
    if plans:
        lines += ["", "PLANS"] + [f"- {number}: {path}" for number, path in plans]
    return lines + [""]


def plan(task, path):
    return "\n".join(_head(task) + [f"Write a plan to {path} (at most {PLAN_WORDS} words): {PLAN}."]) + "\n"


def judge(task, candidates, reply, plan_path=None):
    """With `plan_path` the judge also plans the build; without, it names the candidate it would keep."""
    keep = ', "prefer": "X|Y"' if plan_path is None else ""
    lines = _head(task, candidates) + [
        f"Approve each candidate unless it has {DEFECT}. Write to {reply}:",
        '{"approve": {"X": true|false, "Y": true|false}, "defects": [{"candidate": "X|Y", "file": "...", '
        f'"line": 1, "problem": "...", "fix": "..."}}]{keep}}}',
        "A rejected candidate gets at least one defect, an approved one none."
        + (' "prefer" is the one you would keep.' if plan_path is None else ""),
    ]
    if plan_path is not None:
        lines.append(f"Then write a plan to {plan_path} (at most {PLAN_WORDS} words): {BASE}{PLAN}, "
                     "fixing its defects and taking what the other does better.")
    return "\n".join(lines) + "\n"


def revise(task, candidates, reviews, plans, objections, path):
    """`objections`: plan number -> the reasons it was rejected."""
    lines = _head(task, candidates, reviews, plans) + ["OBJECTIONS"]
    lines += [f"- Plan {number}: {text}" for number, texts in sorted(objections.items()) for text in texts] + [""]
    lines.append(f"Write one revised plan to {path} (at most {PLAN_WORDS} words) that both reviewers can approve: "
                 f"{BASE if candidates else ''}{PLAN}.")
    return "\n".join(lines) + "\n"


def agree(task, candidates, reviews, plans, reply):
    lines = _head(task, candidates, reviews, plans)
    if reviews:
        lines.append("Each plan ends with the defects found in its base, as the builders will get it.")
    lines += [
        "Approve each plan unless it misses the task or would not work; preferring the other is not a reason. "
        f"Write to {reply}:",
        f'{{"approve": {{"1": true|false, "2": true|false}}, "why": {{"<rejected plan>": "<at most {WHY_WORDS} words>"}}}}',
    ]
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    lines = [f"Implement {plan_path} in {work}" + (f", a copy of {base_label}." if base_label else ".")]
    if checks:
        lines.append(f"Run the checks there: {_checks(checks)}.")
    lines.append(f"Write a short note of what you did to {note}.")
    return "\n".join(lines) + "\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
