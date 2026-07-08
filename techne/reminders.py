"""Durable due-dated reminders (techne#207).

Public API
----------
add(db_path, note, due_at)      -> int          insert an active reminder, return id
list_due(db_path, now)          -> list[dict]   active reminders whose due_at <= now
touch_surfaced(db_path, id)                     stamp last_surfaced_at = now()
dismiss(db_path, id)                            set status='done'

Timestamp convention: ISO text ("YYYY-MM-DD HH:MM:SS"), matching every other
TIMESTAMP column in the techne schema. SQLite's lexicographic ordering of that
format makes the due_at <= now comparison correct without any casting, and lets
datetime('now') produce a compatible value directly in SQL.

Why ISO text over epoch int: all other timestamp columns in techne.db (sessions,
observations, knowledge, etc.) are TIMESTAMP stored as ISO text and compared via
datetime() SQL functions. Using the same convention keeps the reminders table
consistent and lets callers reuse the same parsing/formatting helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    """UTC now as an ISO text timestamp matching the TIMESTAMP column convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add(db_path: Path, note: str, due_at: str) -> int:
    """Insert an active reminder and return its id.

    Parameters
    ----------
    db_path:
        Path to techne.db.
    note:
        Human-readable reminder text.
    due_at:
        ISO text timestamp ("YYYY-MM-DD HH:MM:SS") when this reminder becomes
        due. Stored verbatim; callers are responsible for UTC consistency.

    Returns
    -------
    int
        The autoincrement id of the newly-inserted row.
    """
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO reminders (note, due_at, status) VALUES (?, ?, 'active')",
            (note, due_at),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def list_due(db_path: Path, now: str) -> list[dict]:
    """Return active reminders whose due_at <= now, ordered by due_at ascending.

    Parameters
    ----------
    db_path:
        Path to techne.db.
    now:
        ISO text timestamp to compare against due_at. Pass a consistent
        value per call (e.g. _now_iso()) so behaviour is deterministic.

    Returns
    -------
    list[dict]
        Each dict has keys: id, note, due_at, status, created_at,
        last_surfaced_at (may be None).
    """
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, note, due_at, status, created_at, last_surfaced_at"
            "  FROM reminders"
            " WHERE status = 'active' AND due_at <= ?"
            " ORDER BY due_at",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def touch_surfaced(db_path: Path, reminder_id: int) -> None:
    """Stamp last_surfaced_at = now() for the given reminder.

    Called by the post_tool_use hook each time a reminder is injected so the
    ~3h repeat-throttle knows when it was last shown. (techne#207)
    """
    touch_surfaced_many(db_path, [reminder_id])


def touch_surfaced_many(db_path: Path, reminder_ids: list[int]) -> None:
    """Batch-stamp last_surfaced_at for multiple reminders in a single UPDATE.

    Equivalent to calling touch_surfaced for each id but uses one connection
    and one UPDATE WHERE id IN (...). Called by the post_tool_use hook when
    surfacing N reminders at session start to avoid one connection per reminder.
    (techne#207)
    """
    if not reminder_ids:
        return
    from techne.core.database import connect

    now = _now_iso()
    placeholders = ",".join("?" * len(reminder_ids))
    conn = connect(db_path)
    try:
        conn.execute(
            f"UPDATE reminders SET last_surfaced_at = ? WHERE id IN ({placeholders})",
            [now, *reminder_ids],
        )
        conn.commit()
    finally:
        conn.close()


def dismiss(db_path: Path, reminder_id: int) -> None:
    """Mark a reminder as done so it no longer surfaces.

    The row is kept (status='done', not deleted) so the operator can audit
    what was dismissed. list_due() filters on status='active', so dismissed
    reminders are invisible to the hook. (techne#207)
    """
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE reminders SET status = 'done' WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()
    finally:
        conn.close()
