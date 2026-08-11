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


def test_the_cli_remedy_sweeps_even_with_no_embedding_backend(tmp_path, monkeypatch):
    """`techne vectors backfill` is the string vectors:coverage hands the operator.

    The hoist inside backfill_vectors was defeated one frame up: the CLI carried
    the SAME backend_available() gate and `ctx.exit(0)`-ed before the import. On
    exactly the install the sweep exists for — sqlite-vec present, no model —
    threads kept stranding embeddings, the doctor kept reporting them, and the
    only remedy swept nothing. Fixing the function without the caller left the
    defect in place, just harder to see.

    Exercised through the real CLI, because the wiring is the fix.
    """
    from click.testing import CliRunner

    from techne.cli import main
    from techne.core import embeddings as embeddings_mod

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)

    # The CLI resolves its own db path from TECHNE_HOME — seed THAT store, not a
    # sibling fixture, or the command sweeps a database the test never inspects.
    from techne.config import Config

    db = Config.load().db_path
    init_db(db).close()

    conn = _vec_conn(db)
    try:
        _make_vec(conn, "threads_vec")
        _add_vec_row(conn, "threads_vec", 4242)
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["vectors", "backfill"])
    assert result.exit_code == 0, result.output

    conn = _vec_conn(db)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM threads_vec WHERE rowid NOT IN (SELECT rowid FROM threads)"
        ).fetchone()[0]
    finally:
        conn.close()

    assert left == 0, (
        "the CLI exited before sweeping — the remedy vectors:coverage names still "
        "does nothing on a no-backend install"
    )


def test_the_cli_does_not_claim_hygiene_ran_when_it_could_not(tmp_path, monkeypatch):
    """backfill_vectors returns {} for THREE outcomes and the CLI cannot tell
    them apart, so the old message asserted the good one.

    numpy lives only in the [vectors] extra, so `pip install techne` followed by
    `pip install sqlite-vec` — a SMALLER step than the hand-built venv this PR
    already assumes — satisfies the CLI's own import guard and returns at
    backfill_vectors' first line, sweeping nothing. Same for an interpreter that
    cannot load extensions, which the code's own comment names as real.

    Reporting success there is worse than #496's remedy-that-does-nothing: an
    operator told it succeeded stops looking.
    """
    import sys

    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config
    from techne.core import embeddings as embeddings_mod

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)
    cfg = Config.load()
    init_db(cfg.db_path).close()

    # numpy unimportable, sqlite_vec still importable — the CLI's guard passes.
    monkeypatch.setitem(sys.modules, "numpy", None)

    result = CliRunner().invoke(main, ["vectors", "backfill"])

    assert result.exit_code == 0, result.output
    assert "did NOT run" in result.output, (
        f"the CLI claimed hygiene ran when the sweep was never reached: {result.output!r}"
    )
    assert "were swept" not in result.output
    # AND IT MUST NOT NAME sqlite-vec: on this path it imported fine — the CLI's
    # own guard just passed — so blaming it sends the operator back to the one
    # thing they have already ruled out. Honest but wrong is still wrong.
    assert "sqlite-vec could not be loaded" not in result.output, result.output
    assert "numpy" in result.output, result.output


def test_the_cli_reports_the_swept_count_when_it_did_run(tmp_path, monkeypatch):
    """The honest line is also the useful one: a count tells the operator
    something, where the old sentence only reassured."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config
    from techne.core import embeddings as embeddings_mod

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)
    cfg = Config.load()
    init_db(cfg.db_path).close()

    conn = _vec_conn(cfg.db_path)
    try:
        _make_vec(conn, "threads_vec")
        _add_vec_row(conn, "threads_vec", 5150)
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["vectors", "backfill"])

    assert result.exit_code == 0, result.output
    assert "index hygiene: 1 orphaned embedding(s) swept." in result.output, result.output


def test_a_failed_sweep_is_not_reported_as_a_clean_zero(tmp_path, monkeypatch):
    """`orphans_swept: 0` from a sweep that FAILED on every index must not look
    like one that found nothing to do.

    The per-table `try` swallows OperationalError, so without a separate count
    the two states collapse — the same absent-vs-empty problem the KEY's absence
    exists to avoid, reappearing one level in.
    """
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config
    from techne.core import embeddings as embeddings_mod
    from techne.core import search as search_mod

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)
    cfg = Config.load()
    init_db(cfg.db_path).close()

    import sqlite3 as _sq

    def boom(conn, name):
        raise _sq.OperationalError(f"{name} unreadable")

    monkeypatch.setattr(search_mod, "_purge_orphaned_vec", boom)

    result = CliRunner().invoke(main, ["vectors", "backfill"])

    assert result.exit_code == 0, result.output
    assert "INCOMPLETE" in result.output, (
        f"a sweep that failed on every index reported as a clean zero: {result.output!r}"
    )
