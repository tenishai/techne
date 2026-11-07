"""Tests for the techne hooks module — should_capture, summarize, and pre_compact parsing."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from techne.config import Config
from techne.hooks.post_tool_use import (
    _PREVIEW_HEAD_CHARS,
    _PREVIEW_MARKER_BUDGET,
    _PREVIEW_MAX_CHARS,
    SKIP_TOOLS,
    _preview,
    should_capture,
    summarize,
    was_elided,
)
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

    def test_should_capture_default_config_bash_captured(self):
        """Bash IS captured by default since #1020 (capture_bash=True); the
        read-only-command filter keeps navigation noise out."""
        config = _make_config()
        assert should_capture(config, "Bash") is True

    def test_should_capture_bash_disabled(self):
        """Bash is not captured when capture_bash=False."""
        config = _make_config(capture_bash=False)
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

    def test_should_capture_keeps_rail_tools(self):
        """techne's rail tools are captured: check_evidence backs a drain session's
        claims from these rows. They are left out of the reflect thread floor instead
        (techne#1283, tests/test_reflect.py)."""
        config = _make_config()
        for t in ("mcp__techne__techne_reflect_emit", "mcp__techne__techne_reflect_ingest"):
            assert should_capture(config, t) is True, t

    def test_should_capture_grep_and_websearch_captured(self):
        """Grep and WebSearch are captured by default (not in SKIP_TOOLS)."""
        config = _make_config()
        assert should_capture(config, "Grep") is True
        assert should_capture(config, "WebSearch") is True


class TestBashReadonlyNoiseFilter:
    """capture_bash=True still drops pure read-only navigation noise, but keeps
    anything that could carry durable evidence (commits, test runs, mutations)."""

    def _cfg(self):
        return _make_config(capture_bash=True)

    def test_no_tool_input_captures_unconditionally(self):
        """Callers without tool_input (unit tests, replay) can't inspect the
        command, so Bash is captured whenever the flag is on."""
        assert should_capture(self._cfg(), "Bash") is True

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat foo.py",
        "cd /tmp && ls",
        "grep -rn pattern src/ | head -20",
        "pwd",
        "find . -name '*.py'",
        "echo hello",
        "sed -n '1,50p' file.py",
        "FOO=bar ls",
        "/bin/ls -a",
        # quote-aware split: a `|` alternation inside a grep pattern must NOT be
        # mistaken for a pipe to a non-read-only command (else noise leaks through).
        'cd /repo && grep -n "post_tool_use|capture_bash" file.py',
        "grep -E 'foo|bar|baz' src/ | head",
        # `2>&1` is an fd-dup, not a file write — a read-only pipe through it
        # must still count as noise.
        "grep -n foo bar.py 2>&1 | tail",
        "ls -la 2>&1",
        "echo x >&2",
        # single quotes stop substitution
        "echo '$(git push)'",
        # read-only uses of heads that can also write (techne#1339)
        "sed 's/foo/bar/g' f",
        "sed -n '/^Date/p' f",
        "sed -e 's/a/b/' f",
        "awk '{print $1}' f",
        "fd -e py",
        "sort -k1 f",
        "uniq -c f",
        "uniq -f 1 f",
        "tree -L 2",
        "date +%s",
        "hostname -f",
        "echo x >&-",
        # read-only spellings that a naive write rule over-captured (PR 1353 review)
        "sed -n '1,20p' deploy/Dockerfile",
        "sed -n '/type /p' a.py",
        "sed -ne 's/x/y/p' f",
        "sed '/x/{p;d}' f",
        "sed 'y/abc/xyz/' f",
        "date -Iseconds",
        "sort -T/tmp/foo f",
        "fd -tx",
        "uniq --skip-fields 1 f",
        "awk -F'|' '{print $1}' f",
        "awk 'a || b' f",
        "\\ls",
        # PR 1353 review, round 2
        "rg --pretty foo",
        "sed --version",
        "date -d '-5 seconds'",
        "uniq -cf 1 f",
        "echo $((1+2))",
        "ls # git push",
        'echo "a>b"',
        "cat <<'EOF'\nhello > there\nEOF",
        "awk -v x=1 '{print x}' f",
        "ls \\\n-la",
    ])
    def test_readonly_commands_dropped(self, cmd):
        assert should_capture(self._cfg(), "Bash", {"command": cmd}) is False

    @pytest.mark.parametrize("cmd", [
        "git commit -m 'x'",
        "cd repo && git commit -m 'x'",          # signal hidden behind a cd
        "pytest tests/",
        "gh pr create",
        "make build",
        "docker build .",
        "echo hi > out.txt",                     # redirect mutates a file
        "cat template > config.yaml",
        "sed -i 's/a/b/' file.py",               # in-place edit mutates
        "uv pip install requests",
        "ls && pytest",                          # any non-readonly segment => capture
        # mutating find/sort: read word is read-only, but the action writes/deletes
        "find . -name '*.pyc' -delete",
        "find . -type d -exec rm -rf {} +",
        "sort -o out.txt in.txt",
        # techne#1339: command substitution runs a command the head rule never sees
        "echo $(git commit -m x)",
        "echo `git commit -m x`",
        "cd x && echo $(pytest -q)",
        'echo "$(rm x)"',
        "cat <(git push)",
        # techne#1339: read-only heads that write, delete or run something
        "awk 'BEGIN{system(\"rm -rf x\")}'",
        "awk '{print | \"sh\"}' f",
        "sed -n 'w out' f",
        "sed 's/a/b/w out' f",
        "sed 's/a/b/e' f",
        "sed --in-place s/a/b/ f",
        "sed -Ei s/a/b/ f",
        "sed -ni p f",
        "ls >& out",
        "find . -fls o",
        "fd -x rm",
        "sort -ro f",
        "uniq f out",
        "tree -o f",
        "date -s 12:00",
        "hostname newhost",
        # PR 1353 review: a `'` inside double quotes, or escaped, opens no quote
        'echo "it\'s done: $(git commit -m x)"',
        'echo "don\'t forget `touch x`"',
        "echo it\\'s $(touch x)",
        "echo it\\'s; touch x",
        # sed: anything outside the read-only command set captures
        "sed -n 's/a/b/gw out' in",
        "sed -n 's|a|b|w out' in",
        "sed 's#a#touch x#e' in",
        "sed 's/a/touch x/eg' in",
        "sed -n 'wout' in",
        "sed '1etouch x' in",
        "sed -n '1!w out' in",
        "sed -f s.sed in",
        "sed --in s/a/b/ f",
        # awk: a program file, or any option but -v/-F
        "awk -f p.awk in",
        "awk -i inplace '{}' f",
        "awk --include inplace '{}' f",
        # a lone & separates commands
        "ls & touch x",
        "echo starting & git commit -m x",
        "ls |& git push",
        # option spellings that write
        "ls >&1out",
        "tree -ao out",
        "sort --o=out f",
        "sort --output out f",
        "uniq - out",
        "fd -X rm",
        "fd --exec rm",
        "date --set 1",
        "rg --pre ./x y",
        # PR 1353 review, round 2: line continuations join words
        "find . -name in \\\n-delete",
        "sed -n 1p in \\\n-i",
        "sort in \\\n-o out",
        # env -S runs its argument
        "env -S'touch x'",
        "env --split-string='touch x'",
        # an apostrophe in a comment, in $'...', or in a heredoc body opens no quote
        "cd . # don't\ngit commit -m x",
        "ls # it's\ntouch x",
        "echo $'it\\'s'; touch x",
        "echo $'it\\'s' $(touch x)",
        "cat <<EOF\nls it's\nEOF\ntouch x",
        # an unquoted heredoc body runs its substitutions
        "cat <<EOF\n$(touch x)\nEOF",
        # every sed script is checked, however it is given
        "sed -e p -e 'w out' f",
        "sed --expression='w out' f",
        "sed -e'w out' f",
        "sed -- 'w out' f",
        "sed -n '/a/wout' in",
        "echo \"name='$(touch x)'\"",
        # awk options before the program are skipped, not trusted
        "awk -v x=1 'BEGIN{system(\"x\")}'",
        "awk -- 'BEGIN{system(\"x\")}'",
        # PR 1353 review, round 3
        "echo ${x:- #}; touch x",
        "awk '{print > \"o\"}' f",
        "awk '{print >> \"o\"}' f",
        "awk -v o=x '{print > o}' f",
        "awk 'BEGIN{printf \"x\" > \"/tmp/o\"}'",
        "ls \\\n# it's\ntouch x",
        "echo $\\\n(touch x)",
    ])
    def test_signal_commands_captured(self, cmd):
        assert should_capture(self._cfg(), "Bash", {"command": cmd}) is True

    def test_filter_inactive_when_flag_off(self):
        """The noise filter never runs when capture_bash is off (Bash stays off)."""
        cfg = _make_config(capture_bash=False)
        assert should_capture(cfg, "Bash", {"command": "git commit -m x"}) is False


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

    def test_summarize_output_bounded_at_2000_chars(self):
        """The output preview stays within the 2000-char budget."""
        long_output = "x" * 5000
        result = summarize("Bash", {"command": "test"}, long_output)
        assert result.count("x") <= 2000

    def test_summarize_short_output_is_verbatim(self):
        """Output within budget is not elided at all."""
        result = summarize("Bash", {"command": "test"}, "short output")
        assert result == "Ran: test\nOutput: short output"
        assert "elided" not in result

    def test_summarize_keeps_tail_of_long_output(self):
        """The END of long output survives, not just the head.

        Regression guard for the evidence gate: test runners print their verdict
        last, so a head-only preview dropped it and left `tests_green` claims
        unverifiable. Shaped like real pytest output — progress dots, then the
        result line — sized past the 2000-char budget.
        """
        pytest_output = ("." * 4000) + "\n2152 passed, 17 skipped in 68.60s"
        result = summarize("Bash", {"command": "pytest -q"}, pytest_output)
        assert "2152 passed" in result
        assert result.startswith("Ran: pytest -q")

    def test_summarize_marks_the_elision(self):
        """An elided preview says so, and reports how much was dropped."""
        result = summarize("Bash", {"command": "test"}, ("A" * 3000) + "\nTAIL")
        assert "chars elided" in result
        assert result.endswith("TAIL")

    def test_preview_drops_an_unaligned_tail_entirely(self):
        """Output with no newline in the tail degrades to head-only.

        Every character of such a tail is mid-line by definition, so there is no
        safe fragment to keep. Losing it is the correct direction: head-only is
        loss-only, while a retained fragment can fabricate a value.
        """
        result = _preview("A" * 3000)
        assert "chars elided" in result
        assert result.endswith(" …\n")

    def test_preview_elided_count_is_accurate(self):
        """head + elided + tail reconstructs the original length.

        Keeps the marker honest: a wrong count is worse than no count, because a
        reader uses it to decide whether the missing middle mattered.
        """
        original = "z" * 9000
        preview = _preview(original)
        head, rest = preview.split("\n… [", 1)
        count_str, tail = rest.split(" chars elided] …\n", 1)
        assert len(head) + int(count_str) + len(tail) == len(original)

    def test_preview_never_exceeds_budget(self):
        """No input length can push the preview over the 2000-char budget."""
        for size in (2001, 3000, 50_000, 5_000_000):
            assert len(_preview("q" * size)) <= 2000

    def test_preview_never_fabricates_a_smaller_count(self):
        """A mid-line tail cut must not turn "10 failed" into "0 failed".

        The two cut directions fail differently in KIND, which is why the tail is
        line-aligned. A head cut destroys the trailing keyword ("10 failed" ->
        "10 fai"), so it can only fail to match and the gate honestly answers
        "unverifiable" — loss-only. A raw tail cut keeps the keyword and shaves
        the digits ("10 failed" -> "0 failed"), which matches and reports ZERO
        failures, certifying a red suite as green. A false "backed" is strictly
        worse than declining to answer, so this must never regress.

        Input is jest-shaped and positioned so the tail boundary lands inside the
        failure count: summary line, then a coverage table long enough to push it
        exactly to the cut.
        """
        from techne.core.evidence import _parse_test_output

        tail_budget = _PREVIEW_MAX_CHARS - _PREVIEW_HEAD_CHARS - _PREVIEW_MARKER_BUDGET
        after_the_leading_digit = "0 failed, 5 passed, 15 total\n"
        coverage = "  src/mod.ts  |  82.1 |  71.4 |  90.0 |  82.1\n" * 40
        after = (after_the_leading_digit + coverage)[:tail_budget]
        assert len(after) == tail_budget, "input must place the cut inside the count"
        text = ("x" * 3000) + "\nTests:       1" + after

        _passed, failed, _errors = _parse_test_output(_preview(text))
        assert failed != 0, "a cut count was reported as zero failures"

    def test_preview_head_budget_is_pinned(self):
        """The head keeps a substantial prefix, not a token one.

        Guards the head constant itself: git commit output prints its SHA first,
        so shrinking the head silently costs `commit` claims their evidence.
        """
        head = _preview("a\n" * 5000).split("\n… [", 1)[0]
        assert len(head) >= 1000

    def test_preview_tail_budget_is_pinned(self):
        """The tail keeps a substantial suffix, not a token one.

        Guards the tail size independently of tail *survival*: a runner's result
        line can be far longer than the short one used in the other tests.
        """
        tail = _preview("a\n" * 5000).split(" …\n", 1)[1]
        assert len(tail) >= 400

    def test_was_elided_tracks_the_marker_preview_writes(self):
        """was_elided must agree with _preview — they are a writer/reader pair.

        The evidence gate uses this to tell "no failure line was printed" from
        "a failure line may have been dropped" (techne#438), so drift between the
        two silently reintroduces the false-green bug rather than erroring.

        The round-trip assertions below are necessary but NOT sufficient: both
        sides read _PREVIEW_ELISION_MARK, so renaming the constant keeps them
        agreeing and they pass tautologically. The literal below is what actually
        pins the on-disk format — observations already written carry it, so it is
        a compatibility constraint, not a style choice.
        """
        assert " chars elided] …\n" in _preview("a\n" * 5000)
        assert was_elided(_preview("a\n" * 5000)) is True
        assert was_elided(_preview("short output")) is False
        assert was_elided("Ran: pytest\nOutput: 5 passed in 1.2s") is False

    def test_elision_marker_is_not_parsed_as_evidence(self):
        """The marker itself must not read as a commit or a test result.

        The preview injects synthetic text into observations.content, which the
        evidence gate parses. Today the marker is inert only because "elided" is
        not a hex run — that is a coincidence worth pinning, since a large enough
        elided count puts a long digit run inside brackets.
        """
        from techne.core.evidence import _GIT_SHA_RE, _parse_test_output

        preview = _preview("x\n" * 5_000_000)
        marker = "\n… [" + preview.split("\n… [", 1)[1].split(" …\n", 1)[0] + " …\n"
        assert "chars elided" in marker
        assert not _GIT_SHA_RE.search(marker)
        assert _parse_test_output(marker) == (None, None, None)

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
            _find_last_task([padded])
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


