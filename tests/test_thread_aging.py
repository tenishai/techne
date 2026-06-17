"""Tests for thread lifecycle aging — active → completed → stale (issue #72)."""

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.thread_orchestrator import age_threads


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path).close()
    conn = connect(path)
    conn.execute("INSERT INTO sessions (id, started_at) VALUES ('sess-1', CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def config():
    # Defaults: complete_after_hours=48, stale_after_days=30.
    return Config()


def _insert_thread(db_path, *, num, status, age_expr, resume_points="last action: edited foo.py"):
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                resume_points, status, updated_at)
           VALUES (?, 'sess-1', ?, ?, 'summary', ?, ?, datetime('now', ?))""",
        (f"sess-1-{num}", num, f"thread {num}", resume_points, status, age_expr),
    )
    conn.commit()
    conn.close()


def _status(db_path, tid):
    conn = connect(db_path)
    row = conn.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()
    conn.close()
    return row[0]


class TestAgeThreads:
    def test_idle_active_thread_completes_despite_resume_points(self, db_path, config):
        """The #72 regression: resume_points is always populated, so the old
        `resume_points IS NULL OR = ''` gate pinned idle active threads to
        'active' forever. With the gate removed, an idle active thread completes
        even though it has resume_points."""
        _insert_thread(db_path, num=1, status="active", age_expr="-50 hours")
        counts = age_threads(db_path, config)
        assert _status(db_path, "sess-1-1") == "completed"
        assert counts["completed"] == 1

    def test_recently_active_thread_stays_active(self, db_path, config):
        """A thread with recent activity (< complete_after_hours) is untouched —
        the current session's threads must not get completed out from under it."""
        _insert_thread(db_path, num=2, status="active", age_expr="-1 hours")
        age_threads(db_path, config)
        assert _status(db_path, "sess-1-2") == "active"

    def test_old_completed_thread_goes_stale(self, db_path, config):
        _insert_thread(db_path, num=3, status="completed", age_expr="-31 days")
        counts = age_threads(db_path, config)
        assert _status(db_path, "sess-1-3") == "stale"
        assert counts["stale"] == 1

    def test_recent_completed_thread_stays_completed(self, db_path, config):
        _insert_thread(db_path, num=4, status="completed", age_expr="-5 days")
        age_threads(db_path, config)
        assert _status(db_path, "sess-1-4") == "completed"

    def test_full_chain_in_one_pass(self, db_path, config):
        """Mixed set: idle-active → completed, fresh-active stays, old-completed
        → stale. A just-completed thread does NOT immediately go stale (its
        updated_at is reset to now on the active→completed transition)."""
        _insert_thread(db_path, num=10, status="active", age_expr="-50 hours")  # -> completed
        _insert_thread(db_path, num=11, status="active", age_expr="-1 hours")   # stays active
        _insert_thread(db_path, num=12, status="completed", age_expr="-40 days")  # -> stale
        counts = age_threads(db_path, config)
        assert _status(db_path, "sess-1-10") == "completed"
        assert _status(db_path, "sess-1-11") == "active"
        assert _status(db_path, "sess-1-12") == "stale"
        assert counts == {"completed": 1, "stale": 1}
