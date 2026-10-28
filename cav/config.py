"""Case configuration: one reconciliation between two agents over one repo."""

import json
import pathlib
from dataclasses import dataclass, field


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

    def other(self, name):
        return next(n for n in self.agents if n != name)


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

    return Case(
        name=raw["name"],
        host_repo=resolve(raw["repo"]["host"]),
        secondary_repo=resolve(raw["repo"].get("secondary")),
        base=raw["repo"].get("base"),
        branch=raw["repo"].get("branch", "main"),
        description=raw.get("description", ""),
        agents=agents,
        checks=raw.get("checks", []),
        max_rounds=int(raw.get("max_rounds", 3)),
        round_seconds=int(raw.get("round_seconds", 3600)),
        stall_seconds=int(raw.get("stall_seconds", 900)),
        poll_seconds=int(raw.get("poll_seconds", 10)),
        retries=int(raw.get("retries", 1)),
        path=path,
    )
