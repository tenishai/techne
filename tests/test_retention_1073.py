"""Retention for context_injections and the per-session extension_state families (#1073)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from techne.core.database import connect, init_db
from techne.core.extension_state import (
    SESSION_STATE_TTL_SECONDS,
    get_state,
    prune_stale,
    set_state,
)
from techne.hooks._injection_log import (
    PRUNE_BATCH,
    RETENTION_DAYS,
    log_context_injection,
    prune_context_injections,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path)
    # Create the lazily-built table the way the writer does.
    log_context_injection(
        path, source="seed", session_id=None, trigger=None, rows=[], injected_text=""
    )
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM context_injections")
    conn.commit()
    conn.close()
    return path


def _insert(path, *, age_days: float, session_id="s-old", hit_ids=(1,), source="per_turn_recall"):
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            "INSERT INTO context_injections "
            "(source, session_id, timestamp, hit_count, hit_ids, approx_tokens) "
            "VALUES (?, ?, datetime('now', ?), ?, ?, 10)",
            (source, session_id, f"-{age_days} days", len(hit_ids), json.dumps(list(hit_ids))),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _ids(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT id FROM context_injections ORDER BY id")]
    finally:
        conn.close()


# --------------------------------------------------------------- context_injections


def test_rows_past_the_window_go_and_rows_inside_it_stay(db):
    old = _insert(db, age_days=RETENTION_DAYS + 1)
    kept = _insert(db, age_days=RETENTION_DAYS - 1)
    conn = sqlite3.connect(str(db))
    try:
        assert prune_context_injections(conn) == 1
        assert prune_context_injections(conn) == 0, "a second run must be a no-op"
    finally:
        conn.close()
    assert _ids(db) == [kept]
    assert old not in _ids(db)


def test_one_prune_deletes_at_most_one_batch(db):
    for _ in range(7):
        _insert(db, age_days=RETENTION_DAYS + 30)
    conn = sqlite3.connect(str(db))
    try:
        assert prune_context_injections(conn, batch=3) == 3
        assert prune_context_injections(conn, batch=3) == 3
        assert prune_context_injections(conn, batch=3) == 1
    finally:
        conn.close()
    assert _ids(db) == []
    assert PRUNE_BATCH > 0


def test_every_write_prunes(db):
    """The writer is the mechanism: no install has to run anything for the bound."""
    old = _insert(db, age_days=RETENTION_DAYS + 5)
    log_context_injection(
        db,
        source="per_turn_recall",
        session_id="s-new",
        trigger="q",
        rows=[{"id": 7}],
        injected_text="x",
    )
    ids = _ids(db)
    assert old not in ids
    assert len(ids) == 1, "the row just written must survive its own prune"


def test_already_sent_still_reads_the_current_session_after_a_prune(db):
    """user_prompt_submit's re-send suppression reads context_injections.hit_ids."""
    from techne.hooks.user_prompt_submit import _already_sent_this_session

    _insert(db, age_days=RETENTION_DAYS + 5, session_id="s-live", hit_ids=(99,))
    _insert(db, age_days=0, session_id="s-live", hit_ids=(11, 12))
    conn = sqlite3.connect(str(db))
    try:
        prune_context_injections(conn)
    finally:
        conn.close()
    assert _already_sent_this_session(db, "s-live") == {11, 12}


def test_audit_counts_only_retained_rows(db):
    from techne.core.utilization import injection_utilization

    _insert(db, age_days=RETENTION_DAYS + 5, source="session_start")
    _insert(db, age_days=1, source="session_start")
    _insert(db, age_days=2, source="session_start")
    conn = sqlite3.connect(str(db))
    try:
        prune_context_injections(conn)
    finally:
        conn.close()
    rows = {r["source"]: r for r in injection_utilization(db)}
    assert rows["session_start"]["events"] == 2


# --------------------------------------------------------------- extension_state


def _age(path, name, seconds):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "UPDATE extension_state SET updated_at = ? WHERE name = ?",
            (int(time.time()) - seconds, name),
        )
        conn.commit()
    finally:
        conn.close()


def _names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM extension_state")}
    finally:
        conn.close()


def test_prune_stale_takes_old_keys_of_its_prefix_only(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path)
    conn = connect(path)
    try:
        for name in (
            "heuristic_threads.old",
            "heuristic_threads.new",
            "heuristicXthreads.old",  # `_` is a LIKE wildcard; the prefix match must be exact
            "other:old",
        ):
            set_state(conn, name, {"v": 1})
    finally:
        conn.close()
    for name in ("heuristic_threads.old", "heuristicXthreads.old", "other:old"):
        _age(path, name, SESSION_STATE_TTL_SECONDS + 60)

    conn = connect(path)
    try:
        assert prune_stale(conn, "heuristic_threads.") == 1
        assert prune_stale(conn, "heuristic_threads.") == 0
    finally:
        conn.close()
    names = _names(path)
    assert "heuristic_threads.old" not in names
    assert {"heuristic_threads.new", "heuristicXthreads.old", "other:old"} <= names


def test_session_end_prunes_stale_heuristic_cursors(tmp_path):
    from techne.config import Config
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    path = tmp_path / "techne.db"
    init_db(path)
    config = Config()
    config.db_path = path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    conn = connect(path)
    try:
        set_state(conn, "heuristic_threads.gone-session", {"last_obs_count": 3})
    finally:
        conn.close()
    _age(path, "heuristic_threads.gone-session", SESSION_STATE_TTL_SECONDS + 60)

    session_id = start_session(path, project_path="/tmp/p", agent_identity="t")
    conn = connect(path)
    try:
        for i in range(6):
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?, ?, ?)",
                (session_id, "Edit", f"/proj/a.py line {i}"),
            )
        conn.commit()
    finally:
        conn.close()

    run_session_end(config, session_id)

    names = _names(path)
    assert "heuristic_threads.gone-session" not in names
    assert f"heuristic_threads.{session_id}" in names, "the live cursor must survive"


def test_session_start_prunes_stale_deferred_stashes(tmp_path):
    import os
    import subprocess
    import sys

    from techne.hooks.session_start import DEFERRED_CONTEXT_KEY

    home = tmp_path / "home"
    cc_dir = home / ".claude"
    techne_home = home / ".techne"
    cc_dir.mkdir(parents=True)
    techne_home.mkdir(parents=True)
    (techne_home / "config.yaml").write_text("platform: claude-code\n")
    (cc_dir / "settings.json").write_text("{}")
    db = techne_home / "techne.db"
    init_db(db)
    conn = connect(db)
    try:
        set_state(conn, f"{DEFERRED_CONTEXT_KEY}:gone-session", {})
    finally:
        conn.close()
    _age(db, f"{DEFERRED_CONTEXT_KEY}:gone-session", SESSION_STATE_TTL_SECONDS + 60)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cc_dir),
        "TECHNE_HOME": str(techne_home),
    }
    r = subprocess.run(
        [sys.executable, "-m", "techne.hooks.session_start"],
        input=json.dumps({"session_id": "live-1073"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr

    conn = connect(db)
    try:
        assert get_state(conn, f"{DEFERRED_CONTEXT_KEY}:gone-session") is None
        assert get_state(conn, f"{DEFERRED_CONTEXT_KEY}:live-1073") is not None
    finally:
        conn.close()
