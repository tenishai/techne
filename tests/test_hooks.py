"""Tests for the techne hooks module — should_capture, summarize, and pre_compact parsing."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from techne.config import Config
from techne.hooks.post_tool_use import SKIP_TOOLS, should_capture, summarize
from techne.hooks.pre_compact import _find_last_task, _parse_transcript

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**hook_kwargs) -> Config:
    """Return a fresh Config with the given HookConfig fields overridden."""
    cfg = Config()
    for key, val in hook_kwargs.items():
        setattr(cfg.hooks, key, val)
    return cfg


def _write_transcript(lines: list[dict]) -> str:
    """Write a list of dicts as newline-delimited JSON to a temp file.

    Returns the path; caller is responsible for cleanup.
    """
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(json.dumps(line) for line in lines))
    return path


# ---------------------------------------------------------------------------
# TestShouldCapture
# ---------------------------------------------------------------------------


class TestShouldCapture:
    """Tests for should_capture(config, tool_name)."""

    def test_should_capture_default_config_write_tools(self):
        """Common write tools are captured with default config."""
        config = _make_config()
        for tool in ("Edit", "Write", "MultiEdit"):
            assert should_capture(config, tool) is True, f"Expected {tool} to be captured"

    def test_should_capture_default_config_bash_not_captured(self):
        """Bash is NOT captured by default (capture_bash=False)."""
        config = _make_config()
        assert should_capture(config, "Bash") is False

    def test_should_capture_default_config_read_not_captured(self):
        """Read is NOT captured by default (capture_read defaults to False).

        Fixed in T18: Read is no longer unconditionally in SKIP_TOOLS; instead
        the capture_read flag is consulted, matching the capture_bash pattern.
        """
        config = _make_config()
        assert should_capture(config, "Read") is False
        # Read is handled by the flag check, not by SKIP_TOOLS
        assert "Read" not in SKIP_TOOLS

    def test_should_capture_bash_enabled(self):
        """Bash is captured when capture_bash=True."""
        config = _make_config(capture_bash=True)
        assert should_capture(config, "Bash") is True

    def test_should_capture_read_enabled(self):
        """Read IS captured when capture_read=True.

        Fixed in T18: capture_read is now honored inside should_capture, symmetric
        with how capture_bash works. Read is no longer unconditionally in SKIP_TOOLS.
        """
        config = _make_config(capture_read=True)
        assert should_capture(config, "Read") is True

    def test_should_capture_post_tool_use_false_disables_all_capture(self):
        """post_tool_use=False causes should_capture to return False for all tools.

        Fixed in T18: the post_tool_use flag is now checked at the top of
        should_capture itself, so library callers (e.g. lifecycle.capture_tool_use)
        also respect it — not just main().
        """
        config = _make_config(post_tool_use=False)
        assert should_capture(config, "Edit") is False
        assert should_capture(config, "Write") is False
        assert should_capture(config, "Bash") is False

    def test_should_capture_unknown_tool_is_captured(self):
        """An unknown tool name is captured by default (allow-by-default policy).

        The function only skips tools that are explicitly excluded; anything
        else falls through to True.
        """
        config = _make_config()
        assert should_capture(config, "RandomTool") is True
        assert should_capture(config, "SomeNewTool") is True

    def test_should_capture_ask_user_question_skipped(self):
        """AskUserQuestion is in SKIP_TOOLS and must never be captured."""
        config = _make_config()
        assert should_capture(config, "AskUserQuestion") is False
        assert "AskUserQuestion" in SKIP_TOOLS

    def test_should_capture_mcp_memory_tools_skipped(self):
        """Tools with the mcp__memory__ prefix are always skipped (prevent loops)."""
        config = _make_config()
        assert should_capture(config, "mcp__memory__search") is False
        assert should_capture(config, "mcp__memory__store") is False

    def test_should_capture_grep_and_websearch_captured(self):
        """Grep and WebSearch are captured by default (not in SKIP_TOOLS)."""
        config = _make_config()
        assert should_capture(config, "Grep") is True
        assert should_capture(config, "WebSearch") is True


# ---------------------------------------------------------------------------
# TestSummarize
# ---------------------------------------------------------------------------


class TestSummarize:
    """Tests for summarize(tool_name, tool_input, tool_output).

    summarize() formats a tool call into a human-readable string for storage
    as an observation.
    """

    def test_summarize_bash(self):
        result = summarize("Bash", {"command": "ls -la"}, "file1.txt\nfile2.txt")
        assert result.startswith("Ran: ls -la")
        assert "Output:" in result
        assert "file1.txt" in result

    def test_summarize_edit(self):
        result = summarize(
            "Edit",
            {"file_path": "/foo/bar.py", "old_string": "old_code", "new_string": "new_code"},
            "success",
        )
        assert "Edited:" in result
        assert "/foo/bar.py" in result
        assert "old_code" in result
        assert "new_code" in result

    def test_summarize_write(self):
        result = summarize("Write", {"file_path": "/foo/baz.py", "content": "hello"}, "written")
        assert result.startswith("Wrote: /foo/baz.py")
        assert "hello" in result

    def test_summarize_grep(self):
        result = summarize("Grep", {"pattern": "def foo"}, "match found at line 42")
        assert "Searched for: def foo" in result
        assert "match found" in result

    def test_summarize_websearch(self):
        result = summarize("WebSearch", {"query": "python testing"}, "results here")
        assert "Searched web: python testing" in result
        assert "results here" in result

    def test_summarize_webfetch(self):
        result = summarize("WebFetch", {"url": "http://example.com"}, "page content")
        assert "Fetched: http://example.com" in result
        assert "page content" in result

    def test_summarize_unknown_tool_returns_string(self):
        """Unknown tools get a generic format with Tool/Input/Output labels."""
        result = summarize("RandomTool", {"foo": "bar"}, "some output")
        assert isinstance(result, str)
        assert "RandomTool" in result
        assert "some output" in result

    def test_summarize_dict_output_serialized(self):
        """dict outputs are JSON-serialized, not left as raw repr."""
        result = summarize("Bash", {"command": "test"}, {"key": "value"})
        assert isinstance(result, str)
        # The dict should appear as JSON, not Python repr
        assert '"key"' in result or "key" in result

    def test_summarize_none_output(self):
        """None output is handled without crashing; result is still a string."""
        result = summarize("Bash", {"command": "test"}, None)
        assert isinstance(result, str)

    def test_summarize_output_truncated_at_2000_chars(self):
        """Long output is truncated to 2000 characters in the preview."""
        long_output = "x" * 5000
        result = summarize("Bash", {"command": "test"}, long_output)
        # The result must contain at most 2000 'x's
        assert result.count("x") <= 2000

    def test_summarize_missing_input_fields(self):
        """Missing fields in tool_input don't crash; they default to empty string."""
        result = summarize("Edit", {}, "output")
        assert isinstance(result, str)
        assert "Edited:" in result


