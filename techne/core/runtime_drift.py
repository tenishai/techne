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
  2. WHAT DID WE ACTUALLY LOAD? Walks the `techne.*` entries already in `sys.modules`
     and stats each one's file. Runs on every poll that clears the TTL — NOT only after
     (1) fires, which is what it did until r2 found the hole: nothing was baselined
     until a pull had already happened, so a module imported after `begin()` and changed
     by that first pull was recorded at its post-change mtime and never torn. Measured
     0.495 ms for 41 modules, 0.0017% of a 30-second TTL.

WHAT (2) BUYS THAT THE ORDERING CHECK STRUCTURALLY CANNOT: it separates

  * DEPLOYMENT MOVED, nothing we hold differs — the pull touched files this process
    never loaded (tests, docs, an unimported module). Consistent; not worth a warning.
  * TORN — a file we loaded has changed underneath us. We keep serving the OLD
    definition for it, while anything imported from here on comes from the NEW source.
    That mixture is the actually dangerous state, and it is reachable: `mcp_server.py`
    carries 46 function-local `from techne…` imports.

`check_runtime_loaded` is careful to say it is "ordering only… not a tear detector".
This is the tear detector, and it is cheap only because it runs inside the tear.

THE BOUNDED RESIDUAL, STATED RATHER THAN IMPLIED. Both tiers sit behind ONE TTL gate, so
a poll landing inside an open window does no walk at all. A module imported before that
suppressed poll and changed after it is therefore first seen on the next poll AFTER the
window, by which time it is baselined at its POST-change mtime — silent, permanently,
for that module. Verified by probe, not reasoned about.

(NOT "with no intervening poll", which an earlier draft of this paragraph said. With no
poll at all the TTL gate is not the cause — nothing observed the module before it
changed — and the full fix below would not close it either. The discriminator is a poll
that HAPPENS and is suppressed; `test_the_single_ttl_gate_residual_is_real_and_deliberate`
has said so thirty lines away since it was written.) The full fix is to walk ahead of the
TTL gate too, which costs 0.47 ms on EVERY tool call instead of once per window — 3% of
a `techne_status` and 21% of a `techne_search`, both measured against an EMPTY store, so
the share shrinks as a real store makes the tools themselves slower. It also breaks the
TTL property that
`test_the_ttl_actually_suppresses_the_stat` exists to hold. The trade was taken
deliberately: this is a diagnostic, and a diagnostic that taxes every call to close its
own last corner is worse than one that says where its edge is.
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

    #: mtime of each loaded `techne.*` module's file AT SNAPSHOT. A per-module
    #: baseline, not a single process timestamp — see `_torn_modules` for why the
    #: single timestamp was wrong in BOTH directions.
    baseline: dict
    marker_path: Path | None
    #: mtime of `marker_path` when this process started, or None when there is no
    #: deployment marker to watch (a packaged install — see `begin`).
    marker_at: float | None
    ttl: float = DEFAULT_TTL_SECONDS
    _checked_at: float = field(default=0.0, repr=False)
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
            f"({shown}) — Python will not re-read a module already in sys.modules, so "
            f"these stay old for the life of the process, while anything NOT yet "
            f"imported will load from the new source. Restart the techne MCP server "
            f"(or the session that owns it) before trusting results that depend on it."
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
    return RuntimeWatch(baseline=_module_mtimes(), marker_path=marker, marker_at=marker_at, ttl=ttl)


def _module_mtimes() -> dict:
    """`{module name: file mtime}` for every loaded `techne.*` module, right now.

    Modules with no `__file__` (namespace packages, frozen imports) and files that
    cannot be stat'd are omitted: absence of a path is not evidence of anything.
    """
    out = {}
    for name, mod in list(sys.modules.items()):
        if not name.startswith("techne"):
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        try:
            out[name] = Path(path).stat().st_mtime
        except OSError:
            continue
    return out


def _torn_modules(baseline: dict) -> tuple[str, ...]:
    """Loaded modules whose file differs from what it was when we loaded them.

    COMPARES EACH MODULE AGAINST ITS OWN BASELINE, not against a process start time.
    The single-timestamp version (`mtime > started_at`) was wrong in BOTH directions
    and Prudence reproduced both end-to-end:

      * FALSE POSITIVE — a module imported LATE, from a file written after startup,
        has `mtime > started_at` while being the NEWEST code in the process. The
        server named `techne.core.health` as one it was "still serving the OLD
        definitions" of, milliseconds after loading it from the new source in that
        same call. It also broke the silence property, since a lazily-imported module
        made a harmless deployment look torn.
      * FALSE NEGATIVE — it compared a filesystem mtime against a `time.time()` taken
        in this process. Different clock sources, and coarse filesystem timestamps,
        with nothing making the comparison meaningful at the boundary.

    A per-module baseline needs no clock comparison at all: either the file is what it
    was when we read it, or it is not. A module absent from the baseline was imported
    AFTER the snapshot — so it came from the current disk and is by definition not
    torn. It is recorded on sight so a LATER change to it is still caught.
    """
    torn = []
    for name, current in _module_mtimes().items():
        if name not in baseline:
            baseline[name] = current  # newly imported: this IS its baseline
        elif current != baseline[name]:
            torn.append(name)
    return tuple(sorted(torn))


