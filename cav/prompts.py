"""Prompt text for a reconciliation round."""


def round_prompt(agent, other, round_no, case, worktree, review_path, other_review_path,
                 template_path, own_ref, own_sha, other_ref, other_sha, conflicts, checks):
    if conflicts:
        merge_status = "conflicts left for you to resolve: " + ", ".join(conflicts)
    else:
        merge_status = "merged cleanly, no conflicts"
    check_list = "\n".join(f"  - {c}" for c in checks) or "  (none configured)"
    if case.base:
        basis = f"Review BOTH implementations against the base commit {case.base}."
    else:
        basis = ("Review BOTH implementations as independent attempts at the same requirement. "
                 "There is no shared base commit.")
    return f"""You are {agent}, one of two independent agents reconciling two implementations of the same work.

Repository: {case.host_repo}
Round {round_no} of at most {case.max_rounds}.

Your working tree: {worktree}
It is checked out at your previous candidate and already merged with the other candidate.
Merge status: {merge_status}
Base commit: {case.base or 'none - the two implementations are independent'}

Candidates under review:
- yours:  {own_ref} = {own_sha}
- theirs: {other_ref} = {other_sha}

Do this, in order:

1. {basis} Read the code, not only the diff. Compare behaviour, not style. Findings need a file and line.
2. Keep the stronger parts of each side. Edit the files in your working tree so the result is the best
   of both. If one side is simply better for an area, say so and keep it.
3. Explain, area by area, what you kept from each side and why.
4. Write your review to this exact path: {review_path}
   Follow this template exactly: {template_path}
   It must contain exactly one line of the form `MUST_FIX: <number>`.

Rules:
- Edit only files inside your working tree.
- Do NOT run git commit, add, push, checkout, switch, reset, merge, rebase, clean or stash.
  Read-only git (status, diff, show, log) is allowed and encouraged. The orchestrator commits and
  exchanges your result, so the commit identity is not your concern.
- Do not edit the other agent's review: {other_review_path}
- Do not write anywhere under the run directory except your review path.
- Build if you change code. The orchestrator also runs this check matrix afterwards:
{check_list}
- Never claim a test you did not run.
- If the merge left conflicts, resolve them in the files and leave no conflict markers.
- Finish with the review file written. That file is how you are judged done.
"""


def retry_note(attempt, problem):
    return f"""

IMPORTANT: your previous attempt did not produce a usable result ({problem}).
Complete the task now and make sure the review file exists and contains a `MUST_FIX: <number>` line.
Do not start over; work from the current state of your working tree.
"""
