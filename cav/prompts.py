"""What each agent is told. Each agent keeps one session per round, so a round's first prompt carries
the task or plan and the candidates, and every later prompt only what is new. Both agents get the
same words, and nothing names an agent or says whose anything is."""

WORDS, WHY_WORDS = 1000, 100
PLAN = f"(at most {WORDS} words): the smallest changes that do it, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'
REPOS = "Each holds one folder per repository."
BLOCKER = ("A blocker is a defect you would refuse to ship. In code the project builds and runs, it makes the project "
           "fail at what it is for: a wrong result, a feature the task or an agreed plan requires that is missing or "
           "broken, a crash or hang, lost or corrupted user data, a security hole, or slowness that defeats the purpose. "
           "It must happen in normal use, in a scenario you can name; lost user data, a security hole or a system crash "
           "counts even when rare, if it can really happen. Not blockers: unused code, contrived timing or setups, "
           "hardening, style, comments, missing or weak tests.")
CHECKLIST = ("First check each requirement in the agreed plans (numbered items, acceptance criteria, must-not-regress "
             "items) in each candidate, citing file:line; one that is unmet, or present but never takes effect, is a "
             "departure from the plan. Then check for: concurrency and locking, resource and reference leaks, memory in the wrong "
             "context, input and buffer handling, error paths that fail open, byte order and encoding, names or paths "
             "in the wrong form.")
TESTED = " Tests only where the task asks for them."


def plan(task, code, path):
    return f"TASK\n{task}\n\nCODE\n- {code}\n\n{REPOS} Read the code the task touches. Write a plan to {path} {PLAN}.{TESTED}\n"


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
        lines.append(f"AUDIT. The last review found no blocker in {' and '.join(audit)}; assume it missed something "
                     "important and find it. It focused on the newest changes, so start with files no recent round "
                     "changed, and check each bug you find for the same pattern in every repository.")
    lines += ["Each candidate has one folder per repository. Read the plans and all the code in both. "
              + BLOCKER + " " + CHECKLIST,
              ("List every blocker in each candidate: each agreed one still there, by its #number, and any new one. "
               if agreed else "List every blocker in each candidate. ")
              + f'One in both is one entry with "candidate": "both". Write to {reply}:',
              '{"requirements": [{"requirement": "...", "met": {"X": true|false, "Y": true|false}, "evidence": "file:line ..."}], '
              '"blockers": [{"candidate": "X|Y|both", "id": "#n or none", "file": "<path in the candidate>", "line": 1, '
              '"problem": "...", "fix": "..."}]}']
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
                 + ("misses an agreed blocker in its base, builds on the candidate that clearly "
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
