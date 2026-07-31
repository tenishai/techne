#!/usr/bin/env python3
"""techne editable-install preflight guard (standalone, cross-platform).

Wired by ``register_hooks()`` as the FIRST SessionStart hook, ahead of
``techne.hooks.session_start``. Its job: detect when techne's editable-install
pointer has been hijacked to an ephemeral git worktree — which happens when an
agent runs ``pip install -e .`` from inside a ``.claude/worktrees/...`` checkout,
stamping that path into the interpreter-global ``.pth``. Once the worktree is
pruned the pointer dangles, ``import techne`` dies, and it takes the MCP server
(``-32000``) and every techne hook down with it.

DESIGN — this file is deliberately standalone:

  * It does NOT ``import techne`` for its own logic. It cannot: if the pointer is
    dead, importing techne is exactly what fails, so ``python -m
    techne.hooks.preflight`` would be a chicken-and-egg trap. It is invoked by
    absolute path instead.
  * It is self-locating. The canonical repo is derived from THIS file's own path
    (it ships inside the repo at ``techne/platforms/claude_code/``), so there is
    nothing machine-specific to hardcode. It works identically on macOS and
    Linux: the interpreter is whatever ``register_hooks`` wired (``sys.executable``
    at init time), and the repo path is wherever this file lives.

Silent when healthy (no output, no pip). Prints a notice to stderr only when it
repairs, or when it deliberately declines to.
"""
import importlib
import subprocess
import sys
from pathlib import Path

# Canonical repo root = four levels up from this file:
#   <repo>/techne/platforms/claude_code/preflight_guard.py
CANONICAL = Path(__file__).resolve().parents[3]


def _is_worktree_path(p: Path) -> bool:
    """True if ``p`` is a git worktree — ASK GIT, don't guess from the path.

    KEEP IN SYNC with the identical copy in ``hooks._is_worktree_path``. The
    duplication is deliberate: this module runs as a bare script *before* techne is
    known to be importable — importing from techne would defeat the guard's whole
    purpose — so it cannot share the helper. ``tests/test_worktree_guard.py`` fails
    if the two drift.

    A linked worktree is identified by its marker: ``git worktree add`` writes
    ``.git`` as a FILE containing ``gitdir: <main>/.git/worktrees/<name>``, where a
    normal clone has a ``.git`` DIRECTORY.

    This previously matched only paths under ``.claude/worktrees/``, so a worktree
    created anywhere else was invisible to it (techne#434). Note the copies had
    ALREADY drifted — this one omitted the ``.resolve()`` the hooks copy had, so a
    relative path could evade even the old check. That drift is exactly what the
    sync test now prevents.

    A git SUBMODULE also uses a ``.git`` file, but points at ``.git/modules/<name>``;
    the ``worktrees`` segment distinguishes them, and a submodule must NOT be refused.

    No subprocess: this is the preflight hot path and must answer when git is absent.
    """
    p = p.resolve()
    marker = p / ".git"
    try:
        if marker.is_file():
            head = marker.read_text(encoding="utf-8", errors="replace")[:4096]
            norm = head.replace("\\", "/")
            if "gitdir:" in norm and "/.git/worktrees/" in norm:
                return True
    except OSError:
        pass  # unreadable marker: fall through to the path heuristic below

    parts = p.parts
    return any(
        parts[i] == ".claude" and parts[i + 1] == "worktrees"
        for i in range(len(parts) - 1)
    )


def _healthy():
    """``(ok, where)``: techne imports AND resolves under the canonical repo."""
    for m in [m for m in sys.modules if m == "techne" or m.startswith("techne.")]:
        del sys.modules[m]
    importlib.invalidate_caches()
    try:
        import techne
        import techne.mcp_server  # noqa: F401
    except Exception:
        return False, None
    where = Path(techne.__file__).resolve().parent  # <resolved-repo>/techne
    return where == (CANONICAL / "techne"), where


def _pip_install_editable(target: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "pip", "install", "-e", str(target)]
    # PEP 668: system pythons (Ubuntu's /usr/bin/python3, modern Homebrew) are
    # "externally managed" and reject a bare install; a venv is not, and the flag
    # is unnecessary — even rejected by — some venv pips. Gate it on venv detection.
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        cmd.append("--break-system-packages")
    return subprocess.run(cmd, capture_output=True, text=True)


def main() -> int:
    # Only meaningful for an editable/source install. If this file isn't sitting
    # in a real checkout (no pyproject.toml), techne was installed normally and
    # there is no editable pointer to heal — do nothing.
    if not (CANONICAL / "pyproject.toml").exists():
        return 0

    # Never heal *to* a worktree. If this guard is itself running from a worktree
    # (register_hooks refuses to wire that, so it shouldn't happen), reinstalling
    # would just re-arm the footgun. Warn instead of making it worse.
    if _is_worktree_path(CANONICAL):
        print(
            f"[techne-preflight] refusing to auto-heal: guard is running from a git "
            f"worktree ({CANONICAL}). Reinstall techne from your canonical checkout "
            f"and re-run `techne init`.",
            file=sys.stderr,
        )
        return 0

    ok, where = _healthy()
    if ok:
        return 0  # healthy: silent, no pip

    reason = f"resolves to {where}" if where else "import failed"
    print(
        f"[techne-preflight] techne editable pointer stale ({reason}) — "
        f"repairing from {CANONICAL}…",
        file=sys.stderr,
    )
    r = _pip_install_editable(CANONICAL)
    # pip rewrote the .pth on disk, but the running interpreter won't pick that up
    # for a fresh top-level import (.pth is processed only at startup). Add the repo
    # to sys.path so the re-verify below is honest rather than a stale false-negative.
    if str(CANONICAL) not in sys.path:
        sys.path.insert(0, str(CANONICAL))
    ok2, _ = _healthy()
    if ok2 and r.returncode == 0:
        print(
            f"[techne-preflight] repaired — techne now resolves to {CANONICAL}. Green.",
            file=sys.stderr,
        )
    else:
        print(
            "[techne-preflight] repair FAILED — fix manually:\n"
            f"  {sys.executable} -m pip install -e {CANONICAL}\n"
            f"pip stdout:\n{r.stdout}\npip stderr:\n{r.stderr}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
