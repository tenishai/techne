"""Inter-session coordination mailbox (techne#376).

Lets concurrent Claude Code sessions converse without a human router. A message
targets a **channel** (default ``'coord'``, a broadcast everyone watches) or a
specific ``session_id`` (direct). Delivery is push: the post_tool_use hook injects
unread messages into the agent's context on its next tool call, and the
session-start banner surfaces unread + who's live.

Identity is the **self-asserted** session_id (``CLAUDE_CODE_SESSION_ID`` or a
caller-supplied value), consistent with the rest of techne — NO bearer token. The
trust boundary is the local machine: anything that can write to techne.db can do
anything already, so a token would defend a door in a wall that isn't there.

Presence is read-only: ``live_sessions()`` scans Claude Code's own per-session
sidecars (``~/.claude/sessions/<pid>.json``) — no state of our own to keep in sync.

Timestamp convention: ISO text ("YYYY-MM-DD HH:MM:SS"), matching every other
TIMESTAMP column in techne.db.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# The default broadcast channel. For the common <=2-instance case, every session
# watches 'coord' and nobody needs to know anyone else's session_id to be heard.
DEFAULT_CHANNEL = "coord"

# A session sidecar older than this (by updatedAt) is treated as not-live for
# presence. Generous — CC stamps updatedAt on activity; a truly dead session's
# file lingers with a stale timestamp.
_PRESENCE_FRESH_SECONDS = 90 * 60


def _now_iso() -> str:
    """UTC now as an ISO text timestamp matching the TIMESTAMP column convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _channels_for(session_id: str, channels: list[str] | None) -> list[str]:
    """Recipients a session's inbox covers: its watched channels plus itself
    (direct messages). Defaults to the single broadcast channel."""
    chans = list(channels) if channels is not None else [DEFAULT_CHANNEL]
    if session_id not in chans:
        chans = chans + [session_id]
    return chans


def send(
    db_path: Path,
    from_session: str,
    body: str,
    *,
    to: str = DEFAULT_CHANNEL,
    from_name: str | None = None,
    thread_id: str | None = None,
) -> int:
    """Post a message and return its id.

    ``to`` is a channel name (e.g. ``'coord'``) or a specific target session_id.
    ``from_name`` is a display-name snapshot (resolve via ``session_name()`` if the
    caller doesn't supply it).
    """
    from techne.core.database import connect

    if from_name is None:
        from_name = session_name(from_session)
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, thread_id, body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (from_session, from_name, to, thread_id, body, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def _unread_rows(conn, session_id: str, channels: list[str]):
    placeholders = ",".join("?" for _ in channels)
    return conn.execute(
        f"SELECT id, from_session, from_name, recipient, thread_id, body, created_at"
        f"  FROM coord_messages"
        f" WHERE recipient IN ({placeholders})"
        f"   AND from_session != ?"
        f"   AND id NOT IN (SELECT message_id FROM coord_reads WHERE reader_session = ?)"
        f" ORDER BY id",
        (*channels, session_id, session_id),
    ).fetchall()


def inbox(
    db_path: Path,
    session_id: str,
    *,
    channels: list[str] | None = None,
    mark_read: bool = False,
) -> list[dict]:
    """Unread messages for ``session_id`` — those on a watched channel or addressed
    to it directly, not authored by it, with no read receipt yet. Oldest first.

    With ``mark_read=True`` the returned messages are marked read in the same call.
    """
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    conn = connect(db_path)
    try:
        rows = _unread_rows(conn, session_id, chans)
        msgs = [dict(r) for r in rows]
        if mark_read and msgs:
            now = _now_iso()
            conn.executemany(
                "INSERT OR IGNORE INTO coord_reads (message_id, reader_session, read_at)"
                " VALUES (?, ?, ?)",
                [(m["id"], session_id, now) for m in msgs],
            )
            conn.commit()
        return msgs
    finally:
        conn.close()


def unread_count(
    db_path: Path, session_id: str, *, channels: list[str] | None = None
) -> int:
    """How many unread messages ``session_id`` has. Cheap — for the banner/hook."""
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    conn = connect(db_path)
    try:
        return len(_unread_rows(conn, session_id, chans))
    finally:
        conn.close()


def mark_read(
    db_path: Path,
    session_id: str,
    message_ids: list[int] | None = None,
    *,
    channels: list[str] | None = None,
) -> int:
    """Mark messages read for ``session_id``. With ``message_ids`` given, marks
    exactly those; otherwise marks everything currently unread. Returns the number
    of new read receipts written (idempotent — re-marking is a no-op)."""
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        if message_ids is None:
            chans = _channels_for(session_id, channels)
            ids = [r["id"] for r in _unread_rows(conn, session_id, chans)]
        else:
            ids = list(message_ids)
        if not ids:
            return 0
        now = _now_iso()
        cur = conn.executemany(
            "INSERT OR IGNORE INTO coord_reads (message_id, reader_session, read_at)"
            " VALUES (?, ?, ?)",
            [(mid, session_id, now) for mid in ids],
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Presence — read-only view over Claude Code's own per-session sidecars.
# ---------------------------------------------------------------------------


def _sessions_dir() -> Path:
    from techne.platforms.claude_code.paths import claude_config_dir

    return claude_config_dir() / "sessions"


def _session_records() -> list[dict]:
    out: list[dict] = []
    d = _sessions_dir()
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return out


def session_name(session_id: str) -> str | None:
    """The display name (the CLI ``-n`` name, e.g. 'claude_1') for a session_id,
    read from its sidecar. None if unknown."""
    for rec in _session_records():
        if rec.get("sessionId") == session_id:
            return rec.get("name")
    return None


def live_sessions(
    *, exclude_session: str | None = None, fresh_seconds: int = _PRESENCE_FRESH_SECONDS
) -> list[dict]:
    """Sessions Claude Code currently considers live, newest-activity first.

    Reads ``~/.claude/sessions/*.json`` and keeps records whose ``updatedAt`` is
    within ``fresh_seconds``. Returns dicts:
    ``{session_id, name, cwd, status, pid, updated_at_ms}``.
    """
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    live: list[dict] = []
    for rec in _session_records():
        sid = rec.get("sessionId")
        if not sid or sid == exclude_session:
            continue
        updated = rec.get("updatedAt") or 0
        if now_ms - updated > fresh_seconds * 1000:
            continue
        live.append(
            {
                "session_id": sid,
                "name": rec.get("name"),
                "cwd": rec.get("cwd"),
                "status": rec.get("status"),
                "pid": rec.get("pid"),
                "updated_at_ms": updated,
            }
        )
    live.sort(key=lambda r: r["updated_at_ms"], reverse=True)
    return live
