"""What each agent is told. Each agent keeps one session per round, so a round's first prompt carries
the task or plan and the candidates, and every later prompt only what is new. Both agents get the
same words, and nothing names an agent or says whose anything is."""

WORDS, WHY_WORDS = 1000, 100
PLAN = f"(at most {WORDS} words): the smallest changes that do it, each with what and where, and how to check it"
BASE = ' Its first line is "Base: X" or "Base: Y", the candidate to build on.'
BLOCKER = ("A blocker is a real bug that makes the code wrong (wrong results, a crash, a security hole, data loss) "
           "or a real departure from the plan, shown by a scenario you can name. Nothing else counts.")


def plan(task, code, path):
    return f"TASK\n{task}\n\nCODE\n- {code}\n\nRead the code the task touches. Write a plan to {path} {PLAN}.\n"


def review(plan_path, candidates, reply, agreed=None):
    """A round's first prompt. `candidates`: (label, folder) in the order to list them; `agreed`: the
    blockers both reviewers agreed last round, numbered, in the candidates' base."""
    lines = ["PLAN", f"- {plan_path}", "", "CANDIDATES"] + [f"- {label}: {folder}" for label, folder in candidates]
    if agreed:
        lines += ["", "AGREED BLOCKERS (last round's, in the base both candidates were built from)", f"- {agreed}"]
    lines += ["", "Review the plan and both candidates, reading all the code. " + BLOCKER,
              ("List every blocker in each candidate: each agreed one still there, by its #number, and any new one. "
               if agreed else "List every blocker in each candidate. ")
              + f'One in both is one entry with "candidate": "both". Write to {reply}:',
              '{"blockers": [{"candidate": "X|Y|both", "id": "#n or none", "file": "...", "line": 1, '
              '"problem": "...", "fix": "..."}]}']
    return "\n".join(lines) + "\n"


def confirm(blockers, reply):
    return (f"BLOCKERS (another reviewer's)\n- {blockers}\n\n"
            "Read the code for each: is it a blocker, as defined above, in the candidate named? "
            f"Write to {reply}:\n"
            f'{{"blocker": {{"1": true|false, ...}}, "why": {{"<not one>": "<at most {WHY_WORDS} words, citing the code>"}}}}\n')


def fix_plan(blockers, path):
    return (f"AGREED BLOCKERS\n- {blockers}\n\n"
            f"Write a plan to {path} to fix every agreed blocker in one candidate {PLAN}.{BASE}\n")


def vote(plans, reply, fixes=False):
    lines = ["PLANS"] + [f"- {number}: {path}" for number, path in plans] + [""]
    lines.append("Read the code each plan changes and check its claims against it. Approve each plan unless it "
                 + ("misses an agreed blocker in its base or would not work" if fixes else "misses the task or would not work")
                 + f"; preferring one is not a reason to reject the other. Write to {reply}:")
    lines.append(f'{{"approve": {{"1": true|false, "2": true|false}}, "why": {{"<rejected>": "<at most {WHY_WORDS} words>"}}}}')
    return "\n".join(lines) + "\n"


def revise(objections, path, fixes=False):
    lines = ["OBJECTIONS"] + [f"- Plan {number}: {text}" for number, texts in sorted(objections.items())
                              for text in texts] + [""]
    lines.append(f"Write one plan from those two that answers these objections and both voters can approve, to {path} "
                 f"{PLAN}.{BASE if fixes else ''}")
    return "\n".join(lines) + "\n"


def build(plan_path, work, note, base=None, blockers=None):
    """`blockers`: fix these in `work`, a copy of `base`; the plan says what the code should do."""
    if blockers:
        first = f"Fix every blocker in {blockers} in {work}, a copy of {base}. The plan the code follows is {plan_path}."
    else:
        first = f"Implement the agreed plan, {plan_path}, in {work}" + (f", a copy of {base}." if base else ".")
    return f"{first}\nWrite a short note of what you changed to {note}.\n"


def retry_note(problem):
    return f"\nNot usable: {problem}. Answer again.\n"