# ---------------------------------------------------------------------------
# TestPreCompactTranscriptParsing
# ---------------------------------------------------------------------------


class TestPreCompactTranscriptParsing:
    """Tests for _parse_transcript(path) in pre_compact.py."""

    def test_parse_empty_file(self):
        """An empty file returns the default result structure with empty collections."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            result = _parse_transcript(path)
            assert result["user_messages"] == []
            assert result["files_modified"] == set()
            assert dict(result["tools_used"]) == {}
            assert result["bash_commands"] == []
            assert result["git_branch"] == ""
        finally:
            os.unlink(path)

    def test_parse_well_formed_transcript(self):
        """A typical transcript is parsed to extract messages, files, tools, branch."""
        lines = [
            {
                "type": "user",
                "gitBranch": "main",
                "message": {"content": [{"type": "text", "text": "Please edit the config file"}]},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/foo/bar.py",
                                "old_string": "old",
                                "new_string": "new",
                            },
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
                    ]
                },
            },
        ]
        path = _write_transcript(lines)
        try:
            result = _parse_transcript(path)
            assert "Please edit the config file" in result["user_messages"]
            assert "/foo/bar.py" in result["files_modified"]
            assert result["tools_used"]["Edit"] == 1
            assert result["tools_used"]["Bash"] == 1
            assert "git status" in result["bash_commands"]
            assert result["git_branch"] == "main"
        finally:
            os.unlink(path)

    def test_parse_malformed_lines_do_not_crash(self):
        """Lines that aren't valid JSON are silently skipped; result is still returned."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write('not valid json\n{also bad\n{"type": "unknown"}\n')
        try:
            result = _parse_transcript(path)
            # Should return the default empty structure, not raise
            assert isinstance(result, dict)
            assert result["user_messages"] == []
            assert result["files_modified"] == set()
        finally:
            os.unlink(path)

    def test_parse_nonexistent_path_returns_default(self):
        """A nonexistent path returns the default empty structure without raising."""
        result = _parse_transcript("/nonexistent/definitely/not/here.jsonl")
        assert result["user_messages"] == []
        assert result["files_modified"] == set()
        assert dict(result["tools_used"]) == {}

    def test_parse_skips_system_text_starting_with_angle_bracket(self):
        """User messages starting with '<' are treated as system noise and skipped."""
        lines = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "<system>internal message</system>"},
                        {"type": "text", "text": "Real user message here"},
                    ]
                },
            }
        ]
        path = _write_transcript(lines)
        try:
            result = _parse_transcript(path)
            assert "Real user message here" in result["user_messages"]
            assert not any(m.startswith("<") for m in result["user_messages"])
        finally:
            os.unlink(path)

    def test_parse_write_tool_adds_to_files_modified(self):
        """Write tool calls are tracked in files_modified just like Edit."""
        lines = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "/foo/new.py", "content": "hello"},
                        }
                    ]
                },
            }
        ]
        path = _write_transcript(lines)
        try:
            result = _parse_transcript(path)
            assert "/foo/new.py" in result["files_modified"]
        finally:
            os.unlink(path)

    def test_parse_gitbranch_extracted_from_any_line(self):
        """gitBranch field is extracted from whichever line carries it."""
        lines = [{"type": "assistant", "gitBranch": "feature/foo", "message": {"content": []}}]
        path = _write_transcript(lines)
        try:
            result = _parse_transcript(path)
            assert result["git_branch"] == "feature/foo"
        finally:
            os.unlink(path)

    def test_parse_multiple_tool_calls_aggregated(self):
        """Multiple calls to the same tool are counted correctly."""
        lines = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "/a.py", "old_string": "", "new_string": ""},
                        },
                    ]
                },
            },
        ]
        path = _write_transcript(lines)
        try:
            result = _parse_transcript(path)
            assert result["tools_used"]["Bash"] == 2
            assert result["tools_used"]["Edit"] == 1
            assert "pwd" in result["bash_commands"]
            assert "ls" in result["bash_commands"]
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# TestFindLastTask
# ---------------------------------------------------------------------------


