"""What each agent is told, step by step. Short, and the same for both agents: nothing names an
agent or calls anything "yours"; reasons count only when the task is missed or the work breaks."""

import sys

PLAN_WORDS, WHY_WORDS, MAX_DEFECTS = 400, 100, 5


def _sources(sources):
    if not sources:
        return []
    return (["", "BRANCHES TO RECONCILE (read-only folders)"]
            + [f"- Candidate {label}: {path}" for label, path in sorted(sources.items())])


def _checks(checks):
    return "; ".join(c.replace("{python}", f'"{sys.executable}"') for c in checks) or "none configured"


def plan(task, sources, path):
    base = '[first line "Base: Candidate X" or "Base: Candidate Y"]; ' if sources else ""
    return "\n".join([
        "You are one of two agents working on the task below.", "", "TASK", task, *_sources(sources), "",
        f"Write a plan to {path}, at most {PLAN_WORDS} words: {base}the few changes that give most of the "
        "result (the 20% of the work that delivers 80% of the value), each with what and where; what you leave "
        "out on purpose; how it is checked. Meet the task's acceptance criteria and nothing beyond them.",
        "Don't edit code. Don't ask questions.",
    ]) + "\n"


def agree(other_plan, objection, reply, sources):
    lines = [f"The other agent's plan: {other_plan}"]
    if objection:
        lines.append(f'Its objection to your plan: "{objection}"')
    lines += [
        f"Reply by writing JSON to {reply}:",
        '{"accept": true} to adopt the other plan exactly as written, or',
        f'{{"accept": false, "plan": "<your revised plan, at most {PLAN_WORDS} words>", '
        f'"why": "<what is wrong with the other plan, at most {WHY_WORDS} words>"}}.',
        "Accept unless it misses an acceptance criterion or would not work. Preferring your own plan is not a reason.",
    ]
    if sources:
        lines.append('A revised plan keeps its first line "Base: Candidate X" or "Base: Candidate Y".')
    return "\n".join(lines) + "\n"


def build(plan_path, work, checks, note, base_label=None):
    start = f" It starts from Candidate {base_label}, the plan's base." if base_label else ""
    return "\n".join([
        f"Implement this plan: {plan_path}, in {work}.{start} Write the least code that does it.",
        f"Run the checks: {_checks(checks)}.",
        f"Write what you did, in a few lines, to {note}. Change only files in {work}. No git commands. "
        "Don't ask questions.",
    ]) + "\n"


def review(plan_path, diff, work, checks_summary, reply):
    return "\n".join([
        f"Review the change against the task and the plan ({plan_path}): diff {diff}; tree {work}. "
        f"Checks: {checks_summary}.",
        f"Reply by writing JSON to {reply}: {{\"approve\": true}} or {{\"approve\": false, \"defects\": "
        '[{"file": "...", "line": 1, "problem": "...", "fix": "..."}]}:',
        f"at most {MAX_DEFECTS}, only things that are wrong or unmet; never style. Don't edit files. Don't ask questions.",
    ]) + "\n"


def fix(defects, work, checks, note):
    listed = "\n".join(f"- {d.get('file', '')}:{d.get('line', '')} {d['problem']}"
                       + (f" (fix: {d['fix']})" if d.get("fix") else "") for d in defects)
    return "\n".join([
        "The reviewer found these defects:", listed,
        f"Fix only these in {work}, run the checks again ({_checks(checks)}), and update {note}. "
        "No git commands. Don't ask questions.",
    ]) + "\n"


def retry_note(problem):
    return f"\nYour last answer was not usable: {problem}. Fix that and answer again.\n"
