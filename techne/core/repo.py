"""Repo-root discovery — shared helper (techne#209).

Promoted from ``anchor.py`` so both the evidence-anchor layer and the Codex
per-repo db router resolve "which repo owns this path" the same way, rather than
each growing its own copy.
"""

from __future__ import annotations

from pathlib import Path


def find_repo_root(path: Path) -> Path | None:
    """Walk up from *path* looking for a ``.git`` entry; return the repo root.

    Works whether or not *path* itself exists — we start from its parent and
    climb, so a deleted file's repo is still discoverable as long as an ancestor
    directory still holds ``.git``. Returns None when no ancestor is a repo.
    """
    # Start from the file's directory (path may be a file that no longer exists).
    current = path.parent if path.name else path
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None
