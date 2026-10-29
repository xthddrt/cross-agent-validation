"""All git work in one place. The orchestrator owns commits and branches; agents only edit files."""

import os
import pathlib
import shutil
import subprocess

GIT = os.environ.get("CAV_GIT") or shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
EXCHANGE_REMOTE = "cav-exchange"
# Commits the tool creates must not depend on the machine's git identity being configured.
IDENTITY = ("-c", "user.name=Cross-Agent Validation", "-c", "user.email=cav@localhost")

def git(*args, cwd=None, check=True, env=None):
    result = subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_ok(*args, cwd=None):
    return subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True).returncode == 0


class Exchange:
    """Bare repo used as the only channel between the two agents."""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def ensure(self):
        if not (self.path / "HEAD").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            git("init", "--bare", str(self.path))
        return self

    def refs(self):
        out = git("--git-dir", str(self.path), "for-each-ref",
                  "--format=%(refname:short) %(objectname)", check=False)
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def sha(self, branch):
        return git("--git-dir", str(self.path), "rev-parse", "--verify", f"refs/heads/{branch}",
                   check=False) or None

    def tree(self, branch):
        sha = self.sha(branch)
        if not sha:
            return None
        return git("--git-dir", str(self.path), "rev-parse", f"{sha}^{{tree}}")

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

    def ref(self, branch):
        return f"{self.namespace}/{branch}"

    def has(self, branch):
        return git_ok("rev-parse", "--verify", "--quiet", self.ref(branch), cwd=self.path)

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

    def merge(self, worktree, rev):
        result = subprocess.run([GIT, *IDENTITY, "merge", "--no-ff", "--no-edit",
                                 "--allow-unrelated-histories", rev],
                                cwd=str(worktree), capture_output=True, text=True)
        conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False)
        message = (result.stdout + result.stderr).strip().splitlines()
        return result.returncode == 0, [c for c in conflicts.splitlines() if c], (message[-1] if message else "")

    def unmerged(self, worktree):
        out = git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False)
        return [line for line in out.splitlines() if line]

    def conflict_markers(self, worktree):
        """Files the agent left merge markers in. An agent may resolve content without staging it."""
        staged = git("diff", "--cached", "--name-only", cwd=worktree, check=False)
        found = []
        for name in staged.splitlines():
            path = pathlib.Path(worktree) / name
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in text.splitlines()):
                found.append(name)
        return found

    def commit_all(self, worktree, message):
        """Stage everything and commit, including an in-progress merge. Returns (sha, problem)."""
        worktree = pathlib.Path(worktree)
        git("add", "-A", cwd=worktree)
        unmerged = self.unmerged(worktree)
        if unmerged:
            return None, f"unresolved merge conflicts: {', '.join(unmerged[:5])}"
        markers = self.conflict_markers(worktree)
        if markers:
            return None, f"conflict markers left in: {', '.join(markers[:5])}"
        merging = git_ok("rev-parse", "--verify", "--quiet", "MERGE_HEAD", cwd=worktree)
        dirty = git("status", "--porcelain", cwd=worktree)
        if not dirty and not merging:
            return git("rev-parse", "HEAD", cwd=worktree), None
        git(*IDENTITY, "commit", "-m", message, cwd=worktree)
        return git("rev-parse", "HEAD", cwd=worktree), None

    def dirty(self, worktree):
        return bool(git("status", "--porcelain", cwd=worktree, check=False))
