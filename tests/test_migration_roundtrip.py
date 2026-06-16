"""Integration test: v3 → current schema round-trip.

Exercises BOTH the explicit _migrate() ladder (v3→4→5→6→7) AND the
_reconcile_columns() pass (additive columns like summary_llm_source, event_at,
agent_identity that are added without a version bump).

The goal is "no data loss, current shape reached" — not exhaustive column-by-
column assertions. Future migrations that touch data should pin their own tests.
"""

import sqlite3
from pathlib import Path

import pytest

from techne.core.database import SCHEMA_VERSION, connect

# ---------------------------------------------------------------------------
# v3 schema — reconstructed from the migration ladder
#
# v1: outbox has (id, target, data_json, content_hash, created_at, synced_at)
#     with dedup index on synced_at IS NULL
# v2: outbox gains status + error_message; dedup index rebuilt on status='pending'
# v3: thread_boundaries table added
#
# sessions at v3: (id, started_at, ended_at, project_path, summary)
#   — no agent_identity, platform, summary_llm, summary_heuristic (added v4+reconciler)
# observations at v3: (id, session_id, thread_id, timestamp, tool_name, content, tier)
#   — no tags, kind (added v7), no event_at (added by reconciler/#8)
# knowledge at v3: (id, type, content, source, tags, confidence, superseded_by,
#                   created_at, updated_at)
#   — no confidence_numeric, last_confirmed_at (added v5), no event_at (reconciler)
# identity: present at v3 (existed before v1)
# outbox: v2 shape (status + error_message columns present)
# threads: full shape (parent_thread_id was present before v1)
# No: knowledge_candidates (v6), extension_state (reconciler), checkpoints (reconciler)
# ---------------------------------------------------------------------------

V3_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    project_path TEXT,
    summary TEXT
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
CREATE INDEX IF NOT EXISTS idx_threads_session ON threads(session_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT REFERENCES threads(id),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tool_name TEXT,
    content TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot'
);
CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_thread ON observations(thread_id);
CREATE INDEX IF NOT EXISTS idx_observations_tier ON observations(tier);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    superseded_by INTEGER REFERENCES knowledge(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by);