def poll(watch: RuntimeWatch, *, now: float | None = None) -> Drift | None:
    """Walk, then check the marker. Returns None when there is nothing to say.

    ORDER MATTERS AND IS NOT THE OBVIOUS ONE: the module walk runs BEFORE the marker
    stat, so a module is baselined before a pull can change it. Reading the marker first
    and walking only on a hit was the r2 defect — see the walk's own comment below.

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
        # NOTHING NEW inside the window, so say nothing. This previously returned a
        # cached `Drift`, which meant the notice repeated on EVERY tool call for the
        # whole TTL — and the test that was supposed to pin "one warning per
        # deployment" passed `ttl=0.0`, making this branch unreachable. A fixture
        # argument switched off the path that broke the property it was guarding.
        return None
    watch._checked_at = t

    # WALK BEFORE THE MARKER GATE, and this ordering is the whole correctness of tier 2.
    # It used to run only AFTER a pull was detected, which meant nothing was baselined
    # until a pull had already happened — so a module imported after `begin()` and
    # changed by that first pull got baselined at its POST-change mtime and was never
    # torn, for the life of the process. Permanently silent, and not a corner: `begin()`
    # sees ~22 modules and four real tool calls take the process to ~43, with the 21 late
    # arrivals being exactly the function-local imports tier 2 exists to watch (`search`,
    # `knowledge`, `identity`, `health`). It even landed on the branch below whose comment
    # says "the pull touched files we never loaded" when we demonstrably had.
    #
    # Measured on a real server rather than a harness: 22 modules at `begin()`, 41 after
    # tool calls, 0.495 ms to walk them — 0.0017% of a 30-second TTL. (An earlier comment
    # said 0.31 ms and the commit said 0.085; both were small-harness numbers.)
    torn = _torn_modules(watch.baseline)

    try:
        current = watch.marker_path.stat().st_mtime
    except OSError:
        return None
    if current == watch.marker_at or current in watch._reported:
        return None
    if not torn:
        # The deployment moved but nothing this process holds differs. Usually that
        # means the pull touched files we never loaded — consistent, not worth a line.
        #
        # DELIBERATELY NOT RECORDED AS REPORTED. It used to be, which permanently
        # suppressed a marker value whose files had not settled yet: git normally writes
        # the worktree before the reflog, but a ref that moves first would be read as
        # "nothing differs" and never re-examined. Leaving it unrecorded costs NOTHING:
        # the walk above runs before both the marker stat and this check, so it was never
        # conditional on the `add`. Measured both ways — 20 stats per poll either.
        return None
    watch._reported.add(current)
    return Drift(torn=torn, moved_at=current)


#: Where a serving process leaves its verdict for OTHER processes to read. One file
#: per pid: several servers can run at once, and a shared file would make the last
#: writer the only truth.
STATE_DIRNAME = "runtime-state"


def _state_dir(config=None) -> Path:
    from techne.config import techne_home

    return techne_home() / STATE_DIRNAME


def publish(watch: RuntimeWatch, drift: "Drift | None", *, pid: int | None = None) -> None:
    """Record this process's verdict where a DIFFERENT process can read it.

    THE WHOLE REASON PART A NEEDED PART B FIRST. `poll` answers cheaply because the
    process asks itself — but the answer lives in memory, and SessionStart is not that
    process. Asking from outside is what costs `ps` plus git and got #697 withdrawn. So
    the server writes the one thing only it knows, and the reader does an `open()`.

    Best-effort and never raises: failing to publish a diagnostic must not affect
    serving. Written whole then renamed, so a reader never sees a half-file.
    """
    import json
    import os

    pid = os.getpid() if pid is None else pid
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "torn": list(drift.torn) if drift else [],
            "moved_at": drift.moved_at if drift else None,
            "modules": len(watch.baseline),
        }
        tmp = d / f".{pid}.json.tmp"
        tmp.write_text(json.dumps(payload))
        tmp.replace(d / f"{pid}.json")
    except Exception:  # noqa: BLE001
        return


def _alive(pid: int) -> bool:
    """Is this pid still running? `os.kill(pid, 0)` — a signal check, not a spawn.

    A dead server's verdict is not a stale warning to soften; it is a FALSE one. The
    condition it described ended when the process did.
    """
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def live_reports(config=None) -> list[dict]:
    """Verdicts published by servers that are STILL RUNNING. Reaps the rest.

    Cheap by construction — a directory listing, one `open()` each, and a signal check
    per pid. No `ps`, no git, no subprocess. That is what makes the SessionStart check
    an ordinary registered check rather than an `@expensive` one reached around
    `cheap_only`, which is precisely how #697 went wrong.
    """
    import json

    out: list[dict] = []
    try:
        d = _state_dir()
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                pid = int(data.get("pid", 0))
            except Exception:  # noqa: BLE001
                f.unlink(missing_ok=True)
                continue
            if not _alive(pid):
                f.unlink(missing_ok=True)  # the condition ended with the process
                continue
            if data.get("torn"):
                out.append(data)
    except Exception:  # noqa: BLE001
        return out
    return out
