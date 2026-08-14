"""Tests for v1 -> v2 database migration (outbox status column)."""

import sqlite3

import pytest
from click.testing import CliRunner

from techne.core.database import (
    SCHEMA_VERSION,
    _migrate,
    _reconcile_columns,
    _reconcile_tables,
    _reconcile_triggers,
    connect,
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
# techne#132 — reconciler must handle columns with inline `-- …` comments
# ---------------------------------------------------------------------------

# An outbox table missing `status` — which in SCHEMA_SQL carries an inline comment
# AND a DEFAULT. Pre-fix, _reconcile_columns copied the comment + mid-line comma
# into the ALTER and crashed with a syntax error.
OUTBOX_PRE_STATUS = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    data_json TEXT NOT NULL,
    content_hash TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    synced_at TIMESTAMP
);
"""


class TestReconcileCommentedColumn:
    """A SCHEMA_SQL column with an inline `-- …` comment reconciles cleanly."""

    @pytest.fixture
    def pre_status_conn(self, tmp_path):
        db = tmp_path / "pre_status.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(OUTBOX_PRE_STATUS)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO outbox (target, data_json) VALUES ('s', '{}')",
        )
        conn.commit()
        return conn

    def test_commented_column_added_without_crashing(self, pre_status_conn):
        cols_before = {r[1] for r in pre_status_conn.execute("PRAGMA table_info(outbox)")}
        assert "status" not in cols_before

        _reconcile_columns(pre_status_conn)  # pre-#132: OperationalError near ","

        cols_after = {r[1] for r in pre_status_conn.execute("PRAGMA table_info(outbox)")}
        assert "status" in cols_after
        # The NOT NULL DEFAULT applied to the pre-existing row — comment stripped,
        # default preserved.
        row = pre_status_conn.execute("SELECT status FROM outbox").fetchone()
        assert row["status"] == "pending"

    def test_strip_sql_comment_helper(self):
        from techne.core.database import _strip_sql_comment

        line = "    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'synced'"
        assert _strip_sql_comment(line) == "    status TEXT NOT NULL DEFAULT 'pending',"
        assert _strip_sql_comment("    plain TEXT") == "    plain TEXT"

    def test_not_null_without_default_is_skipped_not_crashed(self, tmp_path, monkeypatch):
        """A NOT NULL column with no DEFAULT can't be added to a populated table;
        the reconciler must skip+warn rather than raise (#132 related)."""
        import techne.core.database as dbmod

        db = tmp_path / "nn.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT);"
        )
        conn.execute("INSERT INTO widget (name) VALUES ('x')")
        conn.commit()
        # Inject a SCHEMA_SQL whose widget table adds a NOT NULL column with no DEFAULT.
        monkeypatch.setattr(
            dbmod,
            "SCHEMA_SQL",
            "CREATE TABLE IF NOT EXISTS widget (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    name TEXT,\n"
            "    required TEXT NOT NULL\n"
            ");\n",
        )
        _reconcile_columns(conn)  # must not raise
        cols = {r[1] for r in conn.execute("PRAGMA table_info(widget)")}
        assert "required" not in cols  # skipped, not forced


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


# ---------------------------------------------------------------------------
# v8 -> v9: FTS5 Porter stemming (issue #77)
# ---------------------------------------------------------------------------


class TestV8ToV9Migration:
    """v9 recreates all FTS tables with tokenize='porter unicode61' and
    repopulates them from the content tables, so morphological variants match."""

    @pytest.fixture
    def v8_db(self, tmp_path):
        """A v8 DB: non-porter FTS tables + seeded content rows."""
        db = tmp_path / "v8.db"
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA_FTS_NO_TRIGGERS)  # old (non-porter) FTS definitions
        c.execute("INSERT INTO schema_version (version) VALUES (8)")
        c.execute("INSERT INTO sessions (id, platform) VALUES ('s9', 'test')")
        c.execute(
            "INSERT INTO observations (session_id, timestamp, content, tier) "
            "VALUES ('s9', CURRENT_TIMESTAMP, 'database performance optimization notes', 'hot')"
        )
        c.execute(
            "INSERT INTO knowledge (type, content, tags, confidence, confidence_numeric, "
            "last_confirmed_at) VALUES ('fact', 'structured logging pipelines', 'k', 'medium', "
            "0.6, CURRENT_TIMESTAMP)"
        )
        c.commit()
        return db, c

    def test_pre_migration_has_no_porter(self, v8_db):
        _, c = v8_db
        sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_fts'"
        ).fetchone()[0]
        assert "porter" not in sql

    def test_all_fts_tables_get_porter(self, v8_db):
        _, c = v8_db
        _migrate(c, 8, 9)
        for t in ("threads_fts", "observations_fts", "knowledge_fts"):
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            assert "porter" in sql, f"{t} missing porter tokenizer"

    def test_schema_version_bumped_to_9(self, v8_db):
        _, c = v8_db
        _migrate(c, 8, 9)
        assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9

    def test_content_repopulated_and_stemmed_match(self, v8_db):
        """The core win: a stemmed query ('optimize') matches a row that only
        contains the inflected form ('optimization') after migration."""
        _, c = v8_db
        _migrate(c, 8, 9)
        # exact term still matches (data survived the rebuild)
        assert c.execute(
            "SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH 'optimization'"
        ).fetchone()[0] == 1
        # stemmed variant now matches too (the new behaviour)
        assert c.execute(
            "SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH 'optimize'"
        ).fetchone()[0] == 1
        # knowledge stemming: 'log' matches 'logging'
        assert c.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'log'"
        ).fetchone()[0] == 1

    def test_idempotent_via_connect(self, v8_db):
        """connect() replays cleanly: a second open is a no-op, not a re-migration."""
        db, c = v8_db
        c.close()
        conn1 = connect(db)
        v1 = conn1.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        n1 = conn1.execute("SELECT COUNT(*) FROM observations_fts").fetchone()[0]
        conn1.close()
        conn2 = connect(db)
        v2 = conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        n2 = conn2.execute("SELECT COUNT(*) FROM observations_fts").fetchone()[0]
        conn2.close()
        assert v1 == v2 == SCHEMA_VERSION
        assert n1 == n2 == 1


# ---------------------------------------------------------------------------
# Migration hardening: atomic lock + backup + announce (issue #78)
# ---------------------------------------------------------------------------

import threading  # noqa: E402


