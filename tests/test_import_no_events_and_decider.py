"""techne#941 and techne#953.

#941: a metadata-only transcript (Claude Code writes one when a session is titled and
never takes a turn) failed import with `NOT NULL constraint failed:
sessions.started_at`. The real file was one line: a `custom-title` record.

#953: `reflect promote` / `reflect discard` defaulted `--by` to "human", so an agent
that omitted the flag had its decision audited as a person's.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main

TITLE_ONLY = {
    "type": "custom-title",
    "customTitle": "Claude",
    "sessionId": "d1835329-027c-41e8-b6e1-c76cd18353fe",
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    c = Config()
    c.db_path = db
    c.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


def _title_only(tmp_path):
    p = tmp_path / f"{TITLE_ONLY['sessionId']}.jsonl"
    p.write_text(json.dumps(TITLE_ONLY) + "\n")
    return p


def _rows(cfg, sql, *args):
    from techne.core.database import connect

    conn = connect(cfg.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# --- #941 ---------------------------------------------------------------------


def test_a_title_only_transcript_is_skipped_not_an_error(cfg, tmp_path):
    from techne.platforms.claude_code import import_transcript
    from techne.platforms.claude_code.transcript_import import NO_EVENTS_REASON

    result = import_transcript(cfg.db_path, _title_only(tmp_path))

    assert result["skipped"] is True
    assert result["reason"] == NO_EVENTS_REASON
    assert _rows(cfg, "SELECT id FROM sessions") == [], (
        "no session row for a transcript with no conversation — a defaulted "
        "started_at would record a session nobody observed"
    )


def test_the_cli_treats_a_title_only_file_as_benign(cfg, tmp_path):
    result = CliRunner().invoke(main, ["import", str(_title_only(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "no events" in result.output


def test_a_directory_with_a_title_only_file_still_imports_the_rest(cfg, tmp_path):
    _title_only(tmp_path)
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    rows = [
        {
            "type": "user",
            "sessionId": sid,
            "cwd": "/tmp/p",
            "timestamp": "2026-09-17T00:00:00Z",
            "message": {"role": "user", "content": "hello there"},
        }
    ]
    (tmp_path / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    result = CliRunner().invoke(main, ["import", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [r["id"] for r in _rows(cfg, "SELECT id FROM sessions")] == [sid]


# --- #953 ---------------------------------------------------------------------


def _candidate(cfg, content):
    from techne.core.database import connect
    from techne.core.state import start_session

    sid = start_session(cfg.db_path, project_path="/tmp/p")
    conn = connect(cfg.db_path)
    try:
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
               (scope, type, content, confidence, source_session_id, model_used,
                prompt_hash, status, origin)
               VALUES ('knowledge', 'fact', ?, 'high', ?, 'agent-assisted', ?,
                       'pending', 'reflect')""",
            (content, sid, f"h-{content}"),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.mark.parametrize("verb", ["promote", "discard"])
def test_omitting_by_does_not_record_a_human(cfg, verb):
    cid = _candidate(cfg, f"fact for {verb}")
    result = CliRunner().invoke(main, ["reflect", verb, str(cid)])
    assert result.exit_code == 0, result.output

    (row,) = _rows(cfg, "SELECT decided_by FROM knowledge_candidates WHERE id = ?", cid)
    assert row["decided_by"] == "unspecified", (
        f"`reflect {verb}` without --by recorded {row['decided_by']!r}; the audit trail "
        "must not name an actor nobody specified (techne#953)"
    )


@pytest.mark.parametrize("verb", ["promote", "discard"])
def test_an_explicit_by_is_recorded(cfg, verb):
    cid = _candidate(cfg, f"explicit {verb}")
    result = CliRunner().invoke(main, ["reflect", verb, str(cid), "--by", "judge-blind"])
    assert result.exit_code == 0, result.output

    (row,) = _rows(cfg, "SELECT decided_by FROM knowledge_candidates WHERE id = ?", cid)
    assert row["decided_by"] == "judge-blind"
