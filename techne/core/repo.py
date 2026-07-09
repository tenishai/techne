"""Repo-root discovery — shared helper (techne#209).

Promoted from ``anchor.py`` so both the evidence-anchor layer and the Codex
per-repo db router resolve "which repo owns this path" the same way, rather than
each growing its own copy.
"""

from __future__ import annotations

from pathlib import Path


def find_repo_root(path: Path) -> Path | None:
    """Walk up from *path* looking for a ``.git`` entry; return the repo root.

    Directory-aware: when *path* is an existing directory (e.g. a hook's cwd or
    a repo root) the walk starts AT it, so a repo root passed directly is found.
    Otherwise — a file, existing or deleted — the walk starts at its parent, so
    a deleted file's repo is still discoverable as long as an ancestor still
    holds ``.git``. Returns None when no ancestor is a repo.
    """
    if path.is_dir():
        start = path
    else:
        # A file (possibly deleted): climb from its containing directory.
        start = path.parent if path.name else path
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None
