"""Tests for the Librarian Option D document-card layer (techne#91)."""

from __future__ import annotations

import pytest

from techne.core.cards import (
    card_doc_id,
    fill_card_summary,
    is_pending,
    pending_cards,
    resolve_card,
    upsert_document_card,
)
from techne.core.database import init_db
from techne.core.documents import store_document
from techne.core.egress import select_tier0_injection
from techne.core.knowledge import store


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "cards.db"
    init_db(p).close()
    return p


def _doc(db, url="claude-code-transcript:s1", content="full transcript body"):
    # Documents live in librarian.db (db-per-domain #99); cards in the techne db.
    from techne.core.librarian import librarian_db

    doc_id, _ = store_document(
        librarian_db(db),
        source_type="claude-code-transcript",
        source_url=url,
        content=content,
        title="t",
    )
    return doc_id


def test_upsert_creates_pending_card(db):
    doc_id = _doc(db)
    card_id, changed = upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",
        title="decide the backend",
    )
    assert changed
    rows = [r for r in pending_cards(db)]
    assert len(rows) == 1
    card = rows[0]
    assert card["id"] == card_id
    assert card["content"] == "decide the backend"  # gist = title placeholder
    assert is_pending(card["tags"])
    assert card_doc_id(card["tags"]) == doc_id
    assert "backing:mirror" in card["tags"]


def test_upsert_idempotent_same_version(db):
    doc_id = _doc(db)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert b == a and changed is False


def test_upsert_supersedes_on_version_change(db):
    doc_id = _doc(db)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v2", title="x"
    )
    assert changed and b != a
    # Only one active card per source_url.
    assert len(pending_cards(db)) == 1


def test_fill_card_summary_drops_pending(db):
    doc_id = _doc(db)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert fill_card_summary(db, card_id, "A crisp two-line gist of the session.")
    assert pending_cards(db) == []  # no longer pending
    resolved = resolve_card(db, card_id)
    assert resolved["id"] == doc_id  # pointer still resolves


def test_resolve_card_fetches_document(db):
    doc_id = _doc(db, content="the full body to fetch on demand")
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id, source_version="v1", title="x"
    )
    doc = resolve_card(db, card_id)
    assert doc["id"] == doc_id
    assert "fetch on demand" in doc["content"]


def test_document_card_is_pull_only_excluded_from_egress(db):
    # A high-confidence card must NOT be injected — it's recall-only reference.
    card = store(db, "card gist", type="document_card", confidence="high", tags="document_card")
    fact = store(db, "a real fact", type="fact", confidence="high", tags="memory-type:user")
    injected = [r["id"] for r in select_tier0_injection(db, char_budget=4000)]
    assert fact in injected
    assert card not in injected


def test_emit_card_summary_batch(db):
    doc_id = _doc(db, content="we decided to use SQLite for the store")
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="storage decision"
    )
    from techne.core.cards import emit_card_summary_batch

    batch = emit_card_summary_batch(db)
    assert len(batch) == 1
    item = batch[0]
    assert item["card_id"] == card_id
    assert item["summary"] == ""  # placeholder for the agent to fill
    assert "use SQLite" in item["prompt"]  # the doc content is in the prompt


def test_ingest_card_summaries_fills_and_skips(db):
    doc_id = _doc(db)
    c1, _ = upsert_document_card(db, source_url="u1", doc_id=doc_id, source_version="v1", title="a")
    c2, _ = upsert_document_card(db, source_url="u2", doc_id=doc_id, source_version="v1", title="b")
    from techne.core.cards import ingest_card_summaries

    counts = ingest_card_summaries(
        db,
        [
            {"card_id": c1, "summary": "A crisp gist."},
            {"card_id": c2, "summary": "   "},  # blank → skipped, stays pending
        ],
    )
    assert counts == {"filled": 1, "skipped": 1, "missing": 0}
    still_pending = {r["id"] for r in pending_cards(db)}
    assert c1 not in still_pending and c2 in still_pending


def test_pending_cards_source_prefix_filter(db):
    doc = _doc(db)
    upsert_document_card(db, source_url="docs:a.md", doc_id=doc, source_version="v1", title="a")
    upsert_document_card(
        db, source_url="cc-transcript:s1", doc_id=doc, source_version="v1", title="b"
    )
    all_pending = pending_cards(db)
    docs_only = pending_cards(db, source_prefix="docs:")
    assert len(all_pending) == 2
    assert len(docs_only) == 1 and docs_only[0]["source"] == "docs:a.md"