def _make_v8_db(path):
    """Write a realistic v8 DB: the full current schema (so reconcile is a
    no-op on open) with the FTS tables + version marker rolled back to the
    pre-stemming v8 shape, plus one seeded row. Closed on return.

    Using the full schema (vs a minimal one) keeps the concurrency test honest:
    a real v8 DB being upgraded already has every table, so the only contention
    is the migration itself — not a storm of concurrent CREATE TABLE reconciles
    that wouldn't happen in practice.
    """
    c = connect(path)  # full v9 schema, all tables/triggers
    c.executescript("""
        DROP TABLE IF EXISTS threads_fts;
        DROP TABLE IF EXISTS observations_fts;
        DROP TABLE IF EXISTS knowledge_fts;
        CREATE VIRTUAL TABLE threads_fts USING fts5(
            label, summary, resume_points, content=threads, content_rowid=rowid);
        CREATE VIRTUAL TABLE observations_fts USING fts5(
            content, tags, kind, content=observations, content_rowid=id);
        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            content, tags, content=knowledge, content_rowid=id);
        DELETE FROM schema_version;
        INSERT INTO schema_version (version) VALUES (8);
    """)
    c.execute("INSERT INTO sessions (id, platform) VALUES ('s78', 'test')")
    c.execute(
        "INSERT INTO observations (session_id, timestamp, content, tier) "
        "VALUES ('s78', CURRENT_TIMESTAMP, 'backup probe content', 'hot')"
    )
    c.commit()
    c.close()


class TestMigrationHardening:
    """#78: migrations back up the DB, announce on stderr, and run exactly once
    even under concurrent opens."""

    def test_backup_created_and_restorable(self, tmp_path, capsys):
        db = tmp_path / "techne.db"
        _make_v8_db(db)

        conn = connect(db)  # triggers guarded migration 8 -> latest
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()

        backups = list((tmp_path / "backups").glob(f"techne-v8-to-v{SCHEMA_VERSION}-*.db.bak"))
        assert len(backups) == 1, f"expected one backup, got {backups}"

        # The backup is a real, openable pre-migration snapshot (still v8).
        b = sqlite3.connect(str(backups[0]))
        assert b.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
        # ...and the seeded row survived into the snapshot.
        assert b.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        b.close()

    def test_announce_on_stderr(self, tmp_path, capsys):
        db = tmp_path / "techne.db"
        _make_v8_db(db)
        connect(db).close()
        err = capsys.readouterr().err
        assert f"migrating schema v8 → v{SCHEMA_VERSION}" in err
        assert "backup:" in err

    def test_no_backup_for_fresh_db(self, tmp_path):
        """A brand-new DB initializes directly at SCHEMA_VERSION — no migration,
        so no backup file is written."""
        db = tmp_path / "fresh.db"
        connect(db).close()
        assert not (tmp_path / "backups").exists()

    def test_concurrent_open_migrates_once(self, tmp_path):
        """N threads opening a stale DB at once → the migration body runs once:
        a single v9 row and a single backup, no duplicate-version rows."""
        db = tmp_path / "techne.db"
        _make_v8_db(db)

        barrier = threading.Barrier(6)
        errors = []

        def worker():
            try:
                barrier.wait()  # maximize the race window
                connect(db).close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent connects raised: {errors}"

        check = sqlite3.connect(str(db))
        assert check.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        # Exactly one process bumped to the latest version (no duplicate rows
        # from a double-run).
        assert check.execute(
            f"SELECT COUNT(*) FROM schema_version WHERE version = {SCHEMA_VERSION}"
        ).fetchone()[0] == 1
        check.close()

        # Exactly one migration fired → exactly one backup.
        backups = list((tmp_path / "backups").glob("*.db.bak"))
        assert len(backups) == 1, f"expected one backup, got {backups}"


# ---------------------------------------------------------------------------
# v9 -> v10: rewrite bare (un-namespaced) pending outbox targets (issue #80)
# ---------------------------------------------------------------------------