class TestPostToolUseExitCode:
    """techne#166: PostToolUse must never break the host tool call.

    Config.load() was called OUTSIDE the try/except, so a corrupt/unreadable
    config.yaml would raise and surface as a non-zero hook exit — i.e. a failed
    tool call in the user's session. A memory hook must degrade to a no-op.
    """

    def test_malformed_json_exits_zero(self):
        result = _run_hook_subprocess("techne.hooks.post_tool_use", "not json {")
        assert result.returncode == 0

    def test_missing_fields_exits_zero(self):
        result = _run_hook_subprocess("techne.hooks.post_tool_use", "{}")
        assert result.returncode == 0

    def test_corrupt_config_exits_zero(self, tmp_path: Path):
        """A corrupt config.yaml makes Config.load() raise — the hook must still
        exit 0 (degrade to a no-op), not fail the tool call."""
        (tmp_path / "config.yaml").write_text("{[not: valid yaml::")
        payload = json.dumps({
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x"},
            "tool_response": "ok",
        })
        result = _run_hook_subprocess(
            "techne.hooks.post_tool_use",
            payload,
            env_overrides={"TECHNE_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, (
            f"post_tool_use exited {result.returncode} on corrupt config — "
            f"this would fail the host tool call.\nstderr: {result.stderr}"
        )
        assert "config load failed" in result.stderr


class TestCompactHooksExitCode:
    """techne#166: pre_compact / post_compact had the SAME unguarded Config.load()
    (outside the try/except) as post_tool_use — a corrupt config.yaml would raise
    and break the host's (Pre/Post)Compact lifecycle. They must degrade to no-ops."""

    @pytest.mark.parametrize("module", [
        "techne.hooks.pre_compact",
        "techne.hooks.post_compact",
    ])
    def test_corrupt_config_exits_zero(self, module, tmp_path: Path):
        (tmp_path / "config.yaml").write_text("{[not: valid yaml::")
        # Both hooks exit early without a session_id, so include one to reach Config.load().
        payload = json.dumps({"session_id": "s1", "transcript_path": ""})
        result = _run_hook_subprocess(
            module, payload, env_overrides={"TECHNE_HOME": str(tmp_path)}
        )
        assert result.returncode == 0, (
            f"{module} exited {result.returncode} on corrupt config — "
            f"this breaks the host compaction lifecycle.\nstderr: {result.stderr}"
        )
        assert "config load failed" in result.stderr


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


# ---------------------------------------------------------------------------
# #247 — hook stdin parse failures must emit a stderr diagnostic
#
# All hooks must: (a) exit 0 (never wedge the session), and (b) emit a clear
# message on stderr so the failure is visible rather than silently swallowed.
# ---------------------------------------------------------------------------


class TestHookStdinDiagnostics:
    """#247: each hook must write a diagnostic to stderr on bad-stdin parse failure
    while still exiting 0 (hooks must never wedge the host session)."""

    def test_session_end_bad_stdin_stderr_diagnostic(self):
        result = _run_hook_subprocess("techne.hooks.session_end", "not json {{{")
        assert result.returncode == 0, (
            f"session_end must exit 0 on bad stdin (got {result.returncode})"
        )
        assert "invalid JSON" in result.stderr, (
            f"session_end must emit a stderr diagnostic on bad stdin; got: {result.stderr!r}"
        )

    def test_post_tool_use_bad_stdin_stderr_diagnostic(self):
        result = _run_hook_subprocess("techne.hooks.post_tool_use", "not json {{{")
        assert result.returncode == 0, (
            f"post_tool_use must exit 0 on bad stdin (got {result.returncode})"
        )
        assert "invalid JSON" in result.stderr, (
            f"post_tool_use must emit a stderr diagnostic on bad stdin; got: {result.stderr!r}"
        )

    def test_session_start_bad_stdin_stderr_diagnostic(self):
        result = _run_hook_subprocess("techne.hooks.session_start", "not json {{{")
        assert result.returncode == 0, (
            f"session_start must exit 0 on bad stdin (got {result.returncode})"
        )
        assert "invalid JSON" in result.stderr, (
            f"session_start must emit a stderr diagnostic on bad stdin; got: {result.stderr!r}"
        )

    def test_pre_compact_bad_stdin_stderr_diagnostic(self):
        result = _run_hook_subprocess("techne.hooks.pre_compact", "not json {{{")
        assert result.returncode == 0, (
            f"pre_compact must exit 0 on bad stdin (got {result.returncode})"
        )
        assert "invalid JSON" in result.stderr, (
            f"pre_compact must emit a stderr diagnostic on bad stdin; got: {result.stderr!r}"
        )

    def test_post_compact_bad_stdin_stderr_diagnostic(self):
        result = _run_hook_subprocess("techne.hooks.post_compact", "not json {{{")
        assert result.returncode == 0, (
            f"post_compact must exit 0 on bad stdin (got {result.returncode})"
        )
        assert "invalid JSON" in result.stderr, (
            f"post_compact must emit a stderr diagnostic on bad stdin; got: {result.stderr!r}"
        )

    def test_session_end_empty_stdin_no_spurious_diagnostic(self):
        """Empty stdin is not a parse error — session_end reads it as decode error,
        so we DO expect the diagnostic here. This test asserts exit 0 regardless."""
        result = _run_hook_subprocess("techne.hooks.session_end", "")
        assert result.returncode == 0


# ---- `env` is a transparent prefix, not a read-only utility (techne#524) ----


class TestEnvPrefixIsNotNoise:
    """`env` heading the read-only set dropped whatever it prefixed.

    `_bash_is_readonly_noise` headed on `env` — which IS legitimately read-only
    when run alone — and discarded the WHOLE command, so `env … git commit` was
    dropped as navigation noise. That is the case the filter's own docstring
    names as the reason its fail-safe direction is CAPTURE: "a missed observation
    is far cheaper than a silently-dropped commit".
    """

    @pytest.mark.parametrize(
        "command",
        [
            "env bash -c 'git commit -m x'",
            "/usr/bin/env bash -c 'git commit -m x'",
            "env FOO=1 git commit -m x",
            "env -u X git push",
            "env bash -c 'pytest -q'",
            "env -i -u CC make test",
        ],
    )
    def test_a_real_command_behind_env_is_captured(self, command):
        from techne.hooks.post_tool_use import _bash_is_readonly_noise

        assert _bash_is_readonly_noise(command) is False, command

    @pytest.mark.parametrize("command", ["env", "env -i", "env -u FOO"])
    def test_env_with_no_command_is_still_noise(self, command):
        """The case that must NOT regress: bare `env` really does just print the
        environment, so it stays classified as navigation."""
        from techne.hooks.post_tool_use import _bash_is_readonly_noise

        assert _bash_is_readonly_noise(command) is True, command

    def test_env_is_the_only_transparent_prefix_in_the_readonly_set(self):
        """Bounds the fix, and corrects the issue's own guess.

        #524 speculated that other entries are prefixes with the same bug —
        `nice`, `nohup`, `timeout`, `stdbuf`. Measured: none of them is in the
        set at all, so none was ever dropped, and `env` is the only member that
        is a pass-through rather than a terminal utility. If a future edit adds
        one, this fails and the skip helper needs widening with THAT utility's
        option grammar — not a generic rule, since each differs and guessing
        wrong eats the real command.
        """
        from techne.hooks.post_tool_use import _READONLY_BASH_HEADS

        assert not _READONLY_BASH_HEADS & {"nice", "nohup", "timeout", "stdbuf", "sudo", "xargs"}
        assert "env" in _READONLY_BASH_HEADS

    @pytest.mark.parametrize(
        "command,head",
        [
            ("env FOO=1 git commit -m x", "git"),
            ("env -u CC bash -c pytest", "bash"),
            ("env -i -u CC make test", "make"),
            ("env -uCC bash -c pytest", "bash"),   # value ATTACHED — eats nothing
            ("/usr/bin/env bash -c pytest", "bash"),
            ("git commit -m x", "git"),            # no prefix at all
        ],
    )
    def test_the_skip_lands_on_the_real_head(self, command, head):
        """Pins WHICH token the skip resolves to, not merely the capture decision.

        The decision cannot catch over-skipping: eating the real command lands on
        `commit` or `-m`, neither of which is in the read-only set, so the filter
        still says "capture" and a starvation bug reads as correct. That is the
        exact failure poe_1 hit three times on this grammar in techne#445, and my
        first version of these tests could not see it — a mutation that skipped
        one token too far left all of them green.
        """
        from techne.hooks.post_tool_use import _skip_env_prefix

        tokens = command.split()
        assert tokens[_skip_env_prefix(tokens)] == head, tokens

    def test_the_gate_sees_a_runner_behind_env_too(self):
        """BEHAVIOURAL binding between the two sides, not an identity check.

        `evidence.py` used to carry its own copy of this loop (techne#445); the
        helper now lives here because evidence.py already imports from this
        module and the reverse would be a cycle. An `is` check on the imported
        symbol does NOT prove evidence.py still CALLS it — verified: replacing
        its call site with a no-op leaves such a check green. This asserts the
        outcome that only the call can produce.
        """
        from techne.core.evidence import _command_runs_tests

        # THE bash -c FORMS DO NOT REACH THE NEW CALL SITE (#544 r2). They route
        # through _expand_shell_c -> _shell_c_script, which calls _skip_env_prefix
        # itself (pre-existing) and hands back the inner "pytest -q" — so by the
        # time the segment loop runs, the `env` head is already gone and the call
        # there is a no-op. Call-shaped but inert: the line executes and cannot
        # fail. Deleting it left the ENTIRE suite green.
        assert _command_runs_tests("env bash -c 'pytest -q'") is True
        assert _command_runs_tests("env -u CC bash -c 'pytest -q'") is True

        # THESE are the direct-exec forms, with no `bash -c` to route around the
        # loop, so they are the only ones that exercise the added call site.
        assert _command_runs_tests("env pytest -q") is True
        assert _command_runs_tests("env -u CC pytest -q") is True
        assert _command_runs_tests("/usr/bin/env pytest -q") is True


class TestHookDurationRecording:
    """Issue 1217 step 0: every liveness marker carries how long the run took."""

    def test_record_hook_run_writes_ms_and_ring_capped_at_50(self, tmp_path, monkeypatch):
        import techne.hooks
        from techne.core.database import connect, init_db
        from techne.core.extension_state import get_state
        from techne.hooks import _liveness

        db = tmp_path / "t.db"
        init_db(db).close()
        clock = iter(range(1000, 2000))
        # Pin the clock: _T0 = 10.0 s, each call reads 10.0 s + n ms.
        monkeypatch.setattr(techne.hooks, "_T0", 10.0)
        monkeypatch.setattr(_liveness.time, "monotonic", lambda: 10.0 + next(clock) / 1000)

        for _ in range(_liveness.MS_RECENT_CAP + 7):
            _liveness.record_hook_run(db, "user_prompt_submit", outcome="ok")

        conn = connect(db)
        try:
            last = get_state(conn, "hook:user_prompt_submit:last_run")
            ring = get_state(conn, "hook:user_prompt_submit:ms_recent")
        finally:
            conn.close()
        n = _liveness.MS_RECENT_CAP + 7
        assert last["ms"] == 1000 + n - 1
        assert isinstance(last["cpu_ms"], int) and last["cpu_ms"] > 0
        assert last["outcome"] == "ok"
        # Capped, oldest dropped, order kept.
        assert ring["ms"] == list(range(1000 + n - _liveness.MS_RECENT_CAP, 1000 + n))

    def test_ring_recovers_from_a_malformed_payload(self, tmp_path):
        from techne.core.database import connect, init_db
        from techne.core.extension_state import get_state, set_state
        from techne.hooks import _liveness

        db = tmp_path / "t.db"
        init_db(db).close()
        conn = connect(db)
        set_state(conn, "hook:stop:ms_recent", {"ms": "not a list"})
        conn.close()

        _liveness.record_hook_run(db, "stop", outcome="ok")

        conn = connect(db)
        try:
            ring = get_state(conn, "hook:stop:ms_recent")
        finally:
            conn.close()
        assert isinstance(ring["ms"], list) and len(ring["ms"]) == 1

    def test_hook_subprocess_records_its_duration(self, tmp_path):
        """End to end: a real hook process stamps ms from techne.hooks' import."""
        from techne.core.database import connect, init_db
        from techne.core.extension_state import get_state

        home = tmp_path / "home"
        home.mkdir()
        init_db(home / "techne.db").close()
        env = {**os.environ, "TECHNE_HOME": str(home)}
        payload = json.dumps({"session_id": "s1", "prompt": "hi", "cwd": str(tmp_path)})
        proc = subprocess.run(
            [sys.executable, "-m", "techne.hooks.user_prompt_submit"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        conn = connect(home / "techne.db")
        try:
            last = get_state(conn, "hook:user_prompt_submit:last_run")
            ring = get_state(conn, "hook:user_prompt_submit:ms_recent")
        finally:
            conn.close()
        assert last is not None, proc.stderr
        assert 0 <= last["ms"] < 60_000
        assert last["cpu_ms"] >= 0
        assert ring["ms"] == [last["ms"]]
