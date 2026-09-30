"""Repo-root discovery — shared helper (techne#209).

Promoted from ``anchor.py`` so both the evidence-anchor layer and the Codex
per-repo db router resolve "which repo owns this path" the same way, rather than
each growing its own copy.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def find_repo_root(path: Path, probe: Callable[[Path], bool] | None = None) -> Path | None:
    """Walk up from *path* looking for a ``.git`` entry; return the repo root.

    Directory-aware: when *path* is an existing directory (e.g. a hook's cwd or
    a repo root) the walk starts AT it, so a repo root passed directly is found.
    Otherwise — a file, existing or deleted — the walk starts at its parent, so
    a deleted file's repo is still discoverable as long as an ancestor still
    holds ``.git``. Returns None when no ancestor is a repo.

    AN UNREADABLE ANCESTOR MEANS "NO MARKER HERE", NEVER A RAISE (techne#981),
    the same rule and the same trade as ``threads/features.py::find_repo``
    (techne#963): ``Path.exists()`` and ``Path.is_dir()`` re-raise
    ``PermissionError`` on 3.11-3.13 and swallow it only on 3.14, and this walk
    runs on the SessionStart banner path (``cards:freshness``,
    ``codex:freshness``). Guarded at the CALL SITE so it also covers an injected
    ``probe`` and a future one that opens ``.git`` rather than stat-ing it. The
    answer is the nearest READABLE marker: if the unreadable directory itself
    held the true repo, an outer one is returned with no signal.

    ``probe(candidate) -> bool`` tests one directory for the marker; tests pass a
    raising stub so the guard has teeth on 3.14 and as root.
    """
    if probe is None:

        def probe(candidate: Path) -> bool:
            return (candidate / ".git").exists()

    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    if is_dir:
        start = path
    else:
        # A file (possibly deleted), or a path we cannot stat: climb from its
        # containing directory.
        start = path.parent if path.name else path
    for candidate in [start, *start.parents]:
        try:
            hit = probe(candidate)
        except OSError:
            continue
        if hit:
            return candidate
    return None


def primary_repo_root(path: Path) -> Path | None:
    """The PRIMARY checkout that owns *path* — a linked worktree maps to its parent.

    WHY THIS EXISTS. Reviews and the worktree-per-session SOP
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
    behaviour.

    THAT FALLBACK DOES NOT MEAN THIS "CAN ONLY IMPROVE RESOLUTION, NEVER BREAK A WORKING
    INSTALL" — an earlier draft said so and it was false in both directions (Prudence,
    #719). The inference is the exact fallacy that shipped the bug: breakage never came
    from a MALFORMED piece, it came from a WELL-FORMED `commondir` in a layout where
    `parent` is not the repo root. Hence the name guard below. It is still not a
    totalising guarantee — a bare repo literally named `.git` clears both guards and
    resolves to a directory holding no checkout — which is benign, and stated rather
    than papered over.
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
        # `commondir` names the common git dir, and `parent` is the repo root ONLY when
        # that dir is literally `<root>/.git`. THE ORIGINAL CODE ASSUMED IT ALWAYS WAS,
        # AND THAT WAS FALSE IN BOTH DIRECTIONS (Prudence, #719):
        #
        #   * `.bare + worktrees` (a common layout): common dir is `<x>/.bare`, so
        #     `parent` is `<x>` — not a checkout at all. An indexed worktree that
        #     resolved fine before started resolving to something with no index.
        #   * A BARE REPO NESTED INSIDE ANOTHER REPO is the dangerous one: common dir
        #     `<outer>/.bare` gives `parent == <outer>`, whose `.git` DOES exist, so the
        #     inner repo's worktree resolved to the OUTER REPO'S INDEX — confident
        #     answers about the wrong code, which `_codex` elsewhere calls worse than
        #     saying no. Reproduced before fixing.
        #
        # So the name is checked, not just the existence of a sibling `.git`. Anything
        # else falls back to the worktree's own root, which is the pre-fix behaviour.
        if common_dir.name != ".git":
            return root
        primary = common_dir.parent
        return primary if (primary / ".git").exists() else root
    except (OSError, ValueError, IndexError):
        return root


# --- cheap git-ref reads (techne#937) --------------------------------------
# PROMOTED HERE FROM core/health.py, which is this module's stated purpose: both
# the health layer and the codex freshness check need "what sha is HEAD", and
# this file exists so each layer does not grow its own copy. Plain file reads
# only -- these run inside the SessionStart hook, where a `git` subprocess would
# put latency on every session start.


def git_dirs(root: Path) -> tuple[Path | None, Path | None]:
    """Resolve ``(git_dir, common_dir)`` for a checkout OR a linked worktree.

    In a plain clone ``.git`` is a directory and the two are the same. In a git
    **worktree** ``.git`` is a FILE holding ``gitdir: <main>/.git/worktrees/<n>``,
    and only HEAD is per-worktree — ``refs/`` and ``packed-refs`` live in the
    shared common dir named by that gitdir's ``commondir``. Worktree-per-session
    is the standing dev topology here (techne#743), so treating ``.git``-as-file
    as "not a checkout" would report every dev session's revision as unknowable.
    """
    dot = root / ".git"
    if dot.is_dir():
        return dot, dot
    try:
        raw = dot.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    if not raw.startswith("gitdir:"):
        return None, None
    git_dir = Path(raw.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    common = git_dir
    try:
        c = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        cp = Path(c)
        common = cp if cp.is_absolute() else (git_dir / cp).resolve()
    except OSError:
        pass
    return git_dir, common


def read_ref(git_dir: Path, common_dir: Path, rel: str, _depth: int = 0) -> str | None:
    """Read a sha for ``rel``, following symrefs, across both git dirs.

    Plain file reads only. :func:`collect_health` runs inside the SessionStart
    hook, so shelling out to ``git`` here would put subprocess latency on every
    session start — the constraint the old heuristic cited as its reason for
    guessing instead of looking.
    """
    # Bound the symref chain exactly as git does (its own limit is 5). A cycle
    # (HEAD -> refs/heads/loop -> refs/heads/loop) would otherwise recurse until
    # the stack blows; session_start catches it, so the banner degrades rather
    # than taking the hook down, but a corrupt repo should read as unknown here
    # rather than raise at all.
    if _depth >= 5:
        return None
    for base in (git_dir, common_dir):
        try:
            raw = (base / rel).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if raw.startswith("ref: "):
            return read_ref(git_dir, common_dir, raw[5:].strip(), _depth + 1)
        if raw:
            return raw
    # Never-loose refs (a fresh clone keeps origin/main here) live in packed-refs,
    # which is always in the common dir.
    try:
        for line in (
            (common_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
        ).splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == rel:
                return parts[0].strip()
    except OSError:
        return None
    return None


def head_sha(root: Path) -> str | None:
    """The sha HEAD points at, or None when *root* is not a resolvable checkout.

    No subprocess and no walk: one small file read plus, for a symref, one more.
    That is what lets the codex freshness check run in the banner instead of
    being marked @expensive and disappearing from it (techne#937).
    """
    git_dir, common_dir = git_dirs(root)
    if git_dir is None or common_dir is None:
        return None
    return read_ref(git_dir, common_dir, "HEAD")
