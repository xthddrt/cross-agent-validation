"""Comparing the two candidates: which paths match, which differ, and by how much."""

import subprocess

from .gitops import GIT, git


def _entries(git_dir, tree):
    result = subprocess.run([GIT, "--git-dir", str(git_dir), "ls-tree", "-r", "-z", "--full-tree", tree],
                            capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ls-tree {tree} failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    entries = {}
    for record in result.stdout.split(b"\0"):
        meta, _, path = record.partition(b"\t")
        parts = meta.split()
        if len(parts) >= 3:
            entries[path.decode("utf-8", "replace")] = parts[2].decode()
    return entries


def compare_trees(git_dir, tree_a, tree_b):
    """Every path split into identical and differing, with line counts for the differing ones."""
    entries_a, entries_b = _entries(git_dir, tree_a), _entries(git_dir, tree_b)
    paths = sorted(set(entries_a) | set(entries_b))
    stats = {}
    if entries_a != entries_b:
        # --no-renames: a rename would report "old => new" and miss both paths.
        # -z: paths are not quoted, so non-ASCII names match the ls-tree entries.
        out = git("--git-dir", str(git_dir), "diff", "--numstat", "--no-renames", "-z",
                  tree_a, tree_b, check=False)
        for record in out.split("\0"):
            parts = record.split("\t")
            if len(parts) >= 3:
                stats[parts[2]] = {"added": int(parts[0]) if parts[0].isdigit() else 0,
                                   "removed": int(parts[1]) if parts[1].isdigit() else 0}
    return {
        "trees": {"a": tree_a, "b": tree_b},
        "identical": [path for path in paths if entries_a.get(path) == entries_b.get(path)],
        "differing": [path for path in paths if entries_a.get(path) != entries_b.get(path)],
        "stats": stats,
    }


def ledger(diff):
    """One decision item per differing path, largest first, seen from tree a towards tree b."""
    stats = diff["stats"]

    def size(path):
        item = stats.get(path, {})
        return item.get("added", 0) + item.get("removed", 0)

    return [{"id": f"CF-{index + 1:02d}", "path": path,
             "added": stats.get(path, {}).get("added", 0),
             "removed": stats.get(path, {}).get("removed", 0)}
            for index, path in enumerate(sorted(diff["differing"], key=lambda path: -size(path)))]
