"""Tests for the platform-neutral transcript core (shape + generic indexer)."""

from __future__ import annotations

from pathlib import Path

from techne.config import Config
from techne.core.database import init_db
from techne.core.documents import get_document, list_documents
from techne.core.librarian import librarian_db
from techne.core.transcripts import (
    Transcript,
    TranscriptSource,
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
    config = Config(db_path=db, data_dir=tmp_path)
    source = _FakeSource(
        [
            _t("s1", [("user", "decide backend"), ("assistant", "use SQLite")]),
            _t("s2", [("user", "alpha")]),
            _t("empty", [("assistant", "")]),  # no text → empty
        ]
    )
    counts = index_transcripts(config, source)
    assert counts == {"total": 3, "stored": 2, "unchanged": 0, "empty": 1, "unreadable": 0}

    docs = list_documents(librarian_db(config), source_type="fake-client-transcript")
    assert len(docs) == 2
    urls = {d["source_url"] for d in docs}
    assert urls == {"fake-client-transcript:s1", "fake-client-transcript:s2"}

    # Re-index unchanged → no new rows.
    counts2 = index_transcripts(config, source)
    assert counts2["stored"] == 0 and counts2["unchanged"] == 2 and counts2["empty"] == 1


def test_index_redacts_content_and_title(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)
    source = _FakeSource(
        [_t("secret", [("user", "key is AKIA1234567890ABCDEF"), ("assistant", "ok")])]
    )
    index_transcripts(config, source)
    doc = list_documents(librarian_db(config), source_type="fake-client-transcript")[0]
    full = get_document(librarian_db(config), doc["id"])
    # Secret must not survive in content OR the (first-user-line-derived) title.
    assert "AKIA1234567890ABCDEF" not in full["content"]
    assert "AKIA1234567890ABCDEF" not in (doc["title"] or "")


def test_index_respects_limit(tmp_path):
    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)
    source = _FakeSource([_t(f"s{i}", [("user", f"msg {i}")]) for i in range(5)])
    counts = index_transcripts(config, source, limit=2)
    assert counts["total"] == 2


def test_transcript_gist_is_thread_index(tmp_path):
    from techne.core.cards import pending_cards
    from techne.core.library import index_document
    from techne.core.state import start_session
    from techne.core.threads import Thread, save_threads
    from techne.core.transcripts import build_transcript_gist, fill_transcript_gists

    db = tmp_path / "g.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)
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
        config,
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


class _UnreadableSource(_FakeSource):
    """A source that could not read some of what it was pointed at."""

    def __init__(self, transcripts, unreadable):
        super().__init__(transcripts)
        self.unreadable = unreadable


def test_index_reports_unreadable_from_the_source(tmp_path):
    """`total` counts what was YIELDED, so a source that skips a corrupt file comes up
    short — and a short count with no other signal reads as a smaller corpus."""
    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)
    source = _UnreadableSource(
        [_t("s1", [("user", "alpha")])],
        unreadable=[(Path("/x/corrupt.jsonl.gz"), "zlib.error: invalid code lengths set")],
    )

    counts = index_transcripts(config, source, make_cards=False)

    assert counts["total"] == 1
    assert counts["unreadable"] == 1


def test_index_reports_zero_unreadable_for_a_source_without_the_field(tmp_path):
    """TranscriptSource is a protocol — another platform's adapter need not track it."""
    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)

    counts = index_transcripts(config, _FakeSource([_t("s1", [("user", "alpha")])]))

    assert not hasattr(_FakeSource, "unreadable")
    assert counts["unreadable"] == 0


def test_derive_title_default_takes_every_turn_verbatim():
    """No hook → core stays neutral and knows no client's markers (the old behavior,
    pinned so the hook can never become a silent core-side filter)."""
    turns = [TranscriptTurn("user", "<local-command-caveat>Caveat: generated while\nrunning")]
    assert derive_title(turns) == "<local-command-caveat>Caveat: generated while"


