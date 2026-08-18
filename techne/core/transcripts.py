"""Canonical transcript shape + platform-neutral indexing into the Librarian (techne#91/#57).

A *transcript* is a client's session dialogue. Each client (Claude Code, opencode,
Aider, …) stores it in its own format and location — but recall doesn't care about
formats. So we define ONE canonical shape (`Transcript`) and one neutral indexer;
each platform supplies a small adapter (`TranscriptSource`) whose only job is
"my native format → `Transcript`". Everything downstream — rendering, redaction,
dedup/supersede, storage, FTS/vector recall — is shared and platform-agnostic.

To support a new client: implement `TranscriptSource` (a `platform` name + an
`iter_transcripts()` that yields `Transcript`s). Nothing else changes. The neutral
core never imports a platform module; platforms depend on this, not the reverse.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from techne.config import Config

# Normalized roles the renderer labels; a platform maps its own roles onto these.
_ROLE_LABEL = {"user": "User", "assistant": "Assistant"}


@dataclass
class TranscriptTurn:
    """One conversational turn. `role` is normalized ('user'/'assistant'); other
    roles are allowed and rendered title-cased. `text` is the turn's prose —
    tool-call plumbing is the adapter's to drop (recall wants the dialogue)."""

    role: str
    text: str


@dataclass
class Transcript:
    """The canonical shape every platform's transcript normalizes to.

    id:       stable per-session id (the dedup key — re-indexing supersedes).
    platform: the producing client ('claude-code', 'opencode', …).
    turns:    the conversation, plumbing stripped.
    uri:      original location (path/url) for provenance.
    title:    explicit title; if None, derived from the first user turn.
    meta:     platform-specific extras (carried into the document's meta json).
    """

    id: str
    platform: str
    turns: list[TranscriptTurn]
    uri: str | None = None
    title: str | None = None
    meta: dict = field(default_factory=dict)


@runtime_checkable
class TranscriptSource(Protocol):
    """A platform adapter: discovers and parses that client's transcripts into the
    canonical shape. The ONLY thing a new client must implement."""

    platform: str

    #: OPTIONAL. Transcripts this source could not read at all, as ``(path, error)``
    #: pairs, populated AS IT ITERATES. ``index_transcripts`` reads it via
    #: ``getattr(source, "unreadable", ())`` and reports ``len()`` of it, so a source
    #: that skips a corrupt file is not silently indistinguishable from a smaller
    #: corpus. Declared here because it was previously readable from any source and
    #: documented on none — a new adapter author had no way to learn the attribute
    #: name or its shape (techne#629).
    #:
    #: It must be SIZED (``len()`` is called on it) and it must be a real attribute
    #: on the source, not a generator: a lazy iterator would report 0 before the
    #: walk and be consumed by the reporting. Omit it entirely and the count is 0.
    unreadable: Sequence[tuple[Path, str]]

    def iter_transcripts(self) -> Iterator[Transcript]:
        """Yield each available transcript as a `Transcript`. Lazy (a generator)
        so the indexer can cap with --limit without parsing everything."""
        ...


def render_transcript(turns: list[TranscriptTurn]) -> str:
    """Render turns as readable '## Role\\n\\ntext' markdown, skipping empty turns.
    Platform-neutral — operates only on the canonical shape."""
    parts = []
    for turn in turns:
        text = (turn.text or "").strip()
        if not text:
            continue
        label = _ROLE_LABEL.get(turn.role, turn.role.replace("_", " ").title())
        parts.append(f"## {label}\n\n{text}")
    return "\n\n".join(parts)


def derive_title(turns: list[TranscriptTurn]) -> str | None:
    """Title = the first user turn's first line (what the session was about)."""
    for turn in turns:
        if turn.role == "user" and (turn.text or "").strip():
            return turn.text.strip().split("\n", 1)[0][:80]
    return None


