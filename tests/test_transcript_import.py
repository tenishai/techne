"""Tests for techne.core.transcript_import — JSONL backfill."""

import gzip
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
            _user_text("Hello, please run the tests", "2026-06-01T10:00:00.000Z"),
            _bash_use("tu_1", "pytest tests/", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "file1\nfile2", "2026-06-01T10:00:02.000Z"),
            _user_text("Now commit it", "2026-06-01T10:00:10.000Z"),
            _bash_use("tu_2", "git commit -m wip", "2026-06-01T10:00:11.000Z"),
            _tool_result("tu_2", "Filesystem  Size  Used", "2026-06-01T10:00:12.000Z"),
        ],
    )
    return p


def _bash_use_no_id(cmd: str, ts: str) -> dict:
    """An assistant tool_use block WITHOUT an id (the techne#170 edge)."""
    return {
        "type": "assistant",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]},
    }


def _tool_result_no_id(output: str, ts: str) -> dict:
    return {
        "type": "user",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "content": output}]},
    }


def test_idless_tool_use_skipped_no_clobber(db_path, tmp_path):
    """techne#170: tool_use blocks without an id can't be matched to a result. The old
    `b.get("id", "")` fallback keyed every id-less block on "" so they clobbered each
    other, and a stray id-less tool_result would pop the wrong one — importing a tool
    call under the wrong command. They must be skipped instead."""
    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use_no_id("rm -rf /a", "2026-06-01T10:00:00.000Z"),
            _bash_use_no_id("rm -rf /b", "2026-06-01T10:00:01.000Z"),  # clobbered "" on old code
            _tool_result_no_id("done", "2026-06-01T10:00:02.000Z"),  # popped "" on old code
            _bash_use("tu_real", "pytest -q", "2026-06-01T10:00:03.000Z"),
            _tool_result("tu_real", "file1", "2026-06-01T10:00:04.000Z"),
        ],
    )
    import_transcript(db_path, p)

    conn = connect(db_path)
    try:
        contents = "\n".join(
            r[0]
            for r in conn.execute(
                "SELECT content FROM observations WHERE content LIKE '%Ran:%'"
            ).fetchall()
        )
    finally:
        conn.close()
    assert "pytest -q" in contents  # the real, id-bearing tool call imported
    assert "rm -rf" not in contents  # the id-less ones did NOT (no phantom)


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
        # #515: the importer now normalises to the canonical space-separated shape.
        assert sess["started_at"].startswith("2026-06-01 10:00:00")
        assert sess["ended_at"].startswith("2026-06-01 10:00:12")

        rows = conn.execute(
            "SELECT tool_name, content, timestamp FROM observations "
            "WHERE session_id='sess-fixture' ORDER BY timestamp"
        ).fetchall()
        names = [r["tool_name"] for r in rows]
        assert names == ["UserPrompt", "Bash", "UserPrompt", "Bash"]
        # Bash content is run through the live summarize() formatter
        assert "pytest tests/" in rows[1]["content"]
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
            _bash_use("tu_x", "make build", "2026-06-01T10:00:00.000Z"),
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
            _bash_use("tu_a", "git status", "2026-06-01T10:00:00.000Z"),
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
            _bash_use("tu_a", "git status", "2026-06-01T10:00:00.000Z"),
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
    """The slug and the `projects/` level, NOT the config dir's literal name.

    This asserted `.parent.parent.name == ".claude"`, which is only true when
    CLAUDE_CONFIG_DIR is unset or happens to end in `.claude` — so it failed on
    exactly the layout techne#112 exists to support, one config dir per agent
    (`.../prudence/claude`). It read as an ambient-environment flake and was not:
    it was the test hardcoding one deployment's directory name. Derive the
    expected root from the same resolver the code uses. (techne#477.)
    """
    from techne.platforms.claude_code.transcript_import import PROJECTS_DIR

    d = default_transcripts_dir(Path("/Users/alice/x"))
    assert d.name == "-Users-alice-x"
    assert d.parent.name == "projects"
    assert d.parent == PROJECTS_DIR


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
                        {"type": "text", "text": "Let me run the tests."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
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
            _bash_use("tu_1", "pytest -q", "2026-06-01T10:00:00.000Z"),
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
            _bash_use("tu_ts", "make test", "2026-06-01T10:00:01.000Z"),
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
        # #515: stored in the canonical shape, not the JSONL's ISO form.
        assert "2026-06-01 10:00:01" in tool_row["event_at"]
        assert "2026-06-01 10:00:05" in prompt_row["event_at"]
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


# ---------------------------------------------------------------------------
# backfill_uncaptured — session_end gap-fill (techne#734)
# ---------------------------------------------------------------------------


def _cfg(db_path, *, capture_bash=False, capture_read=False):
    """A Config pointed at the test db with the given capture flags."""
    from techne.config import Config

    cfg = Config()
    cfg.db_path = db_path
    cfg.hooks.capture_bash = capture_bash
    cfg.hooks.capture_read = capture_read
    return cfg


def _drop_backfill_cursor(db_path, session_id):
    """Delete backfill_uncaptured's resume cursor, forcing the next run to rescan."""
    from techne.platforms.claude_code.transcript_import import BACKFILL_CURSOR_PREFIX

    conn = connect(db_path)
    try:
        conn.execute(
            "DELETE FROM extension_state WHERE name = ?", (BACKFILL_CURSOR_PREFIX + session_id,)
        )
        conn.commit()
    finally:
        conn.close()


def _tool_use(tool_id, name, tool_input, ts):
    return {
        "type": "assistant",
        "sessionId": "sess-fixture",
        "timestamp": ts,
        "message": {
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]
        },
    }