# Minimal schema to exercise the outbox namespace migration in isolation.
# Includes the outbox table at its current (v9) shape.
SCHEMA_V9_OUTBOX = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    data_json TEXT NOT NULL,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    synced_at TIMESTAMP
);
"""


@pytest.fixture
def v9_outbox_conn(tmp_path):
    """A v9 DB with a mix of outbox rows covering all target/status cases."""
    db_path = tmp_path / "v9_outbox.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_V9_OUTBOX)
    conn.execute("INSERT INTO schema_version (version) VALUES (9)")

    # bare target — pending → must be rewritten to 'sync:thread_summary'
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?, ?, ?, ?)",
        ("thread_summary", '{"x":1}', "hash_bare_pending", "pending"),
    )
    # already-namespaced sync: target — pending → must NOT be double-prefixed
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?, ?, ?, ?)",
        ("sync:thread_summary", '{"x":2}', "hash_sync_pending", "pending"),
    )
    # sink: target — pending → must NOT be touched (different namespace lane)
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?, ?, ?, ?)",
        ("sink:rookery", '{"x":3}', "hash_sink_pending", "pending"),
    )
    # bare target — synced → terminal row, must NOT be rewritten
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status, synced_at) VALUES (?, ?, ?, ?, ?)",
        ("thread_summary", '{"x":4}', "hash_bare_synced", "synced", "2026-01-01T00:00:00Z"),
    )
    # bare target — dead → terminal row, must NOT be rewritten
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status, error_message) VALUES (?, ?, ?, ?, ?)",
        ("thread_summary", '{"x":5}', "hash_bare_dead", "dead", "delivery failed"),
    )
    conn.commit()
    return conn


class TestV9ToV10Migration:
    """v10 rewrites bare pending outbox targets to the sync: namespace (#80)."""

    def test_bare_pending_target_gets_sync_prefix(self, v9_outbox_conn):
        """A bare pending target like 'thread_summary' becomes 'sync:thread_summary'."""
        _migrate(v9_outbox_conn, 9, 10)
        row = v9_outbox_conn.execute(
            "SELECT target FROM outbox WHERE content_hash = 'hash_bare_pending'"
        ).fetchone()
        assert row["target"] == "sync:thread_summary"

    def test_already_namespaced_sync_row_untouched(self, v9_outbox_conn):
        """A pending row already prefixed 'sync:' is not double-prefixed."""
        _migrate(v9_outbox_conn, 9, 10)
        row = v9_outbox_conn.execute(
            "SELECT target FROM outbox WHERE content_hash = 'hash_sync_pending'"
        ).fetchone()
        assert row["target"] == "sync:thread_summary"

    def test_sink_namespaced_row_untouched(self, v9_outbox_conn):
        """A pending row with a 'sink:' prefix is left alone."""
        _migrate(v9_outbox_conn, 9, 10)
        row = v9_outbox_conn.execute(
            "SELECT target FROM outbox WHERE content_hash = 'hash_sink_pending'"
        ).fetchone()
        assert row["target"] == "sink:rookery"

    def test_synced_terminal_row_untouched(self, v9_outbox_conn):
        """A bare target on a synced (terminal) row is not rewritten."""
        _migrate(v9_outbox_conn, 9, 10)
        row = v9_outbox_conn.execute(
            "SELECT target FROM outbox WHERE content_hash = 'hash_bare_synced'"
        ).fetchone()
        assert row["target"] == "thread_summary"

    def test_dead_terminal_row_untouched(self, v9_outbox_conn):
        """A bare target on a dead (terminal) row is not rewritten."""
        _migrate(v9_outbox_conn, 9, 10)
        row = v9_outbox_conn.execute(
            "SELECT target FROM outbox WHERE content_hash = 'hash_bare_dead'"
        ).fetchone()
        assert row["target"] == "thread_summary"

    def test_schema_version_bumped_to_10(self, v9_outbox_conn):
        """schema_version reaches 10 after the migration."""
        _migrate(v9_outbox_conn, 9, 10)
        max_v = v9_outbox_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_v == 10

    def test_migration_via_connect(self, tmp_path):
        """connect() on a v9 DB runs the migration automatically."""
        from techne.core.database import connect

        db_path = tmp_path / "v9_connect.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(SCHEMA_V9_OUTBOX)
        raw.execute("INSERT INTO schema_version (version) VALUES (9)")
        raw.execute(
            "INSERT INTO outbox (target, data_json, status) VALUES (?, ?, ?)",
            ("thread_summary", '{"x":1}', "pending"),
        )
        raw.commit()
        raw.close()

        conn = connect(db_path)
        try:
            row = conn.execute("SELECT target FROM outbox WHERE status='pending'").fetchone()
            assert row["target"] == "sync:thread_summary"
            # connect() migrates to the latest schema, not just v10 — anchor on
            # SCHEMA_VERSION so this tracks future bumps instead of breaking.
            assert (
                conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                == SCHEMA_VERSION
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# v10 -> v11: evidence-anchor columns on knowledge (issue #90)
# ---------------------------------------------------------------------------


# A v10-shape knowledge table — the pre-#90 columns only, no anchor fields.
SCHEMA_V10_KNOWLEDGE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
"""


@pytest.fixture
def v10_knowledge_conn(tmp_path):
    """A v10 DB whose knowledge table predates the #90 anchor columns, with one
    non-anchored row seeded so we can prove it survives untouched."""
    db_path = tmp_path / "v10_knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_V10_KNOWLEDGE)
    conn.execute("INSERT INTO schema_version (version) VALUES (10)")
    conn.execute(
        "INSERT INTO knowledge (type, content, confidence) VALUES (?, ?, ?)",
        ("fact", "behavioral memory with no anchor", "high"),
    )
    conn.commit()
    return conn


class TestV10ToV11Migration:
    """v11 adds the four nullable evidence-anchor columns to knowledge (#90).

    All assertions are anchored on the SCHEMA_VERSION constant, never a
    hardcoded integer, so this test tracks the schema as it evolves.
    """

    def test_anchor_columns_added(self, v10_knowledge_conn):
        cols_before = {row[1] for row in v10_knowledge_conn.execute("PRAGMA table_info(knowledge)")}
        assert "anchor_path" not in cols_before

        _migrate(v10_knowledge_conn, 10, SCHEMA_VERSION)

        cols_after = {row[1] for row in v10_knowledge_conn.execute("PRAGMA table_info(knowledge)")}
        for col in ("anchor_path", "anchor_symbol", "content_hash", "anchor_git_sha"):
            assert col in cols_after

    def test_existing_row_survives_with_null_anchor(self, v10_knowledge_conn):
        """A pre-#90 row is preserved and its new anchor columns default NULL —
        non-anchored memory is never staleness-checked."""
        _migrate(v10_knowledge_conn, 10, SCHEMA_VERSION)
        row = v10_knowledge_conn.execute(
            "SELECT content, anchor_path, content_hash, anchor_git_sha FROM knowledge"
        ).fetchone()
        assert row["content"] == "behavioral memory with no anchor"
        assert row["anchor_path"] is None
        assert row["content_hash"] is None
        assert row["anchor_git_sha"] is None

    def test_schema_version_bumped(self, v10_knowledge_conn):
        _migrate(v10_knowledge_conn, 10, SCHEMA_VERSION)
        max_v = v10_knowledge_conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_v == SCHEMA_VERSION

    def test_migration_via_connect(self, tmp_path):
        """connect() on a v10 DB runs the migration automatically and reaches
        the current SCHEMA_VERSION with the anchor columns present."""
        db_path = tmp_path / "v10_connect.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(SCHEMA_V10_KNOWLEDGE)
        raw.execute("INSERT INTO schema_version (version) VALUES (10)")
        raw.commit()
        raw.close()

        conn = connect(db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
            assert "anchor_path" in cols
            assert (
                conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                == SCHEMA_VERSION
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# techne#138 — reconcile_indexes: indexes converge on already-existing tables
# ---------------------------------------------------------------------------


class TestReconcileIndexes:
    """An index added to SCHEMA_SQL must land on an upgraded DB whose table
    already exists (the pre-#138 reconciler only created a table's indexes when
    the *table* was missing)."""

    def test_missing_index_created(self, tmp_path):
        from techne.core import migrator

        conn = sqlite3.connect(str(tmp_path / "idx.db"))
        conn.executescript("CREATE TABLE t (a INTEGER, b INTEGER);")  # table, no index
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_t_a" not in before

        schema = (
            "CREATE TABLE IF NOT EXISTS t (a INTEGER, b INTEGER);\n"
            "CREATE INDEX IF NOT EXISTS idx_t_a ON t(a);\n"
        )
        migrator.reconcile_indexes(conn, schema)

        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_t_a" in after
        # Idempotent — a second pass is a no-op, no error.
        migrator.reconcile_indexes(conn, schema)
        conn.close()

    def test_multiline_and_unique_index(self, tmp_path):
        from techne.core import migrator

        conn = sqlite3.connect(str(tmp_path / "idx2.db"))
        conn.executescript("CREATE TABLE u (x INTEGER, y INTEGER);")
        schema = (
            "CREATE TABLE IF NOT EXISTS u (x INTEGER, y INTEGER);\n"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_u_xy\n"
            "    ON u(x, y);\n"
        )
        migrator.reconcile_indexes(conn, schema)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_u_xy" in names
        conn.close()

    def test_all_schema_indexes_present_after_connect(self, tmp_path):
        """Convergence: drop a real SCHEMA_SQL index, then on an UNSTAMPED reopen the
        reconciler restores it (#138). Reconcile is now gated on a sentinel (#140) — it
        runs on a version change / unstamped db, not on every connect — so we clear the
        stamp to simulate the trigger that invokes it."""
        from techne.core import migrator
        from techne.core.database import SCHEMA_SQL, _RECONCILE_SENTINEL

        db_path = tmp_path / "converge.db"
        conn = connect(db_path)  # fresh DB at current schema — all indexes present
        expected = {name for name, _ in migrator._index_statements(SCHEMA_SQL)}
        assert "idx_knowledge_type" in expected  # sanity: we parsed real indexes
        conn.execute("DROP INDEX idx_knowledge_type")
        # Clear the reconcile stamp → next connect treats the db as un-reconciled.
        conn.execute("DELETE FROM extension_state WHERE name = ?", (_RECONCILE_SENTINEL,))
        conn.commit()
        conn.close()

        conn = connect(db_path)  # reopen unstamped → _reconcile_indexes restores it
        try:
            live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert expected <= live  # every SCHEMA_SQL index exists
        finally:
            conn.close()

    def test_reconcile_gated_on_sentinel(self, tmp_path):
        """techne#140: an already-stamped current-version db SKIPS the reconcile passes
        on connect (the hot-path win) — a dropped index is NOT auto-restored while the
        sentinel matches; clearing the stamp re-arms reconcile, which restores it."""
        from techne.core.database import SCHEMA_VERSION, _RECONCILE_SENTINEL
        from techne.core.extension_state import get_state

        db_path = tmp_path / "gated.db"
        conn = connect(db_path)
        # A fresh db is stamped at the current version (so the next connect skips).
        assert get_state(conn, _RECONCILE_SENTINEL) == {"version": SCHEMA_VERSION}
        conn.execute("DROP INDEX idx_knowledge_type")  # drop, but leave the stamp intact
        conn.commit()
        conn.close()

        conn = connect(db_path)  # stamped → reconcile SKIPPED → index stays gone
        try:
            live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert "idx_knowledge_type" not in live, "reconcile should be skipped while stamped"
        finally:
            conn.close()

        # Clearing the stamp re-arms reconcile, which restores the index and re-stamps.
        conn = connect(db_path)
        conn.execute("DELETE FROM extension_state WHERE name = ?", (_RECONCILE_SENTINEL,))
        conn.commit()
        conn.close()
        conn = connect(db_path)
        try:
            live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert "idx_knowledge_type" in live
            assert get_state(conn, _RECONCILE_SENTINEL) == {"version": SCHEMA_VERSION}
        finally:
            conn.close()

    def test_force_reconcile_repairs_stamped_db(self, tmp_path):
        """techne#140: force_reconcile (the `techne reconcile` escape hatch) re-runs the
        passes on an already-stamped db WITHOUT a manual sentinel clear, restoring drift."""
        from techne.core.database import force_reconcile

        db_path = tmp_path / "force.db"
        conn = connect(db_path)  # fresh → stamped
        conn.execute("DROP INDEX idx_knowledge_type")
        conn.commit()
        conn.close()

        # A plain reconnect would skip (stamped); force_reconcile must restore it.
        force_reconcile(db_path)

        conn = connect(db_path)
        try:
            live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert "idx_knowledge_type" in live, "force_reconcile should restore drift"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# techne#169: migration idempotency + step-12 trigger redeploy
# ---------------------------------------------------------------------------


def test_add_column_if_missing_is_idempotent():
    """techne#169: the shared helper used by steps 2/4/5/7 must be a safe no-op on
    replay — re-running after a crash-between-ALTER-and-version-bump must not raise
    'duplicate column name'."""
    from techne.core.database import _add_column_if_missing

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER)")
    _add_column_if_missing(conn, "t", "x", "TEXT")
    _add_column_if_missing(conn, "t", "x", "TEXT")  # replay — must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
    assert "x" in cols
    conn.close()


def test_step2_replay_after_partial_apply_does_not_crash(v1_conn):
    """techne#169: simulate a crash that committed step-2's ALTERs but not the
    version bump. Re-running _migrate must complete (guarded ALTER), not die with
    'duplicate column name'. On the pre-fix bare-ALTER code this raised."""
    v1_conn.execute("ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
    v1_conn.execute("ALTER TABLE outbox ADD COLUMN error_message TEXT")
    v1_conn.commit()

    _migrate(v1_conn, 1, 2)  # must not raise

    cols = {row[1] for row in v1_conn.execute("PRAGMA table_info(outbox)")}
    assert "status" in cols and "error_message" in cols
    assert v1_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2


_OLD_KNOWLEDGE_TRIGGERS = """
DROP TRIGGER IF EXISTS knowledge_ai;
DROP TRIGGER IF EXISTS knowledge_au;
DROP TRIGGER IF EXISTS knowledge_ad;
CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (NEW.id, NEW.content, NEW.tags);
END;
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, OLD.tags);
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (NEW.id, NEW.content, NEW.tags);
END;
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, OLD.tags);
END;
"""


class TestStep12KnowledgeTriggerCoalesce:
    """techne#169: knowledge_ai/ad/au must COALESCE(tags,'') to match the value the
    backfill/rebuild paths index; step 12 redeploys the corrected bodies because the
    name-only reconcile_triggers won't update an existing trigger."""

    def test_fresh_db_triggers_have_coalesce(self, tmp_path):
        conn = connect(tmp_path / "fresh.db")
        try:
            for name in ("knowledge_ai", "knowledge_au", "knowledge_ad"):
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                ).fetchone()[0]
                assert "COALESCE" in sql, name
        finally:
            conn.close()

    def test_step12_redeploys_coalesce_over_old_bodies(self, tmp_path):
        conn = connect(tmp_path / "old.db")  # fresh = v12, correct triggers
        try:
            # Downgrade to the pre-fix (no-COALESCE) trigger bodies + drop all
            # markers >= 12 so _migrate(11, 12) re-runs step 12 as on a real pre-fix
            # DB. Deleting '>= 12' rather than '= 12' is necessary because the fresh
            # DB now starts at SCHEMA_VERSION (>12), and leaving a higher-version row
            # would make MAX(version) > 12 after the migration call.
            conn.executescript(_OLD_KNOWLEDGE_TRIGGERS)
            conn.execute("DELETE FROM schema_version WHERE version >= 12")
            conn.commit()
            assert "COALESCE" not in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='knowledge_au'"
            ).fetchone()[0]

            _migrate(conn, 11, 12)

            for name in ("knowledge_ai", "knowledge_au", "knowledge_ad"):
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                ).fetchone()[0]
                assert "COALESCE" in sql, f"{name} not redeployed with COALESCE"
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 12
        finally:
            conn.close()

    def test_null_tags_row_indexes_and_updates_cleanly(self, tmp_path):
        """Behaviour lock (NOT a bug differentiator): a NULL-tags knowledge row must
        insert, search, and update through the corrected triggers without leaving a
        stale FTS row. NB: this passes on the OLD bare-NEW.tags triggers too — for the
        `tags` column NULL and '' tokenize identically, which is exactly why the
        missing COALESCE caused no demonstrable corruption (techne#169). The real
        guarantee is the redeploy test above asserting the COALESCE body ships."""
        conn = connect(tmp_path / "k.db")  # v12, COALESCE triggers
        try:
            conn.execute(
                "INSERT INTO knowledge (type, content, confidence, tags) "
                "VALUES ('fact', 'alpha apple', 'high', NULL)"
            )
            kid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            assert conn.execute(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'apple'"
            ).fetchone()[0] == 1

            conn.execute("UPDATE knowledge SET content = 'beta banana' WHERE id = ?", (kid,))
            conn.commit()
            assert conn.execute(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'apple'"
            ).fetchone()[0] == 0  # no stale row
            assert conn.execute(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'banana'"
            ).fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# techne#141: migration concurrency — fork racing writers, prove the loser no-ops
