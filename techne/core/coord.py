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


def resolve_recipient(to: str) -> str:
    """Resolve a send target to the value stored in ``recipient`` (techne#376 phase 2).

    A broadcast channel passes through unchanged. Otherwise ``to`` is treated as a
    session **alias** — a live session's display name (the CLI ``-n`` name, e.g.
    'claude_2') — and resolved to that session's ``session_id`` for a direct message.
    Anything that matches no live name is returned as-is (a literal session_id, or an
    offline/unknown target that simply stays undelivered until it connects).
    """
    if to == DEFAULT_CHANNEL:
        return to
    for s in live_sessions():
        if s.get("name") == to:
            return s["session_id"]
    return to


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
    reply_to: int | None = None,
) -> int:
    """Post a message and return its id.

    ``to`` is a channel name (e.g. ``'coord'``), a live session's display-name alias,
    or a target session_id. ``from_name`` is a display-name snapshot (resolved via
    ``session_name()`` if omitted).

    Threading (techne#376 phase 2): pass ``reply_to=<message_id>`` to attach this
    message to that message's conversation — it inherits the parent's ``thread_id``,
    or, if the parent had none, both are grouped under a new thread keyed on the
    parent's id. An explicit ``thread_id`` takes precedence over ``reply_to``.
    """
    from techne.core.database import connect

    if from_name is None:
        from_name = session_name(from_session)
    recipient = resolve_recipient(to)  # channel passthrough, or name-alias -> session_id
    conn = connect(db_path)
    try:
        if reply_to is not None and thread_id is None:
            parent = conn.execute(
                "SELECT thread_id FROM coord_messages WHERE id = ?", (reply_to,)
            ).fetchone()
            if parent is not None:
                thread_id = parent["thread_id"] or str(reply_to)
                if parent["thread_id"] is None:
                    # pull the standalone parent into the new thread so the pair groups
                    conn.execute(
                        "UPDATE coord_messages SET thread_id = ? WHERE id = ?",
                        (thread_id, reply_to),
                    )
        cur = conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, thread_id, body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (from_session, from_name, recipient, thread_id, body, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def threads(
    db_path: Path, session_id: str, *, channels: list[str] | None = None, limit: int = 20
) -> list[dict]:
    """Conversation threads (messages sharing a thread_id) visible to ``session_id``,
    most-recently-active first (techne#376 phase 2).

    Only messages the session could see are included (its channels, direct-to-it, or
    from it), so a thread surfaces with exactly the turns this session is party to.
    Each thread: ``{thread_id, count, last_at, messages: [...]}``.
    """
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    placeholders = ",".join("?" for _ in chans)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, from_session, from_name, recipient, thread_id, body, created_at"
            f"  FROM coord_messages"
            f" WHERE thread_id IS NOT NULL"
            f"   AND (recipient IN ({placeholders}) OR from_session = ?)"
            f" ORDER BY id",
            (*chans, session_id),
        ).fetchall()
    finally:
        conn.close()

    by_thread: dict[str, list[dict]] = {}
    for r in rows:
        by_thread.setdefault(r["thread_id"], []).append(dict(r))
    out = [
        {"thread_id": tid, "count": len(msgs), "last_at": msgs[-1]["created_at"], "messages": msgs}
        for tid, msgs in by_thread.items()
    ]
    out.sort(key=lambda t: t["last_at"], reverse=True)
    return out[:limit]


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
        if not isinstance(updated, (int, float)):
            continue  # malformed sidecar (non-numeric updatedAt) — treat as not-live
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


def _git_worktree(cwd: str | None) -> dict | None:
    """Resolve a cwd to its git working tree: ``{toplevel, branch, is_primary}``.

    One ``git rev-parse`` call. ``is_primary`` is False for a linked worktree (its
    git-dir lives under ``.git/worktrees/``) — that's how we tell "the shared main
    checkout" from an isolated per-session worktree. Returns None if cwd isn't a git
    working tree (the sidecar cwd is the launch dir, so this can legitimately miss).
    """
    import subprocess

    if not cwd:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel", "--git-dir", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    parts = r.stdout.splitlines()
    if len(parts) < 3:
        return None
    toplevel, git_dir, branch = parts[0].strip(), parts[1].strip(), parts[2].strip()
    return {"toplevel": toplevel, "branch": branch, "is_primary": "/worktrees/" not in git_dir}


def working_tree_collisions(session_id: str) -> list[dict]:
    """Live peers that are a genuine collision risk under the worktree SOP (techne#376
    phase 2) — NOT merely "same repo" (isolated worktrees of one repo are the whole
    point). Two cases are flagged:

    1. A peer sharing THIS session's exact git working tree (same ``toplevel``) — same
       HEAD, the commit-bleed the SOP exists to prevent.
    2. A peer doing feature work in the PRIMARY (non-worktree) checkout — i.e. on the
       shared checkout, off the default branch — an SOP violation.

    Best-effort: a peer (or self) whose sidecar cwd isn't a git working tree is
    skipped, since that cwd is the launch dir and may not reflect a later ``cd``.
    Each result: ``{session_id, name, branch, toplevel, reason}``.
    """
    sessions = {s["session_id"]: s for s in live_sessions()}
    me = sessions.get(session_id)
    if not me:
        return []
    mine = _git_worktree(me.get("cwd"))
    if not mine:
        return []
    out: list[dict] = []
    for sid, s in sessions.items():
        if sid == session_id:
            continue
        wt = _git_worktree(s.get("cwd"))
        if not wt:
            continue
        if wt["toplevel"] == mine["toplevel"]:
            reason = "same git working tree (shared HEAD — commit-bleed risk)"
        elif wt["is_primary"] and wt["branch"] not in (None, "", "main", "master", "HEAD"):
            reason = "feature work in the shared (non-worktree) checkout"
        else:
            continue
        out.append(
            {
                "session_id": sid,
                "name": s.get("name"),
                "branch": wt["branch"],
                "toplevel": wt["toplevel"],
                "reason": reason,
            }
        )
    return out
