"""Does this process still run the code that is on disk? (techne#696)

WHY THIS LIVES IN THE SERVER AND NOT IN `checks.py`.

`check_runtime_loaded` (#491) answers a neighbouring question from the OUTSIDE, and
pays for the vantage point: it asks the process table who is running (`ps`) and asks
git when the deployment landed. That is why it is `@expensive`, and why PR #697 was
withdrawn for wiring it into SessionStart — `checks.py:37-41` records that the
`@expensive` marker exists precisely because prose once failed to hold that budget.

The observation that makes this module cheap: **the process that needs to report is
the one that already knows.** A server does not have to ask `ps` when it started; it
has `time.time()`. It does not have to ask git what it loaded; it has `sys.modules`.
No subprocess, at any tier.

TWO TIERS, because the cheap question is the common one:

  1. HAS THE DEPLOYMENT MOVED? One `stat` of `<runtime>/.git/logs/HEAD`, TTL-cached.
     That file's mtime is when the ref last MOVED HERE — the same signal
     `_runtime_deployed_at` uses, and deliberately not committer time, which would
     re-arm on every upstream commit rather than on the pulls that actually matter.
  2. WHAT DID WE ACTUALLY LOAD? Only ever runs after (1) fires. Walks the `techne.*`
     entries already in `sys.modules` and stats each one's file.

WHAT (2) BUYS THAT THE ORDERING CHECK STRUCTURALLY CANNOT: it separates

  * DEPLOYMENT MOVED, nothing we hold differs — the pull touched files this process
    never loaded (tests, docs, an unimported module). Consistent; not worth a warning.
  * TORN — a file we loaded has changed underneath us. We keep serving the OLD
    definition for it, while anything imported from here on comes from the NEW source.
    That mixture is the actually dangerous state, and it is reachable: `mcp_server.py`
    carries 46 function-local `from techne…` imports.

`check_runtime_loaded` is careful to say it is "ordering only… not a tear detector".
This is the tear detector, and it is cheap only because it runs inside the tear.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Seconds between tier-1 stats. A pull is a human action; sub-minute precision buys
#: nothing and a stat per tool call on a busy session is pure waste.
DEFAULT_TTL_SECONDS = 30.0


@dataclass
class RuntimeWatch:
    """One process's view of itself. Created once, at serve start."""

    started_at: float
    marker_path: Path | None
    #: mtime of `marker_path` when this process started, or None when there is no
    #: deployment marker to watch (a packaged install — see `begin`).
    marker_at: float | None
    ttl: float = DEFAULT_TTL_SECONDS
    _checked_at: float = field(default=0.0, repr=False)
    _cached: "Drift | None" = field(default=None, repr=False)
    #: Marker mtimes already reported. One warning per deployment, not per call —
    #: the same discipline `capabilities.invitation` uses for the same reason.
    _reported: set = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class Drift:
    """A live process holding code that no longer matches disk."""

    #: Modules loaded by this process whose source file changed after we loaded it.
    torn: tuple[str, ...]
    moved_at: float

    def message(self) -> str:
        """One line for the agent. States what is true, not what might be.

        Names modules rather than saying "some modules": the whole reason to run this
        inside the process is that we CAN name them, and a warning that cannot say
        which code is stale gives the reader nothing to act on differently.
        """
        n = len(self.torn)
        shown = ", ".join(self.torn[:5]) + (" …" if n > 5 else "")
        return (
            f"techne runtime drift: {n} module(s) this server loaded have changed on "
            f"disk since it started, so it is still serving the OLD definitions "
            f"({shown}). Anything imported from now on comes from the NEW source, so "
            f"this process is running mixed code. Restart the techne MCP server (or "
            f"the session that owns it) before trusting results that depend on it."
        )


def begin(config, *, ttl: float = DEFAULT_TTL_SECONDS) -> RuntimeWatch:
    """Snapshot this process at startup. Never raises, never spawns anything.

    A packaged install has no `.git/logs/HEAD` to watch, so `marker_at` is None and
    `poll` returns None forever. That is the same call `check_runtime_loaded` makes —
    a packaged install has no revision to compare and that is healthy, not red.
    """
    marker: Path | None = None
    marker_at: float | None = None
    try:
        from techne.config import techne_home

        candidate = techne_home() / "runtime" / ".git" / "logs" / "HEAD"
        if candidate.exists():
            marker = candidate
            marker_at = candidate.stat().st_mtime
    except Exception:  # noqa: BLE001 — a diagnostic must never cost a server its start
        marker, marker_at = None, None
    return RuntimeWatch(started_at=time.time(), marker_path=marker, marker_at=marker_at, ttl=ttl)


def _torn_modules(started_at: float) -> tuple[str, ...]:
    """Loaded `techne.*` modules whose file is newer than this process.

    Tier 2. Only ever called after the marker moved, so the `sys.modules` walk is paid
    once per deployment rather than once per call.

    A module with no `__file__` (namespace packages, frozen imports) is skipped rather
    than guessed at: absence of a path is not evidence of staleness.
    """
    out = []
    for name, mod in list(sys.modules.items()):
        if not name.startswith("techne"):
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        try:
            if Path(path).stat().st_mtime > started_at:
                out.append(name)
        except OSError:
            # Deleted or unreadable underneath us. Real, but "the file is gone" is a
            # different fault than "the file changed", and this function claims only
            # the latter.
            continue
    return tuple(sorted(out))


def poll(watch: RuntimeWatch, *, now: float | None = None) -> Drift | None:
    """Tier 1, then tier 2 only if tier 1 fires. Returns None when nothing to say.

    ONE WARNING PER DEPLOYMENT. A warning repeated on every tool call is how a true
    signal becomes something the reader learns to skip — and the condition here does
    NOT self-clear (it persists for the life of the process), so unlike a SessionStart
    banner it would repeat forever. Suppression is keyed on the marker mtime, so a
    SECOND pull during the same process warns again: that is new information.
    """
    if watch.marker_at is None or watch.marker_path is None:
        return None
    t = time.time() if now is None else now
    if t - watch._checked_at < watch.ttl:
        return watch._cached
    watch._checked_at = t
    watch._cached = None
    try:
        current = watch.marker_path.stat().st_mtime
    except OSError:
        return None
    if current == watch.marker_at or current in watch._reported:
        return None
    torn = _torn_modules(watch.started_at)
    if not torn:
        # The deployment moved but nothing this process holds differs — the pull
        # touched files we never loaded. Consistent, and not worth a line.
        watch._reported.add(current)
        return None
    watch._reported.add(current)
    watch._cached = Drift(torn=torn, moved_at=current)
    return watch._cached
