"""Tests for techne.sync.jsonl_records — neutral JSONL parsing primitives.

These are the platform-agnostic helpers promoted out of
platforms/claude_code/transcript_import.py (the #21 leak, #57). They parse the
Anthropic-messages content-block shape, which any compliant client emits.
"""

import json

from techne.sync.jsonl_records import (
    content_blocks,
    extract_text,
    iter_jsonl_records,
    should_import,
    tool_result_text,
)


class TestIterJsonlRecords:
    def test_parses_and_skips_blank_and_invalid(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            '{"a": 1}\n'
            "\n"  # blank line
            "   \n"  # whitespace-only
            "not json\n"  # invalid
            '{"b": 2}\n'
        )
        recs = list(iter_jsonl_records(p))
        assert recs == [{"a": 1}, {"b": 2}]

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert list(iter_jsonl_records(p)) == []


class TestContentBlocks:
    def test_list_content_passthrough(self):
        rec = {"message": {"content": [{"type": "text", "text": "hi"}]}}
        assert content_blocks(rec) == [{"type": "text", "text": "hi"}]

    def test_string_content_wrapped_as_text_block(self):
        rec = {"message": {"content": "hello"}}
        assert content_blocks(rec) == [{"type": "text", "text": "hello"}]

    def test_missing_message_or_content(self):
        assert content_blocks({}) == []
        assert content_blocks({"message": {}}) == []
        assert content_blocks({"message": {"content": None}}) == []
        assert content_blocks({"message": {"content": 42}}) == []


class TestExtractText:
    def test_joins_text_blocks_only(self):
        blocks = [
            {"type": "text", "text": "one"},
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "two"},
        ]
        assert extract_text(blocks) == "one\ntwo"

    def test_skips_blank_and_non_dict(self):
        blocks = [{"type": "text", "text": "  "}, "not a dict", {"type": "text", "text": "x"}]
        assert extract_text(blocks) == "x"

    def test_empty(self):
        assert extract_text([]) == ""


class TestToolResultText:
    def test_string(self):
        assert tool_result_text("plain") == "plain"

    def test_list_of_text_blocks(self):
        assert (
            tool_result_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
            == "a\nb"
        )

    def test_list_with_non_text_dict_is_json(self):
        out = tool_result_text([{"type": "image", "url": "x"}])
        assert json.loads(out) == {"type": "image", "url": "x"}

    def test_dict_falls_back_to_json(self):
        out = tool_result_text({"k": "v"})
        assert json.loads(out) == {"k": "v"}

    def test_none(self):
        assert tool_result_text(None) == ""


class TestShouldImport:
    def test_skips_blocklist(self):
        from techne.hooks.post_tool_use import SKIP_TOOLS

        # Pick any member of the live blocklist; should be filtered out.
        skipped = next(iter(SKIP_TOOLS))
        assert should_import(skipped) is False

    def test_skips_memory_mcp(self):
        assert should_import("mcp__memory__store") is False

    def test_allows_ordinary_tool(self):
        assert should_import("Edit") is True
        assert should_import("mcp__techne__techne_search") is True

    def test_bash_without_command_is_imported(self):
        """No tool_input (or no command) → can't inspect it, so import it."""
        assert should_import("Bash") is True
        assert should_import("Bash", {}) is True

    def test_readonly_bash_dropped(self):
        """Pure read-only navigation carries no durable signal — dropped on import
        too, matching the live-hook denylist (techne#734 consistent noise policy)."""
        for cmd in ("ls -la", "cat foo.py", "cd x && grep -rn y src/", "pwd"):
            assert should_import("Bash", {"command": cmd}) is False, cmd

    def test_signal_bash_imported(self):
        for cmd in ("git commit -m x", "pytest tests/", "cd r && git commit -m x",
                    "echo hi > out.txt", "sed -i 's/a/b/' f.py",
                    "find . -name '*.pyc' -delete", "sort -o out.txt in.txt"):
            assert should_import("Bash", {"command": cmd}) is True, cmd
