"""Case configuration: one reconciliation between two agents over one repo."""

import json
import pathlib
from dataclasses import dataclass, field

BOOTSTRAP = ("refs", "base")
# agent name -> (kind, model) when no case file names them
DEFAULT_MODELS = {
    "deepseek": ("opencode", "deepseek/deepseek-flash#high"),
    "kimi": ("kimi", "kimi-code/k3.1-pro"),
}
# Settings that no longer exist. Silently ignoring them would let a case file claim behaviour the
# tool does not have, so loading such a file fails and names the key.
REMOVED = {
    "canonical_agent": "both candidates have equal standing",
    "carry_forward_limit": "every difference gets a decision",
    "convergence": "there is one acceptance rule: mutual approval with evidence and passing checks",
    "accept": "there is one acceptance rule: mutual approval with evidence and passing checks",
    "strict_paths": "files are no longer classified or normalised",
    "formatter": "the approved candidate is published unchanged",
}


@dataclass
class AgentSpec:
    name: str
    kind: str
    bootstrap_ref: str | None = None
    bootstrap_exclude: list[str] = field(default_factory=list)
    model: str | None = None
    thinking: str | None = None


@dataclass
class Case:
    name: str
    host_repo: pathlib.Path
    base: str | None
    agents: dict[str, AgentSpec]
    checks: list[str] = field(default_factory=list)
    secondary_repo: pathlib.Path | None = None
    branch: str = "main"
    max_rounds: int = 3
    round_seconds: int = 3600
    stall_seconds: int = 900
    retries: int = 1
    task: dict = field(default_factory=dict)
    bootstrap: str = "refs"
    # "plan": the candidates are a plan file, not code (the first stage of a pipeline)
    mode: str = "code"

    def other(self, name):
        return next(n for n in self.agents if n != name)

    @property
    def task_prompt(self):
        return (self.task or {}).get("prompt") or ""

    @property
    def task_acceptance(self):
        return (self.task or {}).get("acceptance") or []

    @property
    def base_invariants(self):
        return (self.task or {}).get("base_behavior_invariants") or []


def _task(raw, root):
    task = raw.get("task") or {}
    if not isinstance(task, dict):
        raise ValueError("task must be an object")
    prompt, prompt_file = task.get("prompt"), task.get("prompt_file")
    if prompt and prompt_file:
        raise ValueError("task.prompt and task.prompt_file are mutually exclusive")
    resolved = {
        "prompt": prompt or "",
        "acceptance": list(task.get("acceptance") or []),
        "context_files": [],
        "base_behavior_invariants": list(task.get("base_behavior_invariants") or []),
    }
    if prompt_file:
        path = (root / prompt_file).resolve()
        if not path.exists():
            raise ValueError(f"task.prompt_file not found: {path}")
        resolved["prompt"] = path.read_text(encoding="utf-8")
    for item in task.get("context_files") or []:
        path = (root / item).resolve()
        if not path.exists():
            raise ValueError(f"task.context_files not found: {path}")
        resolved["context_files"].append(str(path))
    return resolved


def load(path):
    path = pathlib.Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent

    def resolve(value):
        if value is None:
            return None
        candidate = pathlib.Path(value)
        return candidate if candidate.is_absolute() else (root / candidate).resolve()

    for key, why in REMOVED.items():
        if key in raw:
            raise ValueError(f"{key} was removed ({why}); delete it from the case file")
    agents = {name: AgentSpec(name=name, kind=spec["kind"], bootstrap_ref=spec.get("bootstrap_ref"),
                              bootstrap_exclude=spec.get("bootstrap_exclude", []),
                              model=spec.get("model"), thinking=spec.get("thinking"))
              for name, spec in raw["agents"].items()}
    if len(agents) != 2:
        raise ValueError("a case needs exactly two agents")
    if any(a.kind not in ("opencode", "kimi") for a in agents.values()):
        raise ValueError("agent kind must be 'opencode' or 'kimi'")
    bootstrap = raw.get("bootstrap", "refs")
    if bootstrap not in BOOTSTRAP:
        raise ValueError(f"bootstrap must be one of {BOOTSTRAP}")
    base = raw["repo"].get("base")
    if bootstrap == "base" and not base:
        raise ValueError("bootstrap 'base' needs repo.base")

    return Case(
        name=raw["name"],
        host_repo=resolve(raw["repo"]["host"]),
        secondary_repo=resolve(raw["repo"].get("secondary")),
        base=base,
        branch=raw["repo"].get("branch", "main"),
        agents=agents,
        checks=raw.get("checks", []),
        max_rounds=int(raw.get("max_rounds", 3)),
        round_seconds=int(raw.get("round_seconds", 3600)),
        stall_seconds=int(raw.get("stall_seconds", 900)),
        retries=int(raw.get("retries", 1)),
        task=_task(raw, root),
        bootstrap=bootstrap,
    )


def describe(case):
    """Configuration summary for `cav check`."""
    lines = [
        f"case        {case.name}",
        f"host        {case.host_repo}",
        f"base        {case.base or 'none'}   branch {case.branch}",
        f"bootstrap   {case.bootstrap}",
        f"agents      {', '.join(f'{n} ({s.kind}, {s.model or 'default'})' for n, s in case.agents.items())}",
        f"checks      {len(case.checks)}   rounds {case.max_rounds} x {case.round_seconds}s",
        f"task        {'prompt present' if case.task_prompt.strip() else 'blank'}   "
        f"acceptance {len(case.task_acceptance)}   invariants {len(case.base_invariants)}",
    ]
    if not case.host_repo.exists():
        lines.append(f"WARNING: host repo does not exist: {case.host_repo}")
    return "\n".join(lines)
