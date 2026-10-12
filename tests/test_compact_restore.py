"""techne#1242: the post-compaction restore must reach the model.

Claude Code drops a PostCompact hook's stdout, so the restore rides SessionStart
(source=compact) as `additionalContext`. These tests run the real hook modules in
a subprocess and check the JSON Claude Code would read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from techne.core.database import connect, init_db
from techne.core.state import save_checkpoint
from techne.hooks.session_start import REACTIVATION_LINE

SID = "s-compact"


def _home(tmp_path):
    home = tmp_path / "home"
    cc_dir, techne_home = home / ".claude", home / ".techne"
    cc_dir.mkdir(parents=True)
    techne_home.mkdir(parents=True)
    (techne_home / "config.yaml").write_text("platform: claude-code\n")
    (cc_dir / "settings.json").write_text("{}")
    db = techne_home / "techne.db"
    init_db(db).close()
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cc_dir),
        "TECHNE_HOME": str(techne_home),
    }
    return db, env


def _seed(db, n_threads=1):
    conn = connect(db)
    try:
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (SID,))
        for i in range(1, n_threads + 1):
            conn.execute(
                "INSERT INTO threads (id, session_id, thread_num, label, summary, resume_points)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (f"t-{i}", SID, i, f"Thread {i}", f"summary {i}", f"resume {i}"),
            )
        conn.commit()
    finally:
        conn.close()
    save_checkpoint(
        db, SID, "compact", {"last_task": "Fix the compact restore", "files_modified": ["a.py"]}
    )


def _run(module, payload, env):
    r = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r


def test_compact_session_start_delivers_the_restore(tmp_path):
    db, env = _home(tmp_path)
    _seed(db)
    out = json.loads(
        _run("techne.hooks.session_start", {"session_id": SID, "source": "compact"}, env).stdout
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert ctx.startswith(REACTIVATION_LINE.format(source="compact"))
    assert "## Current Session Threads" in ctx and "**Thread 1** — summary 1" in ctx
    assert "Resume: resume 1" in ctx
    assert "## Checkpoint saved " in ctx and " UTC\n" in ctx
    assert "### Last Task\nFix the compact restore" in ctx
    assert "- `a.py`" in ctx


@pytest.mark.parametrize("source", ["resume", "clear"])
def test_other_warm_starts_get_no_restore(tmp_path, source):
    db, env = _home(tmp_path)
    _seed(db)
    out = json.loads(
        _run("techne.hooks.session_start", {"session_id": SID, "source": source}, env).stdout
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Current Session Threads" not in ctx and "Last Task" not in ctx


def test_a_failing_restore_keeps_the_reactivation_line(tmp_path, monkeypatch, capsys):
    """_compact_restore's guard: without it, a raising build_restore empties
    SessionStart's stdout and the session loses its reactivation line."""
    import io

    import techne.hooks.post_compact as post_compact
    from techne.hooks import session_start

    db, env = _home(tmp_path)
    _seed(db)
    for k in ("HOME", "CLAUDE_CONFIG_DIR", "TECHNE_HOME"):
        monkeypatch.setenv(k, env[k])

    def boom(*a, **k):
        raise RuntimeError("restore exploded")

    monkeypatch.setattr(post_compact, "build_restore", boom)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": SID, "source": "compact"}))
    )
    session_start.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["hookSpecificOutput"]["additionalContext"].startswith(
        REACTIVATION_LINE.format(source="compact")
    )
    assert "compact restore skipped: restore exploded" in captured.err


def test_restore_keeps_only_the_newest_threads(tmp_path):
    from techne.hooks.post_compact import MAX_RESTORED_THREADS, build_restore

    db, _ = _home(tmp_path)
    _seed(db, n_threads=MAX_RESTORED_THREADS + 3)
    text, n = build_restore(db, SID)
    assert n == MAX_RESTORED_THREADS
    assert "**Thread 3** —" not in text and "**Thread 4** —" in text
    assert text.index("**Thread 4** —") < text.index(f"**Thread {MAX_RESTORED_THREADS + 3}** —")


def test_post_compact_prints_nothing_and_records_ok(tmp_path):
    db, env = _home(tmp_path)
    _seed(db)
    r = _run("techne.hooks.post_compact", {"session_id": SID, "trigger": "auto"}, env)
    assert r.stdout == "", "PostCompact stdout never reaches the model; printing it implies it does"
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT payload_json FROM extension_state WHERE name = 'hook:post_compact:last_run'"
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0])["outcome"] == "ok"


def test_post_compact_without_checkpoint_is_a_noop(tmp_path):
    db, env = _home(tmp_path)
    r = _run("techne.hooks.post_compact", {"session_id": SID, "trigger": "auto"}, env)
    assert r.stdout == ""
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT payload_json FROM extension_state WHERE name = 'hook:post_compact:last_run'"
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0])["outcome"] == "noop:no-checkpoint"
