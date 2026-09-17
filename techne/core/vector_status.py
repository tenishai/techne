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

import logging
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

log = logging.getLogger(__name__)

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
    # Rows `techne vectors backfill` would embed, per vec table (techne#948).
    unembedded: dict[str, int]
    backfill_needed: bool


#: vec table -> (base table, id column, extra predicate). MIRRORS the population
#: `search.backfill_vectors` selects: live knowledge that is missing OR vec_stale,
#: and every observation / thread row missing from its vec table. If backfill's
#: predicate changes, change this with it, or `backfill needed` lies again.
_BACKFILL_POPULATION = {
    "threads_vec": ("threads", "rowid", ""),
    "observations_vec": ("observations", "id", ""),
    "knowledge_vec": ("knowledge", "id", "superseded_by IS NULL"),
}


def _unembedded_count(conn: sqlite3.Connection, vec_table: str) -> int:
    """Rows backfill would embed for one vec table (techne#948).

    Reads the plain `<vec>_rowids` shadow table, so no sqlite-vec extension is
    needed. A vec table that does not exist yet counts every eligible row: nothing
    of that table is embedded, and backfill creates the table and fills it.
    """
    base, id_col, live = _BACKFILL_POPULATION[vec_table]
    where = [live] if live else []
    try:
        conn.execute(f"SELECT 1 FROM {vec_table}_rowids LIMIT 1")
        missing = f"{id_col} NOT IN (SELECT rowid FROM {vec_table}_rowids)"
        where.append(f"({missing} OR vec_stale = 1)" if base == "knowledge" else missing)
    except sqlite3.OperationalError:
        pass  # no vec table yet: every eligible row is unembedded
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT COUNT(*) FROM {base}{clause}").fetchone()[0]


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


def vector_row_counts(db_path: Path) -> dict[str, int | None]:
    """Row count per vec table, WITHOUT building the embedding backend (techne#944).

    `vector_channel_status` builds the active backend (a model load from the local
    cache) whenever a stamp or a vec table exists, and `probe_backend_when_empty`
    only suppresses that on a fully empty channel. A caller that needs counts alone
    — `techne init`'s next steps — must not pay a model load for them. Value is None
    when the table is absent or sqlite-vec cannot be loaded ("unknown", not zero).
    """
    conn = connect(db_path)
    try:
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            vec_loadable = True
        except (ImportError, AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
            vec_loadable = False
        return {
            name: (
                _table_row_count(conn, name)
                if vec_loadable and _table_dim(conn, name) is not None
                else None
            )
            for name in _VEC_TABLE_NAMES
        }
    finally:
        conn.close()


def vector_channel_status(
    db_path: Path, *, probe_backend_when_empty: bool = True
) -> VectorChannelStatus:
    """Report the vector channel's health without mutating the database.

    ``probe_backend_when_empty`` (default True) controls whether the embedding
    backend is built when the channel is empty — no stored vectors and no
    identity stamp. Building it runs a model load (and, for fastembed, a live
    dim-probe encode), which is real cost on the session-start hot path
    (techne#358/#361 review). When there are no vectors and no stamp the verdict
    is deterministically "not degraded", so the hot-path caller (collect_health)
    passes False to skip the build entirely; the `techne vectors status` CLI
    keeps the default so it still reports the backend on an empty channel.

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

        # Active backend — best-effort, and DEFERRED until we know it can matter.
        # No embedding backend installed is a legitimate, common state (matches
        # backfill's own graceful degrade). Building it is the expensive part (a
        # model load + a live dim-probe encode for fastembed), so we skip it on an
        # empty channel (no stamp, no tables) where the verdict is already
        # "not degraded" — sparing the session-start hot path (techne#361 review).
        # allow_download=False (techne#358): a cold-cache download would block the
        # hook past its budget (the trap infer() guards against); an uncached model
        # raises and reports backend=None, same as "not installed".
        active_backend: dict | None = None
        if stamp is not None or any_table_exists or probe_backend_when_empty:
            try:
                from techne.core.embeddings import _get_embedding_model

                model = _get_embedding_model(allow_download=False)
                active_backend = {"name": model.name, "dim": model.dim}
            except Exception:
                active_backend = None

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

        # "re-embed needed" answers ONE question — did the backend change? — and
        # was read as "is everything embedded?" (techne#948: 25 knowledge rows
        # promoted, knowledge_vec empty, "re-embed needed: False"). The second
        # question gets its own field rather than a wider meaning for the first.
        unembedded = {name: _unembedded_count(conn, name) for name in _VEC_TABLE_NAMES}
        log.debug("vector_channel_status: unembedded=%s", unembedded)

        return {
            "active_backend": active_backend,
            "stamp": stamp,
            "tables": tables,
            "reembed_needed": reembed_needed,
            "unembedded": unembedded,
            "backfill_needed": any(unembedded.values()),
        }
    finally:
        conn.close()