# ---------------------------------------------------------------------------

import os  # noqa: E402
import time  # noqa: E402


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required for this race test")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # os.fork in a threaded interp (3.14)
def test_concurrent_migration_serializes_no_duplicate_versions(tmp_path):
    """techne#141: Claude Code fires session_start + post_tool_use near-simultaneously, so
    on the first open after a code bump several processes can all see a stale version and
    race into the migration body. guarded_migrate's fcntl lock + in-lock double-check must
    serialize them — the loser re-reads the version under the lock and no-ops.

    Fork N writers that all open connect() (which runs the one pending migration step) at
    once via a barrier. THE serialization probe is the child EXIT CODE: if the fcntl lock
    failed, ≥2 processes read the stale version in their double-check, both run the step,
    and the loser's `INSERT INTO schema_version (version) VALUES (N)` hits the UNIQUE
    constraint → IntegrityError → nonzero exit → the `all(e == 0)` assert fails. (The
    final `set(versions)` check is a state sanity-check, not the proof — the UNIQUE column
    means a doubled insert RAISES rather than leaving a duplicate row.)"""
    from techne.core.database import init_db

    db_path = tmp_path / "race.db"
    # Seed a FULL current-schema DB (all tables/triggers present), then roll the recorded
    # version back one so the only pending migration is the latest step — a realistic
    # "first open after a code bump" with the real tables present. (A minimal old schema
    # would crash the migration on missing tables and mask the lock instead of testing it.)
    init_db(db_path).close()
    seed = sqlite3.connect(str(db_path))
    seed.execute("DELETE FROM schema_version")
    seed.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION - 1,))
    seed.commit()
    seed.close()

    n = 8  # enough to force contention; more only adds reconciler churn, not lock coverage
    go = tmp_path / "go"
    pids: list[int] = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:  # ---- child ----
            code = 0
            try:
                # Barrier: release all children together. Bounded so a child can never spin
                # forever as an orphan if the parent dies before writing `go`.
                deadline = time.monotonic() + 30.0
                while not go.exists():
                    if time.monotonic() > deadline:
                        os._exit(4)
                    time.sleep(0.001)
                conn = connect(db_path)  # triggers guarded_migrate to SCHEMA_VERSION
                v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                conn.close()
                code = 0 if v == SCHEMA_VERSION else 2
            except BaseException:
                code = 1
            os._exit(code)
        pids.append(pid)

    go.write_text("go")  # fire the start gun
    exits = [os.WEXITSTATUS(os.waitpid(pid, 0)[1]) for pid in pids]
    assert all(e == 0 for e in exits), f"racing migrators failed (exit codes): {exits}"

    conn = connect(db_path)
    try:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()]
    finally:
        conn.close()
    # Exactly the seed version + the one applied migration — no step ran twice, none skipped.
    assert set(versions) == {SCHEMA_VERSION - 1, SCHEMA_VERSION}, f"unexpected versions: {versions}"


