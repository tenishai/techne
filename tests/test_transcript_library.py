"""Tests for the Claude Code transcript adapter (native JSONL → canonical Transcript)."""

from __future__ import annotations

import json
from pathlib import Path

from techne.core.transcripts import Transcript
from techne.platforms.claude_code.transcript_library import ClaudeCodeTranscriptSource


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_cc_source_yields_canonical_transcripts(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(
        d / "sess-abc.jsonl",
        [
            {"message": {"role": "user", "content": "what is the plan"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "the plan is X"}],
                }
            },
            # pure tool_use → no text → dropped (plumbing, not dialogue)
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                }
            },
        ],
    )
    source = ClaudeCodeTranscriptSource(d)
    assert source.platform == "claude-code"
    transcripts = list(source.iter_transcripts())
    assert len(transcripts) == 1
    t = transcripts[0]
    assert isinstance(t, Transcript)
    assert t.id == "sess-abc"
    assert t.platform == "claude-code"
    assert [(turn.role, turn.text) for turn in t.turns] == [
        ("user", "what is the plan"),
        ("assistant", "the plan is X"),
    ]
    assert t.uri.endswith("sess-abc.jsonl")


def test_cc_source_satisfies_protocol(tmp_path):
    from techne.core.transcripts import TranscriptSource

    d = tmp_path / "t"
    d.mkdir()
    assert isinstance(ClaudeCodeTranscriptSource(d), TranscriptSource)
