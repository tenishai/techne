"""Orphaned ANN embeddings on dead rowids (#408).

A vec0 row whose parent row is gone is unreachable but not harmless: it can
occupy a top-k slot before `search_vec`'s post-cut `if row:` guard drops it,
which is #168's failure shape bounded by `_VEC_SUPERSEDE_OVERFETCH`. Threads
leak one per summary update (INSERT OR REPLACE rotates the rowid); knowledge
leaks one per hard delete.

`_purge_superseded_knowledge_vec` CANNOT cover this — it deletes
`WHERE rowid IN (SELECT id FROM knowledge WHERE superseded_by IS NOT NULL)`,
selecting FROM the parent, so a rowid with no parent is never matched.
`test_the_superseded_purge_does_not_cover_orphans` pins that distinction, so a
future reader does not "simplify" the two into one.
"""

from __future__ import annotations

import pytest

from techne.core.database import init_db
from techne.core.search import (
    _VEC_PARENT,
    _purge_orphaned_vec,
    _purge_superseded_knowledge_vec,
)

pytest.importorskip("sqlite_vec")


def _vec_conn(db):
    """A connection with sqlite_vec loaded — plain connect() cannot touch vec0."""
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _make_vec(conn, name, dim=4):
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0(embedding float[{dim}])")


def _add_vec_row(conn, name, rowid):
    conn.execute(
        f"INSERT INTO {name}(rowid, embedding) VALUES (?, ?)",
        (rowid, b"\x00\x00\x80?" * 4),
    )


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    return p


def test_orphan_is_removed_and_live_row_is_kept(db):
    conn = _vec_conn(db)
    try:
        conn.execute("INSERT INTO sessions (id) VALUES ('s')")
        conn.execute(
            "INSERT INTO threads (session_id, thread_num, label, summary) "
            "VALUES ('s', 1, 'live', 'x')"
        )
        live_rowid = conn.execute("SELECT rowid FROM threads").fetchone()[0]
        _make_vec(conn, "threads_vec")
        _add_vec_row(conn, "threads_vec", live_rowid)
        _add_vec_row(conn, "threads_vec", 9999)  # dead rowid — no thread behind it
        conn.commit()

        removed = _purge_orphaned_vec(conn, "threads_vec")
        conn.commit()

        remaining = [r[0] for r in conn.execute("SELECT rowid FROM threads_vec")]
    finally:
        conn.close()

    assert removed == 1
    assert remaining == [live_rowid], "the live embedding was swept with the orphan"


def test_the_superseded_purge_does_not_cover_orphans(db):
    """The two purges are not interchangeable, and this pins why.

    `vectors:coverage` documents that it verified the superseded purge removed
    0 of 2 dangling rows. This reproduces that rather than trusting the note.
    """
    conn = _vec_conn(db)
    try:
        _make_vec(conn, "knowledge_vec")
        _add_vec_row(conn, "knowledge_vec", 4242)  # hard-deleted knowledge row
        conn.commit()

        by_superseded = _purge_superseded_knowledge_vec(conn)
        conn.commit()
        assert by_superseded == 0, "precondition: the superseded purge cannot see an orphan"

        by_orphan = _purge_orphaned_vec(conn, "knowledge_vec")
        conn.commit()
    finally:
        conn.close()

    assert by_orphan == 1


def test_purge_is_a_noop_when_the_index_does_not_exist(db):
    """The vector channel is optional — an FTS-only install must not raise."""
    conn = _vec_conn(db)
    try:
        assert _purge_orphaned_vec(conn, "threads_vec") == 0
    finally:
        conn.close()


def test_purge_is_idempotent(db):
    conn = _vec_conn(db)
    try:
        _make_vec(conn, "observations_vec")
        _add_vec_row(conn, "observations_vec", 777)
        conn.commit()
        first = _purge_orphaned_vec(conn, "observations_vec")
        second = _purge_orphaned_vec(conn, "observations_vec")
        conn.commit()
    finally:
        conn.close()

    assert (first, second) == (1, 0)


def test_every_vec_table_has_a_declared_parent():
    """The sweep covers all three indexes, not just the one in the issue title.

    The leak mechanism is per-index (rowid rotation, hard delete), not
    knowledge-specific, so an index missing from this map would silently never
    be swept. Bound to the module's own list rather than a second hand-written
    one — that copy is what drifts.
    """
    from techne.core.search import _VEC_TABLE_NAMES

    assert set(_VEC_PARENT) == set(_VEC_TABLE_NAMES)


def test_backfill_sweeps_orphans_end_to_end(db):
    """Exercised through backfill_vectors, because the WIRING is the fix.

    A unit test of the purge alone passes even if nothing ever calls it — which
    is the shape #408 itself describes: the mechanism exists and does not bind.
    """
    from techne.core.search import backfill_vectors

    conn = _vec_conn(db)
    try:
        _make_vec(conn, "threads_vec")
        _add_vec_row(conn, "threads_vec", 31337)
        conn.commit()
    finally:
        conn.close()

    backfill_vectors(db)

    conn = _vec_conn(db)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM threads_vec WHERE rowid NOT IN (SELECT rowid FROM threads)"
        ).fetchone()[0]
    finally:
        conn.close()

    assert left == 0, "backfill_vectors did not sweep the orphan — the purge is not wired in"
