"""Two connections reconciling an empty FTS index at once backfill it once (techne#1259).

reconcile_fts_backfill checked `<fts>_docsize` and then inserted, outside any write
lock, and the reconcile passes run outside the migration flock on the first connect
after an upgrade, when several hooks race. Two processes could both see an empty
index and both insert; the doubled backfill leaves the external-content FTS5 index
failing its integrity-check ("database disk image is malformed").

The race is made deterministic by pausing each connection right after its emptiness
check until the other has made its own (or a timeout passes, which is what happens
once the check is under the write lock and the second connection is blocked).
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from techne.core import database as dbm
from techne.core import migrator
from techne.core.database import init_db
from techne.core.knowledge import store


class _PauseAfterCheck:
    """A connection whose `_docsize` check waits on a shared barrier."""

    def __init__(self, conn: sqlite3.Connection, barrier: threading.Barrier) -> None:
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args)
        if "knowledge_fts_docsize" in sql:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _integrity_ok(db) -> bool:
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO knowledge_fts(knowledge_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


@pytest.mark.parametrize("racers", [1, 2])
def test_racing_backfills_index_once(tmp_path, racers):
    db = tmp_path / "race.db"
    init_db(db).close()
    for i in range(5):
        store(db, f"row {i}", type="fact")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('delete-all')")
    conn.commit()
    conn.close()

    barrier = threading.Barrier(racers, timeout=1.5)
    errors: list[BaseException] = []

    def run() -> None:
        c = sqlite3.connect(db, timeout=10)
        try:
            migrator.reconcile_fts_backfill(
                _PauseAfterCheck(c, barrier),
                {"knowledge_fts": dbm._FTS_BACKFILL["knowledge_fts"]},
                {"knowledge_fts": dbm._FTS_INSERT_COLS["knowledge_fts"]},
            )
        except BaseException as exc:  # noqa: BLE001 -- reported below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert _integrity_ok(db), "a doubled backfill corrupts the external-content FTS index"
    conn = sqlite3.connect(db)
    try:
        hits = conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'row'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits == 5


def test_a_populated_index_is_left_alone(tmp_path):
    db = tmp_path / "ok.db"
    init_db(db).close()
    store(db, "already indexed", type="fact")
    conn = sqlite3.connect(db)
    try:
        migrator.reconcile_fts_backfill(
            conn,
            {"knowledge_fts": dbm._FTS_BACKFILL["knowledge_fts"]},
            {"knowledge_fts": dbm._FTS_INSERT_COLS["knowledge_fts"]},
        )
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _integrity_ok(db)