def test_backfill_imports_gap_tools_only(db_path, tmp_path):
    """Gap-fill imports the tools the live hook skipped (Bash/Read), drops
    read-only Bash noise, and never touches live-captured tools like Edit."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    # Simulate a live-captured Edit already in the ledger.
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO sessions (id, platform) VALUES ('sess-fixture', 'claude-code')")
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES ('sess-fixture', '2026-06-01T10:00:00.000Z', 'Edit', 'Edited: foo.py')"
        )
        conn.commit()
    finally:
        conn.close()

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "git commit -m wip", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "1 file changed", "2026-06-01T10:00:02.000Z"),
            _bash_use("tu_2", "ls -la", "2026-06-01T10:00:03.000Z"),  # read-only noise
            _tool_result("tu_2", "file1\nfile2", "2026-06-01T10:00:04.000Z"),
            _tool_use("tu_3", "Read", {"file_path": "bar.py"}, "2026-06-01T10:00:05.000Z"),
            _tool_result("tu_3", "contents", "2026-06-01T10:00:06.000Z"),
        ],
    )

    result = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert result["backfilled"] == 2  # git commit + Read; ls dropped
    assert set(result["gap_tools"]) == {"Bash", "Read"}

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tool_name, content FROM observations WHERE session_id='sess-fixture'"
        ).fetchall()
        contents = "\n".join(r["content"] for r in rows)
        names = sorted(r["tool_name"] for r in rows)
        assert names == ["Bash", "Edit", "Read"]  # exactly one of each, ls excluded
        assert "git commit -m wip" in contents
        assert "ls -la" not in contents
    finally:
        conn.close()


def test_backfill_is_idempotent(db_path, tmp_path):
    """A second gap-fill (e.g. session_end after a compact) inserts nothing new."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "pytest -q", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "2 passed", "2026-06-01T10:00:02.000Z"),
        ],
    )

    first = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    second = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert first["backfilled"] == 1
    assert second["backfilled"] == 0
    # The second run resumes past the row from the cursor (issue 1218), so it never
    # sees it. A run without the cursor (any reset) must dedup instead.
    assert second["skipped_existing"] == 0
    _drop_backfill_cursor(db_path, "sess-fixture")
    third = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert third["backfilled"] == 0
    assert third["skipped_existing"] == 1

    conn = connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id='sess-fixture' AND tool_name='Bash'"
        ).fetchone()[0]
        assert count == 1  # not doubled
    finally:
        conn.close()


