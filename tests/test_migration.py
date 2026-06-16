"""Tests for v1 -> v2 database migration (outbox status column)."""

import sqlite3

import pytest
from click.testing import CliRunner

from techne.core.database import _migrate, _reconcile_columns

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
        max_version = v1_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
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
        max_version = v1_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_version == 3


# ---------------------------------------------------------------------------
# v3 -> v4 migration: split sessions.summary into typed provenance columns
# (summary_llm, summary_heuristic). Legacy `summary` content stays in place
# and is read with manual-slot priority by effective_summary().
# ---------------------------------------------------------------------------


V3_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_path TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    summary TEXT
);
"""


@pytest.fixture
def v3_conn(tmp_path):
    """v3 schema: outbox at v2 shape + sessions with the pre-split `summary`
    column populated. Used to test v3 -> v4 migration in isolation."""
    db_path = tmp_path / "v3_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_OUTBOX_SCHEMA)
    conn.executescript(V3_SESSIONS_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    conn.execute(
        "INSERT INTO sessions (id, project_path, started_at, summary) VALUES (?, ?, ?, ?)",
        (
            "session-v3-A",
            "/home/dev/proj",
            "2026-01-01T00:00:00+00:00",
            "**Thread 1: foo**\n3 observations; tools: bash(1), grep(2)",
        ),
    )
    conn.commit()
    return conn


class TestV3ToV4Migration:
    """v3 -> v4 adds typed summary columns (summary_llm, summary_heuristic)
    without dropping or rewriting legacy `summary` content."""

    def test_migration_adds_typed_columns(self, v3_conn):
        _migrate(v3_conn, 3, 4)
        cols = {row[1] for row in v3_conn.execute("PRAGMA table_info(sessions)")}
        assert "summary_llm" in cols
        assert "summary_heuristic" in cols

    def test_legacy_summary_preserved(self, v3_conn):
        """Pre-split `summary` content stays where it was. effective_summary()
        will surface it via the manual-slot priority read rule until/unless
        `techne migrate-summaries` reclassifies it."""
        _migrate(v3_conn, 3, 4)
        row = v3_conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id = 'session-v3-A'"
        ).fetchone()
        assert row["summary"].startswith("**Thread 1")
        assert row["summary_llm"] is None
        assert row["summary_heuristic"] is None

    def test_schema_version_bumped_to_4(self, v3_conn):
        _migrate(v3_conn, 3, 4)
        max_version = v3_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_version == 4

    def test_migration_is_idempotent_on_replay(self, v3_conn):
        """Running the migration when already at v4 should be a no-op, not a
        column-already-exists error."""
        _migrate(v3_conn, 3, 4)
        # Second call with from_version=4 means the v4 branch's gate
        # (from_version < 4) is false; nothing should run.
        _migrate(v3_conn, 4, 4)
        cols = {row[1] for row in v3_conn.execute("PRAGMA table_info(sessions)")}
        assert "summary_llm" in cols
        assert "summary_heuristic" in cols


# ---------------------------------------------------------------------------
# Schema reconciliation: auto-ADD missing columns on re-open (#15)
# ---------------------------------------------------------------------------

# Minimal sessions schema missing the agent_identity column that SCHEMA_SQL has.
SESSIONS_PRE_AGENT_IDENTITY = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    project_path TEXT,
    platform TEXT DEFAULT 'claude-code',
    summary TEXT,
    summary_llm TEXT,
    summary_heuristic TEXT
);
"""


class TestSchemaReconciliation:
    """_reconcile_columns() adds columns present in SCHEMA_SQL but absent live."""

    @pytest.fixture
    def old_conn(self, tmp_path):
        db = tmp_path / "reconcile_test.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(SESSIONS_PRE_AGENT_IDENTITY)
        conn.execute("INSERT INTO schema_version (version) VALUES (6)")
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, CURRENT_TIMESTAMP)",
            ("sess-001",),
        )
        conn.commit()
        return conn

    def test_missing_column_added(self, old_conn):
        """agent_identity is in SCHEMA_SQL but not the live table — reconciler adds it."""
        cols_before = {row[1] for row in old_conn.execute("PRAGMA table_info(sessions)")}
        assert "agent_identity" not in cols_before

        _reconcile_columns(old_conn)

        cols_after = {row[1] for row in old_conn.execute("PRAGMA table_info(sessions)")}
        assert "agent_identity" in cols_after

    def test_existing_row_gets_null(self, old_conn):
        """The pre-existing session row has NULL for the new column."""
        _reconcile_columns(old_conn)
        row = old_conn.execute(
            "SELECT agent_identity FROM sessions WHERE id = 'sess-001'"
        ).fetchone()
        assert row["agent_identity"] is None

    def test_idempotent(self, old_conn):
        """Running reconcile twice raises no error."""
        _reconcile_columns(old_conn)
        _reconcile_columns(old_conn)  # should be a no-op; column already present


