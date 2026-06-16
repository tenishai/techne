"""Tests for techne.sync.jsonl_transcript — JsonlTranscriptSource adapter."""

import json
from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.core.extension_state import get_state
from techne.sync.jsonl_transcript import (
    JsonlTranscriptConfig,
    JsonlTranscriptSource,
    SourceAdapter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


def _rec_user(text: str, session: str = "sess-1", cwd: str = "/proj/foo") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": "2026-06-01T10:00:00.000Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _rec_tool_use(tool_id: str, cmd: str, session: str = "sess-1") -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-06-01T10:00:01.000Z",
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": cmd}},
            ]
        },
    }


def _rec_tool_result(tool_id: str, output: str, session: str = "sess-1") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": "2026-06-01T10:00:02.000Z",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": output},
            ]
        },
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _obs_count(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        conn.close()


def _obs_contents(db_path: Path) -> list[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT content FROM observations ORDER BY id").fetchall()
        return [r["content"] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Protocol check
# ---------------------------------------------------------------------------


def test_source_adapter_protocol_satisfied(db_path, tmp_path):
    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    assert isinstance(src, SourceAdapter)


# ---------------------------------------------------------------------------
# Initial walk
# ---------------------------------------------------------------------------


def test_initial_walk_emits_one_obs_per_record(db_path, tmp_path):
    p = tmp_path / "sess-1.jsonl"
    _write_jsonl(
        p,
        [
            _rec_user("hello there"),
            _rec_user("second message"),
            _rec_user("third message"),
        ],
    )

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    count = src.initial_walk()

    assert count == 3
    assert _obs_count(db_path) == 3


def test_initial_walk_empty_dir_emits_zero(db_path, tmp_path):
    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    assert src.initial_walk() == 0
    assert _obs_count(db_path) == 0


def test_initial_walk_nonexistent_dir_emits_zero(db_path, tmp_path):
    missing = tmp_path / "no-such-dir"
    cfg = JsonlTranscriptConfig(paths=[missing])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    assert src.initial_walk() == 0


# ---------------------------------------------------------------------------
# Cursor: second run emits nothing new
# ---------------------------------------------------------------------------


def test_second_walk_emits_zero_when_no_new_lines(db_path, tmp_path):
    p = tmp_path / "sess-1.jsonl"
    _write_jsonl(p, [_rec_user("a"), _rec_user("b")])

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)

    first = src.initial_walk()
    assert first == 2

    second = src.initial_walk()
    assert second == 0
    assert _obs_count(db_path) == 2  # unchanged


# ---------------------------------------------------------------------------
# Append M lines → emit exactly M
# ---------------------------------------------------------------------------


def test_append_lines_emit_exactly_appended_count(db_path, tmp_path):
    p = tmp_path / "sess-1.jsonl"
    _write_jsonl(p, [_rec_user("first")])

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()

    _append_jsonl(p, [_rec_user("second"), _rec_user("third")])
    new = src.initial_walk()

    assert new == 2
    assert _obs_count(db_path) == 3


# ---------------------------------------------------------------------------
# New file in watched dir → starts at offset 0
# ---------------------------------------------------------------------------


def test_new_file_starts_at_offset_zero(db_path, tmp_path):
    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()  # empty dir, zero obs

    # New file appears after first walk
    p = tmp_path / "sess-new.jsonl"
    _write_jsonl(p, [_rec_user("new session message")])

    count = src.initial_walk()
    assert count == 1
    assert _obs_count(db_path) == 1


# ---------------------------------------------------------------------------
# Rotation: file replaced (new inode) → restart from byte 0
# ---------------------------------------------------------------------------


def test_rotation_new_inode_restarts_from_zero(db_path, tmp_path):
    p = tmp_path / "sess-1.jsonl"
    _write_jsonl(p, [_rec_user("original content")])

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()
    assert _obs_count(db_path) == 1

    # Simulate rotation: delete + recreate (new inode on most filesystems).
    p.unlink()
    _write_jsonl(p, [_rec_user("rotated new content"), _rec_user("second line")])

    count = src.initial_walk()
    assert count == 2
    assert _obs_count(db_path) == 3  # 1 original + 2 after rotation


# ---------------------------------------------------------------------------
# Tagging: kind, role, session, project all present in content
# ---------------------------------------------------------------------------


def test_tagging_user_obs(db_path, tmp_path):
    p = tmp_path / "sess-tagged.jsonl"
    _write_jsonl(
        p,
        [
            _rec_user("test prompt", session="session-abc", cwd="/home/user/myproject"),
        ],
    )

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()

    contents = _obs_contents(db_path)
    assert len(contents) == 1
    header = contents[0].split("\n")[0]
    assert "kind:transcript" in header
    assert "role:user" in header
    assert "session:session-abc" in header
    assert "project:myproject" in header


def test_tagging_tool_result_obs(db_path, tmp_path):
    p = tmp_path / "sess-tagged.jsonl"
    _write_jsonl(
        p,
        [
            _rec_tool_use("tu_1", "ls /tmp", session="session-xyz"),
            _rec_tool_result("tu_1", "file1\nfile2", session="session-xyz"),
        ],
    )

    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()

    contents = _obs_contents(db_path)
    assert len(contents) == 1
    header = contents[0].split("\n")[0]
    assert "kind:transcript" in header
    assert "role:tool_result" in header
    assert "session:session-xyz" in header


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_true_when_path_exists(db_path, tmp_path):
    cfg = JsonlTranscriptConfig(paths=[tmp_path])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    assert src.health() is True


def test_health_false_when_no_path_exists(db_path, tmp_path):
    missing = tmp_path / "ghost"
    cfg = JsonlTranscriptConfig(paths=[missing])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    assert src.health() is False


# ---------------------------------------------------------------------------
# Config: cursor persisted in extension_state
# ---------------------------------------------------------------------------


def test_cursor_persisted_in_extension_state(db_path, tmp_path):
    p = tmp_path / "sess-1.jsonl"
    _write_jsonl(p, [_rec_user("hello")])

    cfg = JsonlTranscriptConfig(paths=[tmp_path], adapter_name="test_adapter")
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    src.initial_walk()

    conn = connect(db_path)
    try:
        state = get_state(conn, "test_adapter")
    finally:
        conn.close()

    assert state is not None
    cursors = state.get("cursors", {})
    assert str(p) in cursors
    assert cursors[str(p)]["offset"] > 0


# ---------------------------------------------------------------------------
# Config parsing from yaml dict
# ---------------------------------------------------------------------------


def test_config_parses_sources_block():
    from techne.config import Config

    raw = {
        "sources": {
            "claude_code_transcripts": {
                "paths": ["~/.claude/projects/-Users-me/"],
                "include_globs": ["*.jsonl"],
                "exclude_globs": ["*/tool-results/*"],
                "debounce_ms": 750,
            }
        }
    }
    cfg = Config._from_dict(raw)
    assert cfg.sources.claude_code_transcripts is not None
    tc = cfg.sources.claude_code_transcripts
    assert tc.paths == ["~/.claude/projects/-Users-me/"]
    assert tc.include_globs == ["*.jsonl"]
    assert tc.exclude_globs == ["*/tool-results/*"]
    assert tc.debounce_ms == 750


def test_config_sources_absent_gives_none():
    from techne.config import Config

    cfg = Config._from_dict({})
    assert cfg.sources.claude_code_transcripts is None


# ---------------------------------------------------------------------------
# Include/exclude glob filtering
# ---------------------------------------------------------------------------


def test_include_glob_filters_non_jsonl(db_path, tmp_path):
    (tmp_path / "notes.txt").write_text("not a jsonl file")
    (tmp_path / "sess.jsonl").write_text(json.dumps(_rec_user("hi")) + "\n")

    cfg = JsonlTranscriptConfig(paths=[tmp_path], include_globs=["*.jsonl"])
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    count = src.initial_walk()
    assert count == 1  # only the .jsonl file


def test_exclude_glob_filters_matching_files(db_path, tmp_path):
    subdir = tmp_path / "tool-results"
    subdir.mkdir()
    (subdir / "ignored.jsonl").write_text(json.dumps(_rec_user("should be ignored")) + "\n")
    (tmp_path / "valid.jsonl").write_text(json.dumps(_rec_user("should be indexed")) + "\n")

    cfg = JsonlTranscriptConfig(
        paths=[tmp_path],
        include_globs=["*.jsonl"],
        exclude_globs=["*/tool-results/*"],
    )
    src = JsonlTranscriptSource(db_path=db_path, config=cfg)
    count = src.initial_walk()
    assert count == 1  # excluded subdir file skipped
