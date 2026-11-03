"""Repository root discovery and POSIX path normalization helpers."""

from __future__ import annotations

import os
from pathlib import Path


def find_repo_root(path: Path | str | None = None) -> Path | None:
    """
    Walk up looking for .git (directory or file, e.g. for worktrees/submodules).
    Pure filesystem check, zero subprocess. Returns None if absent.
    """
    if path is None:
        p = Path.cwd().resolve()
    else:
        p = Path(path).resolve()
        if p.is_file():
            p = p.parent

    cur = p
    while True:
        git_entry = cur / ".git"
        if git_entry.exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def rel_posix(path: Path | str, root: Path | str) -> str:
    """Return POSIX relative path from root to path with forward slashes."""
    p = Path(path).resolve()
    r = Path(root).resolve()
    try:
        rel = p.relative_to(r)
        return rel.as_posix()
    except ValueError:
        return p.as_posix()

