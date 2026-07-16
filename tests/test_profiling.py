"""Tests for hot-path profiling (techne#359)."""

import pytest

from techne.core.database import connect, init_db
from techne.core.profiling import (
    format_report,
    profile_import_cost,
    profile_query_plans,
    profile_thread_detection,
    run_profile,
)
from techne.core.state import start_session


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


def _seed_session(db_path, sid="s-prof", n=6):
    start_session(db_path, session_id=sid)
    conn = connect(db_path)
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO observations(session_id, tool_name, content, tier, event_at) "
                "VALUES (?, ?, ?, 'hot', ?)",
                (
                    sid,
                    "Bash" if i % 2 else "Read",
                    f"/repo/file{i}.py work {i}",
                    f"2026-07-16T00:00:0{i}Z",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return sid


def test_query_plans_all_valid(db_path):
    """EXPLAIN succeeds on every hot query (valid SQL) and returns a scan verdict."""
    plans = profile_query_plans(db_path)
    names = {p["name"] for p in plans}
    assert names == {
        "observations_by_session",
        "observations_recent_by_time",
        "resume_active_threads",
    }
    for p in plans:
        assert p["has_scan"] is not None, f"{p['name']} failed to EXPLAIN: {p['plan']}"
        assert p["plan"]  # non-empty plan text


def test_thread_detection_profiled(db_path):
    sid = _seed_session(db_path, n=6)
    result = profile_thread_detection(db_path)
    assert result["session_id"] == sid
    assert result["obs_count"] == 6
    assert isinstance(result["elapsed_s"], float)
    assert result["top_functions"]  # cProfile captured something


def test_thread_detection_explicit_session(db_path):
    _seed_session(db_path, sid="pick-me", n=3)
    result = profile_thread_detection(db_path, session_id="pick-me")
    assert result["session_id"] == "pick-me"
    assert result["obs_count"] == 3


def test_thread_detection_empty_db_graceful(db_path):
    result = profile_thread_detection(db_path)
    assert result["session_id"] is None
    assert result["obs_count"] == 0
    assert result["elapsed_s"] is None


def test_import_cost_smoke():
    result = profile_import_cost()
    assert "cumulative_ms" in result
    # A fresh `import techne.config` should succeed and yield a positive number
    # (or None if importtime output couldn't be parsed — never crash).
    assert result["cumulative_ms"] is None or result["cumulative_ms"] > 0


def test_run_profile_and_format(db_path):
    _seed_session(db_path)
    report = run_profile(db_path)
    assert set(report) == {"import_cost", "thread_detection", "query_plans"}
    text = format_report(report)
    assert "hot-path profile" in text
    assert "query plans" in text


def test_format_report_empty_db(db_path):
    """Renders cleanly with no sessions (no crash on the None branch)."""
    text = format_report(run_profile(db_path))
    assert "no sessions to profile" in text
