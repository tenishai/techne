"""Librarian document ingestion — the shared store+card unit, and a markdown source.

`index_document` is the funnel every document source goes through: redact → store
(dedup/supersede by source_url) → upsert an Option D card. Transcript indexing and
markdown-directory indexing both use it, so adding a new document source is just
"produce (source_type, source_url, title, content) and call index_document" (techne#91).

The core never imports a source; sources import this. `index_markdown_dir` is the
second source (after transcripts) — plain markdown files, no conversation shape —
which is why the shared unit gets factored out here rather than duplicated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from techne.config import Config
from techne.core.cards import upsert_document_card
from techne.core.documents import store_document
from techne.core.knowledge import content_hash
from techne.core.librarian import librarian_db
from techne.core.redaction import redact_credentials


def index_document(
    config: Config,
    *,
    source_type: str,
    source_url: str,
    title: str,
    content: str,
    meta: str | None = None,
    make_card: bool = True,
    backing_mode: str = "mirror",
    summary: str | None = None,
) -> tuple[int, bool]:
    """Store one document (redacted, deduped) and upsert its Option D card.

    The single store+card step shared by every document source. A `summary` makes
    the card non-pending at birth (e.g. a transcript's deterministic thread index);
    omit it to leave the card pending for agent-assisted summarization. Returns
    (doc_id, stored) — stored=True iff a new/changed row was written.

    The document goes to librarian.db (registry-resolved, #102); the gist card goes
    to techne.db (`config.db_path`) so personal recall surfaces it.
    """
    title = redact_credentials(title or "")
    content = redact_credentials(content)
    # Document → librarian.db (db-per-domain); the gist card → techne.db below.
    doc_id, stored = store_document(
        librarian_db(config),
        source_type=source_type,
        source_url=source_url,
        title=title,
        content=content,
        meta=meta,
    )
    if make_card:
        upsert_document_card(
            config.db_path,
            source_url=source_url,
            doc_id=doc_id,
            source_version=content_hash(content),
            title=title or source_url,
            backing_mode=backing_mode,
            summary=redact_credentials(summary) if summary else None,
        )
    return doc_id, stored


_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _markdown_title(text: str, fallback: str) -> str:
    """Title = the first markdown heading, else the filename stem."""
    m = _HEADING.search(text)
    return m.group(1).strip()[:80] if m else fallback


def index_markdown_dir(
    config: Config,
    root: Path,
    *,
    source_type: str = "markdown",
    make_cards: bool = True,
) -> dict:
    """Index every *.md under `root` as a Librarian document (+ card). Deduped by
    source_url=`<source_type>:<relpath>`, so re-running only rewrites changed files.
    Returns counts {total, stored, unchanged, empty}."""
    root = Path(root)
    counts = {"total": 0, "stored": 0, "unchanged": 0, "empty": 0}
    for md in sorted(root.glob("**/*.md")):
        counts["total"] += 1
        text = md.read_text(encoding="utf-8").strip()
        if not text:
            counts["empty"] += 1
            continue
        rel = md.relative_to(root).as_posix()
        _doc_id, stored = index_document(
            config,
            source_type=source_type,
            source_url=f"{source_type}:{rel}",
            title=_markdown_title(text, md.stem),
            content=text,
            meta=json.dumps({"path": str(md), "rel": rel}),
            make_card=make_cards,
        )
        counts["stored" if stored else "unchanged"] += 1
    return counts