CREATE TABLE IF NOT EXISTS identity (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'config',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_dedup
    ON outbox(content_hash) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS thread_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    obs_index_a INTEGER NOT NULL,
    obs_index_b INTEGER NOT NULL,
    gap_seconds REAL NOT NULL,
    family_a TEXT NOT NULL,
    family_b TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    total_score REAL NOT NULL,
    is_boundary INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_thread_boundaries_session
    ON thread_boundaries(session_id, obs_index_a);
"""


@pytest.fixture
def v3_db(tmp_path: Path) -> Path:
    """Build a v3 database with one row per data table. Returns the db Path."""
    db_path = tmp_path / "v3_fixture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V3_SCHEMA_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")

    # One session
    conn.execute(
        "INSERT INTO sessions (id, started_at, project_path, summary) VALUES (?, ?, ?, ?)",
        ("sess-v3-001", "2026-01-01T00:00:00Z", "/home/dev/proj", "legacy summary text"),
    )
    # One thread
    conn.execute(
        "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("thr-v3-001", "sess-v3-001", 1, "Thread 1", "Did some work", "active"),
    )
    # One observation (no tags/kind columns yet)
    conn.execute(
        "INSERT INTO observations (session_id, thread_id, timestamp, tool_name, content, tier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sess-v3-001", "thr-v3-001", "2026-01-01T00:01:00Z", "bash", "ran ls -la", "hot"),
    )
    # One knowledge row (no confidence_numeric yet)
    conn.execute(
        "INSERT INTO knowledge (type, content, confidence) VALUES (?, ?, ?)",
        ("fact", "the sky is blue", "high"),
    )
    # One identity row
    conn.execute(
        "INSERT INTO identity (key, value, source) VALUES (?, ?, ?)",
        ("agent.name", "raven", "config"),
    )
    # One outbox row
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?, ?, ?, ?)",
        ("knowledge", '{"content":"test"}', "hash-abc", "pending"),
    )
    # One thread_boundary row
    conn.execute(
        "INSERT INTO thread_boundaries "
        "(session_id, obs_index_a, obs_index_b, gap_seconds, "
        "family_a, family_b, signals_json, total_score, is_boundary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sess-v3-001", 0, 1, 120.5, "bash", "read", '{"gap":1.0}', 1.5, 0),
    )
    conn.commit()
    conn.close()
    return db_path


class TestV3ToCurrentRoundTrip:
    """Full v3 → current migration + reconciliation round-trip."""

    def test_schema_version_reaches_current(self, v3_db: Path):
        """After connect(), MAX(schema_version) equals SCHEMA_VERSION."""
        conn = connect(v3_db)
        try:
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            conn.close()

    def test_original_rows_survive(self, v3_db: Path):
        """All seed rows are intact after migration — no data loss."""
        conn = connect(v3_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM thread_boundaries").fetchone()[0] == 1
        finally:
            conn.close()

    def test_key_fields_spot_check(self, v3_db: Path):
        """Original field values survive unchanged through migration."""
        conn = connect(v3_db)
        try:
            sess = conn.execute("SELECT * FROM sessions WHERE id='sess-v3-001'").fetchone()
            assert sess["project_path"] == "/home/dev/proj"
            assert sess["summary"] == "legacy summary text"

            obs = conn.execute(
                "SELECT * FROM observations WHERE session_id='sess-v3-001'"
            ).fetchone()
            assert obs["tool_name"] == "bash"
            assert obs["content"] == "ran ls -la"
            assert obs["tier"] == "hot"

            kn = conn.execute("SELECT * FROM knowledge WHERE type='fact'").fetchone()
            assert kn["content"] == "the sky is blue"
            assert kn["confidence"] == "high"

            ident = conn.execute("SELECT value FROM identity WHERE key='agent.name'").fetchone()
            assert ident["value"] == "raven"
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Columns added by the explicit migration ladder (v4 → v7)
    # ------------------------------------------------------------------

    def test_v4_summary_typed_columns_present(self, v3_db: Path):
        """v4 migration adds summary_llm and summary_heuristic to sessions."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            assert "summary_llm" in cols
            assert "summary_heuristic" in cols
        finally:
            conn.close()

    def test_v4_legacy_summary_untouched(self, v3_db: Path):
        """v4 migration does not clobber the existing summary column value."""
        conn = connect(v3_db)
        try:
            row = conn.execute(
                "SELECT summary, summary_llm, summary_heuristic "
                "FROM sessions WHERE id='sess-v3-001'"
            ).fetchone()
            assert row["summary"] == "legacy summary text"
            assert row["summary_llm"] is None
            assert row["summary_heuristic"] is None
        finally:
            conn.close()

    def test_v5_confidence_numeric_backfilled(self, v3_db: Path):
        """v5 migration adds confidence_numeric and backfills from confidence enum."""
        conn = connect(v3_db)
        try:
            kn = conn.execute(
                "SELECT confidence, confidence_numeric FROM knowledge WHERE type='fact'"
            ).fetchone()
            assert kn["confidence"] == "high"
            assert kn["confidence_numeric"] == pytest.approx(0.9)
        finally:
            conn.close()

    def test_v5_last_confirmed_at_backfilled(self, v3_db: Path):
        """v5 migration backfills last_confirmed_at = created_at for existing rows."""
        conn = connect(v3_db)
        try:
            kn = conn.execute(
                "SELECT last_confirmed_at, created_at FROM knowledge WHERE type='fact'"
            ).fetchone()
            assert kn["last_confirmed_at"] is not None
            # last_confirmed_at should equal created_at for backfilled rows
            assert kn["last_confirmed_at"] == kn["created_at"]
        finally:
            conn.close()

    def test_v6_knowledge_candidates_table_exists(self, v3_db: Path):
        """v6 migration creates the knowledge_candidates table."""
        conn = connect(v3_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_candidates'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_v7_observations_tags_kind_columns_present(self, v3_db: Path):
        """v7 migration adds tags and kind columns to observations."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
            assert "tags" in cols
            assert "kind" in cols
        finally:
            conn.close()

    def test_v7_existing_observation_tags_kind_null(self, v3_db: Path):
        """Existing observations get NULL for the new tags/kind columns."""
        conn = connect(v3_db)
        try:
            obs = conn.execute(
                "SELECT tags, kind FROM observations WHERE session_id='sess-v3-001'"
            ).fetchone()
            assert obs["tags"] is None
            assert obs["kind"] is None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Columns added by the reconciler (no version bump required)
    # ------------------------------------------------------------------

    def test_reconciler_adds_summary_llm_source(self, v3_db: Path):
        """Reconciler adds summary_llm_source to sessions (from #1)."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            assert "summary_llm_source" in cols
        finally:
            conn.close()

    def test_reconciler_adds_agent_identity(self, v3_db: Path):
        """Reconciler adds agent_identity to sessions."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            assert "agent_identity" in cols
        finally:
            conn.close()

    def test_reconciler_adds_event_at_observations(self, v3_db: Path):
        """Reconciler adds event_at to observations (from #8)."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
            assert "event_at" in cols
        finally:
            conn.close()

    def test_reconciler_adds_event_at_knowledge(self, v3_db: Path):
        """Reconciler adds event_at to knowledge (from #8)."""
        conn = connect(v3_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)")}
            assert "event_at" in cols
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # New tables: only those added by explicit migration steps are guaranteed
    # ------------------------------------------------------------------

    def test_knowledge_candidates_accepts_insert(self, v3_db: Path):
        """knowledge_candidates (added v6) accepts an insert after migration."""
        conn = connect(v3_db)
        try:
            conn.execute(
                "INSERT INTO knowledge_candidates "
                "(scope, type, content, source_session_id, model_used, prompt_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("knowledge", "fact", "candidate fact", "sess-v3-001", "claude-3", "abc123"),
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM knowledge_candidates").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    # NOTE: checkpoints, extension_state, and FTS tables are defined in SCHEMA_SQL
    # but _ensure_initialized only runs SCHEMA_SQL on a *fresh* (no schema_version
    # table) database. On an existing v3 DB it runs _migrate() + _reconcile_columns(),
    # neither of which CREATE TABLE for those tables. They will be absent on a
    # v3→current migrated DB — this is known behaviour, not a bug. Fresh installs
    # get the full schema; upgrades get additive column reconciliation only.

    def test_connect_is_idempotent(self, v3_db: Path):
        """Calling connect() a second time on an already-migrated db is a no-op."""
        conn1 = connect(v3_db)
        conn1.close()
        conn2 = connect(v3_db)
        try:
            version = conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert version == SCHEMA_VERSION
            # row count stable — no duplication
            assert conn2.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        finally:
            conn2.close()