def test_backfill_noop_when_captured_live(db_path, tmp_path):
    """If capture_bash/read are ON, the live hook already has those tools — gap
    is empty, so backfill inserts nothing (avoids duplicates)."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "git status", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "clean", "2026-06-01T10:00:02.000Z"),
        ],
    )
    result = backfill_uncaptured(
        _cfg(db_path, capture_bash=True, capture_read=True), "sess-fixture", jsonl_path=p
    )
    assert result["backfilled"] == 0
    assert result["gap_tools"] == []
    assert "all gap tools captured live" in result["reason"]


def test_backfill_missing_transcript(db_path, tmp_path):
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    result = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=tmp_path / "nope.jsonl")
    assert result["backfilled"] == 0
    assert "transcript not found" in result["reason"]


# --- archived (.gz) transcripts (#621) -------------------------------------


class TestArchivedTranscripts:
    def test_session_id_for_both_spellings(self):
        from techne.platforms.claude_code.transcript_import import session_id_for

        assert session_id_for(Path("/x/sess-abc.jsonl")) == "sess-abc"
        assert session_id_for(Path("/x/sess-abc.jsonl.gz")) == "sess-abc"
        # a uuid session id has dots in nothing, but the guard must not eat one
        assert session_id_for(Path("/x/a.b.jsonl.gz")) == "a.b"

    def test_iter_transcript_files_prefers_plain_and_sorts(self, tmp_path):
        from techne.platforms.claude_code.transcript_import import iter_transcript_files

        (tmp_path / "b.jsonl").write_text("{}")
        with gzip.open(tmp_path / "a.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("{}")
        # 'c' is mid-archive: both spellings on disk
        (tmp_path / "c.jsonl").write_text("{}")
        with gzip.open(tmp_path / "c.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("{}")
        (tmp_path / "notes.md").write_text("ignore me")

        files = iter_transcript_files(tmp_path)

        assert [f.name for f in files] == ["a.jsonl.gz", "b.jsonl", "c.jsonl"]

    def test_import_directory_imports_an_archived_transcript(self, db_path, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        recs = [
            _user_text("Hello, please run the tests", "2026-06-01T10:00:00.000Z"),
            _bash_use("tu_1", "pytest tests/", "2026-06-01T10:00:01.000Z"),
            _tool_result("tu_1", "2 passed", "2026-06-01T10:00:02.000Z"),
        ]
        with gzip.open(d / "sess-fixture.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")

        results = import_directory(db_path, d)

        assert len(results) == 1
        assert results[0]["session_id"] == "sess-fixture"
        assert not results[0].get("skipped")

    def test_session_id_falls_back_to_the_filename_for_either_spelling(self, db_path, tmp_path):
        """The fallback at import_transcript fires only when NO record carries a
        sessionId — every other fixture in this file sets one, which switches the
        guard off. For a .gz, `.stem` would leave the ".jsonl" on and land the same
        session twice in sessions.id and every observations.session_id."""
        d = tmp_path / "proj"
        d.mkdir()
        recs = [  # deliberately no sessionId on any record
            {
                "type": "assistant",
                "timestamp": "2026-06-01T10:00:01.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "pytest tests/"},
                        }
                    ]
                },
            },
        ]
        _write_jsonl(d / "plain-sess.jsonl", recs)
        with gzip.open(d / "gz-sess.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")

        import_directory(db_path, d)

        conn = connect(db_path)
        try:
            ids = sorted(r[0] for r in conn.execute("SELECT id FROM sessions").fetchall())
        finally:
            conn.close()
        assert ids == ["gz-sess", "plain-sess"]

    def test_session_jsonl_resolves_an_archived_transcript(self, tmp_path, monkeypatch):
        """Session recovery is one of the three glob sites #621 named — without this
        it is the third of the fix that could be deleted without a sound."""
        from techne.platforms.claude_code import transcript_import as ti

        projects = tmp_path / "projects"
        (projects / "slug").mkdir(parents=True)
        monkeypatch.setattr(ti, "PROJECTS_DIR", projects)

        (projects / "slug" / "archived-only.jsonl.gz").write_bytes(gzip.compress(b"{}"))
        assert ti._session_jsonl("archived-only").name == "archived-only.jsonl.gz"
        assert ti._session_jsonl("nope") is None

        # both spellings present: the uncompressed file wins
        (projects / "slug" / "both.jsonl").write_text("{}")
        (projects / "slug" / "both.jsonl.gz").write_bytes(gzip.compress(b"{}"))
        assert ti._session_jsonl("both").name == "both.jsonl"

    def test_session_id_for_rejects_a_non_transcript(self):
        from techne.platforms.claude_code.transcript_import import session_id_for

        with pytest.raises(ValueError):
            session_id_for(Path("/x/notes.md"))

    def test_a_corrupt_archive_is_reported_not_fatal(self, db_path, tmp_path):
        """A truncated .gz must not take the whole directory import down with it —
        import_directory records it as a skip with a reason, and the good file lands."""
        d = tmp_path / "proj"
        d.mkdir()
        (d / "bad.jsonl.gz").write_bytes(b"\x1f\x8b\x08 not actually gzip")
        _write_jsonl(
            d / "sess-fixture.jsonl",
            [_user_text("hi", "2026-06-01T10:00:00.000Z")],
        )

        results = import_directory(db_path, d)

        by_skip = {r.get("session_id"): r for r in results}
        assert by_skip["bad"]["skipped"] is True
        assert "error" in by_skip["bad"]["reason"]
        assert not by_skip["sess-fixture"].get("skipped")


# ---------------------------------------------------------------------------
# techne#632 — `_session_jsonl` is exercised directly, but its use INSIDE
# `backfill_uncaptured` (i.e. the no-jsonl_path branch) was never covered.
# ---------------------------------------------------------------------------


def _bash_transcript_rows(session_id: str, command: str = "pytest -q"):
    """A minimal transcript containing one Bash tool_use — a gap tool when
    capture_bash is off, which is what backfill exists to reclaim."""
    return [
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/tmp/proj",
            "timestamp": "2026-08-18T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        # The PAIRED result. backfill writes on the tool_use/tool_result pair, not
        # on the call alone — a tool_use with no result is an unfinished call.
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": "/tmp/proj",
            "timestamp": "2026-08-18T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "1 passed",
                    }
                ],
            },
        },
    ]


def test_backfill_finds_the_transcript_itself_when_no_path_is_given(tmp_path, monkeypatch):
    """The end-to-end path #632 names: backfill resolving its OWN transcript.

    Every existing test hands `jsonl_path=` in, so the `_session_jsonl(session_id)`
    branch — the one production actually takes at session_end — had no coverage.
    """
    import json as _json

    from techne.config import Config
    from techne.core.database import init_db
    from techne.platforms.claude_code import transcript_import as ti

    session_id = "cccccccc-1111-2222-3333-444444444444"
    projects = tmp_path / "projects"
    slug = projects / "-tmp-proj"
    slug.mkdir(parents=True)
    (slug / f"{session_id}.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in _bash_transcript_rows(session_id)) + "\n"
    )
    monkeypatch.setattr(ti, "PROJECTS_DIR", projects)

    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    cfg.hooks.capture_bash = False  # so Bash is a gap tool worth backfilling

    out = ti.backfill_uncaptured(cfg, session_id)  # NO jsonl_path — the point

    assert out.get("reason") != "transcript not found", (
        "backfill could not locate its own transcript via _session_jsonl (#632)"
    )
    assert out["backfilled"] >= 1, "the Bash row was not reclaimed"


def test_backfill_resolves_an_archived_transcript_end_to_end(tmp_path, monkeypatch):
    """#621's `.jsonl.gz` resolution, driven through backfill rather than through
    `_session_jsonl` alone — the combination is what production runs."""
    import gzip as _gzip
    import json as _json

    from techne.config import Config
    from techne.core.database import init_db
    from techne.platforms.claude_code import transcript_import as ti

    session_id = "dddddddd-1111-2222-3333-444444444444"
    projects = tmp_path / "projects"
    slug = projects / "-tmp-proj"
    slug.mkdir(parents=True)
    raw = "\n".join(_json.dumps(r) for r in _bash_transcript_rows(session_id, "ruff check")) + "\n"
    (slug / f"{session_id}.jsonl.gz").write_bytes(_gzip.compress(raw.encode()))
    monkeypatch.setattr(ti, "PROJECTS_DIR", projects)

    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    cfg.hooks.capture_bash = False

    out = ti.backfill_uncaptured(cfg, session_id)

    assert out.get("reason") != "transcript not found"
    assert out["backfilled"] >= 1, "an archived transcript was not reclaimed by backfill"


def test_backfill_is_idempotent_across_reruns(tmp_path, monkeypatch):
    """The docstring's central claim, driven on the self-resolving path: a second
    session_end (e.g. after a compact) must never double-insert."""
    import json as _json

    from techne.config import Config
    from techne.core.database import init_db
    from techne.platforms.claude_code import transcript_import as ti

    session_id = "eeeeeeee-1111-2222-3333-444444444444"
    projects = tmp_path / "projects"
    slug = projects / "-tmp-proj"
    slug.mkdir(parents=True)
    (slug / f"{session_id}.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in _bash_transcript_rows(session_id)) + "\n"
    )
    monkeypatch.setattr(ti, "PROJECTS_DIR", projects)

    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    cfg.hooks.capture_bash = False

    first = ti.backfill_uncaptured(cfg, session_id)
    second = ti.backfill_uncaptured(cfg, session_id)

    assert first["backfilled"] >= 1
    assert second["backfilled"] == 0, "a re-run double-inserted"
    # Resumed from the cursor (issue 1218); without it the rescan must dedup.
    _drop_backfill_cursor(cfg.db_path, session_id)
    third = ti.backfill_uncaptured(cfg, session_id)
    assert third["backfilled"] == 0, "a rescan without the cursor double-inserted"
    assert third["skipped_existing"] >= 1


# ---------------------------------------------------------------------------
# backfill_uncaptured resume cursor (issue 1218)
# ---------------------------------------------------------------------------


def _ledger(db_path, session_id="sess-fixture"):
    conn = connect(db_path)
    try:
        return sorted(
            tuple(r)
            for r in conn.execute(
                "SELECT tool_name, content, event_at FROM observations WHERE session_id = ?",
                (session_id,),
            )
        )
    finally:
        conn.close()


def _cursor(db_path, session_id="sess-fixture"):
    from techne.core.extension_state import get_state
    from techne.platforms.claude_code.transcript_import import BACKFILL_CURSOR_PREFIX

    conn = connect(db_path)
    try:
        return get_state(conn, BACKFILL_CURSOR_PREFIX + session_id)
    finally:
        conn.close()


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _ts(i: int) -> str:
    return f"2026-06-01T10:{i // 60:02d}:{i % 60:02d}.000Z"


def test_backfill_resumes_from_cursor_and_inserts_only_new_rows(db_path, tmp_path):
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _bash_use("tu_1", "pytest -q", _ts(1)),
            _tool_result("tu_1", "2 passed", _ts(2)),
            _bash_use("tu_2", "git commit -m a", _ts(3)),
            _tool_result("tu_2", "1 file changed", _ts(4)),
        ],
    )
    first = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert first["backfilled"] == 2
    assert _cursor(db_path)["offset"] == p.stat().st_size

    _append_jsonl(
        p,
        [_bash_use("tu_3", "make build", _ts(5)), _tool_result("tu_3", "ok", _ts(6))],
    )
    second = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    # Only the appended pair was read: the first two rows would otherwise be
    # counted as skipped_existing.
    assert second["backfilled"] == 1
    assert second["skipped_existing"] == 0
    assert _cursor(db_path)["offset"] == p.stat().st_size
    assert len(_ledger(db_path)) == 3


def test_backfill_pending_tool_use_spanning_cursor_boundary_is_paired(db_path, tmp_path):
    """A tool_use read on one run and its tool_result on the next still pair up:
    the unresolved tool_use rides in the cursor."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(p, [_bash_use("tu_1", "pytest -q", _ts(1))])
    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 0
    assert "tu_1" in _cursor(db_path)["pending"]

    _append_jsonl(p, [_tool_result("tu_1", "2 passed", _ts(2))])
    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 1
    assert _cursor(db_path)["pending"] == {}
    from techne.core.database import normalize_timestamp

    assert _ledger(db_path)[0][2] == normalize_timestamp(_ts(1))  # the tool_use's time


