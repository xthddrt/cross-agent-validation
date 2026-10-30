"""Git, run without a console window and with a fixed identity for the commits CAV makes."""

import os
import pathlib
import re
import shutil
import subprocess

GIT = os.environ.get("CAV_GIT") or shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
GITHUB = re.compile(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
CLONE_ROOT = pathlib.Path(os.environ.get("CAV_CLONE_ROOT") or pathlib.Path.home() / "Documents" / "Default Project")
# A console program started from a process without a console (the dashboard runs under pythonw)
# gets a window of its own that flashes up and closes; start them all without one.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
IDENTITY = ("-c", "user.name=Cross-Agent Validation", "-c", "user.email=cav@localhost")


def git(*args, cwd=None, check=True, env=None):
    result = subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_ok(*args, cwd=None):
    return subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, creationflags=NO_WINDOW).returncode == 0


def resolve_repo(spec, clone_root=None):
    """A local git repo as given, or a GitHub repo cloned to (or reused, and fetched, from) the clone root."""
    text = (spec or "").strip().strip('"')
    match = GITHUB.match(text)
    if not match:
        path = pathlib.Path(text).expanduser()
        if not text or not path.is_dir() or not git_ok("rev-parse", "--git-dir", cwd=path):
            raise ValueError(f"not a git repository or GitHub URL: {spec!r}")
        return path.resolve()
    url = f"https://github.com/{match.group(1)}/{match.group(2)}.git"
    target = pathlib.Path(clone_root or CLONE_ROOT) / match.group(2)
    if target.exists():
        origin = git("remote", "get-url", "origin", cwd=target, check=False)
        if GITHUB.sub(r"\1/\2", origin).lower() != f"{match.group(1)}/{match.group(2)}".lower():
            raise ValueError(f"{target} already exists with origin {origin or '(none)'}, not {url}")
        git("fetch", "-q", "origin", cwd=target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", url, str(target))
    return target.resolve()


def resolve_ref(repo, ref):
    """A branch's commit, as given or as fetched from origin."""
    for name in (ref, f"origin/{ref}"):
        sha = git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", cwd=repo, check=False)
        if sha:
            return sha
    raise ValueError(f"branch {ref!r} not found in {repo}")
