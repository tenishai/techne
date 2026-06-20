"""Tests for indexing CC JSONL transcripts into the Librarian documents store (#91)."""

from __future__ import annotations

import json
from pathlib import Path

from techne.core.database import init_db
from techne.core.documents import get_document
from techne.platforms.claude_code.transcript_library import (
    index_transcript,
    index_transcripts,
    transcript_to_text,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_transcript_to_text_keeps_prose_skips_plumbing(tmp_path):
    j = tmp_path / "sess-abc.jsonl"
    _write_jsonl(
        j,
        [
            {"message": {"role": "user", "content": "what is the plan"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "the plan is X"}],
                }
            },
            # pure tool_use → no text → skipped
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                }
            },
        ],
    )
    text, turns = transcript_to_text(j)
    assert turns == 2
    assert "## User" in text and "## Assistant" in text
    assert "what is the plan" in text and "the plan is X" in text


def test_index_transcript_stores_and_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    j = tmp_path / "sess-xyz.jsonl"
    _write_jsonl(
        j,
        [
            {"message": {"role": "user", "content": "decide the storage backend"}},
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "use SQLite"}]}},
        ],
    )
    doc_id, status = index_transcript(db, j)
    assert status == "stored"
    doc = get_document(db, doc_id)
    assert doc["source_type"] == "cc-transcript"
    assert doc["source_url"] == "cc-transcript:sess-xyz"
    assert "use SQLite" in doc["content"]
    assert doc["title"] == "decide the storage backend"

    # Re-indexing unchanged content is a no-op.
    _id2, status2 = index_transcript(db, j)
    assert status2 == "unchanged"


def test_index_transcript_redacts(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    j = tmp_path / "sess-secret.jsonl"
    _write_jsonl(
        j,
        [
            {
                "message": {
                    "role": "user",
                    "content": "export AWS_SECRET_ACCESS_KEY=AKIA1234567890ABCDEF",
                }
            }
        ],
    )
    doc_id, status = index_transcript(db, j)
    assert status == "stored"
    doc = get_document(db, doc_id)
    assert "AKIA1234567890ABCDEF" not in doc["content"]  # redaction applied


def test_index_empty_transcript_is_skipped(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    j = tmp_path / "empty.jsonl"
    _write_jsonl(
        j,
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "X", "input": {}}],
                }
            }
        ],
    )
    doc_id, status = index_transcript(db, j)
    assert doc_id is None and status == "empty"


def test_index_transcripts_counts(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(d / "a.jsonl", [{"message": {"role": "user", "content": "alpha"}}])
    _write_jsonl(d / "b.jsonl", [{"message": {"role": "user", "content": "beta"}}])
    _write_jsonl(
        d / "c.jsonl",
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "X", "input": {}}],
                }
            }
        ],
    )
    counts = index_transcripts(db, d)
    assert counts == {"total": 3, "stored": 2, "unchanged": 0, "empty": 1}