def test_backfill_cursor_keeps_only_gap_tool_pending(db_path, tmp_path):
    """The cursor persists pending tool_uses; a Write's full content must not ride
    along when Write is not a gap tool."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _tool_use("tu_w", "Write", {"file_path": "a.py", "content": "x" * 5000}, _ts(1)),
            _bash_use("tu_b", "pytest -q", _ts(2)),
        ],
    )
    backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert set(_cursor(db_path)["pending"]) == {"tu_b"}


def test_backfill_resets_cursor_on_truncation_and_on_head_change(db_path, tmp_path):
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    first_pair = [_bash_use("tu_1", "pytest -q", _ts(1)), _tool_result("tu_1", "ok", _ts(2))]
    _write_jsonl(
        p,
        first_pair
        + [_bash_use("tu_2", "make a", _ts(3)), _tool_result("tu_2", "ok", _ts(4))],
    )
    backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    inode = p.stat().st_ino

    # Truncation, same first line, same inode: only the size check can see it.
    # The file now holds a different second pair, shorter than the stored offset.
    _write_jsonl(
        p, first_pair + [_bash_use("tu_3", "make c", _ts(5)), _tool_result("tu_3", "k", _ts(6))]
    )
    assert p.stat().st_ino == inode
    assert p.stat().st_size < _cursor(db_path)["offset"]
    out = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert out["backfilled"] == 1  # make c, read from byte 0
    assert out["skipped_existing"] == 1  # pytest -q, deduped on the rescan

    # Head change, same inode, file not shorter: only the head hash can see it.
    offset = _cursor(db_path)["offset"]
    _write_jsonl(
        p,
        [
            _bash_use("tu_h", "make head " + "y" * offset, _ts(20)),
            _tool_result("tu_h", "ok", _ts(21)),
        ],
    )
    assert p.stat().st_ino == inode and p.stat().st_size > offset
    out = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert out["backfilled"] == 1


def test_backfill_resets_cursor_when_gap_tools_change(db_path, tmp_path):
    """Turning capture_read off widens the gap; Reads already passed by the cursor
    must still be backfilled."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(
        p,
        [
            _tool_use("tu_r", "Read", {"file_path": "a.py"}, _ts(1)),
            _tool_result("tu_r", "body", _ts(2)),
        ],
    )
    out = backfill_uncaptured(_cfg(db_path, capture_read=True), "sess-fixture", jsonl_path=p)
    assert out["backfilled"] == 0
    out = backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)
    assert out["backfilled"] == 1


