"""Tests for bucket_hash and idempotent thread persistence (issue #16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import connect
from techne.core.thread_orchestrator import (
    Thread,
    link_threads,
    save_threads,
    thread_row_id,
)
from techne.core.threads.persistence import bucket_hash


def test_heuristic_parent_link_persists_to_real_row(tmp_path):
    """techne#165 regression: a heuristic (boundary_signal) thread's parent link must
    resolve to the parent's real `t-<hash>` id. link_threads keyed the (thread_id,
    parent) tuple by the legacy `{session}-{num}` form, so save_threads' link_map lookup
    (which uses thread_row_id) silently missed → parent_thread_id was NULL for every
    heuristic (default-detector) session."""
    db = tmp_path / "t.db"
    conn = connect(db)
    conn.execute("INSERT INTO sessions (id, project_path) VALUES ('prior', '/p')")
    conn.execute("INSERT INTO sessions (id, project_path) VALUES ('cur', '/p')")
    conn.commit()
    conn.close()

    # Parent from a PRIOR session — persisted + FTS-indexed so link_threads can find it.
    parent = Thread(thread_num=1, label="postgres migration planning",
                    summary="planning the postgres migration", observation_indices=[0],
                    boundary_signal="sig")
    save_threads([parent], "prior", db)
    parent_id = thread_row_id("prior", parent)

    # A new heuristic thread in the current session with matching content.
    child = Thread(thread_num=2, label="postgres migration planning",
                   summary="continue the postgres migration", observation_indices=[5],
                   boundary_signal="sig")
    cfg = Config()
    cfg.threads.link_threshold = 0.0  # any FTS match links — deterministic for the test

    links = link_threads([child], "cur", db, cfg)
    child_key, linked_parent = links[0]
    # The fix: the link is keyed by the hash id, NOT the legacy "{session}-{num}".
    assert child_key == thread_row_id("cur", child)
    assert child_key != "cur-2"
    assert linked_parent == parent_id

    save_threads([child], "cur", db, parent_links=links)
    row = connect(db).execute(
        "SELECT parent_thread_id FROM threads WHERE id = ?",
        (thread_row_id("cur", child),),
    ).fetchone()
    assert row[0] == parent_id  # persisted, not NULL


def test_link_threads_skips_same_session_heuristic_candidate(tmp_path):
    """techne#165 follow-up: a heuristic thread must not link to its OWN same-session
    prior row. The id is `t-<hash>` (no session prefix), so the old prefix-based self-skip
    let it self-link once the keys agreed (#172); now skipped by exact session_id match."""
    db = tmp_path / "t.db"
    conn = connect(db)
    conn.execute("INSERT INTO sessions (id, project_path) VALUES ('cur', '/p')")
    conn.commit()
    conn.close()
    # Persist a heuristic thread in 'cur' so it's in threads_fts.
    t1 = Thread(thread_num=1, label="redis caching layer",
                summary="the redis caching layer design", observation_indices=[0],
                boundary_signal="sig")
    save_threads([t1], "cur", db)
    # Re-detect the same content in the SAME session → must NOT link to its own row.
    redetected = Thread(thread_num=1, label="redis caching layer",
                        summary="the redis caching layer design", observation_indices=[0],
                        boundary_signal="sig")
    cfg = Config()
    cfg.threads.link_threshold = 0.0
    links = link_threads([redetected], "cur", db, cfg)
    assert links[0][1] is None  # no parent — same-session self-link skipped

# ---------------------------------------------------------------------------
# bucket_hash unit tests
# ---------------------------------------------------------------------------


class TestBucketHash:
    def test_deterministic(self):
        """Same inputs → same ID."""
        a = bucket_hash("sess-abc", 0, "new_session")
        b = bucket_hash("sess-abc", 0, "new_session")
        assert a == b

    def test_different_boundary_signal_different_id(self):
        a = bucket_hash("sess-abc", 0, "new_session")
        b = bucket_hash("sess-abc", 0, "long_gap:1800s")
        assert a != b

    def test_different_first_obs_different_id(self):
        a = bucket_hash("sess-abc", 0, "new_session")
        b = bucket_hash("sess-abc", 5, "new_session")
        assert a != b

    def test_different_session_different_id(self):
        a = bucket_hash("sess-abc", 0, "new_session")
        b = bucket_hash("sess-xyz", 0, "new_session")
        assert a != b

    def test_prefix_t_dash(self):
        """Output is unambiguously not a UUID: starts with 't-'."""
        h = bucket_hash("s", 0, "new_session")
        assert h.startswith("t-")

    def test_length_stable(self):
        """Fixed length: 't-' + 16 hex chars = 18 chars."""
        h = bucket_hash("session-id", 42, "repo_change")
        assert len(h) == 18


# ---------------------------------------------------------------------------
# Idempotent save_threads (INSERT OR IGNORE path)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path: Path):
    c = connect(tmp_path / "techne.db")
    c.execute("INSERT INTO sessions (id, project_path) VALUES (?, ?)", ("sess-1", "/tmp"))
    c.commit()
    return c, tmp_path / "techne.db"


def _make_thread(
    num: int,
    label: str = "work",
    boundary_signal: str | None = "new_session",
    obs_indices: list[int] | None = None,
) -> Thread:
    return Thread(
        thread_num=num,
        label=label,
        summary="summary",
        observation_indices=obs_indices if obs_indices is not None else [0, 1, 2],
        boundary_signal=boundary_signal,
    )


class TestSaveThreadsIdempotent:
    def test_replay_is_no_op(self, db_conn):
        """Same heuristic thread saved twice → exactly one row."""
        conn, db_path = db_conn
        t = _make_thread(1, boundary_signal="new_session", obs_indices=[0, 1, 2])

        save_threads([t], "sess-1", db_path)
        save_threads([t], "sess-1", db_path)

        c2 = connect(db_path)
        rows = c2.execute("SELECT id FROM threads WHERE session_id='sess-1'").fetchall()
        assert len(rows) == 1

    def test_replay_id_is_bucket_hash(self, db_conn):
        """Persisted ID equals the expected bucket_hash value."""
        conn, db_path = db_conn
        t = _make_thread(1, boundary_signal="new_session", obs_indices=[0, 1, 2])

        save_threads([t], "sess-1", db_path)

        expected_id = bucket_hash("sess-1", 0, "new_session")
        row = (
            connect(db_path).execute("SELECT id FROM threads WHERE session_id='sess-1'").fetchone()
        )
        assert row[0] == expected_id

    def test_legacy_thread_no_boundary_signal(self, db_conn):
        """Thread without boundary_signal gets the old '{session_id}-{num}' id."""
        conn, db_path = db_conn
        t = _make_thread(1, boundary_signal=None, obs_indices=[0, 1])

        save_threads([t], "sess-1", db_path)

        row = (
            connect(db_path).execute("SELECT id FROM threads WHERE session_id='sess-1'").fetchone()
        )
        assert row[0] == "sess-1-1"

    def test_two_heuristic_threads_different_ids(self, db_conn):
        """Two segments in the same session get different deterministic IDs."""
        conn, db_path = db_conn
        t1 = _make_thread(1, boundary_signal="new_session", obs_indices=[0, 1, 2])
        t2 = _make_thread(2, boundary_signal="long_gap:3600s", obs_indices=[3, 4, 5])

        save_threads([t1, t2], "sess-1", db_path)

        rows = (
            connect(db_path)
            .execute("SELECT id FROM threads WHERE session_id='sess-1' ORDER BY thread_num")
            .fetchall()
        )
        assert len(rows) == 2
        assert rows[0][0] != rows[1][0]
        assert rows[0][0] == bucket_hash("sess-1", 0, "new_session")
        assert rows[1][0] == bucket_hash("sess-1", 3, "long_gap:3600s")

    def test_legacy_uuid_threads_still_load(self, db_conn):
        """Manually inserted UUID-format rows survive alongside heuristic rows."""
        conn, db_path = db_conn
        legacy_id = "550e8400-e29b-41d4-a716-446655440000"
        c = connect(db_path)
        c.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (legacy_id, "sess-1", 1, "old label", "old summary", "active"),
        )
        c.commit()

        # Heuristic thread in same session
        t = _make_thread(2, boundary_signal="new_session", obs_indices=[5, 6, 7])
        save_threads([t], "sess-1", db_path)

        rows = (
            connect(db_path)
            .execute("SELECT id FROM threads WHERE session_id='sess-1' ORDER BY thread_num")
            .fetchall()
        )
        ids = [r[0] for r in rows]
        assert legacy_id in ids
        assert any(i.startswith("t-") for i in ids)
