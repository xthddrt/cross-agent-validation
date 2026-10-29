"""Mechanical comparison of candidate trees: textual classes, divergence, settlement."""

import fnmatch
import io
import pathlib
import subprocess
import tokenize

from .gitops import GIT, git

C_FAMILY = {".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".js", ".ts", ".java", ".mjs", ".cjs"}
# .cmd/.bat are not here: `#` is not a comment in batch files.
HASH_FAMILY = {".ps1", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".sys", ".exe",
                   ".dll", ".obj", ".lib", ".pdb", ".bin", ".woff", ".woff2", ".ttf"}


def matches(path, patterns):
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Git's `**/` matches zero or more directories; fnmatch requires at least one.
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False


def normalize_eol(data):
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def normalize_ws(data):
    """Line endings and trailing whitespace only. Blank lines and indentation are left alone:
    they are semantic in Makefiles, YAML and macro continuations."""
    lines = [line.rstrip(b" \t") for line in normalize_eol(data).split(b"\n")]
    return b"\n".join(lines).strip(b"\n") + b"\n"


def _strip_python_comments(text):
    """The tokenizer knows strings and docstrings: a `#` inside them is content, not a comment."""
    try:
        tokens = [token for token in tokenize.generate_tokens(io.StringIO(text).readline)
                  if token.type != tokenize.COMMENT]
        return tokenize.untokenize(tokens)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text


def strip_comments(text, suffix):
    if suffix == ".py":
        return _strip_python_comments(text)
    if suffix in C_FAMILY:
        line_marks, block = ("//",), ("/*", "*/")
    elif suffix in HASH_FAMILY:
        line_marks, block = ("#",), None
    else:
        return text
    out = []
    index = 0
    length = len(text)
    while index < length:
        if block and text.startswith(block[0], index):
            end = text.find(block[1], index + len(block[0]))
            if end < 0:
                break
            index = end + len(block[1])
            continue
        # A hash-family `#` starts a comment only at a word boundary: `${#arr}` or `url#frag` is content.
        if any(text.startswith(mark, index) for mark in line_marks) and (
                block or index == 0 or text[index - 1].isspace()):
            end = text.find("\n", index)
            if end < 0:
                break
            index = end
            continue
        char = text[index]
        if char in "\"'":
            quote = char
            out.append(char)
            index += 1
            while index < length:
                if text[index] == "\\":
                    out.append(text[index:index + 2])
                    index += 2
                    continue
                out.append(text[index])
                index += 1
                if out[-1] == quote:
                    break
            continue
        out.append(char)
        index += 1
    return "".join(out)


def classify(name, ours, theirs, strict_paths):
    """identical | formatting_candidate | comments_candidate | substantive | binary"""
    if ours == theirs:
        return "identical"
    suffix = pathlib.PurePosixPath(name).suffix.lower()
    if suffix in BINARY_SUFFIXES or b"\0" in ours or b"\0" in theirs:
        return "binary"
    if matches(name, strict_paths):
        return "substantive"
    if normalize_ws(ours) == normalize_ws(theirs):
        return "formatting_candidate"
    ours_text = normalize_eol(ours).decode("utf-8", "replace")
    theirs_text = normalize_eol(theirs).decode("utf-8", "replace")
    left = strip_comments(ours_text, suffix)
    right = strip_comments(theirs_text, suffix)
    if normalize_ws(left.encode()) == normalize_ws(right.encode()):
        return "comments_candidate"
    # A removed comment-only line leaves a blank line behind, so an added or deleted comment
    # line would otherwise look substantive. Blank lines are ignored only when the files really
    # differ in comments, never for a blank-line-only change.
    if _code_lines(left) == _code_lines(right) and _code_lines(ours_text) != _code_lines(theirs_text):
        return "comments_candidate"
    return "substantive"


def _code_lines(text):
    return [line.rstrip() for line in text.split("\n") if line.strip()]


def _entries(git_dir, tree):
    result = subprocess.run([GIT, "--git-dir", str(git_dir), "ls-tree", "-r", "-z", "--full-tree", tree],
                            capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ls-tree {tree} failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    entries = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        parts = meta.split()
        if len(parts) >= 3:
            entries[path.decode("utf-8", "replace")] = parts[2].decode()
    return entries


def _read_blobs(git_dir, shas):
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    proc = subprocess.Popen([GIT, "--git-dir", str(git_dir), "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    payload, _ = proc.communicate("".join(sha + "\n" for sha in shas).encode())
    blobs = {}
    offset = 0
    for sha in shas:
        newline = payload.find(b"\n", offset)
        if newline < 0:
            break
        header = payload[offset:newline].split()
        if len(header) < 3 or header[1] != b"blob":
            offset = newline + 1
            continue
        size = int(header[2])
        blobs[sha] = payload[newline + 1:newline + 1 + size]
        offset = newline + 1 + size + 1
    return blobs


def compare_trees(git_dir, tree_a, tree_b, strict_paths=()):
    """Classify every path between two trees. Returns the divergence artifact."""
    entries_a = _entries(git_dir, tree_a)
    entries_b = _entries(git_dir, tree_b)
    paths = sorted(set(entries_a) | set(entries_b))
    differing = [p for p in paths if entries_a.get(p) != entries_b.get(p)]
    blobs = _read_blobs(git_dir, [s for p in differing for s in (entries_a.get(p), entries_b.get(p)) if s])

    files = {key: [] for key in
             ("identical", "formatting_candidate", "comments_candidate", "substantive", "binary")}
    for path in paths:
        ours, theirs = entries_a.get(path), entries_b.get(path)
        if ours == theirs:
            files["identical"].append(path)
            continue
        kind = classify(path, blobs.get(ours, b""), blobs.get(theirs, b""), strict_paths)
        files[kind].append(path)

    stats = {}
    if differing:
        # --no-renames: git's rename detection reports "old => new" and the stats miss both paths.
        # -z: paths are not quoted, so non-ASCII names match the ls-tree entries.
        out = git("--git-dir", str(git_dir), "diff", "--numstat", "--no-renames", "-z",
                  tree_a, tree_b, check=False)
        for record in out.split("\0"):
            parts = record.split("\t")
            if len(parts) >= 3:
                added = int(parts[0]) if parts[0].isdigit() else 0
                removed = int(parts[1]) if parts[1].isdigit() else 0
                stats[parts[2]] = {"added": added, "removed": removed}

    return {
        "trees": {"a": tree_a, "b": tree_b},
        "files": files,
        "stats": stats,
        "totals": {key: len(value) for key, value in files.items()},
    }


def carry_forward(git_dir, source_tree, baseline_tree, strict_paths=(), limit=50):
    """Prior-candidate paths whose content is not present in the shared baseline."""
    diff = compare_trees(git_dir, source_tree, baseline_tree, strict_paths)
    paths = [p for p in diff["files"]["substantive"] + diff["files"]["binary"]]
    paths.sort(key=lambda p: -(diff["stats"].get(p, {}).get("added", 0)
                               + diff["stats"].get(p, {}).get("removed", 0)))
    items = [{"id": f"CF-{index + 1:02d}", "path": path,
              "added": diff["stats"].get(path, {}).get("added", 0),
              "removed": diff["stats"].get(path, {}).get("removed", 0)}
             for index, path in enumerate(paths[:limit])]
    return {"items": items, "total": len(paths), "omitted": max(0, len(paths) - len(items))}


def apply_safe_normalization(worktree, strict_paths=(), paths=None):
    """Line endings and trailing whitespace on files that are not `strict_paths`.

    `paths` restricts the pass to the files that actually differ cosmetically between the two
    candidates; without it every file in the worktree is rewritten, which would make the final
    commit differ from the candidate the agents reviewed.
    """
    worktree = pathlib.Path(worktree)
    changed = []
    targets = [worktree / item for item in paths] if paths is not None else sorted(worktree.rglob("*"))
    for path in targets:
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(worktree).as_posix()
        if relative.startswith(".git/") or matches(relative, strict_paths):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            original = path.read_bytes()
        except OSError:
            continue
        if b"\0" in original:
            continue
        normalized = normalize_ws(original)
        if normalized != original:
            path.write_bytes(normalized)
            changed.append(relative)
    return changed
