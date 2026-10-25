"""techne#1241: an empty schema_version table must not brick every connect()."""

from __future__ import annotations

import sqlite3

import pytest

from techne.core import database
from techne.core.database import SCHEMA_VERSION, connect


def _empty_version_store(path):
    """The state a kill between SCHEMA_SQL's commit and the version insert leaves."""
    raw = sqlite3.connect(str(path))
    raw.executescript(database.SCHEMA_SQL)
    raw.execute("DELETE FROM schema_version")
    raw.commit()
    raw.close()


def test_connect_finishes_an_init_left_with_an_empty_schema_version(tmp_path):
    db = tmp_path / "techne.db"
    _empty_version_store(db)
    conn = connect(db)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        assert database._reconciled_version(conn) == SCHEMA_VERSION
        conn.execute("SELECT count(*) FROM knowledge").fetchone()
    finally:
        conn.close()
    connect(db).close()  # and it stays healthy on the next open


def test_an_empty_schema_version_is_not_migrated_from_v0(tmp_path, monkeypatch):
    db = tmp_path / "techne.db"
    _empty_version_store(db)
    called = []
    monkeypatch.setattr(database, "_guarded_migrate", lambda *a: called.append(a))
    connect(db).close()
    assert called == []


def test_connect_closes_its_connection_when_init_raises(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    def boom(conn, db_path):
        raise RuntimeError("disk full")

    monkeypatch.setattr(database.sqlite3, "connect", spy)
    monkeypatch.setattr(database, "_ensure_initialized", boom)
    with pytest.raises(RuntimeError):
        connect(tmp_path / "techne.db")
    (conn,) = opened
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # closed
