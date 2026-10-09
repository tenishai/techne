"""Hook liveness markers — every hook asserts WHEN IT LAST RAN and WHAT IT DID.

The rule this exists to enforce (Richard, 2026-08-03, techne#468): **a rail that
emits no signal is not finished.** Telemetry is a build requirement for a hook,
not an optional extra bolted on later.

Why it matters, concretely (techne#942): on 2026-07-31 ``per_turn_recall`` and
``codex_grep_assist`` were both ``true`` in config and *absent* from
settings.json. They were dead for three days. Nothing said so, because a hook
that does not run produces exactly the same output as a hook that ran and had
nothing to do — silence. Four of seven hooks emitted no durable signal at all,
so their liveness was not merely unknown, it was **undecidable**.

WHAT THIS RECORDS, per hook, in ``extension_state``:

* ``hook:<name>:last_run``   — ``{"ts": ..., "outcome": ..., "detail": ...,
  "ms": ..., "cpu_ms": ...}``
* ``hook:<name>:ms_recent``  — ``{"ms": [...]}``, the last ``MS_RECENT_CAP``
  ``ms`` values, oldest first (issue 1217). ``last_run`` is overwritten on
  every run, so a p50 has to come from here.
* ``hook:<name>:last_error`` — ``{"ts": ..., "stage": ..., "msg": ...}``, cleared
  on the next clean run so a fixed hook stops nagging.

``ms`` is wall time from the ``techne.hooks`` package import to this call
(see ``techne/hooks/__init__.py``); it leaves out interpreter boot. ``cpu_ms``
is the process's CPU time so far, boot included. Neither is a full wall-clock
duration; Claude Code's transcript ``durationMs`` is the cross-check.

``outcome`` is the part that answers the operator's actual question — "when I
run something I want to know it works, or it doesn't." A bare timestamp says the
hook *executed*; it does not distinguish "injected 8 symbols" from "found no
indexed db and silently gave up". Both are legitimate, but only one of them is
the thing you meant to build, so the no-op paths name themselves
(``noop:no-db``, ``noop:no-results``, …) rather than all collapsing into silence.

DESIGN CONSTRAINTS:

* **Never raises.** A hook must not fail because its bookkeeping failed —
  Claude Code treats a non-zero hook exit as a hard failure, and several hooks
  (notably ``session_end``) deliberately exit 0 on every path for that reason.
  So every call here is best-effort and swallows its own exceptions to stderr.
* **Never mutates anything but its own two keys.** Health markers are strictly
  observational (techne#468 rule 2): they report, they do not self-heal.
  Auto-repair is how the interpreter got poisoned three times (techne#458).
* **Cheap.** These run in hot paths (``pre_tool_use`` fires on every matching
  tool call), so this is one short-lived connection and one transaction: the
  ``last_run`` and ``ms_recent`` writes, with no queries beyond the ring read
  and the stale-error check.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

# Outcome vocabulary. Not an enum on purpose: hooks add their own specific
# `noop:<reason>` strings, and a closed enum would push authors back toward
# emitting nothing rather than naming a new case.
OUTCOME_OK = "ok"
OUTCOME_NOOP = "noop"

#: How long a liveness write waits for the db lock before giving up. Deliberately
#: far below `connect()`'s 5s default (database.py): that budget exists so real
#: work queues under WAL, and bookkeeping must not inherit it — a marker is not
#: worth adding seconds to a tool call.
LOCK_WAIT_MS = 200

#: How many recent ``ms`` samples ``hook:<name>:ms_recent`` keeps (issue 1217).
MS_RECENT_CAP = 50


def now_iso() -> str:
    """UTC timestamp, second precision — matches session_start's existing format."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def record_hook_run(
    db_path,
    hook: str,
    *,
    outcome: str = OUTCOME_OK,
    detail: str | None = None,
    error: dict | None = None,
    ts: str | None = None,
) -> None:
    """Record that ``hook`` ran, what it did, and whether it failed.

    Best-effort by contract: any failure here is printed to stderr and swallowed,
    because a hook must never die of its own bookkeeping.

    Args:
        db_path: techne db path.
        hook: hook name, e.g. ``"session_end"``. Becomes ``hook:<name>:last_run``.
        outcome: what the run actually did — ``"ok"`` when it did its job, or a
            ``"noop:<reason>"`` string naming why it did nothing. The no-op
            reasons are the point: they turn a silent ``sys.exit(0)`` into a
            fact.
        detail: optional short human-readable extra (counts, a path).
        error: ``{"stage": ..., "msg": ...}`` when the run failed. ``None`` on a
            clean run, which CLEARS any previously recorded error.
        ts: override the timestamp (tests); defaults to now.
    """
    from techne.hooks._timeout import HookTimeout

    stamp = ts or now_iso()
    # Timed BEFORE the connection opens, so the bookkeeping's own cost is not
    # billed to the hook.
    from techne.hooks import _T0

    ms = round((time.monotonic() - _T0) * 1000)
    cpu_ms = round(time.process_time() * 1000)
    try:
        from techne.core.database import connect
        from techne.core.extension_state import get_state, set_state

        conn = connect(db_path)
        # `connect()` sets PRAGMA busy_timeout=5000 so that real work waits its
        # turn under WAL. Bookkeeping must NOT inherit that: `pre_tool_use`
        # fires on every Grep and its liveness write is outside any
        # `bounded_hook`, so a contended db would add five seconds to a tool
        # call for a row nobody is blocked on. A marker that cannot get the
        # lock promptly is worth less than the latency of waiting for it —
        # give up and stay silent for this run instead.
        conn.execute(f"PRAGMA busy_timeout={LOCK_WAIT_MS}")
        try:
            payload: dict = {"ts": stamp, "outcome": outcome}
            if detail:
                payload["detail"] = detail
            payload["ms"] = ms
            payload["cpu_ms"] = cpu_ms
            set_state(conn, f"hook:{hook}:last_run", payload, commit=False)

            # Read-modify-write, but not racy: the last_run INSERT above opened
            # the transaction and holds the write lock, so two hooks finishing
            # together append in turn rather than one dropping the other's sample.
            ring_key = f"hook:{hook}:ms_recent"
            recent = (get_state(conn, ring_key) or {}).get("ms")
            if not isinstance(recent, list):
                recent = []
            recent.append(ms)
            set_state(conn, ring_key, {"ms": recent[-MS_RECENT_CAP:]}, commit=False)

            error_key = f"hook:{hook}:last_error"
            if error is not None:
                stamped = dict(error)
                stamped.setdefault("ts", stamp)
                set_state(conn, error_key, stamped, commit=False)
            elif get_state(conn, error_key):
                # Clear a stale error so a hook that has been fixed stops
                # reporting a failure it no longer has. Guarded by a read so the
                # common (never-failed) case does not write on every run.
                set_state(conn, error_key, {}, commit=False)
            # One transaction for all of it: one commit, and no reader ever sees
            # a last_run whose ms is missing from the ring.
            conn.commit()
        finally:
            conn.close()
    except HookTimeout:
        # NEVER swallow the bounded_hook alarm. HookTimeout is a RuntimeError, so
        # the blanket handler below would eat it — and most record_hook_run calls
        # sit INSIDE a `with bounded_hook(...)` block. Eating it there does three
        # things, all worse than the silence this module exists to remove: the
        # block runs far past its wall-clock budget, bounded_hook's `timed_out`
        # flag stays False so it prints the SOFT "approaching timeout" warning
        # instead of "work was dropped", and the marker never lands — leaving a
        # STALE last_run from a previous session that reads as healthy.
        # Same guard, same reason, as session_end.py's backfill block.
        raise
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a hook
        print(f"techne: could not persist {hook} liveness: {exc}", file=sys.stderr)


