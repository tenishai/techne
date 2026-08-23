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


def primary_repo_root(path: Path) -> Path | None:
    """The PRIMARY checkout that owns *path* — a linked worktree maps to its parent.

    WHY THIS EXISTS (tenishai/agents#53). Reviews and the worktree-per-session SOP
    (techne#376/#740) run inside `git worktree` checkouts, and `find_repo_root` stops at
    the worktree because a worktree has its own `.git`. The codex is db-per-repo and is
    indexed against the PRIMARY checkout, so every query from inside a worktree resolved
    to a path with no index and answered "no codex index for the active repo". Measured
    across 340 posted reviews: the impact pass ran in 6% of them, because the reviewer's
    persona correctly says to fall back to grep when codex returns nothing. The tool was
    pointed at the wrong path; nobody was ignoring instructions.

    Resolving to the primary is also the SEMANTICALLY RIGHT answer, not merely the one
    with a db: the impact pass asks "what calls this TODAY, on the base branch, that the
    PR must not break". The worktree is checked out at the PR head. The primary's graph
    is the question being asked.

    READ PATH ONLY — see `resolve_repo_db_for_query`. The write path deliberately does
    NOT follow this, or `reindex_edited_file` would write a PR branch's symbols into the
    base graph that other sessions are querying.

    NO STRING HEURISTICS. Git's own layout answers it: a linked worktree's `.git` is a
    FILE pointing at `<primary>/.git/worktrees/<name>`, and that directory holds a
    `commondir` file naming `<primary>/.git`. `coord.py` warns from experience against
    splitting on `/worktrees/` — a repo whose own path contains that segment defeats it.
    Any malformed or missing piece falls back to *path*'s own root, which is today's
    behaviour, so this can only improve resolution and never break it.
    """
    root = find_repo_root(path)
    if root is None:
        return None
    dot_git = root / ".git"
    if dot_git.is_dir():
        return root  # the primary checkout
    try:
        # ".git" is a file: "gitdir: <abs-or-rel path to the worktree's git dir>"
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return root
        git_dir = Path(pointer.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        # `commondir` names the primary's `.git`; the repo root is its parent.
        primary = common_dir.parent
        return primary if (primary / ".git").exists() else root
    except (OSError, ValueError, IndexError):
        return root
