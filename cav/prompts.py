"""What each agent is told. Each agent keeps one session per round, so a round's first prompt carries
the task or plan and the candidates, and every later prompt only what is new. Both agents get the
same words, and nothing names an agent or says whose anything is."""

WORDS, WHY_WORDS = 1000, 100
PLAN = f"(at most {WORDS} words): the smallest changes that do it, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'
REPOS = "Each holds one folder per repository."
BLOCKER = ("A blocker is a real bug that makes the code wrong (wrong results, a crash, a security hole, data loss) "
           "or a real departure from an agreed plan, shown by a scenario you can name. Nothing else counts.")
CHECKLIST = ("First check every requirement in the agreed plans (each numbered item, acceptance criterion and must-not-"
             "regress item) in each candidate, citing file:line; one that is not met, or is present but never takes "
             "effect, is a blocker. Then look for these failure classes: concurrency and locking, resource and reference "
             "leaks, memory used in the wrong context, input and buffer handling, error paths that fail open, byte order "
             "and encoding, names or paths in the wrong form.")
TEST_GAPS = ("A missing, weak or broken test, or an unmet requirement about tests, is not a blocker when the code it "
             'covers is right; list it in "test_gaps" instead.')
TESTED = " Each fix comes with a test in the project's own tests that fails without it."


def plan(task, code, path):
    return f"TASK\n{task}\n\nCODE\n- {code}\n\n{REPOS} Read the code the task touches. Write a plan to {path} {PLAN}.\n"


def review(plans, candidates, reply, agreed=None, audit=()):
    """A round's first prompt. `plans`: every plan both agents approved, oldest first; `candidates`:
    (label, folder, checks) in the order to list them; `agreed`: the blockers both reviewers agreed last
    round, numbered, in the candidates' base; `audit`: the candidates the last review found clear,
    which this review audits."""
    lines = (["AGREED PLANS (oldest first; the code follows all of them)"] + [f"- {plan}" for plan in plans]
             + ["", "CANDIDATES"] + [f"- {label}: {folder} (checks: {checks})" for label, folder, checks in candidates])
    if agreed:
        lines += ["", "AGREED BLOCKERS (last round's, in the base both candidates were built from)", f"- {agreed}"]
    lines += [""]
    if audit:
        lines.append(f"AUDIT. The last review found no blocker in {' and '.join(audit)}. Assume it missed something "
                     "important, and find it.")
    lines += ["Each candidate holds one folder per repository. Review the plans and both candidates, reading all the code. "
              + BLOCKER + " " + CHECKLIST + " " + TEST_GAPS,
              ("List every blocker in each candidate: each agreed one still there, by its #number, and any new one. "
               if agreed else "List every blocker in each candidate. ")
              + f'One in both is one entry with "candidate": "both". Write to {reply}:',
              '{"requirements": [{"requirement": "...", "met": {"X": true|false, "Y": true|false}, "evidence": "file:line ..."}], '
              '"blockers": [{"candidate": "X|Y|both", "id": "#n or none", "file": "<path in the candidate>", "line": 1, '
              '"problem": "...", "fix": "..."}], "test_gaps": [{"candidate": "X|Y|both", "gap": "..."}]}']
    return "\n".join(lines) + "\n"


def confirm(blockers, reply):
    return (f"BLOCKERS (another reviewer's)\n- {blockers}\n\n"
            "Read the code for each: is it a blocker, as defined above, in the candidate named? "
            f"Write to {reply}:\n"
            f'{{"blocker": {{"1": true|false, ...}}, "why": {{"<not one>": "<at most {WHY_WORDS} words, citing the code>"}}}}\n')


def challenged(blockers, reply):
    """`blockers`: this reviewer's blockers the other voted down, each with the other's reason."""
    return (f"VOTED DOWN (your blockers the other reviewer did not accept, with its reason)\n- {blockers}\n\n"
            "Read each reason. Keep a blocker only if the code still shows it is one; otherwise withdraw it. "
            f"Write to {reply}:\n"
            f'{{"keep": {{"1": true|false, ...}}, "why": {{"<kept>": "<at most {WHY_WORDS} words, citing the code>"}}}}\n')


def fix_plan(blockers, plans, path):
    """`plans`: every plan both agents approved so far, oldest first."""
    return (f"AGREED BLOCKERS\n- {blockers}\n\nAGREED PLANS (oldest first; the code follows all of them)\n"
            + "".join(f"- {plan}\n" for plan in plans) +
            f"\nWrite a plan to {path} to fix every agreed blocker in one candidate, keeping to the agreed plans except "
            f"where a blocker needs otherwise {PLAN}.{TESTED} Choose as the base the candidate that needs the least work "
            f"to be right, and take what the other candidate does better.{BASE}\n")


def vote(plans, reply, fixes=False):
    lines = ["PLANS"] + [f"- {number}: {path}" for number, path in plans] + [""]
    lines.append("Read the code each plan changes and check its claims against it. Approve each plan unless it "
                 + ("misses an agreed blocker in its base, fixes one without a test, builds on the candidate that clearly "
                    "needs more work, undoes an agreed plan that no blocker requires undoing, or would not work" if fixes
                    else "misses the task or would not work")
                 + f"; preferring one is not a reason to reject the other. Write to {reply}:")
    lines.append(f'{{"approve": {{"1": true|false, "2": true|false}}, "why": {{"<rejected>": "<at most {WHY_WORDS} words>"}}}}')
    return "\n".join(lines) + "\n"


def revise(objections, path, fixes=False):
    lines = ["OBJECTIONS"] + [f"- Plan {number}: {text}" for number, texts in sorted(objections.items())
                              for text in texts] + [""]
    rules = (" Like the plans before it, it fixes every agreed blocker in its base, keeps to the agreed plans except where "
             "a blocker needs otherwise, builds on the candidate that needs the least work to be right, and takes what "
             "the other candidate does better." + TESTED + BASE) if fixes else ""
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"{PLAN}.{rules}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, note, base=None, checks=()):
    """`checks`: commands to run in `work` before finishing."""
    return (f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base}," if base else ",")
            + " with the tests it names." + (f" Run the checks there: {'; '.join(checks)}." if checks else "")
            + f"\nWrite a short note of what you changed to {note}.\n")


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
