# cross-agent-validation

Two AI agents (DeepSeek in **OpenCode**, Kimi in **Kimi Code**) take a task, or a plan and two sets of branches, across one or more repositories, to one result both agree has no blockers.

With several repos, a candidate is one branch per repo, so X and Y are each a set: the agents read one folder per repo, every build changes the whole set, and the result is a branch in every repo.

```
task      (repos + task) both write a plan, then vote on both; a plan both approve is the plan, otherwise
          both revise from the objections and vote again, until one is approved. Both build it.
review    (plan + two branch sets, or the two builds) both review every agreed plan and both candidates,
          blind, and list only blockers: real bugs that make the code wrong, or real departures from an
          agreed plan (the first plan, and every fix plan since).
confirm   each votes on the other's blockers; a blocker both agree on stays, the rest are dropped.
          A candidate with no agreed blocker is the result.
fix       after every review with no clear candidate: both plan the fix of every agreed blocker on a
          base they choose, keeping to every plan agreed before (each fix plan sees them all), voting
          and revising until one plan is approved; both build it. The next review checks those
          blockers, and new ones may be raised; both must agree on each.
result    kept as the local branch cav/<run> in every repo. Nothing is pushed.
```

There is no round limit: a run ends when a candidate has no blocker both agents agree on, or when you stop it.

**No bias.** Candidates and answer files are labelled at random, exported without `.git`, and reviewed in fresh sessions.
Agents work outside every repository, in the blind folders below (`<blind folder>/<random>`); CAV commits the builds.

## Setup

Windows and Linux run the same code; CAV picks each platform's folders and commands itself. Python 3.10 or newer, git,
and the OpenCode and Kimi Code desktop apps (Kimi Code running). Then, once per machine:

```
py -m cav setup        # Windows
python3 -m cav setup   # Linux
```

It checks git and Node, finds each agent's CLI, and installs ponytail in OpenCode, Kimi Code and Claude Code (whatever
is already installed is left alone; run it again after fixing a step it reports). Node is for ponytail's Claude Code hooks.
On Windows use `py`, not `python` (the Store stub). Below, `py` stands for `python3` on Linux.

| | Windows | Linux |
|---|---|---|
| OpenCode CLI | `%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources` | `/opt/OpenCode/resources` |
| | then the newest in `%APPDATA%\ai.opencode.desktop\cli`, then `opencode` on PATH | then the newest in `~/.config/ai.opencode.desktop/cli`, then `opencode` on PATH |
| Claude Code CLI | `claude` on PATH, `~\.local\bin`, then the Claude desktop app's own | the same, in `~/.local/bin` and `~/.config/Claude/claude-code` |
| Added to agents' PATH | `C:\Program Files\Git\cmd`, `C:\Program Files\nodejs` | `~/.local/bin` |
| Agents' blind folders | `%LOCALAPPDATA%\cav-blind` | `~/.local/share/cav-blind` |
| GitHub clones | `Documents\Default Project` | `~/Documents/Default Project` |
| `checks` run in | `cmd` | `/bin/sh` |

Override any of them with `CAV_OPENCODE_CLI`, `CAV_CLAUDE_CLI`, `CAV_GIT`, `CAV_BLIND_ROOT`, `CAV_CLONE_ROOT` or `CAV_PONYTAIL_DIR`.

## Use

```
py -m cav serve      # the dashboard: link kept in runs/serve.json; start, watch and stop runs
py -m cav run --spec spec.json
```

`spec.json` is `{"repos": [{"repo": "<path or GitHub URL>", "branches": ["<set 1>", "<set 2>"]}, ...], "task": "..."}`, or with branches `"plan": "..."` instead of `"task"`. An empty branch in a set means that repo's main; no branches at all means a task. The dashboard's form writes it for you.

A GitHub URL is cloned to the GitHub clones folder above (or that clone is reused and fetched); a task then starts from its default branch. Kimi Code desktop must be running.

## Tests

```
py -m unittest discover -s tests -t .
```
