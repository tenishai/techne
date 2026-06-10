"""Tests for `techne threads show <session>`."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core import heuristic_threads as ht
from techne.core.database import connect


@pytest.fixture
def cfg_with_session(tmp_path, monkeypatch):
    """Build a config + populated DB and patch Config.load() to return it."""
    db_path = tmp_path / "techne.db"
    cfg = Config()
    cfg.db_path = db_path
    cfg.data_dir = tmp_path
    cfg.platform = "claude-code"

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, project_path, started_at) VALUES (?, ?, ?)",
        ("sess-abc-001", "/tmp/proj", "2026-06-09T12:00:00"),
    )

    base = datetime(2026, 6, 9, 12, 0)
    # Build a session that should obviously split into 2 threads
    rows = []
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        rows.append(("sess-abc-001", ts, "Edit", f"/tmp/proj/a.py line {i}"))
    for i in range(4):
        # 2-hour gap
        ts = (base + timedelta(minutes=120 + i)).isoformat()
        rows.append(("sess-abc-001", ts, "Bash", f"echo step {i}"))

    conn.executemany(
        "INSERT INTO observations (session_id, timestamp, tool_name, content) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return cfg


def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, catch_exceptions=False, env=env)


class TestThreadsShow:
    def test_unknown_session(self, cfg_with_session):
        result = _invoke(["threads", "show", "nope-nope-nope"])
        assert result.exit_code == 0
        assert "No session matching" in result.output

    def test_live_detector_runs_and_reports_two_threads(self, cfg_with_session):
        result = _invoke(["threads", "show", "sess-abc", "--no-color"])
        assert result.exit_code == 0, result.output
        assert "Session sess-abc-001" in result.output
        # Two segments because of the 2h gap + family change
        assert "2 threads" in result.output
        assert "Thread 1" in result.output
        assert "Thread 2" in result.output

    def test_boundary_line_shows_signals(self, cfg_with_session):
        result = _invoke(["threads", "show", "sess-abc", "--no-color"])
        # The 2h gap pair should print a score line containing 'gap_hard'
        assert "gap_hard" in result.output
        # And a score >= 1.0 somewhere
        assert "score=1." in result.output

    def test_stored_mode_errors_when_nothing_stored(self, cfg_with_session):
        result = _invoke(["threads", "show", "sess-abc", "--detector", "stored"])
        assert result.exit_code == 0
        assert "No stored boundaries" in result.output

    def test_auto_uses_stored_when_present(self, cfg_with_session):
        # Persist boundaries first by running the detector and saving.
        conn = connect(cfg_with_session.db_path)
        obs_rows = conn.execute(
            "SELECT timestamp, tool_name, content FROM observations "
            "WHERE session_id = 'sess-abc-001' ORDER BY timestamp"
        ).fetchall()
        observations = [
            {"tool_name": r["tool_name"], "content": r["content"], "timestamp": r["timestamp"]}
            for r in obs_rows
        ]
        segments, scores = ht.detect_threads(observations)
        ht.save_boundaries(
            conn, "sess-abc-001", scores,
            ht.boundary_indices_from_segments(segments),
        )
        conn.close()

        result = _invoke(["threads", "show", "sess-abc", "--no-color"])
        assert "(stored)" in result.output

    def test_live_mode_overrides_stored(self, cfg_with_session):
        conn = connect(cfg_with_session.db_path)
        obs_rows = conn.execute(
            "SELECT timestamp, tool_name, content FROM observations "
            "WHERE session_id = 'sess-abc-001' ORDER BY timestamp"
        ).fetchall()
        observations = [
            {"tool_name": r["tool_name"], "content": r["content"], "timestamp": r["timestamp"]}
            for r in obs_rows
        ]
        segments, scores = ht.detect_threads(observations)
        ht.save_boundaries(
            conn, "sess-abc-001", scores,
            ht.boundary_indices_from_segments(segments),
        )
        conn.close()

        result = _invoke(["threads", "show", "sess-abc", "--no-color", "--detector", "live"])
        assert "(live)" in result.output

    def test_no_observations_session(self, cfg_with_session, tmp_path):
        conn = connect(cfg_with_session.db_path)
        conn.execute(
            "INSERT INTO sessions (id, project_path, started_at) VALUES (?, ?, ?)",
            ("sess-empty", "/tmp", "2026-06-09T11:00:00"),
        )
        conn.commit()
        conn.close()
        result = _invoke(["threads", "show", "sess-empty", "--no-color"])
        assert "(no observations)" in result.output