# ---------------------------------------------------------------------------
# #56-16: schema-parser robustness — mid-block comment must not prematurely
# flush a CREATE TABLE statement
# ---------------------------------------------------------------------------


class TestSchemaTableStatementsComments:
    """_schema_table_statements must treat statement boundaries as the CREATE TABLE
    lines themselves (not a comment-based heuristic), so a -- comment inside the
    CREATE TABLE body does not prematurely end the statement (techne#56)."""

    def test_mid_block_comment_parses_as_one_statement(self):
        """A CREATE TABLE with a -- comment INSIDE the parens is ONE statement
        under the paren-balance parser; the old comment heuristic would have
        prematurely flushed the half-built statement."""
        from techne.core.migrator import _schema_table_statements

        schema = (
            "CREATE TABLE IF NOT EXISTS my_table (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    -- this is a mid-block comment\n"
            "    name TEXT NOT NULL,\n"
            "    value TEXT\n"
            ");\n"
        )
        results = _schema_table_statements(schema)
        assert len(results) == 1, (
            f"Expected 1 statement, got {len(results)}: {results}"
        )
        name, sql = results[0]
        assert name == "my_table"
        # The closing paren + semicolon and all columns must be in the statement.
        assert "name TEXT NOT NULL" in sql
        assert "value TEXT" in sql

    def test_multi_table_with_mid_block_comments(self):
        """Two CREATE TABLE blocks, each with an interior comment, parse as
        two independent statements — not merged or split wrongly."""
        from techne.core.migrator import _schema_table_statements

        schema = (
            "CREATE TABLE IF NOT EXISTS foo (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    -- comment inside foo\n"
            "    x TEXT\n"
            ");\n"
            "\n"
            "CREATE TABLE IF NOT EXISTS bar (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    -- comment inside bar\n"
            "    y TEXT\n"
            ");\n"
        )
        results = _schema_table_statements(schema)
        assert len(results) == 2, (
            f"Expected 2 statements, got {len(results)}: {[r[0] for r in results]}"
        )
        names = {name for name, _ in results}
        assert names == {"foo", "bar"}

    def test_comment_after_semicolon_does_not_merge(self):
        """A -- comment line that appears AFTER the closing ';' must NOT cause
        the next CREATE TABLE to be merged into the prior block."""
        from techne.core.migrator import _schema_table_statements

        schema = (
            "CREATE TABLE IF NOT EXISTS t1 (\n"
            "    id INTEGER PRIMARY KEY\n"
            ");\n"
            "-- separator comment between tables\n"
            "CREATE TABLE IF NOT EXISTS t2 (\n"
            "    id INTEGER PRIMARY KEY\n"
            ");\n"
        )
        results = _schema_table_statements(schema)
        assert len(results) == 2, (
            f"Expected 2 statements, got {len(results)}: {[r[0] for r in results]}"
        )
        names = {name for name, _ in results}
        assert names == {"t1", "t2"}


# ---------------------------------------------------------------------------
# v13 -> v14: exactly-once reflect ingest (issue #236)
# Partial UNIQUE(prompt_hash, content) WHERE origin='reflect' on
# knowledge_candidates, with a dedup pass that must run BEFORE the index so a DB
# already carrying duplicate reflect candidates upgrades cleanly.
# ---------------------------------------------------------------------------

