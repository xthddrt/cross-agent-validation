"""What each agent is told. Each agent keeps one session through a cycle (judge, confirm and plan, or
plan; plan rounds; build), so the cycle's first prompt carries the task and the candidates and every
later prompt only what is new. The same for both agents, and nothing names an agent or says whose
anything is."""

import sys

PLAN_WORDS, WHY_WORDS, REAL_WORDS = 1000, 250, 100
PLAN = "the smallest changes that complete the task, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'


def severity(against):
    return ('Tag each defect "blocker" or "minor". A blocker is a security hole, wrong results on a path the task '
            f"needs, a crash, data loss, or a clear departure from {against}, shown by a scenario you can name. "
            "A minor is anything else that is wrong. Neither is style, or a different way to do the same thing.")


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks)


def plan(task, code, path):
    """A cycle's first prompt when there is nothing to judge yet; `code` is a read-only copy of the repo."""
    return (f"TASK\n{task}\n\nCODE\n- {code}\n\n"
            f"Read the code the task touches. Write a plan to {path} (at most {PLAN_WORDS} words): {PLAN}.\n")


def judge(task, candidates, reply, final=False, built_from=None, full=True):
    """A cycle's first prompt. `candidates`: (label, folder, checks, changes) in the order to list them,
    `changes` a diff against the base the candidate was built from, or None; `built_from`: the plan both
    candidates implemented, if they are builds; `full`: read everything (the run's first judge), else
    start from the changes. The final judge also names the candidate it would keep."""
    keep = ', "prefer": "X|Y"' if final else ""
    lines = ["TASK", task, ""]
    if built_from is not None:
        lines += ["PLAN (both candidates implement it)", f"- {built_from}", ""]
    lines += ["CANDIDATES"]
    lines += [f"- {label}: {folder} (checks: {checks}" + (f"; changes: {changes}" if changes else "") + ")"
              for label, folder, checks, changes in candidates] + [""]
    start = ("read every file." if full else
             "start from each one's changes and check the plan was followed, then read whatever else you need.")
    lines += [
        f"Review both candidates; {start} " + severity("the plan" if built_from is not None else "the task"),
        'List every defect, however many; one in both candidates is one entry with "candidate": "both". '
        f"Write to {reply}:",
        '{"defects": [{"candidate": "X|Y|both", "severity": "blocker|minor", "file": "...", "line": 1, '
        f'"problem": "...", "fix": "..."}}]{keep}}}',
    ]
    if keep:
        lines.append('"prefer" is the candidate you would keep.')
    return "\n".join(lines) + "\n"


def confirm(blockers, reply):
    """`blockers`: a file with the other judge's blockers, numbered."""
    return (f"BLOCKERS (another judge's)\n- {blockers}\n\n"
            "Read the code for each and decide whether it is real: a blocker as defined above, not a misreading. "
            f"Write to {reply}:\n"
            f'{{"real": {{"1": true|false, ...}}, "why": {{"<not real>": "<at most {REAL_WORDS} words, citing the code>"}}}}\n')


def fix_plan(defects, path):
    """After the confirmations: `defects` lists, per candidate, the blockers both judges confirmed and the minors."""
    return (f"DEFECTS (blockers both judges confirmed, and minor defects)\n- {defects}\n\n"
            f"Write a plan to {path} (at most {PLAN_WORDS} words): {PLAN}. It fixes every blocker in its base, "
            "citing each by file:line, fixes minor defects only where the fix is small, and takes what the other "
            f"candidate does better.{BASE}\n")


def agree(plans, reply, attached=False):
    """A vote. `plans`: (number, file); `attached`: the plans end with the defects found in their base."""
    lines = ["PLANS" + (" (each ends with the defects found in its base; the builders get it as shown)"
                         if attached else "")]
    lines += [f"- {number}: {path}" for number, path in plans] + [""]
    unfixed = ", leaves a listed blocker unfixed, or fixes a defect that is not real" if attached else ""
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
    fixes = " It fixes every blocker listed with the plans." if base else ""
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"(at most {PLAN_WORDS} words): {PLAN}.{fixes}{BASE if base else ''}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    """`base_label`: the candidate the plan builds on; such a plan ends with the defects found in it."""
    lines = [f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base_label}." if base_label else ".")]
    if base_label:
        lines.append("Fix every blocker listed at its end, and the minor defects only where the fix is small.")
    if checks:
        lines.append(f"Run the checks there: {_checks(checks)}.")
    lines.append(f"Write a note to {note}: what you changed" + (", and how you fixed each blocker." if base_label else "."))
    return "\n".join(lines) + "\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
