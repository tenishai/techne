"""Tests for the platform-neutral transcript core (shape + generic indexer)."""

from __future__ import annotations

from techne.core.database import init_db
from techne.core.documents import get_document, list_documents
from techne.core.transcripts import (
    Transcript,
    TranscriptTurn,
    derive_title,
    index_transcripts,
    render_transcript,
)


class _FakeSource:
    """A platform with no Claude Code at all — proves the core is neutral."""

    platform = "fake-client"

    def __init__(self, transcripts):
        self._transcripts = transcripts

    def iter_transcripts(self):
        yield from self._transcripts


def _t(tid, turns, **kw):
    return Transcript(
        id=tid,
        platform="fake-client",
        turns=[TranscriptTurn(role=r, text=x) for r, x in turns],
        **kw,
    )


def test_render_transcript_labels_and_skips_empty():
    turns = [
        TranscriptTurn("user", "hello"),
        TranscriptTurn("assistant", ""),  # empty → skipped
        TranscriptTurn("assistant", "hi"),
        TranscriptTurn("tool_runner", "ran"),  # non-standard role → title-cased
    ]
    out = render_transcript(turns)
    assert "## User\n\nhello" in out
    assert "## Assistant\n\nhi" in out
    assert "## Tool Runner\n\nran" in out
    assert out.count("## Assistant") == 1  # the empty one didn't render


def test_derive_title_first_user_line():
    turns = [
        TranscriptTurn("assistant", "preamble"),
        TranscriptTurn("user", "design the store\nmore"),
    ]
    assert derive_title(turns) == "design the store"


def test_index_neutral_source_stores_and_dedups(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    source = _FakeSource(
        [
            _t("s1", [("user", "decide backend"), ("assistant", "use SQLite")]),
            _t("s2", [("user", "alpha")]),
            _t("empty", [("assistant", "")]),  # no text → empty
        ]
    )
    counts = index_transcripts(db, source)
    assert counts == {"total": 3, "stored": 2, "unchanged": 0, "empty": 1}

    docs = list_documents(db, source_type="fake-client-transcript")
    assert len(docs) == 2
    urls = {d["source_url"] for d in docs}
    assert urls == {"fake-client-transcript:s1", "fake-client-transcript:s2"}

    # Re-index unchanged → no new rows.
    counts2 = index_transcripts(db, source)
    assert counts2["stored"] == 0 and counts2["unchanged"] == 2 and counts2["empty"] == 1


def test_index_redacts_content_and_title(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    source = _FakeSource(
        [_t("secret", [("user", "key is AKIA1234567890ABCDEF"), ("assistant", "ok")])]
    )
    index_transcripts(db, source)
    doc = list_documents(db, source_type="fake-client-transcript")[0]
    full = get_document(db, doc["id"])
    # Secret must not survive in content OR the (first-user-line-derived) title.
    assert "AKIA1234567890ABCDEF" not in full["content"]
    assert "AKIA1234567890ABCDEF" not in (doc["title"] or "")


def test_index_respects_limit(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    source = _FakeSource([_t(f"s{i}", [("user", f"msg {i}")]) for i in range(5)])
    counts = index_transcripts(db, source, limit=2)
    assert counts["total"] == 2
