"""Per-session token usage from Claude Code transcripts (#689)."""

from __future__ import annotations

import gzip
import io
import json
import sqlite3

import pytest

from techne.config import Config
from techne.core.database import _migrate, init_db
from techne.platforms.claude_code.usage import (
    record_session_usage,
    session_transcript_paths,
    sum_usage,
)

SID = "sess-usage"


def _asst(mid, i=0, o=0, cw=0, cr=0, *, req=None, model="claude-opus-5-5"):
    msg = {
        "model": model,
        "usage": {
            "input_tokens": i,
            "output_tokens": o,
            "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr,
            "service_tier": "standard",
        },
    }
    if mid is not None:
        msg["id"] = mid
    rec = {"type": "assistant", "sessionId": SID, "message": msg}
    if req:
        rec["requestId"] = req
    return rec


def _write(path, records, *, gz=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
    else:
        path.write_text(text)
    return path


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = tmp_path / "techne.db"
    conn = init_db(c.db_path)
    conn.execute("INSERT INTO sessions (id, platform) VALUES (?, 'claude-code')", (SID,))
    conn.commit()
    conn.close()
    return c


def _row(cfg, sid=SID):
    conn = sqlite3.connect(cfg.db_path)
    try:
        return conn.execute(
            "SELECT tokens_in, tokens_out, tokens_cache_write, tokens_cache_read,"
            " usage_updated_at FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()


# ---- sum_usage --------------------------------------------------------------


def test_one_message_logged_per_content_block_is_counted_once(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [_asst("m1", 3, 100, 50, 1000), _asst("m1", 3, 100, 50, 1000), _asst("m2", 1, 7, 0, 9)],
    )
    assert sum_usage([p]) == {
        "tokens_in": 4,
        "tokens_out": 107,
        "tokens_cache_write": 50,
        "tokens_cache_read": 1009,
    }


def test_conflicting_duplicates_keep_the_fieldwise_max(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_asst("m1", 5, 10, 0, 0), _asst("m1", 2, 40, 0, 0)])
    assert sum_usage([p])["tokens_in"] == 5
    assert sum_usage([p])["tokens_out"] == 40


def test_request_id_is_the_fallback_key(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [_asst(None, o=10, req="r1"), _asst(None, o=10, req="r1"), _asst(None, o=1, req="r2")],
    )
    assert sum_usage([p])["tokens_out"] == 11


def test_non_assistant_and_usage_less_records_are_ignored(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"usage": {"output_tokens": 999}}},
            {"type": "assistant", "message": {"id": "x"}},
            _asst("m1", o=5),
        ],
    )
    assert sum_usage([p])["tokens_out"] == 5


def test_synthetic_zero_usage_adds_zero_and_still_counts_as_measured(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_asst("syn", model="<synthetic>")])
    assert sum_usage([p]) == dict.fromkeys(
        ("tokens_in", "tokens_out", "tokens_cache_write", "tokens_cache_read"), 0
    )


def test_no_usage_records_returns_none(tmp_path):
    p = _write(tmp_path / "t.jsonl", [{"type": "user", "message": {"content": "hi"}}])
    assert sum_usage([p]) is None


def test_gzipped_transcript_is_read(tmp_path):
    p = _write(tmp_path / "t.jsonl.gz", [_asst("m1", o=12), _asst("m1", o=12)], gz=True)
    assert sum_usage([p])["tokens_out"] == 12


# ---- session_transcript_paths ------------------------------------------------


def test_subagent_transcripts_are_included_and_other_files_are_not(tmp_path):
    main = _write(tmp_path / "proj" / f"{SID}.jsonl", [_asst("m1", o=1)])
    sub = tmp_path / "proj" / SID / "subagents"
    a = _write(sub / "agent-a.jsonl", [_asst("s1", o=20)])
    b = _write(sub / "agent-b.jsonl.gz", [_asst("s2", o=300)], gz=True)
    (sub / "agent-a.meta.json").write_text('{"agentType": "Explore"}')
    (sub / "agent-c.jsonl.swp").write_bytes(b"\xff\xfe not a transcript")
    assert session_transcript_paths(SID, main) == [main, a, b]
    assert sum_usage(session_transcript_paths(SID, main))["tokens_out"] == 321


def test_paths_are_located_when_not_given(tmp_path, monkeypatch):
    import techne.platforms.claude_code.transcript_import as ti

    monkeypatch.setattr(ti, "PROJECTS_DIR", tmp_path)
    main = _write(tmp_path / "slug" / f"{SID}.jsonl", [_asst("m1", o=1)])
    assert session_transcript_paths(SID) == [main]
    assert session_transcript_paths("no-such-session") == []


# ---- record_session_usage ----------------------------------------------------


def test_record_sets_the_columns_and_is_idempotent(cfg, tmp_path):
    dup = [_asst("m1", 1, 2, 3, 4), _asst("m1", 1, 2, 3, 4)]
    main = _write(tmp_path / "p" / f"{SID}.jsonl", dup)
    _write(tmp_path / "p" / SID / "subagents" / "agent-x.jsonl", [_asst("s1", 10, 20, 30, 40)])
    assert _row(cfg)[:4] == (None, None, None, None)

    out = record_session_usage(cfg, SID, main)
    assert out["recorded"] is True and out["files"] == 2
    first = _row(cfg)
    assert first[:4] == (11, 22, 33, 44)
    assert first[4] is not None

    record_session_usage(cfg, SID, main)
    assert _row(cfg)[:4] == (11, 22, 33, 44)


def test_no_usage_leaves_the_columns_null(cfg, tmp_path):
    main = _write(tmp_path / f"{SID}.jsonl", [{"type": "user", "message": {"content": "x"}}])
    out = record_session_usage(cfg, SID, main)
    assert out == {"recorded": False, "reason": "no usage records"}
    assert _row(cfg) == (None, None, None, None, None)


