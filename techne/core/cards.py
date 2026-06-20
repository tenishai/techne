"""Document cards — the Librarian Option D recall layer (techne#91).

A *card* is a compact knowledge row (type=document_card) that points at a row in
the `documents` table. Recall returns the card's GIST; full content is fetched ON
DEMAND via the pointer — "remember a thing exists + roughly what it says, fetch the
detail when needed." Cards are pull-only (recall-able, never injected).

Mirror-backed for now: the pointer is a local `documents.id` and `source_version`
is the document's content hash. (Pointer-backed sources — Jira/Confluence, live
fetch through limenal — come later, cabal#15.)

The gist is summarized agent-side (techne can't call a model — no API key). A card
is created `pending` with the document title as a stand-in gist, then filled later
via the emit/ingest summarization pass — the same shape as reflect.

State encoded in the knowledge row (no schema change):
  source = the document's source_url (the stable pointer)
  tags   = document_card, backing:<mode>, doc:<id>, ver:<source_version> [, pending]
"""

from __future__ import annotations

from pathlib import Path

from techne.core.database import connect
from techne.core.documents import get_document
from techne.core.knowledge import CONFIDENCE_NUMERIC

CARD_TYPE = "document_card"


def _tag_value(tags: str | None, prefix: str) -> str | None:
    for t in (tags or "").split(","):
        t = t.strip()
        if t.startswith(prefix):
            return t[len(prefix) :]
    return None


def card_doc_id(tags: str | None) -> int | None:
    v = _tag_value(tags, "doc:")
    return int(v) if v and v.isdigit() else None


def is_pending(tags: str | None) -> bool:
    return "pending" in [t.strip() for t in (tags or "").split(",")]


def _card_tags(doc_id: int, source_version: str, backing_mode: str, pending: bool) -> str:
    parts = [CARD_TYPE, f"backing:{backing_mode}", f"doc:{doc_id}", f"ver:{source_version}"]
    if pending:
        parts.append("pending")
    return ",".join(parts)


def _insert_card(conn, content: str, source_url: str, tags: str, confidence: str) -> int:
    cur = conn.execute(
        """INSERT INTO knowledge (type, content, source, tags, confidence, confidence_numeric)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (CARD_TYPE, content, source_url, tags, confidence, CONFIDENCE_NUMERIC[confidence]),
    )
    return cur.lastrowid


def upsert_document_card(
    db_path: Path,
    *,
    source_url: str,
    doc_id: int,
    source_version: str,
    title: str,
    backing_mode: str = "mirror",
    summary: str | None = None,
    confidence: str = "medium",
) -> tuple[int, bool]:
    """Create/refresh the card for a document. One active card per source_url.

    - No active card → insert (pending unless a summary is given).
    - Active card, same source_version → no-op.
    - Active card, version changed → supersede it, insert a fresh (pending) card.

    Returns (card_id, changed).
    """
    pending = summary is None
    content = summary or title or f"document {doc_id}"
    tags = _card_tags(doc_id, source_version, backing_mode, pending)

    conn = connect(db_path)
    try:
        existing = conn.execute(
            """SELECT id, tags FROM knowledge
               WHERE type = ? AND source = ? AND superseded_by IS NULL""",
            (CARD_TYPE, source_url),
        ).fetchone()

        if existing is not None and _tag_value(existing["tags"], "ver:") == str(source_version):
            return existing["id"], False

        new_id = _insert_card(conn, content, source_url, tags, confidence)
        if existing is not None:
            conn.execute(
                """UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_id, existing["id"]),
            )
        conn.commit()
        return new_id, True
    finally:
        conn.close()


def fill_card_summary(db_path: Path, card_id: int, summary: str) -> bool:
    """Fill a card's gist in place and drop `pending`. No supersede — the document
    version is unchanged, only the summary got better. Returns True if updated."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT tags FROM knowledge WHERE id = ? AND type = ?", (card_id, CARD_TYPE)
        ).fetchone()
        if row is None:
            return False
        tags = ",".join(t for t in (row["tags"] or "").split(",") if t.strip() != "pending")
        conn.execute(
            """UPDATE knowledge
               SET content = ?, tags = ?, updated_at = CURRENT_TIMESTAMP,
                   last_confirmed_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (summary, tags, card_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def pending_cards(db_path: Path, limit: int = 100) -> list[dict]:
    """Active cards still awaiting an agent-written gist (the emit work-list)."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, content, source, tags FROM knowledge
               WHERE type = ? AND superseded_by IS NULL AND tags LIKE '%pending%'
               ORDER BY updated_at DESC LIMIT ?""",
            (CARD_TYPE, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_card(db_path: Path, card_id: int) -> dict | None:
    """Fetch the full document a card points at — the on-demand half of recall."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT tags FROM knowledge WHERE id = ? AND type = ?", (card_id, CARD_TYPE)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    doc_id = card_doc_id(row["tags"])
    return get_document(db_path, doc_id) if doc_id is not None else None
