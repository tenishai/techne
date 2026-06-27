"""Tests for techne.core.transcript_import — JSONL backfill."""

import json
from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.platforms.claude_code import import_directory, import_transcript
from techne.platforms.claude_code.transcript_import import (
    default_transcripts_dir,
    project_slug_for_cwd,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _bash_use(tool_id: str, cmd: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": cmd}},
            ],
        },
    }


def _tool_result(tool_id: str, output: str, ts: str) -> dict:
    return {
        "type": "user",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}]
        },
    }


def _user_text(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "cwd": "/some/project",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _ai_title(title: str) -> dict:
    return {"type": "ai-title", "aiTitle": title, "sessionId": "sess-fixture"}


@pytest.fixture
def transcript(tmp_path):
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "mode", "mode": "normal", "sessionId": "sess-fixture"},
            _user_text("Hello, please run ls", "2026-06-01T10:00:00.000Z"),
            _bash_use("tu_1", "ls /tmp", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "file1\nfile2", "2026-06-01T10:00:02.000Z"),
            _user_text("Now check disk space", "2026-06-01T10:00:10.000Z"),
            _bash_use("tu_2", "df -h", "2026-06-01T10:00:11.000Z"),
            _tool_result("tu_2", "Filesystem  Size  Used", "2026-06-01T10:00:12.000Z"),
        ],
    )
    return p


def test_imports_tool_calls_and_prompts(db_path, transcript):
    result = import_transcript(db_path, transcript)

    assert result["skipped"] is False
    assert result["session_id"] == "sess-fixture"
    assert result["project_path"] == "/some/project"
    assert result["tool_obs"] == 2
    assert result["prompt_obs"] == 2

    conn = connect(db_path)
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id='sess-fixture'").fetchone()
        assert sess is not None
        assert sess["project_path"] == "/some/project"
        assert sess["started_at"].startswith("2026-06-01T10:00:00")
        assert sess["ended_at"].startswith("2026-06-01T10:00:12")

        rows = conn.execute(
            "SELECT tool_name, content, timestamp FROM observations "
            "WHERE session_id='sess-fixture' ORDER BY timestamp"
        ).fetchall()
        names = [r["tool_name"] for r in rows]
        assert names == ["UserPrompt", "Bash", "UserPrompt", "Bash"]
        # Bash content is run through the live summarize() formatter
        assert "ls /tmp" in rows[1]["content"]
        assert "file1" in rows[1]["content"]
    finally:
        conn.close()


def test_idempotent_on_second_import(db_path, transcript):
    import_transcript(db_path, transcript)
    second = import_transcript(db_path, transcript)

    assert second["skipped"] is True
    assert "already has" in second["reason"]

    conn = connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id='sess-fixture'"
        ).fetchone()[0]
        assert count == 4  # 2 prompts + 2 tool calls — not doubled
    finally:
        conn.close()


def test_no_prompts_flag(db_path, transcript):
    result = import_transcript(db_path, transcript, include_user_prompts=False)
    assert result["tool_obs"] == 2
    assert result["prompt_obs"] == 0


def test_import_directory_skips_empty(db_path, tmp_path):
    _write_jsonl(
        tmp_path / "good.jsonl",
        [
            _bash_use("tu_x", "echo hi", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_x", "hi", "2026-06-01T10:00:01.000Z"),
        ],
    )
    (tmp_path / "empty.jsonl").write_text("")

    results = import_directory(db_path, tmp_path)
    assert len(results) == 2

    by_skip = {r["skipped"]: r for r in results}
    assert by_skip[False]["tool_obs"] == 1
    assert by_skip[True]["reason"] == "empty"


def test_ai_title_stamps_summary_llm(db_path, tmp_path):
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _ai_title("Review fleet-terminal.md file"),
            _bash_use("tu_a", "ls", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_a", "ok", "2026-06-01T10:00:01.000Z"),
            _ai_title("Review fleet-terminal.md file"),  # re-emission, same value
        ],
    )

    result = import_transcript(db_path, p)
    assert result["summary_set"] is True

    conn = connect(db_path)
    try:
        sess = conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id='sess-fixture'"
        ).fetchone()
        assert sess["summary_llm"] == "Review fleet-terminal.md file"
        # Manual + heuristic slots untouched
        assert sess["summary"] is None
        assert sess["summary_heuristic"] is None
    finally:
        conn.close()


