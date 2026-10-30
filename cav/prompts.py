"""What each agent is told. Each agent keeps one session through a cycle (judge or plan, plan rounds,
build), so the cycle's first prompt carries the task and the candidates and every later prompt only
what is new. The same for both agents, and nothing names an agent or says whose anything is."""

import sys

PLAN_WORDS, WHY_WORDS = 1000, 250
PLAN = "the smallest changes that complete the task, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'


def defect(against):
    return (f"A defect is a code bug (broken logic, wrong results, a crash, a security hole or data loss, "
            f"shown by a scenario you can name) or a clear departure from {against}. "
            f"Not style, or a different way to do the same thing.")


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks)


def plan(task, code, path):
    """A cycle's first prompt when there is nothing to judge yet; `code` is a read-only copy of the repo."""
    return (f"TASK\n{task}\n\nCODE\n- {code}\n\n"
            f"Read the code the task touches. Write a plan to {path} (at most {PLAN_WORDS} words): {PLAN}.\n")


def judge(task, candidates, reply, plan_path=None, built_from=None):
    """A cycle's first prompt. `candidates`: (label, folder, checks) in the order to list them;
    `built_from`: the plan both candidates implemented, if they are builds. With `plan_path` the judge
    also plans the next build; without, it names the candidate it would keep."""
    keep = ', "prefer": "X|Y"' if plan_path is None else ""
    lines = ["TASK", task, ""]
    if built_from is not None:
        lines += ["PLAN (both candidates implement it)", f"- {built_from}", ""]
    lines += ["CANDIDATES"]
    lines += [f"- {label}: {folder} (checks: {checks})" for label, folder, checks in candidates] + [""]
    lines += [
        "Review both candidates; read every file. " + defect("the plan" if built_from is not None else "the task"),
        "List every defect in each candidate, however many. Approve a candidate only if it has none. "
        f"Write to {reply}:",
        '{"approve": {"X": true|false, "Y": true|false}, "defects": [{"candidate": "X|Y", "file": "...", '
        f'"line": 1, "problem": "...", "fix": "..."}}]{keep}}}',
    ]
    if keep:
        lines.append('"prefer" is the candidate you would keep.')
    if plan_path is not None:
        lines.append(f"Then write a plan to {plan_path} (at most {PLAN_WORDS} words): {PLAN}. It fixes every "
                     f"defect you listed in its base, citing each by file:line, and takes what the other candidate "
                     f"does better.{BASE}")
    return "\n".join(lines) + "\n"


def agree(plans, reply, reviews=(), attached=False):
    """A vote. `plans`: (number, file); `reviews`: the judges' reviews, listed in the first round only;
    `attached`: the plans end with the defects the reviews found in their base."""
    lines = []
    if reviews:
        lines += ["REVIEWS"] + [f"- {path}" for path in sorted(map(str, reviews))] + [""]
    lines += ["PLANS" + (" (each ends with the defects found in its base; the builders get it as shown)"
                         if attached else "")]
    lines += [f"- {number}: {path}" for number, path in plans] + [""]
    unfixed = ", leaves a real listed defect unfixed, or fixes one that is not real" if attached else ""
    lines += [
        "Read the code each plan changes and check its claims against it. Approve each plan unless it misses the "
        f"task, would not work{unfixed}; preferring one plan is not a reason to reject the other. "
        f"Write to {reply}:",
        f'{{"approve": {{"1": true|false, "2": true|false}}, "why": {{"<rejected plan>": "<at most {WHY_WORDS} words>"}}}}',
    ]
    return "\n".join(lines) + "\n"


def revise(objections, path, base=False):
    """After a vote with no plan approved by both. `objections`: plan number -> the reasons."""
    lines = ["OBJECTIONS"] + [f"- Plan {number}: {text}" for number, texts in sorted(objections.items())
                              for text in texts] + [""]
    fixes = " It fixes every defect listed with the plans, or shows from the code why one is not real." if base else ""
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"(at most {PLAN_WORDS} words): {PLAN}.{fixes}{BASE if base else ''}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    """`base_label`: the candidate the plan builds on; such a plan ends with the defects found in it."""
    lines = [f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base_label}." if base_label else ".")]
    if base_label:
        lines.append("Fix every defect listed at its end, except one the plan shows is not real.")
    if checks:
        lines.append(f"Run the checks there: {_checks(checks)}.")
    lines.append(f"Write a note to {note}: what you changed" + (", and for each listed defect, how you fixed it or that the plan shows it is not real." if base_label else "."))
    return "\n".join(lines) + "\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