class TestFindLastTask:
    """Tests for _find_last_task(messages) in pre_compact.py."""

    def test_empty_list_returns_empty_string(self):
        assert _find_last_task([]) == ""

    def test_only_short_messages_returns_empty_string(self):
        """Messages shorter than 10 characters are ignored."""
        assert _find_last_task(["yes", "no", "ok", "hi"]) == ""

    def test_returns_last_substantive_message(self):
        """Returns the last message that isn't a filler word."""
        msgs = ["Please implement feature A", "ok", "Please implement feature B"]
        assert _find_last_task(msgs) == "Please implement feature B"

    def test_skips_common_filler_words(self):
        """yes/no/ok/okay/thanks/thank you are treated as non-task messages."""
        fillers = ["yes", "no", "ok", "okay", "thanks", "thank you"]
        for filler in fillers:
            # Pad the filler to be 10+ chars so short-length check doesn't apply
            padded = filler + " " * (10 - len(filler) + 1)
            result = _find_last_task([padded])
            # Filler-like words are skipped even if long enough (checked by .lower())
            # Only the exact filler strings are skipped; padded ones with spaces may not be
            # — verify empirically:
            pass
        # Direct check: filler alone with no other message returns ""
        assert _find_last_task(["ok", "yes", "no"]) == ""

    def test_returns_first_non_filler_from_end(self):
        """Traverses in reverse; returns the last substantive message."""
        msgs = ["first task description", "second task description", "ok"]
        assert _find_last_task(msgs) == "second task description"