def test_summary_backfills_on_reimport(db_path, tmp_path):
    # First import had no ai-title; later transcript has one.
    # Re-running should stamp summary_llm even though observations are skipped.
    p1 = tmp_path / "no-title.jsonl"
    _write_jsonl(
        p1,
        [
            _bash_use("tu_a", "ls", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_a", "ok", "2026-06-01T10:00:01.000Z"),
        ],
    )
    first = import_transcript(db_path, p1)
    assert first["summary_set"] is False
    assert first["skipped"] is False

    p2 = tmp_path / "with-title.jsonl"
    _write_jsonl(
        p2,
        [
            _ai_title("Check techne status"),
            _bash_use("tu_a", "ls", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_a", "ok", "2026-06-01T10:00:01.000Z"),
        ],
    )
    second = import_transcript(db_path, p2)
    assert second["skipped"] is True
    assert second["summary_set"] is True

    conn = connect(db_path)
    try:
        sess = conn.execute("SELECT summary_llm FROM sessions WHERE id='sess-fixture'").fetchone()
        assert sess["summary_llm"] == "Check techne status"
    finally:
        conn.close()


def test_ai_title_coexists_with_heuristic_summary(db_path, tmp_path):
    # Heuristic summary lives in summary_heuristic; ai-title goes to summary_llm.
    # Neither clobbers the other.
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _ai_title("Review fleet-terminal.md file"),
            _bash_use("tu_a", "ls", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_a", "ok", "2026-06-01T10:00:01.000Z"),
        ],
    )
    # Pre-create with a heuristic summary already populated
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions (id, summary_heuristic, platform)
               VALUES ('sess-fixture', '**Thread 1: ...** breakdown', 'claude-code')"""
        )
        conn.commit()
    finally:
        conn.close()

    import_transcript(db_path, p)

    conn = connect(db_path)
    try:
        sess = conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id='sess-fixture'"
        ).fetchone()
        assert sess["summary_llm"] == "Review fleet-terminal.md file"
        assert sess["summary_heuristic"] == "**Thread 1: ...** breakdown"
        assert sess["summary"] is None
    finally:
        conn.close()


def test_effective_summary_priority():
    from techne.core.state import effective_summary

    # manual wins
    assert (
        effective_summary({"summary": "manual", "summary_llm": "llm", "summary_heuristic": "h"})
        == "manual"
    )
    # LLM beats heuristic
    assert (
        effective_summary({"summary": None, "summary_llm": "llm", "summary_heuristic": "h"})
        == "llm"
    )
    # heuristic is the floor
    assert effective_summary({"summary": "", "summary_llm": None, "summary_heuristic": "h"}) == "h"
    # nothing → empty
    assert (
        effective_summary({"summary": None, "summary_llm": None, "summary_heuristic": None}) == ""
    )


def test_looks_heuristic_patterns():
    from techne.core.state import looks_heuristic

    assert looks_heuristic("**Thread 1: Work on infra** — 126 observations")
    assert looks_heuristic("80 observations; tools: bash(24), filesystem(24)")
    assert looks_heuristic("some prefix\n## Resume Points\n\n### foo")
    # Negatives — should NOT misclassify
    assert not looks_heuristic("")
    assert not looks_heuristic("Review fleet-terminal.md file")  # LLM-style title
    assert not looks_heuristic("Manually written summary about a refactor")
    assert not looks_heuristic("Just discussing observations in prose")  # word not pattern


def test_migrate_legacy_summaries_moves_heuristic_shapes(db_path):
    from techne.core.state import migrate_legacy_summaries

    heuristic_multi = "**Thread 1: pane.txt** — 126 observations\n\n**Thread 2: db** — 60 obs"
    heuristic_single = "65 observations; tools: bash(24), filesystem(24)"
    manual = "Reviewed the home network setup yesterday."
    llm_title = "Review fleet-terminal.md file"

    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sessions (id, summary, platform) VALUES (?, ?, 'claude-code')",
            [
                ("multi", heuristic_multi),
                ("single", heuristic_single),
                ("manual", manual),
                ("llm-like", llm_title),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    # Dry-run first — counts but doesn't change anything
    dry = migrate_legacy_summaries(db_path, dry_run=True)
    assert dry["moved"] == 2
    assert dry["cleared"] == 0
    assert dry["scanned"] == 4
    conn = connect(db_path)
    try:
        # nothing actually moved
        row = conn.execute(
            "SELECT summary, summary_heuristic FROM sessions WHERE id='multi'"
        ).fetchone()
        assert row["summary"] == heuristic_multi
        assert row["summary_heuristic"] is None
    finally:
        conn.close()

    # Real run
    result = migrate_legacy_summaries(db_path)
    assert result["moved"] == 2
    assert result["skipped"] == 2

    conn = connect(db_path)
    try:
        rows = {
            r["id"]: r
            for r in conn.execute("SELECT id, summary, summary_heuristic FROM sessions").fetchall()
        }
        # heuristic-shaped → moved
        assert rows["multi"]["summary"] is None
        assert rows["multi"]["summary_heuristic"] == heuristic_multi
        assert rows["single"]["summary"] is None
        assert rows["single"]["summary_heuristic"] == heuristic_single
        # non-heuristic → untouched
        assert rows["manual"]["summary"] == manual
        assert rows["manual"]["summary_heuristic"] is None
        assert rows["llm-like"]["summary"] == llm_title
        assert rows["llm-like"]["summary_heuristic"] is None
    finally:
        conn.close()


def test_migrate_legacy_summaries_clears_legacy_when_fresher_exists(db_path):
    """When summary_heuristic already has a fresher value, the legacy
    heuristic-shaped `summary` is discarded — it's the same kind of content,
    older, and was only blocking the priority read."""
    from techne.core.state import migrate_legacy_summaries

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions (id, summary, summary_heuristic, platform)
               VALUES ('s', '**Thread 1: legacy** — old', 'fresher heuristic content', 'claude-code')"""
        )
        conn.commit()
    finally:
        conn.close()

    result = migrate_legacy_summaries(db_path)
    assert result["moved"] == 0
    assert result["cleared"] == 1
    assert result["skipped"] == 0

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT summary, summary_heuristic FROM sessions WHERE id='s'"
        ).fetchone()
        # Legacy cleared; fresher heuristic preserved
        assert row["summary"] is None
        assert row["summary_heuristic"] == "fresher heuristic content"
    finally:
        conn.close()