_SCHEMA_V13_KC = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT,
    model_used TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    promoted_to_id INTEGER,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL DEFAULT 'add',
    target_ids TEXT,
    origin TEXT NOT NULL DEFAULT 'reflect',
    faithfulness_ok INTEGER,
    faithfulness_warning TEXT
);
"""


class TestV13ToCurrentMigration:
    """v13 → SCHEMA_VERSION: dedup reflect candidates (v14) and add reminders table (v15+)."""

    @pytest.fixture
    def v13_conn(self, tmp_path):
        db = tmp_path / "v13.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_V13_KC)
        conn.execute("INSERT INTO schema_version (version) VALUES (13)")
        conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
        # Two IDENTICAL reflect candidates (same prompt_hash + content) — exactly the
        # duplicate a pre-#236 re-ingest produced. The migration must dedup them before
        # the UNIQUE index, or CREATE UNIQUE INDEX would fail.
        for _ in range(2):
            conn.execute(
                "INSERT INTO knowledge_candidates "
                "(scope, type, content, source_session_id, model_used, prompt_hash, origin) "
                "VALUES ('knowledge','decision','dup content','s1','m','hh','reflect')"
            )
        # A non-reflect row sharing the same (prompt_hash, content) must survive: the
        # index (and the dedup) are scoped to origin='reflect'.
        conn.execute(
            "INSERT INTO knowledge_candidates "
            "(scope, type, content, source_session_id, model_used, prompt_hash, origin) "
            "VALUES ('knowledge','decision','dup content','s1','m','hh','dream')"
        )
        conn.commit()
        return conn

    def test_dedups_reflect_leaves_one_row(self, v13_conn):
        _migrate(v13_conn, 13, SCHEMA_VERSION)
        reflect_rows = v13_conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE origin='reflect'"
        ).fetchone()[0]
        assert reflect_rows == 1

    def test_non_reflect_row_untouched(self, v13_conn):
        _migrate(v13_conn, 13, SCHEMA_VERSION)
        dream_rows = v13_conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE origin='dream'"
        ).fetchone()[0]
        assert dream_rows == 1

    def test_partial_unique_index_created_and_enforced(self, v13_conn):
        _migrate(v13_conn, 13, SCHEMA_VERSION)
        idx = v13_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_kc_reflect_dedup'"
        ).fetchone()
        assert idx is not None
        # Enforced: a fresh duplicate reflect insert now raises (the ingest path uses
        # ON CONFLICT DO NOTHING; a plain INSERT surfaces the constraint).
        with pytest.raises(sqlite3.IntegrityError):
            v13_conn.execute(
                "INSERT INTO knowledge_candidates "
                "(scope, type, content, source_session_id, model_used, prompt_hash, origin) "
                "VALUES ('knowledge','decision','dup content','s1','m','hh','reflect')"
            )

    def test_schema_version_bumped_to_current(self, v13_conn):
        _migrate(v13_conn, 13, SCHEMA_VERSION)
        max_v = v13_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_v == SCHEMA_VERSION

    def test_migration_idempotent_on_replay(self, v13_conn):
        _migrate(v13_conn, 13, SCHEMA_VERSION)
        _migrate(v13_conn, SCHEMA_VERSION, SCHEMA_VERSION)  # no-op, must not raise
        reflect_rows = v13_conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE origin='reflect'"
        ).fetchone()[0]
        assert reflect_rows == 1


# ---------------------------------------------------------------------------
# v13 -> v14: pre-origin schema path (backfill branch)
#
# The existing v13 fixture pre-declares the `origin` column, so
# _add_column_if_missing is always a no-op under that fixture — the backfill
# branch is never exercised. This class uses a genuinely pre-origin schema
# (knowledge_candidates WITHOUT `origin`) to cover that path.
# ---------------------------------------------------------------------------

_SCHEMA_V13_KC_NO_ORIGIN = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT,
    model_used TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    promoted_to_id INTEGER,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL DEFAULT 'add',
    target_ids TEXT,
    faithfulness_ok INTEGER,
    faithfulness_warning TEXT
);
"""


class TestV13ToCurrentMigrationPreOriginSchema:
    """v13 → SCHEMA_VERSION on a DB whose knowledge_candidates has no `origin` column.

    This exercises the _add_column_if_missing backfill path that is always a
    no-op under the standard v13 fixture (which pre-declares `origin`).
    """

    @pytest.fixture
    def v13_no_origin_conn(self, tmp_path):
        db = tmp_path / "v13_no_origin.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_V13_KC_NO_ORIGIN)
        conn.execute("INSERT INTO schema_version (version) VALUES (13)")
        conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
        # Two IDENTICAL reflect candidates — duplicates a pre-#236 re-ingest produced.
        # At this point there is no `origin` column; the migration must add it and
        # backfill before running the dedup and creating the UNIQUE index.
        for _ in range(2):
            conn.execute(
                "INSERT INTO knowledge_candidates "
                "(scope, type, content, source_session_id, model_used, prompt_hash) "
                "VALUES ('knowledge','decision','dup content','s1','m','hh')"
            )
        conn.commit()
        return conn

    def test_origin_column_added_and_backfilled(self, v13_no_origin_conn):
        """Migration adds `origin` to a pre-origin schema and backfills it to 'reflect'."""
        _migrate(v13_no_origin_conn, 13, SCHEMA_VERSION)
        cols = {row[1] for row in v13_no_origin_conn.execute(
            "PRAGMA table_info(knowledge_candidates)"
        )}
        assert "origin" in cols
        # All rows were pre-origin reflect rows → backfilled to 'reflect'
        backfilled = v13_no_origin_conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE origin = 'reflect'"
        ).fetchone()[0]
        assert backfilled >= 1  # at least the surviving dedup row

    def test_dedups_keep_min_id(self, v13_no_origin_conn):
        """After migration exactly one reflect row survives — MIN(id) wins."""
        _migrate(v13_no_origin_conn, 13, SCHEMA_VERSION)
        rows = v13_no_origin_conn.execute(
            "SELECT id FROM knowledge_candidates WHERE origin = 'reflect'"
        ).fetchall()
        assert len(rows) == 1
        # The surviving row has id=1 (MIN of the two inserts)
        assert rows[0]["id"] == 1

    def test_partial_unique_index_created(self, v13_no_origin_conn):
        """The partial UNIQUE index is created even on a pre-origin schema."""
        _migrate(v13_no_origin_conn, 13, SCHEMA_VERSION)
        idx = v13_no_origin_conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_kc_reflect_dedup'"
        ).fetchone()
        assert idx is not None

    def test_schema_version_bumped(self, v13_no_origin_conn):
        """Version bumps to SCHEMA_VERSION on the pre-origin path."""
        _migrate(v13_no_origin_conn, 13, SCHEMA_VERSION)
        max_v = v13_no_origin_conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_v == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# v15 step: reminders table (techne#207)
#
# The fresh-init path (init_db / SCHEMA_SQL) already covers the reminders table
# in test_reminders.py. This class covers the MIGRATION path specifically:
# a pre-v15 db must gain the table and its covering index via _migrate.
# ---------------------------------------------------------------------------

_SCHEMA_V14_MINIMAL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    source_session_id TEXT NOT NULL,
    source_thread_id TEXT,
    model_used TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    promoted_to_id INTEGER,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL DEFAULT 'add',
    target_ids TEXT,
    origin TEXT NOT NULL DEFAULT 'reflect',
    faithfulness_ok INTEGER,
    faithfulness_warning TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kc_reflect_dedup
    ON knowledge_candidates(prompt_hash, content) WHERE origin = 'reflect';
