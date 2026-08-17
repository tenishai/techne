"""Tests for the Claude Code transcript adapter (native JSONL → canonical Transcript)."""

from __future__ import annotations

import gzip
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


# --- archived (.gz) transcripts (#621) -------------------------------------


def _write_jsonl_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in records))


_TURNS = [
    {"message": {"role": "user", "content": "what is the plan"}},
    {"message": {"role": "assistant", "content": [{"type": "text", "text": "the plan is X"}]}},
]


def test_archived_transcript_is_indexed_with_the_right_session_id(tmp_path):
    """The id becomes the Librarian's source_url, so a wrong one indexes the same
    session twice. Path.stem on "sess-abc.jsonl.gz" yields "sess-abc.jsonl"."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl_gz(d / "sess-abc.jsonl.gz", _TURNS)

    transcripts = list(ClaudeCodeTranscriptSource(d).iter_transcripts())

    assert len(transcripts) == 1
    assert transcripts[0].id == "sess-abc"
    assert [(t.role, t.text) for t in transcripts[0].turns] == [
        ("user", "what is the plan"),
        ("assistant", "the plan is X"),
    ]


def test_archived_and_plain_are_read_identically(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(d / "plain.jsonl", _TURNS)
    _write_jsonl_gz(d / "archived.jsonl.gz", _TURNS)

    by_id = {t.id: t for t in ClaudeCodeTranscriptSource(d).iter_transcripts()}

    assert set(by_id) == {"plain", "archived"}
    assert [(t.role, t.text) for t in by_id["archived"].turns] == [
        (t.role, t.text) for t in by_id["plain"].turns
    ]


def test_mid_archive_session_is_yielded_once_preferring_the_plain_file(tmp_path):
    """An archive run that wrote the .gz but has not yet unlinked the .jsonl must not
    yield the session twice — that would double-import append-only observations."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(d / "sess-abc.jsonl", _TURNS)
    _write_jsonl_gz(d / "sess-abc.jsonl.gz", _TURNS)

    transcripts = list(ClaudeCodeTranscriptSource(d).iter_transcripts())

    assert [t.id for t in transcripts] == ["sess-abc"]
    assert transcripts[0].uri.endswith("sess-abc.jsonl")


def test_an_unreadable_archive_is_recorded_not_fatal(tmp_path):
    """One corrupt .gz must not end the indexing run, and must not pass silently —
    a skip nobody can count turns the next coverage measurement into a wrong answer."""
    d = tmp_path / "transcripts"
    d.mkdir()
    (d / "truncated.jsonl.gz").write_bytes(gzip.compress(b'{"message": {}}')[:10])
    _write_jsonl(d / "good.jsonl", _TURNS)

    source = ClaudeCodeTranscriptSource(d)
    transcripts = list(source.iter_transcripts())

    assert [t.id for t in transcripts] == ["good"]
    assert [p.name for p, _ in source.unreadable] == ["truncated.jsonl.gz"]