def test_missing_transcript_writes_nothing(cfg, tmp_path, monkeypatch):
    import techne.platforms.claude_code.transcript_import as ti

    monkeypatch.setattr(ti, "PROJECTS_DIR", tmp_path / "empty")
    assert record_session_usage(cfg, SID)["reason"] == "transcript not found"
    assert _row(cfg) == (None, None, None, None, None)


def test_missing_session_row_writes_nothing(cfg, tmp_path):
    main = _write(tmp_path / "other.jsonl", [_asst("m1", o=5)])
    out = record_session_usage(cfg, "other", main)
    assert out == {"recorded": False, "reason": "no session row"}
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='other'").fetchone()[0] == 0
    conn.close()


def test_errors_are_returned_not_raised(cfg, tmp_path):
    main = _write(tmp_path / f"{SID}.jsonl", [_asst("m1", o=5)])
    cfg.db_path = tmp_path / "missing-dir" / "x" / "techne.db"
    out = record_session_usage(cfg, SID, main)
    assert out["recorded"] is False and out["reason"]


def test_hook_timeout_is_not_swallowed(cfg, tmp_path, monkeypatch):
    from techne.hooks._timeout import HookTimeout
    from techne.platforms.claude_code import usage

    def alarm(paths):
        raise HookTimeout("session_end timed out")

    monkeypatch.setattr(usage, "sum_usage", alarm)
    main = _write(tmp_path / f"{SID}.jsonl", [_asst("m1", o=5)])
    with pytest.raises(HookTimeout):
        record_session_usage(cfg, SID, main)


# ---- migration v30 -------------------------------------------------------------

USAGE_COLS = {
    "tokens_in",
    "tokens_out",
    "tokens_cache_write",
    "tokens_cache_read",
    "usage_updated_at",
}


def test_migration_30_adds_the_columns_and_replays(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TIMESTAMP)")
    conn.execute("INSERT INTO sessions (id) VALUES ('s')")
    _migrate(conn, 29, 30)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert USAGE_COLS <= cols
    assert conn.execute("SELECT tokens_out FROM sessions").fetchone()[0] is None
    _migrate(conn, 29, 30)  # a replayed step must not die on "duplicate column"
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 30
    conn.close()


def test_migration_30_without_a_sessions_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    _migrate(conn, 29, 30)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 30
    conn.close()


def test_fresh_schema_has_the_columns(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert USAGE_COLS <= cols


# ---- session_end hook -------------------------------------------------------------


def _run_hook(monkeypatch, cfg, payload):
    from techne.hooks import session_end

    monkeypatch.setattr("techne.config.Config.load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        session_end.main()
    assert exc.value.code == 0


def test_session_end_records_usage_below_the_observation_threshold(cfg, tmp_path, monkeypatch):
    dup = [_asst("m1", 1, 2, 3, 4), _asst("m1", 1, 2, 3, 4)]
    main = _write(tmp_path / "p" / f"{SID}.jsonl", dup)
    _write(tmp_path / "p" / SID / "subagents" / "agent-x.jsonl", [_asst("s1", o=100)])
    _run_hook(
        monkeypatch,
        cfg,
        {"session_id": SID, "hook_event_name": "SessionEnd", "transcript_path": str(main)},
    )
    assert _row(cfg)[:4] == (1, 102, 3, 4)


def test_stop_does_not_read_usage(cfg, tmp_path, monkeypatch):
    """Stop fires every turn; a whole-transcript read there would undo issue 1218.
    Recorded rather than raised: the hook's own catch-all would swallow a raise."""
    main = _write(tmp_path / f"{SID}.jsonl", [_asst("m1", o=5)])
    calls = []
    monkeypatch.setattr(
        "techne.platforms.claude_code.usage.record_session_usage",
        lambda *a, **k: calls.append(a) or {"recorded": False},
    )
    payload = {"session_id": SID, "hook_event_name": "Stop", "transcript_path": str(main)}
    _run_hook(monkeypatch, cfg, payload)
    assert calls == []
    assert _row(cfg)[:4] == (None, None, None, None)


# ---- CLI backfill ---------------------------------------------------------------------


def test_usage_backfill_cli(cfg, tmp_path, monkeypatch):
    from click.testing import CliRunner

    import techne.platforms.claude_code.transcript_import as ti
    from techne.cli import main as cli

    monkeypatch.setattr(ti, "PROJECTS_DIR", tmp_path / "projects")
    _write(tmp_path / "projects" / "slug" / f"{SID}.jsonl", [_asst("m1", o=7), _asst("m1", o=7)])
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("INSERT INTO sessions (id) VALUES ('gone')")
    conn.commit()
    conn.close()

    runner = CliRunner()
    r = runner.invoke(cli, ["usage-backfill"], obj={"config": cfg})
    assert r.exit_code == 0, r.output
    assert "1 of 2 sessions recorded" in r.output
    assert "1 transcript not found" in r.output
    assert _row(cfg)[1] == 7

    # Already-filled rows are skipped without --force, re-read with it.
    _write(tmp_path / "projects" / "slug" / f"{SID}.jsonl", [_asst("m1", o=7), _asst("m2", o=1)])
    r = runner.invoke(cli, ["usage-backfill"], obj={"config": cfg})
    assert "0 of 1 sessions recorded" in r.output
    assert _row(cfg)[1] == 7
    r = runner.invoke(cli, ["usage-backfill", "--force"], obj={"config": cfg})
    assert "1 of 2 sessions recorded" in r.output
    assert _row(cfg)[1] == 8
