"""Tests for v1 -> v2 database migration (outbox status column)."""

import sqlite3

import pytest

from techne.core.database import _migrate


V1_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    data_json TEXT NOT NULL,
    content_hash TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    synced_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_dedup
    ON outbox(content_hash) WHERE synced_at IS NULL;
"""


@pytest.fixture
def v1_conn(tmp_path):
    """Create a v1 database with sample data and return an open connection."""
    db_path = tmp_path / "v1_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_OUTBOX_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")

    # Three v1 rows:
    #   1. pending — synced_at IS NULL
    #   2. synced  — synced_at is a real timestamp
    #   3. dead    — synced_at = 'DEAD:some error'
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, synced_at) VALUES (?, ?, ?, ?)",
        ("knowledge", '{"content": "pending item"}', "hash_pending", None),
    )
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, synced_at) VALUES (?, ?, ?, ?)",
        ("knowledge", '{"content": "synced item"}', "hash_synced", "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, synced_at) VALUES (?, ?, ?, ?)",
        ("knowledge", '{"content": "dead item"}', "hash_dead", "DEAD:some error"),
    )
    conn.commit()
    return conn


class TestV1ToV2Migration:
    def test_migration_adds_columns(self, v1_conn):
        """After migration, the outbox table has status and error_message columns."""
        _migrate(v1_conn, 1, 2)
        cols = {row[1] for row in v1_conn.execute("PRAGMA table_info(outbox)")}
        assert "status" in cols
        assert "error_message" in cols

    def test_pending_row_status(self, v1_conn):
        """Row with synced_at IS NULL gets status='pending'."""
        _migrate(v1_conn, 1, 2)
        row = v1_conn.execute(
            "SELECT status, error_message FROM outbox WHERE content_hash = 'hash_pending'"
        ).fetchone()
        assert row["status"] == "pending"
        assert row["error_message"] is None

    def test_synced_row_status(self, v1_conn):
        """Row with a real timestamp gets status='synced'."""
        _migrate(v1_conn, 1, 2)
        row = v1_conn.execute(
            "SELECT status, error_message FROM outbox WHERE content_hash = 'hash_synced'"
        ).fetchone()
        assert row["status"] == "synced"
        assert row["error_message"] is None

    def test_dead_row_status_and_error(self, v1_conn):
        """Row with 'DEAD:...' synced_at gets status='dead', error_message set, synced_at cleared."""
        _migrate(v1_conn, 1, 2)
        row = v1_conn.execute(
            "SELECT status, error_message, synced_at FROM outbox WHERE content_hash = 'hash_dead'"
        ).fetchone()
        assert row["status"] == "dead"
        assert row["error_message"] == "some error"
        assert row["synced_at"] is None

    def test_schema_version_bumped(self, v1_conn):
        """After migration, schema_version contains a row with version=2."""
        _migrate(v1_conn, 1, 2)
        max_version = v1_conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_version == 2

    def test_dedup_index_recreated(self, v1_conn):
        """The dedup index exists and uses the new status='pending' predicate."""
        _migrate(v1_conn, 1, 2)
        idx = v1_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_outbox_dedup'"
        ).fetchone()
        assert idx is not None
        assert "status" in idx[0].lower()


class TestV2ToV3Migration:
    """v2 -> v3 adds the thread_boundaries table for the heuristic detector."""

    def test_migration_creates_thread_boundaries(self, v1_conn):
        # Get from v1 to v2 first, then v3.
        _migrate(v1_conn, 1, 2)
        _migrate(v1_conn, 2, 3)
        row = v1_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thread_boundaries'"
        ).fetchone()
        assert row is not None

    def test_thread_boundaries_columns(self, v1_conn):
        _migrate(v1_conn, 1, 2)
        _migrate(v1_conn, 2, 3)
        cols = {row[1] for row in v1_conn.execute("PRAGMA table_info(thread_boundaries)")}
        for required in (
            "session_id",
            "obs_index_a",
            "obs_index_b",
            "gap_seconds",
            "family_a",
            "family_b",
            "signals_json",
            "total_score",
            "is_boundary",
        ):
            assert required in cols

    def test_thread_boundaries_index(self, v1_conn):
        _migrate(v1_conn, 1, 2)
        _migrate(v1_conn, 2, 3)
        idx = v1_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_thread_boundaries_session'"
        ).fetchone()
        assert idx is not None

    def test_schema_version_bumped_to_3(self, v1_conn):
        _migrate(v1_conn, 1, 2)
        _migrate(v1_conn, 2, 3)
        max_version = v1_conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_version == 3
