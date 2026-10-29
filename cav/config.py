"""Case configuration: one reconciliation between two agents over one repo."""

import json
import pathlib
from dataclasses import dataclass, field

CONVERGENCE = ("strict", "functional")
BOOTSTRAP = ("refs", "base")


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
    description: str = ""
    branch: str = "main"
    max_rounds: int = 3
    round_seconds: int = 3600
    stall_seconds: int = 900
    poll_seconds: int = 10
    retries: int = 1
    path: pathlib.Path | None = None

    version: int = 1
    task: dict = field(default_factory=dict)
    convergence: str = "strict"
    canonical_agent: str | None = None
    strict_paths: list[str] = field(default_factory=list)
    formatter: str | None = None
    carry_forward_limit: int = 50
    bootstrap: str = "refs"

    def other(self, name):
        return next(n for n in self.agents if n != name)

    def canonical(self):
        return self.canonical_agent or next(iter(self.agents))

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
        "prompt_file": None,
        "acceptance": list(task.get("acceptance") or []),
        "context_files": [],
        "base_behavior_invariants": list(task.get("base_behavior_invariants") or []),
    }
    if prompt_file:
        path = (root / prompt_file).resolve()
        if not path.exists():
            raise ValueError(f"task.prompt_file not found: {path}")
        resolved["prompt_file"] = str(path)
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

    agents = {}
    for name, spec in raw["agents"].items():
        agents[name] = AgentSpec(
            name=name,
            kind=spec["kind"],
            bootstrap_ref=spec.get("bootstrap_ref"),
            bootstrap_exclude=spec.get("bootstrap_exclude", []),
            model=spec.get("model"),
            thinking=spec.get("thinking"),
        )
    if len(agents) != 2:
        raise ValueError("a case needs exactly two agents")
    if any(a.kind not in ("opencode", "kimi") for a in agents.values()):
        raise ValueError("agent kind must be 'opencode' or 'kimi'")

    convergence = raw.get("convergence") or raw.get("accept") or "strict"
    if convergence not in CONVERGENCE:
        raise ValueError(f"convergence must be one of {CONVERGENCE}")
    canonical = raw.get("canonical_agent")
    if canonical and canonical not in agents:
        raise ValueError(f"canonical_agent {canonical!r} is not one of the agents")
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
        description=raw.get("description", ""),
        agents=agents,
        checks=raw.get("checks", []),
        max_rounds=int(raw.get("max_rounds", 3)),
        round_seconds=int(raw.get("round_seconds", 3600)),
        stall_seconds=int(raw.get("stall_seconds", 900)),
        poll_seconds=float(raw.get("poll_seconds", 10)),
        retries=int(raw.get("retries", 1)),
        path=path,
        version=int(raw.get("version", 1)),
        task=_task(raw, root),
        convergence=convergence,
        canonical_agent=canonical or next(iter(agents)),
        strict_paths=list(raw.get("strict_paths") or []),
        formatter=raw.get("formatter"),
        carry_forward_limit=int(raw.get("carry_forward_limit", 50)),
        bootstrap=bootstrap,
    )


def describe(case):
    """Configuration summary for `cav check`."""
    lines = [
        f"case        {case.name} (schema v{case.version})",
        f"host        {case.host_repo}",
        f"base        {case.base or 'none'}   branch {case.branch}",
        f"bootstrap   {case.bootstrap}",
        f"agents      {', '.join(f'{n} ({s.kind}, {s.model or 'default'})' for n, s in case.agents.items())}",
        f"convergence {case.convergence}   canonical {case.canonical()}",
        f"checks      {len(case.checks)}   strict_paths {len(case.strict_paths)}",
        f"task        {'prompt present' if case.task_prompt.strip() else 'blank'}   "
        f"acceptance {len(case.task_acceptance)}   invariants {len(case.base_invariants)}",
        f"carry_fwd   limit {case.carry_forward_limit}",
    ]
    if not case.host_repo.exists():
        lines.append(f"WARNING: host repo does not exist: {case.host_repo}")
    if case.convergence == "functional" and case.version < 2:
        lines.append("WARNING: functional convergence on a v1 case; structured reviews are required")
    return "\n".join(lines)
