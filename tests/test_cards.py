"""Tests for the Librarian Option D document-card layer (techne#91)."""

from __future__ import annotations

import pytest

from techne.config import Config
from techne.core.cards import (
    card_doc_id,
    fill_card_summary,
    find_dangling_cards,
    is_pending,
    pending_cards,
    resolve_card,
    upsert_document_card,
)
from techne.core.database import init_db
from techne.core.documents import store_document
from techne.core.egress import select_tier0_injection
from techne.core.knowledge import content_fingerprint, store


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p).close()
    return p


@pytest.fixture()
def config(db, tmp_path):
    # Domain stores are registry-resolved from the Config now (#102); cards live in
    # techne.db (db), documents in the registry's librarian.db.
    return Config(db_path=db, data_dir=tmp_path)


def _doc(config, url="claude-code-transcript:s1", content="full transcript body"):
    # Documents live in librarian.db (db-per-domain #99); cards in the techne db.
    from techne.core.librarian import librarian_db

    doc_id, _ = store_document(
        librarian_db(config),
        source_type="claude-code-transcript",
        source_url=url,
        content=content,
        title="t",
    )
    return doc_id


def test_upsert_creates_pending_card(db, config):
    doc_id = _doc(config)
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


def test_upsert_idempotent_same_version(db, config):
    doc_id = _doc(config)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert b == a and changed is False


def test_upsert_supersedes_on_version_change(db, config):
    doc_id = _doc(config)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v2", title="x"
    )
    assert changed and b != a
    # Only one active card per source_url.
    assert len(pending_cards(db)) == 1


def test_fill_card_summary_drops_pending(db, config):
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert fill_card_summary(db, card_id, "A crisp two-line gist of the session.")
    assert pending_cards(db) == []  # no longer pending
    resolved = resolve_card(config, card_id)
    assert resolved["id"] == doc_id  # pointer still resolves


def test_fill_card_summary_flags_vec_stale(db, config):
    """techne#382: editing a card's gist in place flags the row for re-embed
    (vec_stale=1), so backfill regenerates its vector instead of matching the
    OLD gist. A fresh card is unflagged (its vector, if any, matches its gist)."""
    from techne.core.database import connect

    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )

    def _vec_stale():
        conn = connect(db)
        try:
            return conn.execute(
                "SELECT vec_stale FROM knowledge WHERE id = ?", (card_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    assert _vec_stale() == 0  # freshly created — not flagged
    assert fill_card_summary(db, card_id, "A crisp two-line gist of the session.")
    assert _vec_stale() == 1  # gist edited in place → flagged for re-embed


def test_resolve_card_fetches_document(db, config):
    doc_id = _doc(config, content="the full body to fetch on demand")
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id, source_version="v1", title="x"
    )
    doc = resolve_card(config, card_id)
    assert doc["id"] == doc_id
    assert "fetch on demand" in doc["content"]


def test_document_card_is_pull_only_excluded_from_egress(db):
    # A high-confidence card must NOT be injected — it's recall-only reference.
    card = store(db, "card gist", type="document_card", confidence="high", tags="document_card")
    fact = store(db, "a real fact", type="fact", confidence="high", tags="memory-type:user")
    injected = [r["id"] for r in select_tier0_injection(db, char_budget=4000)]
    assert fact in injected
    assert card not in injected


def test_emit_card_summary_batch(db, config):
    doc_id = _doc(config, content="we decided to use SQLite for the store")
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="storage decision"
    )
    from techne.core.cards import emit_card_summary_batch

    batch = emit_card_summary_batch(config)
    assert len(batch) == 1
    item = batch[0]
    assert item["card_id"] == card_id
    assert item["summary"] == ""  # placeholder for the agent to fill
    assert "use SQLite" in item["prompt"]  # the doc content is in the prompt


def test_ingest_card_summaries_fills_and_skips(db, config):
    doc_id = _doc(config)
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


def test_pending_cards_source_prefix_filter(db, config):
    doc = _doc(config)
    upsert_document_card(db, source_url="docs:a.md", doc_id=doc, source_version="v1", title="a")
    upsert_document_card(
        db, source_url="cc-transcript:s1", doc_id=doc, source_version="v1", title="b"
    )
    all_pending = pending_cards(db)
    docs_only = pending_cards(db, source_prefix="docs:")
    assert len(all_pending) == 2
    assert len(docs_only) == 1 and docs_only[0]["source"] == "docs:a.md"


def test_find_dangling_cards_clean(db, config):
    content = "the full body to fetch on demand"
    doc_id = _doc(config, content=content)
    upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version=content_fingerprint(content), title="x"
    )
    report = find_dangling_cards(config)
    assert report == {"total": 1, "dangling": [], "stale": []}


def test_find_dangling_cards_reports_missing_pointer(db, config):
    """#104: a card whose doc id doesn't exist in librarian.db (e.g. librarian.db
    rebuilt out of sync) is reported, not silently None."""
    upsert_document_card(db, source_url="u", doc_id=9999, source_version="v1", title="x")
    report = find_dangling_cards(config)
    assert report["total"] == 1
    assert len(report["dangling"]) == 1 and report["dangling"][0]["doc_id"] == 9999
    assert report["stale"] == []


def test_find_dangling_cards_reports_stale_version(db, config):
    """A card whose ver: no longer matches the live document content is flagged."""
    doc_id = _doc(config, content="current body")
    upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="hash-of-an-older-body", title="x"
    )
    report = find_dangling_cards(config)
    assert report["dangling"] == []
    assert len(report["stale"]) == 1 and report["stale"][0]["doc_id"] == doc_id


# ---------------------------------------------------------------------------
# #244 — write-site redaction: fill_card_summary
# ---------------------------------------------------------------------------


def test_fill_card_summary_redacts_credential(db, config):
    """#244: fill_card_summary must store the agent gist redacted (techne#244).

    GitHub PAT in the gist — uses a long-established pattern so this test does
    not depend on any single new #243 pattern.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    gist_with_secret = (
        "The repo was cloned using ghp_abcdefghijklmnopqrstuvwxyz1234 "
        "and the migration was applied."
    )
    result = fill_card_summary(db, card_id, gist_with_secret)
    assert result is True

    # Read the knowledge row back directly to verify the stored value
    from techne.core.database import connect

    conn = connect(db)
    row = conn.execute("SELECT content FROM knowledge WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    stored_content = row["content"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in stored_content
    assert "<redacted:github>" in stored_content


def test_insert_card_redacts_content_defense_in_depth(db, config):
    """#244 (review): _insert_card redacts at the INSERT boundary itself, so a
    credential reaching the card content by any path (here a doc title used as the
    stand-in gist) is redacted at rest — not only when the upstream caller remembers.
    """
    doc_id = _doc(config)
    # No summary → the title becomes the card's content (the placeholder gist),
    # exercising _insert_card's own redaction rather than an upstream wrapper.
    card_id, changed = upsert_document_card(
        db,
        source_url="u-secret",
        doc_id=doc_id,
        source_version="v1",
        title="issue mentions ghp_abcdefghijklmnopqrstuvwxyz1234 in the body",
    )
    assert changed

    from techne.core.database import connect

    conn = connect(db)
    row = conn.execute("SELECT content FROM knowledge WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in row["content"]
    assert "<redacted:github>" in row["content"]
