"""Thread-summary sync checkpoints (techne#1187).

Claude Code fires Stop at the end of every turn. Before #1187 each Stop re-enqueued
every thread summary and stamped `ended_at`, so a live session read as ended and a
team backend got one near-duplicate row per thread per turn.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.state import start_session
from techne.lifecycle import (
    MODE_CHECKPOINT,
    MODE_COMPACT,
    MODE_FINAL,
    finalize_quiet_sessions,
    run_session_end,
)


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "t.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"
    config.sync.adapter = "filesystem"
    config.sync.path = str(tmp_path / "sync-out")
    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    return config, session_id


_BASE = datetime(2026, 1, 1, 12, 0)


def _add_obs(config, session_id, n, start=0, base=_BASE):
    """n observations of one task, so detection keeps them in one thread."""
    conn = connect(config.db_path)
    try:
        for i in range(start, start + n):
            conn.execute(
                "INSERT INTO observations (session_id, timestamp, tool_name, content) "
                "VALUES (?, ?, ?, ?)",
                (session_id, (base + timedelta(seconds=i)).isoformat(), "Edit", f"/proj/a.py {i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return start + n


def _summary_rows(config, status=None):
    conn = connect(config.db_path)
    try:
        sql = "SELECT data_json, status FROM outbox WHERE target = 'sync:thread_summary'"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [json.loads(r["data_json"]) for r in rows if status is None or r["status"] == status]


def _ended_at(config, session_id):
    conn = connect(config.db_path)
    try:
        return conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()[
            0
        ]
    finally:
        conn.close()


def _sync_state(config, session_id):
    from techne.core.extension_state import get_state

    conn = connect(config.db_path)
    try:
        return get_state(conn, f"thread_sync.{session_id}")
    finally:
        conn.close()


def test_turns_below_the_gate_enqueue_nothing_and_final_sends_once(env):
    config, sid = env
    config.threads.checkpoint_min_interval_minutes = 0
    n = _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)  # first send: 12 new obs
    first = len(_summary_rows(config))
    assert first >= 1

    # Four more turns of 2 observations each: 8 new since the last send.
    for _ in range(4):
        n = _add_obs(config, sid, 2, start=n)
        run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert len(_summary_rows(config)) == first  # 8 new: below the gate

    run_session_end(config, sid, mode=MODE_FINAL)
    rows = _summary_rows(config)
    # Coalesced: the final revision replaced the pending one for the same thread.
    assert len(rows) == first
    assert len({r["thread_id"] for r in rows}) == len(rows)


def test_checkpoint_leaves_ended_at_alone_and_final_stamps_it(env):
    config, sid = env
    _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert _ended_at(config, sid) is None
    run_session_end(config, sid, mode=MODE_FINAL)
    assert _ended_at(config, sid) is not None


def test_final_after_an_unchanged_checkpoint_still_ends_the_session(env):
    # The last Stop already detected at this observation count, so the final pass
    # hits the incremental gate. It must still stamp the end.
    config, sid = env
    _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    run_session_end(config, sid, mode=MODE_FINAL)
    assert _ended_at(config, sid) is not None
    assert _sync_state(config, sid)["finalized"] is True


def test_final_sends_work_a_checkpoint_held_back(env):
    config, sid = env
    config.threads.checkpoint_min_interval_minutes = 0
    n = _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    sent = {r["thread_id"] for r in _summary_rows(config)}
    # Mark the first send delivered, so a later row is visibly a new one.
    conn = connect(config.db_path)
    conn.execute("UPDATE outbox SET status = 'synced'")
    conn.commit()
    conn.close()

    _add_obs(config, sid, 3, start=n)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert _summary_rows(config, status="pending") == []
    assert _sync_state(config, sid)["owed"] is True

    run_session_end(config, sid, mode=MODE_FINAL)
    pending = _summary_rows(config, status="pending")
    # The same threads again, now carrying the 3 observations the checkpoint held.
    assert {r["thread_id"] for r in pending} == sent
    assert _sync_state(config, sid)["owed"] is False


def test_checkpoint_respects_the_interval(env):
    config, sid = env
    config.threads.checkpoint_min_interval_minutes = 5
    n = _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    conn = connect(config.db_path)
    conn.execute("UPDATE outbox SET status = 'synced'")
    conn.commit()
    conn.close()

    _add_obs(config, sid, 12, start=n)  # enough growth, but under 5 minutes later
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert _summary_rows(config, status="pending") == []


def test_unchanged_thread_is_never_resent(env):
    config, sid = env
    _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_FINAL)
    before = len(_summary_rows(config))
    run_session_end(config, sid, mode=MODE_FINAL)
    run_session_end(config, sid, mode=MODE_COMPACT)
    assert len(_summary_rows(config)) == before


def test_compact_sends_new_work_without_ending(env):
    config, sid = env
    _add_obs(config, sid, 3)
    run_session_end(config, sid, mode=MODE_COMPACT)
    assert _summary_rows(config)  # below the checkpoint gate, sent anyway
    assert _ended_at(config, sid) is None


def test_noop_adapter_enqueues_nothing_and_keeps_no_state(env):
    config, sid = env
    config.sync.adapter = "noop"
    _add_obs(config, sid, 12)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    run_session_end(config, sid, mode=MODE_FINAL)
    assert _summary_rows(config) == []
    assert _sync_state(config, sid) is None
    assert _ended_at(config, sid) is not None


def test_unknown_mode_is_rejected(env):
    config, sid = env
    with pytest.raises(ValueError):
        run_session_end(config, sid, mode="per-turn")


def _age_sync_state(config, sid, hours):
    conn = connect(config.db_path)
    conn.execute(
        "UPDATE extension_state SET updated_at = ? WHERE name = ?",
        (int(time.time()) - hours * 3600, f"thread_sync.{sid}"),
    )
    conn.commit()
    conn.close()


def test_quiet_session_with_owed_work_is_finalized(env):
    from techne.core.state import ABANDONED_SESSION_IDLE_HOURS

    config, sid = env
    _add_obs(config, sid, 3)  # below the gate: owed
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert _summary_rows(config) == []

    # Still recent: a live peer must not be finalized.
    assert finalize_quiet_sessions(config) == []

    _age_sync_state(config, sid, ABANDONED_SESSION_IDLE_HOURS + 1)
    assert finalize_quiet_sessions(config, exclude_session_id="someone-else") == [sid]
    assert _summary_rows(config)
    assert _sync_state(config, sid)["finalized"] is True
    assert finalize_quiet_sessions(config) == []  # done once


def test_resumed_session_is_swept_again_after_a_second_quiet_death(env):
    """A sweep finalizes a quiet session; the operator resumes it, leaves work below
    the gate, and it dies again without SessionEnd. The sweep must finalize it a
    second time, or that held-back work is never sent (Prudence, PR 1198 r1)."""
    from techne.core.state import ABANDONED_SESSION_IDLE_HOURS

    config, sid = env
    n = _add_obs(config, sid, 3)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    _age_sync_state(config, sid, ABANDONED_SESSION_IDLE_HOURS + 1)
    assert finalize_quiet_sessions(config) == [sid]
    first = _summary_rows(config, status="pending")

    _add_obs(config, sid, 3, start=n)  # resumed: below the gate, so owed again
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    assert _sync_state(config, sid)["owed"] is True
    assert _sync_state(config, sid)["finalized"] is False
    _age_sync_state(config, sid, ABANDONED_SESSION_IDLE_HOURS + 1)
    assert finalize_quiet_sessions(config) == [sid]
    pending = _summary_rows(config, status="pending")
    # The newer revision replaced the still-pending one for the same thread.
    assert len(pending) == len(first)
    assert _sync_state(config, sid)["owed"] is False


def test_quiet_session_sweep_skips_the_calling_session(env):
    from techne.core.state import ABANDONED_SESSION_IDLE_HOURS

    config, sid = env
    _add_obs(config, sid, 3)
    run_session_end(config, sid, mode=MODE_CHECKPOINT)
    _age_sync_state(config, sid, ABANDONED_SESSION_IDLE_HOURS + 1)
    assert finalize_quiet_sessions(config, exclude_session_id=sid) == []


def test_supersede_key_replaces_only_pending_rows(tmp_path):
    from techne.sync.outbox import enqueue

    db = tmp_path / "o.db"
    init_db(db)
    enqueue(
        db, "sync:thread_summary", {"thread_id": "t-1", "summary": "a"}, supersede_key="thread_id"
    )
    conn = connect(db)
    conn.execute("UPDATE outbox SET status = 'synced'")
    conn.commit()
    conn.close()
    enqueue(
        db, "sync:thread_summary", {"thread_id": "t-1", "summary": "b"}, supersede_key="thread_id"
    )
    enqueue(
        db, "sync:thread_summary", {"thread_id": "t-1", "summary": "c"}, supersede_key="thread_id"
    )
    enqueue(
        db, "sync:thread_summary", {"thread_id": "t-2", "summary": "x"}, supersede_key="thread_id"
    )
    conn = connect(db)
    rows = conn.execute("SELECT status, data_json FROM outbox ORDER BY id").fetchall()
    conn.close()
    got = [(r["status"], json.loads(r["data_json"])["summary"]) for r in rows]
    assert got == [("synced", "a"), ("pending", "c"), ("pending", "x")]


def test_checkpoint_settings_round_trip(tmp_path):
    import yaml

    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "threads": {
                    "checkpoint_min_new_observations": 25,
                    "checkpoint_min_interval_minutes": 0,
                }
            }
        )
    )
    cfg = Config.load(p)
    assert cfg.threads.checkpoint_min_new_observations == 25
    assert cfg.threads.checkpoint_min_interval_minutes == 0


def test_hook_runs_stop_as_checkpoint_and_session_end_as_final(tmp_path):
    """End to end through the real hook: the event name picks the mode."""
    import os
    import subprocess
    import sys

    config = Config.load()  # TECHNE_HOME is pinned to a tmp dir by conftest
    init_db(config.db_path)
    sid = start_session(config.db_path, project_path="/proj", agent_identity="t")
    # Dated now: an old session is quiet, and the abandoned sweep would end it.
    _add_obs(
        config, sid, 12, base=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    )

    def fire(event):
        stdin = json.dumps({"session_id": sid, "hook_event_name": event})
        r = subprocess.run(
            [sys.executable, "-m", "techne.hooks.session_end"],
            input=stdin,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=60,
        )
        assert r.returncode == 0, r.stderr
        return r

    fire("Stop")
    assert _ended_at(config, sid) is None
    fire("SessionEnd")
    assert _ended_at(config, sid) is not None