def test_derive_title_skips_turns_the_hook_calls_plumbing():
    """The hook reports plumbing → keep looking, don't give up (techne#2806)."""
    turns = [
        TranscriptTurn("user", "PLUMBING wrapper"),
        TranscriptTurn("assistant", "ignored — titles come from user turns"),
        TranscriptTurn("user", "design the store\nmore"),
    ]
    hook = lambda t: None if t.startswith("PLUMBING") else t  # noqa: E731
    assert derive_title(turns, dialogue_text=hook) == "design the store"


def test_derive_title_uses_text_the_hook_rewrote():
    """A hook may strip a prefix rather than reject the turn; the title comes from
    what it returns, not from the raw text."""
    turns = [TranscriptTurn("user", "<ctx>noise</ctx>the real ask")]
    hook = lambda t: t.replace("<ctx>noise</ctx>", "")  # noqa: E731
    assert derive_title(turns, dialogue_text=hook) == "the real ask"


def test_derive_title_none_when_hook_rejects_every_turn():
    """An all-plumbing session has no dialogue to title it with. Returning None lets
    index_transcripts fall through to its honest `transcript <id>` fallback rather
    than titling the document with a wrapper."""
    turns = [TranscriptTurn("user", "PLUMBING a"), TranscriptTurn("user", "PLUMBING b")]
    hook = lambda t: None  # noqa: E731
    assert derive_title(turns, dialogue_text=hook) is None


def test_index_transcripts_uses_the_sources_dialogue_hook(tmp_path):
    """End-to-end: a source that exposes the hook gets dialogue-derived titles, and
    a source that doesn't is completely unaffected."""
    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)

    turns = [("user", "WRAPPER injected by the client"), ("user", "the actual question")]

    plain = _FakeSource([_t("s-plain", turns)])
    index_transcripts(config, plain)
    assert get_document(librarian_db(config), 1)["title"] == "WRAPPER injected by the client"

    hooked = _FakeSource([_t("s-hooked", turns)])
    hooked.dialogue_text = lambda t: None if t.startswith("WRAPPER") else t
    index_transcripts(config, hooked)
    titles = {d["title"] for d in list_documents(librarian_db(config))}
    assert "the actual question" in titles


def test_optional_hooks_are_not_structural_members_of_the_protocol():
    """techne#818. ``TranscriptSource`` is ``@runtime_checkable``, and
    ``isinstance()`` enforces every ANNOTATED non-method data member by ``hasattr``
    on EVERY Python this project supports — measured 2026-08-29 on 3.9 through
    3.14, and independently re-measured in review against the verbatim pre-fix
    member set. NOT "3.12+", which earlier copies of this note claimed: 3.12
    rewrote the machinery (``__protocol_attrs__`` caching), it did not introduce
    the enforcement. The distinction matters because ``requires-python`` is >=3.11
    while CI matrixes only 3.12 and 3.13, so "3.12+" tells the one reader most
    likely to be checking that the hazard is not theirs.

    Annotating an optional hook therefore makes it REQUIRED, which is
    the opposite of what both hooks are documented to be: #817 did it to
    ``dialogue_text`` and #629 had already done it to ``unreadable``, so the
    minimal adapter shape they exist for stopped satisfying the Protocol.

    Both are conventions read with ``getattr``. Neither may be annotated.
    """

    class Minimal:
        """The #96/#666 adapter shape — declares no optional hook at all."""

        platform = "opencode"

        def iter_transcripts(self):
            return iter(())

    class WithUnreadableOnly:
        """Takes one hook and skips the other; both directions must pass."""

        platform = "opencode"
        unreadable = []

        def iter_transcripts(self):
            return iter(())

    class NotASource:
        platform = "opencode"  # no iter_transcripts

    assert isinstance(Minimal(), TranscriptSource)
    assert isinstance(WithUnreadableOnly(), TranscriptSource)
    # The Protocol keeps its teeth: the one genuinely REQUIRED member still bites.
    assert not isinstance(NotASource(), TranscriptSource)