def test_end_session_rejects_unknown_source(db_path):
    """Unknown source must raise — silently routing to 'manual' would corrupt
    the highest-priority summary slot when a typo or new enum case slips in."""
    from techne.core.state import end_session, set_session_summary

    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO sessions (id, platform) VALUES ('s', 'claude-code')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="unknown summary source"):
        end_session(db_path, "s", summary="x", source="auto")
    with pytest.raises(ValueError, match="unknown summary source"):
        set_session_summary(db_path, "s", "x", source="parent-model")


def test_end_session_routes_by_source(db_path):
    from techne.core.state import end_session

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, platform) VALUES ('s1', 'claude-code'), ('s2', 'claude-code'), ('s3', 'claude-code')"
        )
        conn.commit()
    finally:
        conn.close()

    end_session(db_path, "s1", summary="manual text")
    end_session(db_path, "s2", summary="llm text", source="llm")
    end_session(db_path, "s3", summary="heuristic text", source="heuristic")

    conn = connect(db_path)
    try:
        s1 = conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id='s1'"
        ).fetchone()
        s2 = conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id='s2'"
        ).fetchone()
        s3 = conn.execute(
            "SELECT summary, summary_llm, summary_heuristic FROM sessions WHERE id='s3'"
        ).fetchone()
        assert s1["summary"] == "manual text" and s1["summary_llm"] is None
        assert s2["summary_llm"] == "llm text" and s2["summary"] is None
        assert s3["summary_heuristic"] == "heuristic text" and s3["summary"] is None
    finally:
        conn.close()