"""


class TestV15StepReminders:
    """v15 migration step creates the reminders table and its covering index (techne#207).

    Tests here verify the MIGRATION path. The fresh-db path is covered by
    tests/test_reminders.py which builds via init_db / SCHEMA_SQL.
    """

    @pytest.fixture
    def v14_conn(self, tmp_path):
        db = tmp_path / "v14.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_V14_MINIMAL)
        conn.execute("INSERT INTO schema_version (version) VALUES (14)")
        conn.commit()
        return conn

    def test_reminders_table_created_by_migration(self, v14_conn):
        """Migrating from v14 to SCHEMA_VERSION creates the reminders table."""
        tables_before = {
            r[0]
            for r in v14_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "reminders" not in tables_before

        _migrate(v14_conn, 14, SCHEMA_VERSION)

        tables_after = {
            r[0]
            for r in v14_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "reminders" in tables_after, "reminders table not created by v15 migration step"

    def test_reminders_index_created_by_migration(self, v14_conn):
        """The covering index idx_reminders_status_due is created alongside the table."""
        _migrate(v14_conn, 14, SCHEMA_VERSION)
        idx = v14_conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND name='idx_reminders_status_due'"
        ).fetchone()
        assert idx is not None, "idx_reminders_status_due not created by v15 migration step"

    def test_reminders_table_accepts_insert_and_query(self, v14_conn):
        """The migrated reminders table has the expected columns and accepts rows."""
        _migrate(v14_conn, 14, SCHEMA_VERSION)
        v14_conn.execute(
            "INSERT INTO reminders (note, due_at, status) VALUES (?, ?, 'active')",
            ("migration-probe reminder", "2026-12-01 09:00:00"),
        )
        v14_conn.commit()
        row = v14_conn.execute(
            "SELECT note, status, last_surfaced_at FROM reminders"
        ).fetchone()
        assert row["note"] == "migration-probe reminder"
        assert row["status"] == "active"
        assert row["last_surfaced_at"] is None

    def test_schema_version_bumped_to_current(self, v14_conn):
        """Version is bumped to SCHEMA_VERSION after the v15 step."""
        _migrate(v14_conn, 14, SCHEMA_VERSION)
        max_v = v14_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_v == SCHEMA_VERSION


class TestV16ToV17ThreadBoundariesDedup:
    """v16 -> v17: collapse duplicate thread_boundaries rows (run_session_end
    re-inserted a session's full boundary set every turn before the delete-before-
    insert fix, with no dedup guard) and enforce one-row-per-(session, pair) so the
    class can never silently recur.

    This is a *temporal* property — the defect only appears after the same session
    is recomputed N times, which a single-call unit test never exercises. That is
    precisely why it escaped review and tests originally; this class pins it down.
    """

    @pytest.fixture
    def v16_conn(self):
        """A pre-v17 DB: old thread_boundaries index + 3 distinct boundary triples
        each duplicated across 5 simulated turns (15 rows), later turns scored higher."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL,
                                         applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO schema_version (version) VALUES (16);
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            INSERT INTO sessions (id) VALUES ('s1');
            CREATE TABLE thread_boundaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                obs_index_a INTEGER NOT NULL, obs_index_b INTEGER NOT NULL,
                gap_seconds REAL NOT NULL, family_a TEXT NOT NULL, family_b TEXT NOT NULL,
                signals_json TEXT NOT NULL, total_score REAL NOT NULL,
                is_boundary INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX idx_thread_boundaries_session
                ON thread_boundaries(session_id, obs_index_a);
            """
        )
        for turn in range(5):  # 5 recomputes of the same session
            for a in range(3):
                conn.execute(
                    "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
                    "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
                    "VALUES ('s1', ?, ?, 60.0, 'fs', 'fs', '{}', ?, 0)",
                    (a, a + 1, float(turn)),
                )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM thread_boundaries").fetchone()[0] == 15
        return conn

    def test_collapses_to_distinct_triples(self, v16_conn):
        _migrate(v16_conn, 16, 17)
        assert v16_conn.execute("SELECT COUNT(*) FROM thread_boundaries").fetchone()[0] == 3

    def test_keeps_most_recent_row_per_triple(self, v16_conn):
        """MAX(id) is kept, so the surviving rows carry the latest recompute's score."""
        _migrate(v16_conn, 16, 17)
        scores = {r[0] for r in v16_conn.execute("SELECT total_score FROM thread_boundaries")}
        assert scores == {4.0}  # last turn

    def test_swaps_to_unique_triple_index(self, v16_conn):
        _migrate(v16_conn, 16, 17)
        idx = {r["name"]: r["unique"] for r in v16_conn.execute(
            "SELECT name, \"unique\" FROM pragma_index_list('thread_boundaries')"
        )}
        assert idx.get("idx_thread_boundaries_triple") == 1
        assert "idx_thread_boundaries_session" not in idx

    def test_unique_constraint_blocks_future_duplicate(self, v16_conn):
        """The whole point: a future non-atomic re-insert fails loudly, not silently."""
        _migrate(v16_conn, 16, 17)
        with pytest.raises(sqlite3.IntegrityError):
            v16_conn.execute(
                "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
                "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
                "VALUES ('s1', 0, 1, 60.0, 'fs', 'fs', '{}', 9.0, 0)"
            )

    def test_version_bumped(self, v16_conn):
        _migrate(v16_conn, 16, 17)
        assert v16_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 17


# ---------------------------------------------------------------------------
# v20 -> v21: namespace coord read receipts (techne#446)
# ---------------------------------------------------------------------------

V20_COORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS coord_reads (
    message_id INTEGER NOT NULL,
    reader_session TEXT NOT NULL,
    read_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, reader_session)
);
"""

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def v20_coord_conn(tmp_path):
    """A pre-#446 store: receipts keyed on bare session UUIDs."""
    db_path = tmp_path / "v20.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(V20_COORD_SCHEMA)
    conn.executemany(
        "INSERT INTO coord_reads (message_id, reader_session, read_at) VALUES (?, ?, ?)",
        [(1, _UUID_A, "2026-08-01 00:00:00"), (2, _UUID_A, "2026-08-01 00:00:00"),
         (1, _UUID_B, "2026-08-02 00:00:00")],
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (20)")
    conn.commit()
    yield conn
    conn.close()


class TestV21CoordReceiptNamespacing:
    """The migration TAGS existing rows; it deliberately does not rewrite them.

    The UUID->name map lives in Claude Code's per-session sidecars, and the
    sidecars for dead sessions are gone — so inventing a mapping would fabricate
    the very "who read what" claim the receipt exists to record.
    """

    def test_bare_uuids_gain_the_uuid_namespace(self, v20_coord_conn):
        _migrate(v20_coord_conn, 20, 21)
        keys = sorted(
            r[0] for r in v20_coord_conn.execute("SELECT DISTINCT reader_session FROM coord_reads")
        )
        assert keys == [f"uuid:{_UUID_A}", f"uuid:{_UUID_B}"]

    def test_no_receipt_is_lost(self, v20_coord_conn):
        before = v20_coord_conn.execute("SELECT COUNT(*) FROM coord_reads").fetchone()[0]
        _migrate(v20_coord_conn, 20, 21)
        after = v20_coord_conn.execute("SELECT COUNT(*) FROM coord_reads").fetchone()[0]
        assert before == after == 3

    def test_rerunning_does_not_double_prefix(self, v20_coord_conn):
        """Idempotent by construction — only bare values match. A double-prefixed
        `uuid:uuid:...` would silently orphan the receipt it was meant to preserve."""
        _migrate(v20_coord_conn, 20, 21)
        _migrate(v20_coord_conn, 20, 21)
        keys = [r[0] for r in v20_coord_conn.execute("SELECT reader_session FROM coord_reads")]
        assert all(k.count("uuid:") == 1 for k in keys), keys

    def test_already_namespaced_rows_are_left_alone(self, v20_coord_conn):
        v20_coord_conn.execute(
            "INSERT INTO coord_reads (message_id, reader_session, read_at) VALUES (?, ?, ?)",
            (3, "name:poe_1", "2026-08-03 00:00:00"),
        )
        _migrate(v20_coord_conn, 20, 21)
        row = v20_coord_conn.execute(
            "SELECT reader_session FROM coord_reads WHERE message_id = 3"
        ).fetchone()
        assert row[0] == "name:poe_1"

    def test_version_bumped(self, v20_coord_conn):
        _migrate(v20_coord_conn, 20, 21)
        v = v20_coord_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert v == 21


class TestBackupGrowthBounds:
    """#574: a backup that fills the disk stops being a safety net.

    Measured before this landed: one v18->v19 transition produced TWELVE
    full-size copies (11 failed attempts, each backing up first), and nothing
    ever pruned any of them — 1.4 GB accumulated, oldest six weeks.
    """

    def _db(self, tmp_path):
        """A small real SQLite file, since backup_db refuses absent/empty ones."""
        p = tmp_path / "techne.db"
        c = sqlite3.connect(str(p))
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        return p, c

    def test_retry_of_the_same_transition_reuses_the_existing_backup(self, tmp_path):
        """The 12x case. Retries must not each copy the whole database.

        Asserting on the PATH is not enough and this test used to do exactly
        that: the backup name is second-resolution, so a re-copy inside the same
        second lands on the same path and looks identical to a reuse. Mutation
        proved it — deleting the reuse branch left this green. The observable
        difference is whether bytes were written, so pin the MTIME.
        """
        from techne.core.migrator import backup_db

        p, c = self._db(tmp_path)
        first = backup_db(c, p, 18, 19)
        assert first is not None
        # Age it slightly — still well inside the reuse window — so that a
        # re-copy would visibly advance the mtime.
        aged = first.stat().st_mtime - 30
        os.utime(first, (aged, aged))

        again = backup_db(c, p, 18, 19)
        assert again == first
        assert again.stat().st_mtime == aged, "reuse must not rewrite the file"
        assert len(list((tmp_path / "backups").glob("*.db.bak"))) == 1

    def test_reuse_is_scoped_to_the_same_transition(self, tmp_path):
        """A DIFFERENT transition is a different rollback point and needs its own."""
        from techne.core.migrator import backup_db

        p, c = self._db(tmp_path)
        a = backup_db(c, p, 18, 19)
        b = backup_db(c, p, 19, 20)

        assert a != b
        assert len(list((tmp_path / "backups").glob("*.db.bak"))) == 2

    def test_a_stale_backup_is_not_reused(self, tmp_path, monkeypatch):
        """Beyond the window the live DB has moved on — restoring the old snapshot
        would discard real work, so take a fresh copy."""
        import techne.core.migrator as m

        p, c = self._db(tmp_path)
        first = m.backup_db(c, p, 18, 19)
        assert first is not None
        # Age it past the window rather than sleeping.
        old = first.stat().st_mtime - (m.BACKUP_REUSE_WINDOW_S + 60)
        os.utime(first, (old, old))

        again = m.backup_db(c, p, 18, 19)
        assert again is not None
        # NOT `again != first`: the filename is second-resolution, so a re-copy
        # inside the same second reuses the same PATH. What must be true is that
        # a fresh snapshot was actually written — i.e. the aged mtime is gone.
        assert again.stat().st_mtime > old, "a stale backup must be re-copied, not reused"

    def test_old_backups_are_pruned_to_the_keep_limit(self, tmp_path):
        """Nothing pruned these before; 1.4 GB had accumulated, oldest six weeks.

        Exercises _prune_backups directly with explicit mtimes: driving it through
        backup_db makes the assertion depend on same-second filename resolution
        and on when utime lands relative to the in-call prune.
        """
        from techne.core.migrator import _prune_backups

        d = tmp_path / "backups"
        d.mkdir()
        made = []
        for i, (frm, to) in enumerate(((1, 2), (2, 3), (3, 4), (4, 5))):
            f = d / f"techne-v{frm}-to-v{to}-2026010{i + 1}-000000.db.bak"
            f.write_text("x")
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))  # strictly ascending
            made.append(f)

        newest = made[-1]
        deleted = _prune_backups(d, "techne", keep=2, protect=newest)

        remaining = sorted(q.name for q in d.glob("*.db.bak"))
        assert len(remaining) == 2, remaining
        assert newest.name in remaining, "the newest must survive"
        assert made[0].name not in remaining, "the oldest must be pruned"
        assert {q.name for q in deleted} == {made[0].name, made[1].name}

    def test_backup_db_actually_invokes_pruning(self, tmp_path, monkeypatch):
        """Integration cover for the call site.

        The keep-limit test exercises _prune_backups directly, so it stays green
        even if backup_db stops calling it — mutation proved that too. This pins
        the wiring.
        """
        import techne.core.migrator as m

        monkeypatch.setattr(m, "BACKUP_KEEP", 1)
        monkeypatch.setattr(m, "BACKUP_REUSE_WINDOW_S", 0)  # force a fresh copy each time
        p, c = self._db(tmp_path)

        for i, (frm, to) in enumerate(((1, 2), (2, 3), (3, 4))):
            b = m.backup_db(c, p, frm, to)
            assert b is not None
            # Push each finished backup deterministically into the past so the
            # next call's prune sees an unambiguous ordering rather than a tie
            # between files written in the same second.
            past = 1_700_000_000 + i
            os.utime(b, (past, past))

        remaining = list((tmp_path / "backups").glob("*.db.bak"))
        assert len(remaining) == 1, [q.name for q in remaining]

    def test_the_backup_just_taken_is_never_pruned(self, tmp_path, monkeypatch):
        """Deleting the file the caller is about to rely on would be the failure
        this whole mechanism exists to prevent."""
        import techne.core.migrator as m

        monkeypatch.setattr(m, "BACKUP_KEEP", 0)
        monkeypatch.setattr(m, "BACKUP_REUSE_WINDOW_S", 0)
        p, c = self._db(tmp_path)

        b = m.backup_db(c, p, 1, 2)
        assert b is not None and b.exists(), "protect= must survive keep=0"

    def test_pruning_only_touches_this_database(self, tmp_path, monkeypatch):
        """backups/ is shared across stores (techne, archivist, codex, ...)."""
        import techne.core.migrator as m

        monkeypatch.setattr(m, "BACKUP_KEEP", 1)
        monkeypatch.setattr(m, "BACKUP_REUSE_WINDOW_S", 0)
        p, c = self._db(tmp_path)
        (tmp_path / "backups").mkdir(exist_ok=True)
        other = tmp_path / "backups" / "archivist-v1-to-v2-20260101-000000.db.bak"
        other.write_text("not mine")

        m.backup_db(c, p, 1, 2)
        m.backup_db(c, p, 2, 3)

        assert other.exists(), "a sibling store's backups must not be pruned"
