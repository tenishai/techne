"""Schema drift: v18 migration heal + reconcile drift detector (techne#360).

Covers the two instances the review found — #355 (observations_fts triggers
stuck content-only, tags/kind never indexed) and #353 (zombie index) — plus the
general drift *detector* that makes future redefinition-drift visible instead of
silently never-applied.
"""

import pytest

from techne.core.database import (
    SCHEMA_VERSION,
    _migrate,
    _reconcile_indexes,
    _reconcile_triggers,
    connect,
    init_db,
)

# The pre-#355 drifted form: content-only, no tags/kind.
_OLD_CONTENT_ONLY_TRIGGERS = """
    DROP TRIGGER IF EXISTS observations_ai;
    DROP TRIGGER IF EXISTS observations_ad;
    DROP TRIGGER IF EXISTS observations_au;
    CREATE TRIGGER observations_ai AFTER INSERT ON observations BEGIN
        INSERT INTO observations_fts(rowid, content) VALUES (NEW.id, NEW.content);
    END;
    CREATE TRIGGER observations_ad AFTER DELETE ON observations BEGIN
        INSERT INTO observations_fts(observations_fts, rowid, content)
        VALUES ('delete', OLD.id, OLD.content);
    END;
    CREATE TRIGGER observations_au AFTER UPDATE ON observations BEGIN
        INSERT INTO observations_fts(observations_fts, rowid, content)
        VALUES ('delete', OLD.id, OLD.content);
        INSERT INTO observations_fts(rowid, content) VALUES (NEW.id, NEW.content);
    END;
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


def _rewind_to_v17(conn):
    """Simulate a real pre-v18 DB: a fresh init lands at SCHEMA_VERSION (18), so
    rewind the version stamp to 17 before exercising the v18 migration (otherwise
    the block re-inserts an existing version and trips the UNIQUE constraint —
    which real usage never hits, since _migrate is called with the live version)."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (17)")
    conn.commit()


def test_fresh_db_is_current_version(db):
    conn = connect(db)
    try:
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        # Bumped 21 -> 24. THREE steps land between: 22 is techne#514's
        # reflection_queue (PR #527), 23 is techne#515's timestamp-format backfill
        # (PR #531), 24 is techne#516's ended_at backfill. The literal is
        # deliberate: it makes a SCHEMA_VERSION bump fail here so it cannot pass
        # unnoticed. It did exactly that job on #514 — this test is one of the two
        # that caught that bump, and updating it is the acknowledgement, not a
        # formality.
        assert v == SCHEMA_VERSION == 24
        # Fresh DB already has the correct (tags/kind) triggers.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_ai'"
        ).fetchone()[0]
        assert "tags" in sql and "kind" in sql
    finally:
        conn.close()


def test_v18_heals_drifted_observations_triggers(db):
    conn = connect(db)
    try:
        _rewind_to_v17(conn)
        conn.executescript(_OLD_CONTENT_ONLY_TRIGGERS)
        conn.commit()
        before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_ai'"
        ).fetchone()[0]
        assert "tags" not in before  # drifted

        _migrate(conn, 17, 18)

        after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_ai'"
        ).fetchone()[0]
        assert "tags" in after and "kind" in after  # healed
    finally:
        conn.close()


def test_v18_makes_tags_fts_searchable(db):
    """The real #355 payoff: after the heal + rebuild, an inserted tag is
    matchable in observations_fts (it wasn't, under the content-only trigger)."""
    conn = connect(db)
    try:
        _rewind_to_v17(conn)
        conn.executescript(_OLD_CONTENT_ONLY_TRIGGERS)
        conn.commit()
        _migrate(conn, 17, 18)

        conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, tags, kind) "
            "VALUES ('s1', 'Bash', 'some body text', 'hot', 'zebratag', 'fact-shaped')"
        )
        conn.commit()
        hit = conn.execute(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH 'zebratag'"
        ).fetchone()
        assert hit is not None  # the tag is indexed → searchable
    finally:
        conn.close()


def test_v18_drops_zombie_index(db):
    conn = connect(db)
    try:
        _rewind_to_v17(conn)
        # Re-create the zombie the v17 migration was meant to remove.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_boundaries_session "
            "ON thread_boundaries(session_id, obs_index_a)"
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_thread_boundaries_session'"
        ).fetchone()

        _migrate(conn, 17, 18)

        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_thread_boundaries_session'"
        ).fetchone()
    finally:
        conn.close()


def test_reconcile_warns_on_trigger_drift(db, capsys):
    conn = connect(db)
    try:
        conn.executescript(_OLD_CONTENT_ONLY_TRIGGERS)
        conn.commit()
        capsys.readouterr()  # clear
        _reconcile_triggers(conn)
        err = capsys.readouterr().err
        assert "observations_ai" in err
        assert "DRIFTED" in err
    finally:
        conn.close()


def test_reconcile_no_warn_when_clean(db, capsys):
    """A non-drifted DB emits no drift warning — guards the normalizer against
    false positives (IF NOT EXISTS / whitespace differences must compare equal)."""
    conn = connect(db)
    try:
        capsys.readouterr()
        _reconcile_triggers(conn)
        _reconcile_indexes(conn)
        err = capsys.readouterr().err
        assert "DRIFTED" not in err
    finally:
        conn.close()
