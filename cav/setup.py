"""`cav setup`, the same on Windows and Linux: check what a run needs and put ponytail in every coding agent.

Each step finds its tool the way a run does (agents.py knows each platform's install folders), so a step that
passes here passes in a run. Safe to run again: whatever is already installed is left alone.
"""

import os
import shutil
import subprocess
import sys

from . import agents
from .gitops import GIT, NO_WINDOW

PONYTAIL_GITHUB = "https://github.com/DietrichGebert/ponytail"
PONYTAIL_NPM = "@dietrichgebert/ponytail"


def _run(cmd, timeout=300):
    """A CLI step: (ok, output). Its output is shown, since an install can say why it failed."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=NO_WINDOW,
                                env=dict(os.environ, PATH=agents.with_tool_dirs(os.environ.get("PATH", ""))))
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _has_ponytail(output):
    return "ponytail" in output.lower()


def opencode():
    cli = agents._opencode_cli()
    ok, listed = _run([cli, "plugin", "list"])
    if ok and _has_ponytail(listed):
        return "ponytail already installed"
    ok, output = _run([cli, "plugin", "add", PONYTAIL_NPM])
    if not ok:
        raise RuntimeError(f"opencode plugin add {PONYTAIL_NPM} failed: {output[-500:]}")
    return f"installed {PONYTAIL_NPM}"


def kimi():
    kimi = agents.KimiAgent(agents.AgentSpec("kimi", "kimi"), os.devnull)
    try:
        kimi._connect()
    except (OSError, ValueError, KeyError, RuntimeError):
        raise RuntimeError("Kimi Code is not running; start the Kimi Code app, then run setup again")

    def installed():
        plugins = (kimi._call("GET", "/api/v1/plugins").get("data") or {}).get("plugins") or []
        return [plugin for plugin in plugins if _has_ponytail(str(plugin.get("name") or plugin.get("id") or ""))]

    found = installed()
    if not found:
        for _ in range(2):  # a fresh Kimi profile's first install can answer success and install nothing
            kimi._call("POST", "/api/v1/plugins", {"source": PONYTAIL_GITHUB}, timeout=300)
            found = installed()
            if found:
                break
        if not found:
            raise RuntimeError(f"Kimi Code accepted {PONYTAIL_GITHUB} but does not list ponytail")
        detail = f"installed {PONYTAIL_GITHUB}"
    else:
        detail = "ponytail already installed"
    for plugin in found:
        if plugin.get("enabled") is False:
            name = plugin.get("id") or plugin.get("name")
            kimi._call("POST", f"/api/v1/plugins/{name}:enable", {})
            detail += ", enabled"
    agents._ponytail_plugin()  # the copy the Claude agent loads; raises if it is not where a run looks
    return detail


def claude():
    cli = agents._claude_cli()
    ok, listed = _run([cli, "plugin", "list"])
    if ok and _has_ponytail(listed):
        return "ponytail already installed"
    ok, marketplaces = _run([cli, "plugin", "marketplace", "list"])
    if not (ok and _has_ponytail(marketplaces)):
        ok, output = _run([cli, "plugin", "marketplace", "add", "DietrichGebert/ponytail"])
        if not ok:
            raise RuntimeError(f"claude plugin marketplace add failed: {output[-500:]}")
    ok, output = _run([cli, "plugin", "install", "ponytail@ponytail"])
    if not ok:
        raise RuntimeError(f"claude plugin install ponytail@ponytail failed: {output[-500:]}")
    return "installed ponytail@ponytail"


def tools():
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python 3.10 or newer is needed, this is {sys.version.split()[0]}")
    if not shutil.which(GIT):
        raise RuntimeError("git not found; install it (or set CAV_GIT)")
    return f"Python {sys.version.split()[0]}, git"


def node():
    if not shutil.which("node", path=agents.with_tool_dirs(os.environ.get("PATH", ""))):
        raise RuntimeError("node not found: ponytail's Claude Code hooks run on Node; install Node "
                           f"({'nodejs.org' if agents.WINDOWS else 'nodejs.org, or into ~/.local/bin'}) "
                           "if you run the claude agent")
    return "found"


# Kimi before Claude: the Claude agent loads the ponytail copy that Kimi installs
STEPS = (("git", tools), ("node", node), ("opencode", opencode), ("kimi", kimi), ("claude", claude))


def main(only=None):
    print(f"cav setup on {'Windows' if agents.WINDOWS else sys.platform}")
    failed = 0
    for name, step in STEPS:
        if only and name not in only:
            continue
        try:
            print(f"  ok    {name}: {step()}", flush=True)
        except RuntimeError as error:
            failed += 1
            print(f"  FAIL  {name}: {error}", flush=True)
    print("ready" if not failed else f"{failed} step(s) need attention; fix them and run setup again")
    return 1 if failed else 0
