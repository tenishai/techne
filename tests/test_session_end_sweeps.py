"""The Stop hook's maintenance sweeps are rate-limited (issue 1218).

`close_abandoned_sessions` and `finalize_quiet_sessions` only touch sessions
silent for 6 hours, so running them on every Stop (every turn) changed nothing
and cost ~100 ms. A checkpoint now runs them only when the last sweep is at
least `hooks.sweep_interval_minutes` old; the final pass always runs them.
"""

from __future__ import annotations

import importlib

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.lifecycle import MODE_CHECKPOINT, MODE_FINAL, maybe_run_maintenance_sweeps


def _state_module():
    """The `techne.core.state` that `maybe_run_maintenance_sweeps` will import.

    Its `from techne.core.state import ...` reads `sys.modules`. A dotted-string
    `monkeypatch.setattr` walks package attributes instead, and those can point at a
    different module object: tests/test_platform_decoupling.py re-imports
    `techne.core.state` and then restores the old `sys.modules` entry, leaving
    `techne.core.state` (the attribute) on the new copy. Under xdist that split
    made every sweep test here fail on whichever worker ran both.
    """
    return importlib.import_module("techne.core.state")


@pytest.fixture
def swept(tmp_path, monkeypatch):
    """A config on a fresh store, with both sweeps replaced by call recorders."""
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    calls = {"close": 0, "finalize": 0}

    def close(db_path):
        calls["close"] += 1

    def finalize(config, *, exclude_session_id="", limit=1):
        calls["finalize"] += 1
        assert exclude_session_id == "sid-live"
        return []

    monkeypatch.setattr(_state_module(), "close_abandoned_sessions", close)
    monkeypatch.setattr("techne.lifecycle.finalize_quiet_sessions", finalize)
    return cfg, calls


def test_checkpoint_skips_sweeps_within_interval(swept):
    cfg, calls = swept
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000)
    assert not maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000 + 1799)
    assert calls == {"close": 1, "finalize": 1}


def test_sweep_runs_again_after_interval(swept):
    cfg, calls = swept
    maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000)
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000 + 1800)
    assert calls == {"close": 2, "finalize": 2}


def test_final_always_sweeps(swept):
    cfg, calls = swept
    maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000)
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_FINAL, now=10_001)
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_FINAL, now=10_002)
    assert calls == {"close": 3, "finalize": 3}


def test_final_sweep_restarts_the_checkpoint_interval(swept):
    cfg, calls = swept
    maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_FINAL, now=10_000)
    assert not maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_060)
    assert calls["close"] == 1


def test_interval_zero_sweeps_every_checkpoint(swept):
    cfg, calls = swept
    cfg.hooks.sweep_interval_minutes = 0
    for t in (10_000, 10_000, 10_001):
        assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=t)
    assert calls["close"] == 3


def test_failed_sweep_is_retried_on_the_next_checkpoint(swept, monkeypatch):
    cfg, calls = swept

    def boom(db_path):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_state_module(), "close_abandoned_sessions", boom)
    with pytest.raises(RuntimeError):
        maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000)
    monkeypatch.setattr(
        _state_module(),
        "close_abandoned_sessions",
        lambda db_path: calls.__setitem__("close", calls["close"] + 1),
    )
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_001)



def test_owed_backlog_drains_one_per_stop_not_one_per_interval(swept, monkeypatch):
    """finalize_quiet_sessions finalizes one owed session per call, so the stamp
    must wait until it finds none: otherwise five owed sessions take five
    intervals (2.5 h of turns at the default) instead of five Stops. Prudence,
    PR 1229 r1, measured 1 of 5 drained over 20 Stops before this."""
    cfg, calls = swept
    owed = ["s1", "s2", "s3", "s4", "s5"]

    def finalize(config, *, exclude_session_id="", limit=1):
        calls["finalize"] += 1
        return [owed.pop(0)] if owed else []

    monkeypatch.setattr("techne.lifecycle.finalize_quiet_sessions", finalize)
    for i in range(20):  # 30 s apart, all well inside the 30-minute interval
        maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000 + 30 * i)
    assert owed == []
    # Five Stops drained the backlog, the sixth found nothing and stamped, and
    # the rest were throttled again.
    assert calls["finalize"] == 6

def test_clock_going_back_does_not_suppress_sweeps(swept):
    cfg, calls = swept
    maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=10_000)
    assert maybe_run_maintenance_sweeps(cfg, "sid-live", MODE_CHECKPOINT, now=5_000)


def test_sweep_interval_is_read_from_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("hooks:\n  sweep_interval_minutes: 7\n")
    assert Config.load(p).hooks.sweep_interval_minutes == 7
    assert Config().hooks.sweep_interval_minutes == 30


def test_stop_hook_sweeps_once_per_interval_and_session_end_always(tmp_path):
    """End to end through the real hook: the first Stop sweeps an abandoned
    session, a second Stop inside the interval does not, SessionEnd does."""
    import json
    import os
    import subprocess
    import sys
    from datetime import datetime, timedelta, timezone

    from techne.core.database import connect
    from techne.core.state import start_session

    config = Config.load()  # TECHNE_HOME is pinned to a tmp dir by conftest
    init_db(config.db_path).close()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def session_with_obs(n, base):
        sid = start_session(config.db_path, project_path="/proj", agent_identity="t")
        conn = connect(config.db_path)
        try:
            for i in range(n):
                conn.execute(
                    "INSERT INTO observations (session_id, timestamp, tool_name, content)"
                    " VALUES (?, ?, 'Edit', ?)",
                    (sid, (base + timedelta(seconds=i)).isoformat(), f"/proj/a.py {i}"),
                )
            conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (base.isoformat(), sid))
            conn.commit()
        finally:
            conn.close()
        return sid

    def ended(sid):
        conn = connect(config.db_path)
        try:
            return conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
        finally:
            conn.close()

    live = session_with_obs(12, now - timedelta(minutes=1))

    def fire(event):
        r = subprocess.run(
            [sys.executable, "-m", "techne.hooks.session_end"],
            input=json.dumps({"session_id": live, "hook_event_name": event}),
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=60,
        )
        assert r.returncode == 0, r.stderr

    first = session_with_obs(1, now - timedelta(days=2))
    fire("Stop")
    assert ended(first) is not None

    second = session_with_obs(1, now - timedelta(days=2))
    fire("Stop")
    assert ended(second) is None, "a Stop inside the interval swept again"
    fire("SessionEnd")
    assert ended(second) is not None
