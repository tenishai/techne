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
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from techne.core.documents import store_document
from techne.core.redaction import redact_credentials

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


def index_transcripts(
    db_path: Path,
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
    {total, stored, unchanged, empty}.
    """
    counts = {"total": 0, "stored": 0, "unchanged": 0, "empty": 0}
    transcripts = source.iter_transcripts()
    if limit:
        transcripts = itertools.islice(transcripts, limit)

    for t in transcripts:
        counts["total"] += 1
        text = render_transcript(t.turns)
        if not text:
            counts["empty"] += 1
            continue
        # Redact the title too — it's derived from the first user line, which can
        # carry a secret just as the body can.
        title = redact_credentials(t.title or derive_title(t.turns) or f"transcript {t.id[:12]}")
        content = redact_credentials(text)
        source_url = f"{t.platform}-transcript:{t.id}"
        meta = json.dumps(
            {"platform": t.platform, "id": t.id, "uri": t.uri, "turns": len(t.turns), **t.meta}
        )
        doc_id, stored = store_document(
            db_path,
            source_type=f"{t.platform}-transcript",
            source_url=source_url,
            title=title,
            content=content,
            meta=meta,
        )
        counts["stored" if stored else "unchanged"] += 1
        if make_cards:
            from techne.core.cards import upsert_document_card
            from techne.core.knowledge import content_hash

            upsert_document_card(
                db_path,
                source_url=source_url,
                doc_id=doc_id,
                source_version=content_hash(content),
                title=title,
                backing_mode="mirror",
            )
    return counts