# ---------------------------------------------------------------------------
# summary_llm_source column (#1) — reconciler adds it; round-trip via state
# ---------------------------------------------------------------------------

SESSIONS_PRE_LLM_SOURCE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    project_path TEXT,
    agent_identity TEXT,
    platform TEXT DEFAULT 'claude-code',
    summary TEXT,
    summary_llm TEXT,
    summary_heuristic TEXT
);
"""


class TestSummaryLlmSourceColumn:
    """summary_llm_source column is added by the reconciler and round-trips."""

    @pytest.fixture
    def pre_source_conn(self, tmp_path):
        db = tmp_path / "pre_source.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(SESSIONS_PRE_LLM_SOURCE)
        conn.execute("INSERT INTO schema_version (version) VALUES (7)")
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, CURRENT_TIMESTAMP)",
            ("sess-llm-src",),
        )
        conn.commit()
        return conn

    def test_reconciler_adds_column(self, pre_source_conn):
        """summary_llm_source absent from live table → reconciler adds it."""
        cols_before = {row[1] for row in pre_source_conn.execute("PRAGMA table_info(sessions)")}
        assert "summary_llm_source" not in cols_before

        _reconcile_columns(pre_source_conn)

        cols_after = {row[1] for row in pre_source_conn.execute("PRAGMA table_info(sessions)")}
        assert "summary_llm_source" in cols_after

    def test_existing_row_gets_null(self, pre_source_conn):
        """Pre-existing session row has NULL for the new column after reconcile."""
        _reconcile_columns(pre_source_conn)
        row = pre_source_conn.execute(
            "SELECT summary_llm_source FROM sessions WHERE id = 'sess-llm-src'"
        ).fetchone()
        assert row["summary_llm_source"] is None

    def test_round_trip_via_state(self, tmp_path):
        """end_session with source='llm' + summary_llm_source persists and reads back."""
        from techne.core.database import connect
        from techne.core.state import end_session, start_session

        db = tmp_path / "roundtrip.db"
        sid = start_session(db, project_path="/proj", platform="test")
        end_session(
            db, sid, summary="AI-generated title", source="llm", summary_llm_source="ai-title"
        )

        conn = connect(db)
        row = conn.execute(
            "SELECT summary_llm, summary_llm_source FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert row["summary_llm"] == "AI-generated title"
        assert row["summary_llm_source"] == "ai-title"

    def test_non_llm_source_does_not_set_llm_source(self, tmp_path):
        """summary_llm_source is not touched when source != 'llm'."""
        from techne.core.database import connect
        from techne.core.state import end_session, start_session

        db = tmp_path / "non_llm.db"
        sid = start_session(db)
        end_session(
            db, sid, summary="manual override", source="manual", summary_llm_source="ai-title"
        )

        conn = connect(db)
        row = conn.execute(
            "SELECT summary_llm_source FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert row["summary_llm_source"] is None


# ---------------------------------------------------------------------------
# migrate-summaries CLI: no-op detection (issue #4)
# ---------------------------------------------------------------------------


class TestMigrateSummariesNoop:
    """When schema >= v4 and no heuristic-shaped rows remain, the command
    prints 'nothing to do (already migrated)' and exits 0 without touching data."""

    def test_noop_when_already_migrated(self, tmp_path, monkeypatch):
        """Fresh v4+ db with no heuristic-shaped legacy summary → early exit."""
        from techne.cli import main
        from techne.core.database import init_db
        from techne.core.state import end_session, start_session

        # Point Config.load() at tmp_path so the CLI reads the right db.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

        # Init the db at the default location under the tmp HOME.
        data_dir = tmp_path / ".techne"
        data_dir.mkdir(parents=True, exist_ok=True)
        db = data_dir / "techne.db"
        init_db(db)

        sid = start_session(db, project_path="/proj")
        # Store a plain manual summary — NOT heuristic-shaped.
        end_session(db, sid, summary="manually written notes", source="manual")

        runner = CliRunner()
        result = runner.invoke(main, ["migrate-summaries"])

        assert result.exit_code == 0, result.output
        assert "nothing to do (already migrated)" in result.output
