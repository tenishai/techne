"""Tests for the Librarian documents cutover techne.db → librarian.db (techne#99/#100)."""

from __future__ import annotations

from techne.core.database import connect, init_db
from techne.core.documents import get_document, store_document
from techne.core.librarian import (
    LIBRARIAN_SCHEMA,
    librarian_db,
    migrate_documents_to_librarian,
)


def _techne_with_legacy_docs(tmp_path):
    """A techne.db that still has the `documents` table (the pre-split state the
    cutover migrates out of)."""
    db = tmp_path / "techne.db"
    conn = init_db(db)
    conn.executescript(LIBRARIAN_SCHEMA)  # simulate documents having lived in techne.db
    conn.commit()
    conn.close()
    return db


def _has_documents_table(db) -> bool:
    conn = connect(db)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_migrate_moves_documents_and_drops_source(tmp_path):
    techne = _techne_with_legacy_docs(tmp_path)
    id1, _ = store_document(techne, source_type="t", source_url="u1", content="alpha", title="A")
    id2, _ = store_document(techne, source_type="t", source_url="u2", content="beta", title="B")

    result = migrate_documents_to_librarian(techne)
    assert result["dropped"] is True and result["verified"] == 2

    # source documents table dropped from techne.db
    assert not _has_documents_table(techne)
    # present in librarian.db with PRESERVED ids (so cards resolve)
    lib = librarian_db(techne)
    assert get_document(lib, id1)["content"] == "alpha"
    assert get_document(lib, id2)["content"] == "beta"


def test_migrate_noop_without_documents_table(tmp_path):
    db = tmp_path / "techne.db"
    init_db(db).close()  # plain techne.db, no documents (post-split / fresh install)
    result = migrate_documents_to_librarian(db)
    assert result == {"moved": 0, "dropped": False, "note": "no documents table in techne.db"}


def test_migrate_refuses_to_drop_on_id_collision(tmp_path):
    """The #100 bug: a non-empty librarian.db whose ids collide with techne.db's must
    NOT cause a legacy doc to be silently dropped (INSERT OR IGNORE skips it, a count
    check would still pass). Verify-by-identity must catch it and KEEP the source."""
    techne = _techne_with_legacy_docs(tmp_path)
    legacy_id, _ = store_document(
        techne, source_type="t", source_url="legacy-url", content="LEGACY CONTENT", title="L"
    )
    # pre-seed librarian.db with a DIFFERENT doc that grabs the same autoincrement id
    lib = librarian_db(techne)
    other_id, _ = store_document(
        lib, source_type="t", source_url="other-url", content="OTHER CONTENT", title="O"
    )
    assert other_id == legacy_id  # both got id=1 → collision

    result = migrate_documents_to_librarian(techne)

    assert result["dropped"] is False
    assert "verify failed" in result["note"]
    # legacy doc is SAFE — still in techne.db, not dropped
    assert _has_documents_table(techne)
    conn = connect(techne)
    try:
        row = conn.execute(
            "SELECT content FROM documents WHERE source_url='legacy-url'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["content"] == "LEGACY CONTENT"
    # librarian.db's pre-existing doc is untouched (not clobbered by the legacy id)
    assert get_document(lib, legacy_id)["content"] == "OTHER CONTENT"