def test_project_slug_for_cwd(tmp_path):
    # techne#167: delegates to the single authoritative paths.project_slug, so the
    # two formerly-divergent slug functions can no longer disagree on the same cwd.
    from techne.platforms.claude_code.paths import project_slug

    assert project_slug_for_cwd(tmp_path) == project_slug(tmp_path)
    # A path with no symlink prefix on either host resolves unchanged → exact slug.
    assert project_slug_for_cwd(Path("/Users/alice/working/proj")) == "-Users-alice-working-proj"
    assert project_slug_for_cwd(Path("/")) == "-"


def test_project_slug_replaces_all_non_alphanumerics(tmp_path):
    """techne#167: CC's kS regex is /[^a-zA-Z0-9]/g — dots, underscores, and spaces
    in the leaf all become '-', not just '/'. tmp_path is a real (resolved) dir so
    the realpath is concrete; we append a non-existent leaf so resolve() leaves it
    appended verbatim, then check the formula reproduces it."""
    import re

    weird = tmp_path / "my.proj_v2 final"
    expected = re.sub(r"[^a-zA-Z0-9]", "-", str(tmp_path.resolve()) + "/my.proj_v2 final")
    assert project_slug_for_cwd(weird) == expected
    # Concretely: dot, underscore, and space in the leaf all collapsed to '-'.
    assert project_slug_for_cwd(weird).endswith("-my-proj-v2-final")


def test_default_transcripts_dir_under_home(tmp_path):
    d = default_transcripts_dir(Path("/Users/alice/x"))
    assert d.name == "-Users-alice-x"
    assert d.parent.name == "projects"
    assert d.parent.parent.name == ".claude"


def test_angle_prefix_filter_suppresses_injected_blocks(db_path, tmp_path):
    """`<techne_context>...` and similar injected blocks must NOT become
    UserPrompt observations. Regression guard: if the injection prefix ever
    changes, this test breaks rather than silently flooding memory with the
    full system-reminder text."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _user_text(
                "<techne_context>\nsome injected state\n</techne_context>",
                "2026-06-01T10:00:00.000Z",
            ),
            _user_text("<system-reminder>do X</system-reminder>", "2026-06-01T10:00:01.000Z"),
        ],
    )

    result = import_transcript(db_path, p)
    assert result["prompt_obs"] == 0


def test_inline_angle_is_captured(db_path, tmp_path):
    """Only a leading `<` suppresses — text that merely contains `<` mid-string
    (e.g. a comparison) is a real prompt and must be captured."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _user_text("verify that x < y for small inputs", "2026-06-01T10:00:00.000Z"),
        ],
    )

    result = import_transcript(db_path, p)
    assert result["prompt_obs"] == 1


def test_plain_text_assistant_turn_captured_as_assistant_text(db_path, tmp_path):
    """Assistant records that are pure prose (no tool_use) must produce an
    AssistantText observation. The previous dead-elif silently dropped them."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "sessionId": "sess-fixture",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {
                    "content": [
                        {"type": "text", "text": "Here is my reasoning about the change."},
                    ],
                },
            },
        ],
    )

    result = import_transcript(db_path, p)
    assert result["assistant_text_obs"] == 1
    assert result["tool_obs"] == 0
    assert result["prompt_obs"] == 0

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tool_name, content FROM observations WHERE session_id='sess-fixture'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "AssistantText"
        assert "reasoning about the change" in rows[0]["content"]
    finally:
        conn.close()


def test_assistant_text_alongside_tool_use_both_captured(db_path, tmp_path):
    """Mixed assistant record (text + tool_use) emits BOTH an AssistantText
    observation and the eventual tool observation when the tool result lands."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "sessionId": "sess-fixture",
                "timestamp": "2026-06-01T10:00:00.000Z",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check the directory."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "ls /tmp"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "sess-fixture",
                "timestamp": "2026-06-01T10:00:01.000Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "file1\nfile2"},
                    ],
                },
            },
        ],
    )

    result = import_transcript(db_path, p)
    assert result["assistant_text_obs"] == 1
    assert result["tool_obs"] == 1


