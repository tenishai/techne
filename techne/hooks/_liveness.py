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

* ``hook:<name>:last_run``   — ``{"ts": ..., "outcome": ..., "detail": ...}``
* ``hook:<name>:last_error`` — ``{"ts": ..., "stage": ..., "msg": ...}``, cleared
  on the next clean run so a fixed hook stops nagging.

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
  tool call), so this is one short-lived connection and two key writes, with no
  queries beyond the stale-error check.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

# Outcome vocabulary. Not an enum on purpose: hooks add their own specific
# `noop:<reason>` strings, and a closed enum would push authors back toward
# emitting nothing rather than naming a new case.
OUTCOME_OK = "ok"
OUTCOME_NOOP = "noop"


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
    stamp = ts or now_iso()
    try:
        from techne.core.database import connect
        from techne.core.extension_state import get_state, set_state

        conn = connect(db_path)
        try:
            payload: dict = {"ts": stamp, "outcome": outcome}
            if detail:
                payload["detail"] = detail
            set_state(conn, f"hook:{hook}:last_run", payload)

            error_key = f"hook:{hook}:last_error"
            if error is not None:
                stamped = dict(error)
                stamped.setdefault("ts", stamp)
                set_state(conn, error_key, stamped)
            elif get_state(conn, error_key):
                # Clear a stale error so a hook that has been fixed stops
                # reporting a failure it no longer has. Guarded by a read so the
                # common (never-failed) case does not write on every run.
                set_state(conn, error_key, {})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a hook
        print(f"techne: could not persist {hook} liveness: {exc}", file=sys.stderr)


def resolve_db_path():
    """Best-effort config load for the db path, or None.

    Several hooks need to record liveness on paths where config load itself is
    what failed. Returns None rather than raising so the caller can degrade.
    """
    try:
        from techne.config import Config

        return Config.load().db_path
    except Exception:  # noqa: BLE001
        return None