def test_backfill_partial_last_line_not_consumed(db_path, tmp_path):
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(p, [_bash_use("tu_1", "pytest -q", _ts(1))])
    complete = p.stat().st_size
    tail = json.dumps(_tool_result("tu_1", "2 passed", _ts(2))) + "\n"
    with p.open("a") as f:
        f.write(tail[:20])  # the writer is mid-line

    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 0
    assert _cursor(db_path)["offset"] == complete

    with p.open("a") as f:
        f.write(tail[20:])
    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 1
    assert _cursor(db_path)["offset"] == p.stat().st_size


def test_backfill_unterminated_complete_last_line_is_read_but_not_passed(db_path, tmp_path):
    """A last line that parses but has no newline yet is backfilled (as the
    whole-file reader did), and the cursor stays before it so the next run sees
    it again; the dedup keeps that from double-inserting."""
    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    p = tmp_path / "sess-fixture.jsonl"
    _write_jsonl(p, [_bash_use("tu_1", "pytest -q", _ts(1))])
    complete = p.stat().st_size
    with p.open("a") as f:
        f.write(json.dumps(_tool_result("tu_1", "2 passed", _ts(2))))

    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 1
    assert _cursor(db_path)["offset"] == complete
    with p.open("a") as f:
        f.write("\n")
    assert backfill_uncaptured(_cfg(db_path), "sess-fixture", jsonl_path=p)["backfilled"] == 0
    assert len(_ledger(db_path)) == 1
    assert _cursor(db_path)["offset"] == p.stat().st_size