def resolve_db_path():
    """The db path for hooks that must record liveness BEFORE (or despite) a
    config load, or None if even the fallback cannot be determined.

    The paths this exists for are the awkward ones: a hook that bails on missing
    ``session_id`` or on a config load that itself raised. Asking ``Config`` on
    the second is circular — ``Config.load()`` is the thing that just failed —
    so this falls back to the CONVENTIONAL location (``$TECHNE_HOME`` or
    ``~/.techne/techne.db``) rather than giving up.

    That fallback can be wrong for a non-default install, which is the honest
    trade: a marker written to the default path is recoverable and visible,
    whereas the alternative is exactly the undecidable silence techne#468 exists
    to remove. Callers must treat None as "cannot record" and carry on.
    """
    try:
        from techne.config import Config

        return Config.load().db_path
    except Exception:  # noqa: BLE001 — that failure is WHY we are here
        pass
    try:
        import os
        from pathlib import Path

        # $TECHNE_HOME ONLY — never ~/.techne (#469 review, finding 6). Config
        # supports an explicit `db_path:` pointing anywhere, and in the
        # multi-agent layout techne#112 exists to support, ~/.techne/techne.db
        # is A DIFFERENT AGENT'S LIVE STORE. Guessing it gets BOTH halves wrong:
        # the store that mattered stays silent (the undecidable state this
        # exists to remove) and an innocent store is made to read LIVE for a
        # hook that never ran against it. $TECHNE_HOME is the one signal that
        # cannot belong to another agent, so it is the only fallback taken.
        home = os.environ.get("TECHNE_HOME")
        if not home:
            return None
        candidate = Path(home) / "techne.db"
        # Only use it if the store already exists: creating a db as a side
        # effect of a failed config load would be a mutation, and health
        # bookkeeping must not create state it was merely trying to describe.
        return candidate if candidate.exists() else None
    except Exception:  # noqa: BLE001
        return None
