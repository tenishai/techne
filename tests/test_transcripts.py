"""Tests for the platform-neutral transcript core (shape + generic indexer)."""

from __future__ import annotations

from techne.core.database import init_db
from techne.core.documents import get_document, list_documents
from techne.core.librarian import librarian_db
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

    docs = list_documents(librarian_db(db), source_type="fake-client-transcript")
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
    doc = list_documents(librarian_db(db), source_type="fake-client-transcript")[0]
    full = get_document(librarian_db(db), doc["id"])
    # Secret must not survive in content OR the (first-user-line-derived) title.
    assert "AKIA1234567890ABCDEF" not in full["content"]
    assert "AKIA1234567890ABCDEF" not in (doc["title"] or "")


def test_index_respects_limit(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    source = _FakeSource([_t(f"s{i}", [("user", f"msg {i}")]) for i in range(5)])
    counts = index_transcripts(db, source, limit=2)
    assert counts["total"] == 2


def test_transcript_gist_is_thread_index(tmp_path):
    from techne.core.cards import pending_cards
    from techne.core.library import index_document
    from techne.core.state import start_session
    from techne.core.threads import Thread, save_threads
    from techne.core.transcripts import build_transcript_gist, fill_transcript_gists

    db = tmp_path / "g.db"
    init_db(db).close()
    sid = start_session(db, project_path="/p", agent_identity="t")
    save_threads(
        [
            Thread(thread_num=1, label="egress work", summary="s", observation_indices=[0]),
            Thread(thread_num=2, label="probe work", summary="s", observation_indices=[1]),
        ],
        sid,
        db,
        parent_links=None,
    )
    index_document(
        db,
        source_type="claude-code-transcript",
        source_url=f"claude-code-transcript:{sid}",
        title="t",
        content="body",
    )
    gist = build_transcript_gist(db, sid)
    assert "2 threads" in gist and "egress work" in gist and "probe work" in gist

    counts = fill_transcript_gists(db)
    assert counts["filled"] == 1
    assert pending_cards(db) == []  # thread-index gist cleared pending


def test_transcript_gist_none_without_threads(tmp_path):
    from techne.core.state import start_session
    from techne.core.transcripts import build_transcript_gist

    db = tmp_path / "g.db"
    init_db(db).close()
    sid = start_session(db, project_path="/p", agent_identity="t")
    assert build_transcript_gist(db, sid) is None  # no threads → no index