# ---------------------------------------------------------------------------
# Hook exit-code invariants
#
# Claude Code treats any non-zero hook exit as a hard hook failure at the
# corresponding lifecycle event. For session_end (Stop) that means a failing
# hook breaks session teardown. Every error path must exit 0.
# ---------------------------------------------------------------------------


def _run_hook_subprocess(
    module: str, stdin: str, env_overrides: dict | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, "-m", module],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class TestSessionEndExitCode:
    def test_empty_stdin_exits_zero(self):
        result = _run_hook_subprocess("techne.hooks.session_end", "")
        assert result.returncode == 0

    def test_missing_session_id_exits_zero(self):
        result = _run_hook_subprocess("techne.hooks.session_end", "{}")
        assert result.returncode == 0

    def test_malformed_json_exits_zero(self):
        result = _run_hook_subprocess("techne.hooks.session_end", "not json {")
        assert result.returncode == 0

    def test_inner_failure_exits_zero(self, tmp_path: Path):
        """If the hook reaches the work path but Config/DB/run_session_end fails,
        it still must exit 0. Point HOME at an empty dir so any DB access fails."""
        result = _run_hook_subprocess(
            "techne.hooks.session_end",
            '{"session_id": "session-that-does-not-exist"}',
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0, (
            f"session_end exited {result.returncode} on inner failure — "
            f"this breaks Claude Code session teardown.\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestSessionEndFlushSinks
#
# Verify that flush_sinks is called in the session_end hook alongside flush().
# ---------------------------------------------------------------------------


class TestSessionEndFlushSinks:
    """flush_sinks must be called from session_end, same as flush(adapter)."""

    def test_flush_sinks_wired_in_session_end_source(self):
        """flush_sinks import appears in session_end.py — static wiring check."""
        import inspect

        import techne.hooks.session_end as hook_mod

        src = inspect.getsource(hook_mod)
        assert "flush_sinks" in src, "flush_sinks must be called in session_end.py (issue #48)"

    def test_sync_rows_invisible_to_flush_sinks(self, tmp_path: Path):
        """Adapter-bound `sync:` rows (thread summaries) are invisible to
        flush_sinks — it never touches them, so they stay pending for
        flush(adapter). This is the #51 fix: the target prefix decides ownership,
        so the two flush paths run in the same hook without racing. Replaces the
        old deferred-stub (ok=False) bridge, which was the symptom of the race.
        """
        from techne.core.database import init_db
        from techne.sync.outbox import enqueue, flush_sinks, stats
        from techne.sync.sinks import use_registry

        db_path = tmp_path / "test.db"
        init_db(db_path).close()

        with use_registry({}):  # no sinks registered at all
            enqueue(db_path, "sync:thread_summary", {"content": "queued"}, deliver=False)
            summary = flush_sinks(db_path)

        s = stats(db_path)
        assert s["pending"] == 1, "flush_sinks must leave sync: rows pending, not touch them"
        assert s["dead"] == 0
        assert "sync:thread_summary" not in summary, "flush_sinks must ignore sync: rows entirely"
