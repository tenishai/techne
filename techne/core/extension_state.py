"""Durable key-value sidecar for hooks and sync adapters.

Thin helpers around the ``extension_state`` table. JSON encode/decode at the
boundary; callers own the schema of each payload dict.

    from techne.core.extension_state import get_state, set_state
    state = get_state(conn, "my_ext") or {}
    state["cursor"] = 42
    set_state(conn, "my_ext", state)
"""

import json
import sqlite3
import time

from techne._lazylog import get_logger

log = get_logger(__name__)


def get_state(conn: sqlite3.Connection, name: str) -> dict | None:
    """Return the stored payload for *name*, or None if absent."""
    row = conn.execute(
        "SELECT payload_json FROM extension_state WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_state(conn: sqlite3.Connection, name: str, payload: dict, *, commit: bool = True) -> None:
    """Upsert *payload* for *name*.

    Commits immediately when *commit* is True (default).  Pass ``commit=False``
    to keep the write in the caller's open transaction — the caller is then
    responsible for commit/rollback.
    """
    conn.execute(
        """INSERT INTO extension_state (name, payload_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               payload_json = excluded.payload_json,
               updated_at   = excluded.updated_at""",
        (name, json.dumps(payload, separators=(",", ":")), int(time.time())),
    )
    if commit:
        conn.commit()


def clear_state(conn: sqlite3.Connection, name: str, *, commit: bool = True) -> bool:
    """Delete the payload for *name*. Returns True if a row was actually removed.

    The counterpart to `set_state`, for the case where a stored fact stops being
    true rather than changing value. A payload that describes a configuration
    that no longer exists is worse than no payload: `get_state` cannot tell a
    current claim from a retracted one, so a reader keeps reporting the old
    state indefinitely. Deleting is how a writer says "there is nothing to say
    here any more".
    """
    cur = conn.execute("DELETE FROM extension_state WHERE name = ?", (name,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


#: Age past which a per-session key is dead weight. Matches the 7 days the hooks'
#: own per-session families already use (post_tool_use, user_prompt_submit): far
#: longer than any realistic gap inside one session.
SESSION_STATE_TTL_SECONDS = 7 * 24 * 60 * 60


def prune_stale(
    conn: sqlite3.Connection,
    prefix: str,
    *,
    max_age_seconds: int = SESSION_STATE_TTL_SECONDS,
    commit: bool = True,
) -> int:
    """Delete keys starting with *prefix* not written for *max_age_seconds*.

    For per-session families (one key per session id), which otherwise gain a row
    per session forever (techne#1073). Matches the prefix exactly rather than with
    LIKE, whose ``_`` is a wildcard. Returns the count removed.
    """
    cur = conn.execute(
        "DELETE FROM extension_state WHERE substr(name, 1, ?) = ? AND updated_at < ?",
        (len(prefix), prefix, int(time.time()) - int(max_age_seconds)),
    )
    if commit:
        conn.commit()
    if cur.rowcount:
        log.debug("extension_state: pruned %d stale %r key(s)", cur.rowcount, prefix)
    return cur.rowcount
