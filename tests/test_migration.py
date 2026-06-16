"""Tests for v1 -> v2 database migration (outbox status column)."""

import sqlite3

import pytest
from click.testing import CliRunner

from techne.core.database import (
    _migrate,
    _reconcile_columns,
    _reconcile_tables,
    _reconcile_triggers,
)

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
        """Row with 'DEAD:...' synced_at gets status='dead' and error_message set."""
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
# event_at bi-temporal column (#8) — reconciler adds it to existing DBs
# ---------------------------------------------------------------------------

# Minimal observations/knowledge schema WITHOUT event_at (simulates a v7 DB).
SCHEMA_PRE_EVENT_AT = """
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
    summary_heuristic TEXT,
    summary_llm_source TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT REFERENCES threads(id),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tool_name TEXT,
    content TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot',
    tags TEXT,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    confidence_numeric REAL NOT NULL DEFAULT 0.6,
    last_confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES knowledge(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class TestEventAtColumn:
    """Reconciler adds event_at to observations and knowledge on existing DBs."""

    @pytest.fixture
    def pre_event_at_conn(self, tmp_path):
        db = tmp_path / "pre_event_at.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_PRE_EVENT_AT)
        conn.execute("INSERT INTO schema_version (version) VALUES (7)")
        conn.execute("INSERT INTO sessions (id, started_at) VALUES ('s1', CURRENT_TIMESTAMP)")
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier) "
            "VALUES ('s1', CURRENT_TIMESTAMP, 'old obs', 'hot')"
        )
        conn.execute("INSERT INTO knowledge (type, content) VALUES ('fact', 'old fact')")
        conn.commit()
        return conn

    def test_reconciler_adds_event_at_to_observations(self, pre_event_at_conn):
        cols_before = {r[1] for r in pre_event_at_conn.execute("PRAGMA table_info(observations)")}
        assert "event_at" not in cols_before

        _reconcile_columns(pre_event_at_conn)

        cols_after = {r[1] for r in pre_event_at_conn.execute("PRAGMA table_info(observations)")}
        assert "event_at" in cols_after

    def test_reconciler_adds_event_at_to_knowledge(self, pre_event_at_conn):
        cols_before = {r[1] for r in pre_event_at_conn.execute("PRAGMA table_info(knowledge)")}
        assert "event_at" not in cols_before

        _reconcile_columns(pre_event_at_conn)

        cols_after = {r[1] for r in pre_event_at_conn.execute("PRAGMA table_info(knowledge)")}
        assert "event_at" in cols_after

    def test_existing_rows_get_null_event_at(self, pre_event_at_conn):
        _reconcile_columns(pre_event_at_conn)

        obs = pre_event_at_conn.execute("SELECT event_at FROM observations").fetchone()
        assert obs["event_at"] is None

        kn = pre_event_at_conn.execute("SELECT event_at FROM knowledge").fetchone()
        assert kn["event_at"] is None

    def test_insert_with_explicit_event_at_round_trips(self, tmp_path):
        """INSERT with event_at set persists and reads back correctly."""
        from techne.core.database import connect

        db = tmp_path / "roundtrip.db"
        conn = connect(db)
        try:
            conn.execute("INSERT INTO sessions (id, platform) VALUES ('s2', 'test')")
            conn.execute(
                "INSERT INTO observations (session_id, timestamp, content, tier, event_at) "
                "VALUES ('s2', '2026-01-01T09:00:00Z', 'test obs', 'hot', '2026-01-01T08:00:00Z')"
            )
            conn.execute(
                "INSERT INTO knowledge (type, content, event_at) "
                "VALUES ('fact', 'test fact', '2026-01-01T07:00:00Z')"
            )
            conn.commit()

            obs = conn.execute("SELECT event_at FROM observations WHERE session_id='s2'").fetchone()
            assert obs["event_at"] == "2026-01-01T08:00:00Z"

            kn = conn.execute("SELECT event_at FROM knowledge WHERE type='fact'").fetchone()
            assert kn["event_at"] == "2026-01-01T07:00:00Z"
        finally:
            conn.close()

    def test_coalesce_ordering_prefers_event_at(self, tmp_path):
        """COALESCE(event_at, created_at) orders by event time, not ingestion time.

        Row A: ingested first (created_at earlier), but event happened second.
        Row B: ingested second, but event happened first.
        ORDER BY COALESCE(event_at, created_at) should return B then A.
        """
        from techne.core.database import connect

        db = tmp_path / "coalesce.db"
        conn = connect(db)
        try:
            conn.execute("INSERT INTO sessions (id, platform) VALUES ('s3', 'test')")
            # Row A: created_at=09:00, event_at=11:00 (late event, ingested early)
            conn.execute(
                "INSERT INTO knowledge (type, content, created_at, event_at) "
                "VALUES ('fact', 'row-A', '2026-01-01T09:00:00Z', '2026-01-01T11:00:00Z')"
            )
            # Row B: created_at=10:00, event_at=08:00 (early event, ingested late)
            conn.execute(
                "INSERT INTO knowledge (type, content, created_at, event_at) "
                "VALUES ('fact', 'row-B', '2026-01-01T10:00:00Z', '2026-01-01T08:00:00Z')"
            )
            conn.commit()

            rows = conn.execute(
                "SELECT content FROM knowledge WHERE type='fact' "
                "ORDER BY COALESCE(event_at, created_at)"
            ).fetchall()
            contents = [r["content"] for r in rows]
            assert contents == ["row-B", "row-A"]
        finally:
            conn.close()


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


# ---------------------------------------------------------------------------
# _reconcile_tables() — creates missing tables on existing DBs (#43)
# ---------------------------------------------------------------------------

# A minimal schema with schema_version + sessions only (no extension_state,
# no checkpoints, no FTS tables — simulates an upgraded DB that pre-dates
# those tables being added to SCHEMA_SQL).
SCHEMA_WITHOUT_EXTENSION_STATE = """
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
    summary_heuristic TEXT,
    summary_llm_source TEXT
);
"""


class TestReconcileTables:
    """_reconcile_tables() creates tables present in SCHEMA_SQL but absent live."""

    @pytest.fixture
    def conn_missing_tables(self, tmp_path):
        """DB with schema_version + sessions only — all other SCHEMA_SQL tables absent."""
        db = tmp_path / "missing_tables.db"
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA_WITHOUT_EXTENSION_STATE)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.commit()
        return c

    def test_missing_table_created(self, conn_missing_tables):
        """extension_state absent before reconcile → present after."""
        tables_before = {
            r[0]
            for r in conn_missing_tables.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "extension_state" not in tables_before

        _reconcile_tables(conn_missing_tables)

        tables_after = {
            r[0]
            for r in conn_missing_tables.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "extension_state" in tables_after

    def test_missing_table_accepts_insert(self, conn_missing_tables):
        """After reconcile, extension_state accepts a row insert without error."""
        _reconcile_tables(conn_missing_tables)
        conn_missing_tables.execute(
            "INSERT INTO extension_state (name, payload_json, updated_at) VALUES (?, ?, ?)",
            ("hook.state", '{"k":"v"}', 1718000000),
        )
        conn_missing_tables.commit()
        count = conn_missing_tables.execute("SELECT COUNT(*) FROM extension_state").fetchone()[0]
        assert count == 1

    def test_virtual_fts_tables_created(self, conn_missing_tables):
        """FTS virtual tables (threads_fts, observations_fts, knowledge_fts) are created."""
        # FTS tables depend on their content tables existing; wire the full set.
        _reconcile_tables(conn_missing_tables)
        for fts_table in ("threads_fts", "observations_fts", "knowledge_fts"):
            row = conn_missing_tables.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (fts_table,)
            ).fetchone()
            assert row is not None, f"{fts_table} was not created by _reconcile_tables()"

    def test_idempotent_no_error(self, conn_missing_tables):
        """Running _reconcile_tables() twice raises no error (IF NOT EXISTS guards)."""
        _reconcile_tables(conn_missing_tables)
        _reconcile_tables(conn_missing_tables)  # second pass must be a no-op

    def test_existing_tables_not_touched(self, conn_missing_tables):
        """The sessions table that was already present survives unchanged."""
        conn_missing_tables.execute("INSERT INTO sessions (id, platform) VALUES ('s-keep', 'test')")
        conn_missing_tables.commit()

        _reconcile_tables(conn_missing_tables)

        count = conn_missing_tables.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 1

    def test_mixed_missing_table_and_missing_column(self, tmp_path):
        """Reconciler handles a DB where a table is missing AND an existing
        table has a missing column — both are fixed in one connect() call."""
        from techne.core.database import connect

        db = tmp_path / "mixed.db"
        conn_raw = sqlite3.connect(str(db))
        # sessions without agent_identity; no extension_state table at all.
        conn_raw.executescript(
            SCHEMA_WITHOUT_EXTENSION_STATE.replace("    agent_identity TEXT,\n", "")
        )
        conn_raw.execute("INSERT INTO schema_version (version) VALUES (7)")
        conn_raw.commit()
        conn_raw.close()

        conn = connect(db)
        try:
            # extension_state created by _reconcile_tables
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='extension_state'"
            ).fetchone()
            assert row is not None

            # agent_identity added by _reconcile_columns
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            assert "agent_identity" in cols
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _reconcile_triggers() — creates missing FTS triggers on existing DBs (#45)
# ---------------------------------------------------------------------------

# A schema with FTS virtual tables present but NO triggers — simulates an
# existing DB that was upgraded by the #43 reconciler (tables yes, triggers no).
SCHEMA_FTS_NO_TRIGGERS = """
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
    summary_heuristic TEXT,
    summary_llm_source TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_num INTEGER NOT NULL,
    label TEXT NOT NULL,
    summary TEXT NOT NULL,
    resume_points TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    parent_thread_id TEXT REFERENCES threads(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT REFERENCES threads(id),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_at TIMESTAMP,
    tool_name TEXT,
    content TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot',
    tags TEXT,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    confidence_numeric REAL NOT NULL DEFAULT 0.6,
    last_confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES knowledge(id),
    event_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5(
    label, summary, resume_points, content=threads, content_rowid=rowid
);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    content, tags, kind, content=observations, content_rowid=id
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, tags, content=knowledge, content_rowid=id
);
"""


class TestReconcileTriggers:
    """_reconcile_triggers() creates FTS triggers present in FTS_TRIGGERS_SQL
    but absent from the live database."""

    @pytest.fixture
    def conn_no_triggers(self, tmp_path):
        """DB with FTS table present but triggers absent."""
        db = tmp_path / "no_triggers.db"
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA_FTS_NO_TRIGGERS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.commit()
        return c

    def test_missing_triggers_created(self, conn_no_triggers):
        """observations_ai/au/ad absent before reconcile → present after."""
        triggers_before = {
            r[0]
            for r in conn_no_triggers.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "observations_ai" not in triggers_before

        _reconcile_triggers(conn_no_triggers)

        triggers_after = {
            r[0]
            for r in conn_no_triggers.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for name in ("observations_ai", "observations_ad", "observations_au"):
            assert name in triggers_after, f"trigger {name!r} not created by reconciler"

    def test_fts_populated_after_reconcile(self, conn_no_triggers):
        """INSERT into observations after reconcile populates observations_fts."""
        _reconcile_triggers(conn_no_triggers)

        conn_no_triggers.execute("INSERT INTO sessions (id, platform) VALUES ('s-trig', 'test')")
        conn_no_triggers.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier) "
            "VALUES ('s-trig', CURRENT_TIMESTAMP, 'triggerreconcileprobe', 'hot')"
        )
        conn_no_triggers.commit()

        row = conn_no_triggers.execute(
            "SELECT rowid FROM observations_fts WHERE content MATCH 'triggerreconcileprobe'"
        ).fetchone()
        assert row is not None, "FTS not populated after _reconcile_triggers()"

    def test_idempotent(self, conn_no_triggers):
        """Running _reconcile_triggers() twice raises no error — second pass is a no-op."""
        _reconcile_triggers(conn_no_triggers)
        triggers_after_first = {
            r[0]
            for r in conn_no_triggers.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        _reconcile_triggers(conn_no_triggers)
        triggers_after_second = {
            r[0]
            for r in conn_no_triggers.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert triggers_after_first == triggers_after_second

    def test_connect_creates_triggers_on_upgrade(self, tmp_path):
        """connect() on a DB with FTS tables but no triggers installs all triggers."""
        from techne.core.database import connect

        # Build a raw DB: schema_version at v7, FTS table present, no triggers.
        db = tmp_path / "upgrade_no_trig.db"
        c = sqlite3.connect(str(db))
        c.executescript(SCHEMA_FTS_NO_TRIGGERS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.commit()
        c.close()

        conn = connect(db)
        try:
            live_triggers = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
            assert "observations_ai" in live_triggers
            assert "observations_ad" in live_triggers
            assert "observations_au" in live_triggers
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Gap 1 (issue #50): observations_fts indexes tags + kind
# ---------------------------------------------------------------------------


class TestObservationFtsTagsKind:
    """observations_fts must index tags and kind columns (#50 Gap 1)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        from techne.core.database import connect

        p = tmp_path / "fts_tags.db"
        conn = connect(p)
        conn.close()
        return p

    def test_observation_tag_searchable_via_fts(self, db_path):
        """A tag token written to observations.tags is findable via FTS MATCH."""
        from techne.core.database import connect
        from techne.core.search import search_fts
        from techne.core.state import start_session

        session_id = start_session(db_path)
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier, tags, kind) "
            "VALUES (?, CURRENT_TIMESTAMP, 'routine status update', 'hot',"
            " 'arch-decision refactor', 'decision-shaped')",
            (session_id,),
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, "arch-decision", table="observations")
        assert len(results) == 1, "tag token 'arch-decision' not found via FTS"

    def test_observation_kind_searchable_via_fts(self, db_path):
        """A kind token written to observations.kind is findable via FTS MATCH."""
        from techne.core.database import connect
        from techne.core.search import search_fts
        from techne.core.state import start_session

        session_id = start_session(db_path)
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier, tags, kind) "
            "VALUES (?, CURRENT_TIMESTAMP, 'checked the logs', 'hot', '', 'question-shaped')",
            (session_id,),
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, "question-shaped", table="observations")
        assert len(results) == 1, "kind token 'question-shaped' not found via FTS"

    def test_null_tags_and_kind_handled(self, db_path):
        """Observation with NULL tags/kind inserts without error and content is searchable."""
        from techne.core.database import connect
        from techne.core.search import search_fts
        from techne.core.state import start_session

        session_id = start_session(db_path)
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier) "
            "VALUES (?, CURRENT_TIMESTAMP, 'nulltagsprobe unique', 'hot')",
            (session_id,),
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, "nulltagsprobe", table="observations")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Gap 2 (issue #50): _reconcile_fts_backfill populates FTS for historical rows
# ---------------------------------------------------------------------------

# Reuse SCHEMA_FTS_NO_TRIGGERS as the base — it has content tables + FTS tables
# but no triggers, and the old single-column observations_fts definition.
# We seed rows before calling the reconciler so they look like "historical" data.

SCHEMA_HISTORICAL_ROWS = SCHEMA_FTS_NO_TRIGGERS


class TestReconcileFtsBackfill:
    """_reconcile_fts_backfill() populates FTS indexes for pre-existing rows (#50 Gap 2)."""

    @pytest.fixture
    def conn_with_rows(self, tmp_path):
        """DB at v7: has rows in observations/threads/knowledge, empty FTS indexes."""
        db = tmp_path / "historical.db"
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        # Use the old single-column observations_fts so we can test backfill into
        # the new multi-column shape after the migration drops+recreates it.
        c.executescript(SCHEMA_HISTORICAL_ROWS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        # Seed a session + observation row (no triggers, so FTS stays empty)
        c.execute("INSERT INTO sessions (id, platform) VALUES ('s-hist', 'test')")
        c.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier, tags, kind) "
            "VALUES ('s-hist', CURRENT_TIMESTAMP, 'historicalprobe content', 'hot',"
            " 'backfill-tag', 'fact-shaped')"
        )
        # Seed a knowledge row
        c.execute(
            "INSERT INTO knowledge"
            " (type, content, tags, confidence, confidence_numeric, last_confirmed_at) "
            "VALUES ('fact', 'knowledge-backfill-probe text', 'ktag', 'medium', 0.6,"
            " CURRENT_TIMESTAMP)"
        )
        c.commit()
        return c

    def test_fts_empty_before_backfill(self, conn_with_rows):
        """Sanity: FTS index has no indexed docs before reconciler runs.

        Content-table FTS5 VTs report COUNT(*) from the source table even when
        the index is empty — use the _docsize shadow table instead, which only
        counts actually-indexed documents.
        """
        count = conn_with_rows.execute("SELECT COUNT(*) FROM observations_fts_docsize").fetchone()[
            0
        ]
        assert count == 0

    def test_backfill_populates_observations_fts(self, tmp_path):
        """connect() on upgraded DB backfills observations_fts for historical rows."""
        from techne.core.database import connect

        # Build the DB raw (at v7, old schema, rows inserted, no triggers/backfill)
        db = tmp_path / "backfill_obs.db"
        c = sqlite3.connect(str(db))
        c.executescript(SCHEMA_HISTORICAL_ROWS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.execute("INSERT INTO sessions (id, platform) VALUES ('s-bf', 'test')")
        c.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier, tags, kind) "
            "VALUES ('s-bf', CURRENT_TIMESTAMP, 'historicalbfprobe', 'hot', 'bf-tag',"
            " 'fact-shaped')"
        )
        c.commit()
        c.close()

        # connect() runs _migrate (v7→v8 drops+recreates observations_fts, backfills)
        # then _reconcile_fts_backfill for any remaining empty FTS tables.
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT rowid FROM observations_fts"
                " WHERE observations_fts MATCH 'historicalbfprobe'"
            ).fetchone()
            assert row is not None, "historical observation not found in FTS after backfill"

            # Also verify tags were backfilled into the new multi-column index.
            # Wrap in quotes so FTS5 treats it as a phrase, not NOT-operator syntax.
            row_tag = conn.execute(
                "SELECT rowid FROM observations_fts WHERE observations_fts MATCH '\"bf-tag\"'",
            ).fetchone()
            assert row_tag is not None, "historical tag not indexed in FTS after backfill"
        finally:
            conn.close()

    def test_backfill_populates_knowledge_fts(self, tmp_path):
        """connect() on upgraded DB backfills knowledge_fts for historical rows."""
        from techne.core.database import connect

        db = tmp_path / "backfill_know.db"
        c = sqlite3.connect(str(db))
        c.executescript(SCHEMA_HISTORICAL_ROWS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.execute("INSERT INTO sessions (id, platform) VALUES ('s-kbf', 'test')")
        c.execute(
            "INSERT INTO knowledge"
            " (type, content, tags, confidence, confidence_numeric, last_confirmed_at) "
            "VALUES ('fact', 'knowledgebfprobe text', 'ktag-bf', 'medium', 0.6, CURRENT_TIMESTAMP)"
        )
        c.commit()
        c.close()

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'knowledgebfprobe'"
            ).fetchone()
            assert row is not None, "historical knowledge not found in FTS after backfill"
        finally:
            conn.close()

    def test_backfill_idempotent(self, tmp_path):
        """Running connect() twice doesn't duplicate FTS rows."""
        from techne.core.database import connect

        db = tmp_path / "backfill_idem.db"
        c = sqlite3.connect(str(db))
        c.executescript(SCHEMA_HISTORICAL_ROWS)
        c.execute("INSERT INTO schema_version (version) VALUES (7)")
        c.execute("INSERT INTO sessions (id, platform) VALUES ('s-idem', 'test')")
        c.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier) "
            "VALUES ('s-idem', CURRENT_TIMESTAMP, 'idempotentbfprobe', 'hot')"
        )
        c.commit()
        c.close()

        # First connect: backfills
        conn1 = connect(db)
        count1 = conn1.execute("SELECT COUNT(*) FROM observations_fts").fetchone()[0]
        conn1.close()

        # Second connect: should be a no-op (FTS already populated)
        conn2 = connect(db)
        count2 = conn2.execute("SELECT COUNT(*) FROM observations_fts").fetchone()[0]
        conn2.close()

        assert count1 == count2 == 1, f"idempotency broken: first={count1}, second={count2}"
