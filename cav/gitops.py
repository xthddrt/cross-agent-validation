"""All git work in one place. CAV owns commits and branches; agents only edit files."""

import os
import pathlib
import shutil
import subprocess

GIT = os.environ.get("CAV_GIT") or shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
EXCHANGE_REMOTE = "cav-exchange"
# A console program started from a process without a console (the dashboard runs under pythonw)
# gets a window of its own that flashes up and closes; start them all without one.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
# Commits the tool creates must not depend on the machine's git identity being configured.
IDENTITY = ("-c", "user.name=Cross-Agent Validation", "-c", "user.email=cav@localhost")


def git(*args, cwd=None, check=True, env=None):
    result = subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env, creationflags=NO_WINDOW)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_ok(*args, cwd=None):
    return subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, creationflags=NO_WINDOW).returncode == 0


class Exchange:
    """Bare repo used as the only channel between the two agents."""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def ensure(self):
        if not (self.path / "HEAD").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            git("init", "--bare", str(self.path))
        return self

    def sha(self, branch):
        return git("--git-dir", str(self.path), "rev-parse", "--verify", f"refs/heads/{branch}",
                   check=False) or None

    def tree_of(self, sha):
        return git("--git-dir", str(self.path), "rev-parse", f"{sha}^{{tree}}")


class Repo:
    """The working repo that hosts worktrees and pushes candidates to the exchange.

    Push and fetch address the exchange by path, never through a named remote: a remote is
    repo-global state, so a second run (or another tool run) would silently repoint it and
    this run's commits would land in the wrong exchange.
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.exchange_path = None
        self.namespace = "refs/cav/run"

    def attach(self, exchange, run_id=None):
        self.exchange_path = str(exchange.path)
        self.namespace = f"refs/cav/{run_id or 'run'}"
        if git("remote", "get-url", EXCHANGE_REMOTE, cwd=self.path, check=False):
            git("remote", "remove", EXCHANGE_REMOTE, cwd=self.path, check=False)
        return self

    def fetch(self):
        git("fetch", self.exchange_path, "--prune",
            f"+refs/heads/*:{self.namespace}/*", cwd=self.path)
        return self

    def sha(self, rev):
        return git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=self.path)

    def tree(self, rev):
        return git("rev-parse", f"{rev}^{{tree}}", cwd=self.path)

    def push(self, sha, branch):
        git("push", self.exchange_path, f"{sha}:refs/heads/{branch}", cwd=self.path)
        return sha

    def worktree_add(self, dest, rev):
        dest = pathlib.Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(dest), rev, cwd=self.path)
        return dest

    def worktree_remove(self, dest):
        git("worktree", "remove", "--force", str(dest), cwd=self.path, check=False)
        git("worktree", "prune", cwd=self.path, check=False)

    def commit_all(self, worktree, message):
        """Commit everything the agent changed; an unchanged worktree keeps its HEAD."""
        git("add", "-A", cwd=worktree)
        if git("status", "--porcelain", cwd=worktree):
            git(*IDENTITY, "commit", "-m", message, cwd=worktree)
        return git("rev-parse", "HEAD", cwd=worktree)
