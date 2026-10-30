"""What each agent is told. Each agent keeps one session through a cycle (audit or verify, triage and
plan, or plan; plan rounds; build), so the cycle's first prompt carries the task and the candidates and
every later prompt only what is new. The same for both agents, and nothing names an agent or says
whose anything is."""

import sys

PLAN_WORDS, WHY_WORDS, REAL_WORDS = 1000, 250, 100
PLAN = "the smallest changes that complete the task, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'
NO_NEW = "new command, tool, mode or state machine"
DEFER = (f'It fixes every blocker in its base, citing each by its #number, except one that would need a {NO_NEW}: '
         'name those on a last line "Deferred: #n, #m". It adds none itself.')
DEFECTS = ('{"defects": [{"candidate": "X|Y|both", "severity": "blocker|minor", "file": "...", "line": 1, '
           '"problem": "...", "fix": "..."}]}')


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


def _head(task, candidates, built_from, ledger=None):
    lines = ["TASK", task, ""]
    if built_from is not None:
        lines += ["PLAN (both candidates implement it)", f"- {built_from}", ""]
    if ledger is not None:
        lines += ["LEDGER (blockers both judges agreed; each must be fixed)", f"- {ledger}", ""]
    lines += ["CANDIDATES"]
    lines += [f"- {label}: {folder} (checks: {checks}" + (f"; changes: {changes}" if changes else "") + ")"
              for label, folder, checks, changes in candidates] + [""]
    return lines


def audit(task, candidates, reply, final=False, built_from=None):
    """The run's first judge, a cycle's first prompt. `candidates`: (label, folder, checks, changes) in the
    order to list them; `built_from`: the plan both candidates implemented, if they are builds."""
    keep = ', "prefer": "X|Y"' if final else ""
    lines = _head(task, candidates, built_from) + [
        "Review both candidates; read every file. " + severity("the plan" if built_from is not None else "the task"),
        'List every defect, however many; one in both candidates is one entry with "candidate": "both". '
        f"Write to {reply}:",
        DEFECTS[:-1] + keep + "}",
    ]
    if keep:
        lines.append('"prefer" is the candidate you would keep.')
    return "\n".join(lines) + "\n"


def more(reply):
    """Straight after the audit, in the same session."""
    return (f"Look again, in every file, for defects you have not listed. Write them to {reply} in the same form, "
            'or {"defects": []} if there are none.\n')


def verify(task, candidates, reply, ledger, final=False, built_from=None):
    """A later judge, a cycle's first prompt: the ledger's blockers, fixed or not, then what the changes broke.
    `ledger`: a file of the open blockers, numbered #n, or None if there are none."""
    keep = ', "prefer": "X|Y"' if final else ""
    lines = _head(task, candidates, built_from, ledger)
    fixed = '"fixed": {"X": {"<n>": true|false, ...}, "Y": {...}}, ' if ledger is not None else ""
    lines += [
        ("For each candidate, check every ledger blocker against the code: fixed or not. Then review " if ledger
         else "Review ") + "each candidate's changes for anything they broke, and the rest of the code only for "
        "blockers. " + severity("the plan" if built_from is not None else "the task"),
        'List every new defect, however many; one in both candidates is one entry with "candidate": "both". '
        f"Write to {reply}:",
        "{" + fixed + DEFECTS[1:-1] + keep + "}",
    ]
    if keep:
        lines.append('"prefer" is the candidate you would keep.')
    return "\n".join(lines) + "\n"


def triage(blockers, reply):
    """`blockers`: a file with the other judge's blockers, numbered."""
    return (f"BLOCKERS (another judge's)\n- {blockers}\n\n"
            'Read the code for each and rate it "blocker", "minor" or "not real" (a misreading), as defined above. '
            f"Write to {reply}:\n"
            f'{{"rating": {{"1": "blocker|minor|not real", ...}}, "why": {{"<not a blocker>": "<at most {REAL_WORDS} '
            'words, citing the code>"}}\n')


def challenged(blockers, reply):
    """`blockers`: a file with this judge's blockers the other rated lower, each with the other's reason."""
    return (f"CHALLENGED (your blockers another judge rated lower, with its reason)\n- {blockers}\n\n"
            'Read each reason and rate the defect again: "blocker", "minor" or "not real". '
            f"Write to {reply}:\n"
            f'{{"rating": {{"1": "blocker|minor|not real", ...}}, "why": {{"<still a blocker>": "<at most {REAL_WORDS} '
            'words, citing the code>"}}\n')


def fix_plan(defects, path):
    """After triage: `defects` lists, per candidate, the ledger's open blockers (#n) and the minor defects."""
    return (f"DEFECTS (the ledger's open blockers, which both judges agreed, and minor defects)\n- {defects}\n\n"
            f"Write a plan to {path} (at most {PLAN_WORDS} words): {PLAN}. {DEFER} It fixes minor defects only "
            f"where the fix is small, and takes what the other candidate does better.{BASE}\n")


def agree(plans, reply, attached=False):
    """A vote. `plans`: (number, file); `attached`: the plans end with the defects found in their base."""
    lines = ["PLANS" + (" (each ends with the defects found in its base; the builders get it as shown)"
                         if attached else "")]
    lines += [f"- {number}: {path}" for number, path in plans] + [""]
    unfixed = (", leaves a listed blocker unfixed without deferring it, defers one a small change could fix, "
               f"fixes a defect that is not real, or adds a {NO_NEW}" if attached else "")
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
    fixes = f" {DEFER}" if base else ""
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"(at most {PLAN_WORDS} words): {PLAN}.{fixes}{BASE if base else ''}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    """`base_label`: the candidate the plan builds on; such a plan ends with the defects found in it."""
    lines = [f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base_label}." if base_label else ".")]
    if base_label:
        lines.append(f"Fix every blocker listed at its end except those it defers, and the minor defects only where "
                     f"the fix is small. Add no {NO_NEW}.")
    if checks:
        lines.append(f"Run the checks there: {_checks(checks)}.")
    lines.append(f"Write a note to {note}: what you changed" + (", and how you fixed each blocker." if base_label else "."))
    return "\n".join(lines) + "\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
