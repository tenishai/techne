"""Tests for the audit content-hash layer (techne#270).

Covers:
  (a) Observation with a credential stores non-null content_hash AND redacted
      content, with content != raw.
  (b) Observation with no sensitive content stores content_hash=NULL.
  (c) verify_observation_hash returns True for the exact raw plaintext, False
      for a wrong candidate, and False when no hash is stored.
  (d) Migration v15→v16 adds content_hash column to an existing DB.

Uses real redaction patterns from techne/core/redaction.py so the test is
honest about what constitutes a "credential" (no hand-rolled mocking).
"""

import hashlib
import sqlite3

import pytest

from techne.core.audit import verify_observation_hash
from techne.core.database import _migrate, connect, init_db
from techne.core.state import add_observation, start_session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A real Anthropic API key shape that redact_credentials() catches as sk-ant.
# Must be ≥20 chars after the prefix per the redaction pattern.
_RAW_WITH_CRED = "tool output: key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCD"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit_test.db"
    conn = init_db(path)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# (a) Credential observation: non-null hash, redacted content, content != raw
# ---------------------------------------------------------------------------


class TestObservationWithCredential:
    def test_content_hash_is_set(self, db_path):
        """An observation containing a credential gets a non-null content_hash."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", _RAW_WITH_CRED)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content, content_hash FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["content_hash"] is not None, "content_hash should be set when redaction fires"

    def test_content_is_redacted(self, db_path):
        """The stored content must not contain the raw credential."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", _RAW_WITH_CRED)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert "sk-ant-api03" not in row["content"], "raw credential must not appear in stored content"
        assert "<redacted:sk-ant>" in row["content"], "redaction placeholder must be present"

    def test_stored_content_differs_from_raw(self, db_path):
        """The stored content must differ from the original raw payload."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", _RAW_WITH_CRED)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row["content"] != _RAW_WITH_CRED

    def test_stored_hash_matches_raw_payload(self, db_path):
        """The stored content_hash must be SHA-256 of the raw (pre-redaction) payload."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", _RAW_WITH_CRED)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        expected = hashlib.sha256(_RAW_WITH_CRED.encode()).hexdigest()
        assert row["content_hash"] == expected


# ---------------------------------------------------------------------------
# (b) Clean observation: content_hash stays NULL
# ---------------------------------------------------------------------------


class TestObservationWithoutCredential:
    def test_content_hash_is_null(self, db_path):
        """An observation with no credential stores content_hash=NULL."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Read", "ordinary tool output without any secrets")

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content, content_hash FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["content_hash"] is None, (
            "content_hash should be NULL when no redaction occurred"
        )

    def test_content_unchanged(self, db_path):
        """Clean content is stored verbatim."""
        raw = "ordinary tool output without any secrets"
        sid = start_session(db_path)
        add_observation(db_path, sid, "Read", raw)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row["content"] == raw


# ---------------------------------------------------------------------------
# (c) verify_observation_hash
# ---------------------------------------------------------------------------


class TestVerifyObservationHash:
    def _insert_observation(self, db_path, raw_content: str) -> int:
        """Helper: add an observation and return its DB row id."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", raw_content)
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT id FROM observations WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        return row["id"]

    def test_returns_true_for_exact_raw_plaintext(self, db_path):
        """verify_observation_hash returns True when given the exact raw payload."""
        obs_id = self._insert_observation(db_path, _RAW_WITH_CRED)
        assert verify_observation_hash(db_path, obs_id, _RAW_WITH_CRED) is True

    def test_returns_false_for_wrong_candidate(self, db_path):
        """verify_observation_hash returns False for a wrong candidate."""
        obs_id = self._insert_observation(db_path, _RAW_WITH_CRED)
        assert verify_observation_hash(db_path, obs_id, "wrong plaintext") is False

    def test_returns_false_for_redacted_content(self, db_path):
        """Passing the stored (redacted) content as the candidate should not match."""
        obs_id = self._insert_observation(db_path, _RAW_WITH_CRED)
        # Retrieve the stored redacted content and use it as the candidate
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content FROM observations WHERE id = ?", (obs_id,)
            ).fetchone()
        finally:
            conn.close()
        # The redacted content is NOT the raw — hash must not match
        assert verify_observation_hash(db_path, obs_id, row["content"]) is False

    def test_returns_false_when_no_hash_stored(self, db_path):
        """Returns False for observations where content_hash is NULL (no redaction)."""
        obs_id = self._insert_observation(db_path, "no credentials here at all")
        assert verify_observation_hash(db_path, obs_id, "no credentials here at all") is False

    def test_returns_false_for_nonexistent_obs_id(self, db_path):
        """Returns False when the observation row doesn't exist."""
        assert verify_observation_hash(db_path, 999999, _RAW_WITH_CRED) is False


# ---------------------------------------------------------------------------
# (d) Migration v15→v16: adds content_hash column to an existing DB
# ---------------------------------------------------------------------------

# Minimal v15 schema: just enough tables for the migration to run safely.
# Mirrors the real v15 state: observations table present without content_hash.
_V15_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_at TIMESTAMP,
    tool_name TEXT,
    content TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot',
    tags TEXT,
    kind TEXT
);
"""


@pytest.fixture
def v15_conn(tmp_path):
    """Open connection to a v15-shaped database (no content_hash column)."""
    db_path = tmp_path / "v15_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_V15_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (15)")
    conn.commit()
    return conn


class TestMigrationV15ToV16:
    def test_column_missing_before_migration(self, v15_conn):
        """Sanity: content_hash column must not exist in the v15 schema."""
        cols = {row[1] for row in v15_conn.execute("PRAGMA table_info(observations)")}
        assert "content_hash" not in cols, "pre-migration DB should not have content_hash"

    def test_migration_adds_content_hash_column(self, v15_conn):
        """After v15→v16 migration, observations has a content_hash column."""
        _migrate(v15_conn, 15, 16)
        cols = {row[1] for row in v15_conn.execute("PRAGMA table_info(observations)")}
        assert "content_hash" in cols, "migration v15→v16 must add content_hash column"

    def test_schema_version_bumped_to_16(self, v15_conn):
        """schema_version contains version=16 after migration."""
        _migrate(v15_conn, 15, 16)
        max_version = v15_conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_version == 16

    def test_existing_rows_get_null_content_hash(self, v15_conn):
        """Existing observation rows get content_hash=NULL after migration."""
        # Insert a pre-migration row
        v15_conn.execute(
            "INSERT INTO sessions (id) VALUES ('sess-001')"
        )
        v15_conn.execute(
            "INSERT INTO observations (session_id, content, tier) VALUES ('sess-001', 'old content', 'hot')"
        )
        v15_conn.commit()

        _migrate(v15_conn, 15, 16)

        row = v15_conn.execute(
            "SELECT content_hash FROM observations WHERE session_id = 'sess-001'"
        ).fetchone()
        assert row is not None
        assert row["content_hash"] is None, "pre-migration rows must have content_hash=NULL"

    def test_migration_is_idempotent(self, v15_conn):
        """Running the v16 migration step twice does not raise (idempotent via _add_column_if_missing)."""
        _migrate(v15_conn, 15, 16)
        # Re-running should be a no-op, not raise "duplicate column name"
        # We can't call _migrate(15, 16) again (version guard would skip it),
        # so call the helper directly to simulate the idempotency contract.
        from techne.core.database import _add_column_if_missing
        _add_column_if_missing(v15_conn, "observations", "content_hash", "TEXT")  # must not raise
