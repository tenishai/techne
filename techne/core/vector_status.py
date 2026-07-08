"""Read-only vector-channel health check (techne#284).

`search.backfill_vectors()` (and the identity migration it drives,
`search._ensure_vec_backend_identity`) are the ONLY code paths allowed to
DROP/recreate the threads_vec/observations_vec/knowledge_vec tables — that
migration is reviewed/merged as part of the #283 stack and is intentionally
left untouched here. This module exists because `techne vectors status`
needs to answer "is a re-embed needed?" WITHOUT calling
`_ensure_vec_backend_identity`, since that function mutates (drops tables)
as a side effect of merely detecting a mismatch. So the staleness predicate
below is reimplemented, read-only: same comparison (stamped identity /
existing table width vs. the active backend), zero writes.

Kept out of core/search.py deliberately — the task's constraint is additive
only, no edits to search.py's migration path; a new file avoids any risk of
drifting search.py's DROP-capable code.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TypedDict

from techne.core.database import connect
from techne.core.extension_state import get_state

# Mirrors search.py's _VEC_BACKEND_STATE_NAME — the extension_state row name
# under which the embedding-backend identity stamp is kept. Duplicated as a
# literal (not imported) so this read-only module has zero import-time
# coupling to search.py's migration internals; the string itself is a stable
# storage key, not migration logic.
_VEC_BACKEND_STATE_NAME = "embedding_backend"

_VEC_TABLE_NAMES = ("threads_vec", "observations_vec", "knowledge_vec")

# vec0's CREATE VIRTUAL TABLE SQL embeds the column width literally, e.g.
# "CREATE VIRTUAL TABLE threads_vec USING vec0(embedding float[384])" — the
# dim is readable straight out of sqlite_master.sql with no sqlite-vec
# extension load required. That's what keeps table-width inspection cheap
# and read-only (no `enable_load_extension` needed just to check a count).
_DIM_RE = re.compile(r"float\[(\d+)\]")


class _TableStatus(TypedDict):
    exists: bool
    dim: int | None
    rows: int | None


class VectorChannelStatus(TypedDict):
    active_backend: dict | None  # {"name": str, "dim": int} or None
    stamp: dict | None  # stored extension_state identity, or None
    tables: dict[str, _TableStatus]
    reembed_needed: bool


def _table_dim(conn: sqlite3.Connection, name: str) -> int | None:
    """Extract the vec0 column width from sqlite_master.sql, or None if the
    table doesn't exist or its CREATE statement doesn't match the expected
    vec0 shape (defensive — should not happen for tables this module owns)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    m = _DIM_RE.search(row[0])
    return int(m.group(1)) if m else None


def _table_row_count(conn: sqlite3.Connection, name: str) -> int | None:
    """Row count for a vec0 table. Requires the sqlite-vec extension loaded
    on `conn` (vec0 is a virtual table type; plain sqlite3 can see it in
    sqlite_master but can't SELECT from it without the extension). Returns
    None if the extension isn't loaded/loadable — callers treat that as
    "unknown", not "zero"."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def vector_channel_status(db_path: Path) -> VectorChannelStatus:
    """Report the vector channel's health without mutating the database.

    Never drops/recreates/writes anything — this is the non-mutating twin of
    search._ensure_vec_backend_identity's mismatch DETECTION (not its DROP
    side effect). Safe to call as often as desired, e.g. from a status
    command or a health check, with zero risk of triggering a re-embed.

    "re-embed needed" is True when the vector index is stale relative to the
    currently active embedding backend — either:
      1. A stored identity stamp exists and disagrees with the active
         backend's (name, dim), OR
      2. No stamp exists, but an existing vec table's column width (read
         from sqlite_master, not by loading the row into vec0) disagrees
         with the active backend's dim — a pre-stamp DB (see search.py's
         "assumed fastembed" comment) whose tables predate this migration.
      3. No stamp AND no existing tables → nothing to embed yet; not
         "stale", just empty. reembed_needed is False (backfill will create
         the tables fresh on first real backfill, not because of staleness).
    """
    conn = connect(db_path)
    try:
        # Active backend — best-effort. No embedding backend installed is a
        # legitimate, common state (matches backfill's own graceful degrade).
        active_backend: dict | None = None
        try:
            from techne.core.embeddings import _get_embedding_model

            model = _get_embedding_model()
            active_backend = {"name": model.name, "dim": model.dim}
        except Exception:
            active_backend = None

        stamp = get_state(conn, _VEC_BACKEND_STATE_NAME)

        # Try to load sqlite-vec read-only so we can report row counts too.
        # If unavailable, table existence/dim still comes from sqlite_master
        # (no extension needed for that), only `rows` degrades to None.
        vec_loadable = True
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
            vec_loadable = False

        tables: dict[str, _TableStatus] = {}
        any_table_exists = False
        existing_dims: set[int] = set()
        for name in _VEC_TABLE_NAMES:
            dim = _table_dim(conn, name)
            exists = dim is not None
            if exists:
                any_table_exists = True
                existing_dims.add(dim)
            rows = _table_row_count(conn, name) if (exists and vec_loadable) else None
            tables[name] = {"exists": exists, "dim": dim, "rows": rows}

        # Staleness predicate — read-only mirror of
        # search._ensure_vec_backend_identity's mismatch check, minus the DROP.
        reembed_needed = False
        if active_backend is not None:
            if stamp is not None:
                reembed_needed = (
                    stamp.get("name") != active_backend["name"]
                    or stamp.get("dim") != active_backend["dim"]
                )
            elif any_table_exists:
                # Unstamped but tables exist: stale unless every existing
                # table's width already matches the active backend's dim
                # (the "assumed fastembed, backend still fastembed" no-op
                # case from search.py's identity migration).
                reembed_needed = existing_dims != {active_backend["dim"]}
            # else: unstamped AND no tables — fresh DB, nothing to re-embed.

        return {
            "active_backend": active_backend,
            "stamp": stamp,
            "tables": tables,
            "reembed_needed": reembed_needed,
        }
    finally:
        conn.close()
