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

**No bias.** No prompt names an agent or says whose anything is: candidates are X and Y, labelled at random and read from plain folders exported without `.git`; files are numbered at random; each agent starts a fresh session every round, so it never reviews knowing which build is its own. Everything the agents read and write sits in a blind area outside every git repo (`%LOCALAPPDATA%\cav-blind\<random>`, or `CAV_BLIND_ROOT`); CAV commits the builds itself.

## Use

```
py -m cav serve      # the dashboard: link kept in runs\serve.json; start, watch and stop runs
py -m cav run --spec spec.json
```

`spec.json` is `{"repos": [{"repo": "<path or GitHub URL>", "branches": ["<set 1>", "<set 2>"]}, ...], "task": "..."}`, or with branches `"plan": "..."` instead of `"task"`. An empty branch in a set means that repo's main; no branches at all means a task. The dashboard's form writes it for you.

A GitHub URL is cloned to `Documents\Default Project\<repo>` (or that clone is reused and fetched); a task then starts from its default branch. On Windows use `py`, not `python` (the Store stub). Kimi Code desktop must be running.

## Layout

```
cav/flow.py      the run
cav/prompts.py   what each agent is told
cav/agents.py    OpenCode and Kimi adapters: sessions, approvals, ponytail
cav/web.py, web/ the dashboard
cav/events.py    the event log the dashboard streams
cav/gitops.py    git
cav/cli.py       run / serve
```

## Tests

```bat
py -m unittest discover -s tests -t .
```