def test_backfill_cursor_equals_full_rescan(tmp_path):
    """Property: growing a transcript in arbitrary byte chunks (mid-line splits
    included) and backfilling after each leaves the same ledger as one full pass."""
    import random

    from techne.platforms.claude_code.transcript_import import backfill_uncaptured

    rng = random.Random(1218)
    records = []
    for i in range(40):
        tid = f"tu_{i}"
        kind = rng.choice(["Bash", "Bash", "Read", "Edit"])
        inp = (
            {"command": f"make target{i}"}
            if kind == "Bash"
            else {"file_path": f"f{i}.py"}
        )
        records.append(_tool_use(tid, kind, inp, _ts(2 * i)))
        if rng.random() < 0.5:  # interleave another call before this one's result
            records.append(_user_text(f"prompt {i}", _ts(2 * i)))
        records.append(_tool_result(tid, f"out {i}", _ts(2 * i + 1)))
    blob = "".join(json.dumps(r) + "\n" for r in records).encode()

    full_db = tmp_path / "full.db"
    init_db(full_db).close()
    full_path = tmp_path / "full" / "sess-fixture.jsonl"
    full_path.parent.mkdir()
    full_path.write_bytes(blob)
    backfill_uncaptured(_cfg(full_db), "sess-fixture", jsonl_path=full_path)
    expected = _ledger(full_db)
    assert len(expected) > 10

    for trial in range(5):
        db = tmp_path / f"chunked{trial}.db"
        init_db(db).close()
        path = tmp_path / f"chunked{trial}" / "sess-fixture.jsonl"
        path.parent.mkdir()
        path.write_bytes(b"")
        # Random cuts almost never land at a line's end, so also cut at two
        # newline positions: the file then ends in a complete, parseable line
        # with no newline, which must be read but not passed (Prudence, PR 1229 r1).
        # Passing it leaves the same ledger (the next read starts at its "\n"), so
        # the offset check below is what pins it.
        newlines = [i for i, b in enumerate(blob) if b == 0x0A]
        cuts = sorted(set(rng.sample(range(1, len(blob)), 12) + rng.sample(newlines, 2)))
        cuts.append(len(blob))
        prev = 0
        for cut in cuts:
            with path.open("ab") as f:
                f.write(blob[prev:cut])
            prev = cut
            backfill_uncaptured(_cfg(db), "sess-fixture", jsonl_path=path)
            cursor = _cursor(db)
            if cursor is not None:  # no cursor until the first line arrives
                off = cursor["offset"]
                assert off == 0 or blob[off - 1] == 0x0A, (
                    f"trial {trial}: cursor at {off} is past a line with no newline"
                )
        assert _ledger(db) == expected, f"trial {trial}, cuts {cuts}"


def test_session_end_passes_payload_transcript_path_to_backfill(tmp_path, monkeypatch):
    """The Stop/SessionEnd payload names the transcript; using it skips the glob
    over every project dir. A missing or malformed one falls back to the glob."""
    from techne.hooks.session_end import _transcript_path

    p = tmp_path / "s.jsonl"
    p.write_text("{}\n")
    assert _transcript_path({"transcript_path": str(p)}) == p
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _transcript_path({"transcript_path": "~/s.jsonl"}) == p
    assert _transcript_path({"transcript_path": str(tmp_path / "gone.jsonl")}) is None
    assert _transcript_path({"transcript_path": 7}) is None
    assert _transcript_path({}) is None