def test_text_alongside_tool_result_is_captured(db_path, tmp_path):
    """When a user message contains BOTH a tool_result and a text block
    (Claude Code emits this when a tool is cancelled and the user follows up),
    the text block must still become a UserPrompt observation. The previous
    `paired_any` short-circuit silently dropped this text."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "long-running cmd", "2026-06-01T10:00:00.000Z"),
            {
                "type": "user",
                "sessionId": "sess-fixture",
                "timestamp": "2026-06-01T10:00:01.000Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "cancelled"},
                        {"type": "text", "text": "skip that — let's do something else"},
                    ],
                },
            },
        ],
    )

    result = import_transcript(db_path, p)
    assert result["tool_obs"] == 1
    assert result["prompt_obs"] == 1

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tool_name, content FROM observations "
            "WHERE session_id='sess-fixture' ORDER BY id"
        ).fetchall()
        names = [r["tool_name"] for r in rows]
        assert "Bash" in names
        assert "UserPrompt" in names
        prompt = next(r for r in rows if r["tool_name"] == "UserPrompt")
        assert "skip that" in prompt["content"]
    finally:
        conn.close()


def test_force_bypasses_existing_observations_gate(db_path, tmp_path):
    """The idempotency gate skips sessions that already have ANY observations,
    which conflates live-captured and previously-imported. `force=True` is the
    escape hatch when live capture was partial and the full historical
    transcript needs to land on top."""
    # Pre-populate the session with a single live-captured observation.
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO sessions (id, platform) VALUES ('sess-fixture', 'claude-code')")
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES ('sess-fixture', '2026-06-01T09:59:00.000Z', 'Bash', 'pre-existing')"
        )
        conn.commit()
    finally:
        conn.close()

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "ls /tmp", "2026-06-01T10:00:00.000Z"),
            _tool_result("tu_1", "file1", "2026-06-01T10:00:01.000Z"),
        ],
    )

    # Default behavior: skipped, no new obs
    default_result = import_transcript(db_path, p)
    assert default_result["skipped"] is True
    assert default_result["tool_obs"] == 0

    # With force: not skipped, transcript obs added on top
    forced = import_transcript(db_path, p, force=True)
    assert forced["skipped"] is False
    assert forced["tool_obs"] == 1

    conn = connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id='sess-fixture'"
        ).fetchone()[0]
        assert count == 2  # 1 pre-existing + 1 force-imported
    finally:
        conn.close()


def test_event_at_populated_from_transcript_timestamp(db_path, tmp_path):
    """JSONL ingest threads transcript timestamps through as event_at on observations."""
    p = tmp_path / "sess-ts.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_ts", "echo hi", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_ts", "hi", "2026-06-01T10:00:02.000Z"),
            _user_text("check something", "2026-06-01T10:00:05.000Z"),
        ],
    )

    result = import_transcript(db_path, p)
    assert result["tool_obs"] == 1
    assert result["prompt_obs"] == 1

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT tool_name, event_at FROM observations ORDER BY id").fetchall()
        # Both tool obs and prompt obs must have event_at set from transcript ts.
        for row in rows:
            assert row["event_at"] is not None, f"{row['tool_name']} missing event_at"
        tool_row = next(r for r in rows if r["tool_name"] == "Bash")
        prompt_row = next(r for r in rows if r["tool_name"] == "UserPrompt")
        # Tool event_at = assistant ts (tu["ts"]), prompt event_at = user record ts.
        assert "2026-06-01T10:00:01" in tool_row["event_at"]
        assert "2026-06-01T10:00:05" in prompt_row["event_at"]
    finally:
        conn.close()


def test_unpaired_tool_use_does_not_crash(db_path, tmp_path):
    p = tmp_path / "orphan.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_orphan", "ls", "2026-06-01T10:00:00.000Z"),
            # No matching tool_result follows
        ],
    )
    result = import_transcript(db_path, p)
    assert result["tool_obs"] == 0
    assert result["skipped"] is False
