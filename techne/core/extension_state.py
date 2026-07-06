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
