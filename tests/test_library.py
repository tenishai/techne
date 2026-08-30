"""Tests for the shared document-ingestion unit + markdown-directory source (techne#91)."""

from __future__ import annotations

from techne.config import Config
from techne.core.cards import card_doc_id, pending_cards
from techne.core.database import init_db
from techne.core.documents import get_document, list_documents
from techne.core.librarian import librarian_db
from techne.core.library import index_document, index_markdown_dir


def _db(tmp_path):
    # index_document now takes a Config (registry-routed stores, #102); the core
    # techne db holds the cards, librarian.db (registry-resolved) holds the docs.
    p = tmp_path / "techne.db"
    init_db(p).close()
    return Config(db_path=p, data_dir=tmp_path)


def _lib(cfg):
    # documents live in librarian.db now (db-per-domain #99)
    return librarian_db(cfg)


def test_index_document_stores_and_cards(tmp_path):
    db = _db(tmp_path)
    doc_id, stored = index_document(
        db,
        source_type="docs",
        source_url="docs:eval.md",
        title="Eval",
        content="how techne is evaluated",
    )
    assert stored
    assert get_document(_lib(db), doc_id)["content"] == "how techne is evaluated"
    cards = pending_cards(db.db_path)
    assert len(cards) == 1 and card_doc_id(cards[0]["tags"]) == doc_id


def test_index_document_redacts(tmp_path):
    db = _db(tmp_path)
    index_document(
        db,
        source_type="docs",
        source_url="docs:secret.md",
        title="AKIA1234567890ABCDEF",
        content="key AKIA1234567890ABCDEF here",
    )
    doc = list_documents(_lib(db), source_type="docs")[0]
    full = get_document(_lib(db), doc["id"])
    assert "AKIA1234567890ABCDEF" not in full["content"]
    assert "AKIA1234567890ABCDEF" not in (doc["title"] or "")


def test_index_document_make_card_false(tmp_path):
    db = _db(tmp_path)
    index_document(
        db, source_type="docs", source_url="docs:x.md", title="x", content="y", make_card=False
    )
    assert pending_cards(db.db_path) == []


def test_index_markdown_dir(tmp_path):
    db = _db(tmp_path)
    root = tmp_path / "docs"
    (root / "proposals").mkdir(parents=True)
    (root / "eval.md").write_text("# Eval\n\nhow we measure recall", encoding="utf-8")
    (root / "proposals" / "outbox.md").write_text("# Outbox sinks\n\nthe design", encoding="utf-8")
    (root / "empty.md").write_text("   ", encoding="utf-8")

    counts = index_markdown_dir(db, root, source_type="docs")
    assert counts == {
        "total": 3,
        "stored": 2,
        "unchanged": 0,
        "empty": 1,
        # techne#819 — a fresh index repairs nothing; the key is present and zero so
        # this path cannot report a pure no-op while having written.
        "titles_repaired": 0,
    }

    docs = list_documents(_lib(db), source_type="docs")
    urls = {d["source_url"] for d in docs}
    assert urls == {"docs:eval.md", "docs:proposals/outbox.md"}
    titles = {d["title"] for d in docs}
    assert titles == {"Eval", "Outbox sinks"}  # first markdown heading

    # Re-index unchanged → no rewrites; nested path preserved.
    counts2 = index_markdown_dir(db, root, source_type="docs")
    assert counts2["stored"] == 0 and counts2["unchanged"] == 2