def build_transcript_gist(db_path: Path, session_id: str) -> str | None:
    """The honest gist for a (possibly long, multi-topic) transcript: a thread
    INDEX — what the session covered + when — built from techne's thread detection,
    not a prose summary that pretends one sentence captures a day's work. Returns
    None when the session has no detected threads (card stays pending/title)."""
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        sess = conn.execute(
            "SELECT started_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        threads = conn.execute(
            "SELECT label FROM threads WHERE session_id = ? ORDER BY thread_num", (session_id,)
        ).fetchall()
    finally:
        conn.close()
    if not threads:
        return None
    when = sess["started_at"][:16] if sess and sess["started_at"] else "?"
    labels = "; ".join(t["label"] for t in threads)
    return f"Session transcript ({when}, {len(threads)} threads): {labels}"


def fill_transcript_gists(db_path: Path) -> dict:
    """(Re)generate the thread-index gist for every active transcript card. Idempotent
    — run after new sessions/threads land. Returns {filled, skipped}."""
    from techne.core.cards import CARD_TYPE, fill_card_summary
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, source FROM knowledge
               WHERE type = ? AND superseded_by IS NULL AND source LIKE '%-transcript:%'""",
            (CARD_TYPE,),
        ).fetchall()
    finally:
        conn.close()

    counts = {"filled": 0, "skipped": 0}
    for r in rows:
        session_id = r["source"].split("-transcript:", 1)[1]
        gist = build_transcript_gist(db_path, session_id)
        if gist and fill_card_summary(db_path, r["id"], gist):
            counts["filled"] += 1
        else:
            counts["skipped"] += 1
    return counts


def index_transcripts(
    config: Config,
    source: TranscriptSource,
    *,
    limit: int | None = None,
    make_cards: bool = True,
) -> dict:
    """Index every transcript a `source` yields into the Librarian documents store.

    Platform-agnostic: works with ANY TranscriptSource. Content is redacted; rows
    are deduped/superseded by source_url so re-running only rewrites changed
    sessions. When `make_cards`, each document also gets a pull-only Option D card
    (gist+pointer, pending) so it rides knowledge recall. Returns counts
    {total, stored, unchanged, empty, unreadable}.

    ``unreadable`` is how many transcripts the source could not read at all. It is
    reported because ``total`` counts what was YIELDED — a source that skips a corrupt
    file comes up short, and a short count with no other signal is indistinguishable
    from a smaller corpus. Sources that do not track it report 0.
    """
    counts = {"total": 0, "stored": 0, "unchanged": 0, "empty": 0, "unreadable": 0}
    transcripts = source.iter_transcripts()
    # `is not None`, not truthiness: `--limit 0` used to mean NO LIMIT and iterate
    # the whole directory — measured, and the opposite of what the flag says
    # (noted alongside techne#631). An explicit 0 is a legitimate "index nothing",
    # which is how every other bounded surface in this codebase reads it.
    if limit is not None and limit > 0:
        transcripts = itertools.islice(transcripts, limit)
    elif limit == 0:
        transcripts = iter(())

    from techne.core.library import index_document

    for t in transcripts:
        counts["total"] += 1
        text = render_transcript(t.turns)
        if not text:
            counts["empty"] += 1
            continue
        title = t.title or derive_title(t.turns) or f"transcript {t.id[:12]}"
        meta = json.dumps(
            {"platform": t.platform, "id": t.id, "uri": t.uri, "turns": len(t.turns), **t.meta}
        )
        # index_document redacts (content + title), stores, and upserts the card.
        # Transcript cards are born with the thread-index gist (not pending) — a
        # prose summary of a long multi-topic session would be dishonest.
        _doc_id, stored = index_document(
            config,
            source_type=f"{t.platform}-transcript",
            source_url=f"{t.platform}-transcript:{t.id}",
            title=title,
            content=text,
            meta=meta,
            make_card=make_cards,
            # thread index is built from techne.db (core threads), not the doc store
            summary=build_transcript_gist(config.db_path, t.id) if make_cards else None,
        )
        counts["stored" if stored else "unchanged"] += 1
    # AFTER the loop — the source populates this as it iterates, so reading it earlier
    # would report 0. Under `limit` this covers only the transcripts actually reached.
    counts["unreadable"] = len(getattr(source, "unreadable", ()))
    return counts
