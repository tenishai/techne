"""Librarian domain — the documents store, in its own db file (techne#99).

db-per-domain (`docs/db-per-domain.md`): `documents` no longer lives in techne core's
`SCHEMA_SQL` — it lives in `librarian.db`, reached through the store registry
(techne#98). This module OWNS the Librarian schema and hands callers the migrated
store. Gist cards stay in `techne.db` (`knowledge`, type=document_card) and resolve
here cross-db (a keyed get, no ATTACH).
"""

from __future__ import annotations

from pathlib import Path

from techne.core.stores import SqliteStore

# The documents schema — moved verbatim out of core SCHEMA_SQL/FTS_TRIGGERS_SQL.
# Applied to librarian.db via the store's migrate(), not to techne.db.
LIBRARIAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    meta TEXT,
    owned_by TEXT NOT NULL DEFAULT 'librarian',
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents(source_url);
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_superseded ON documents(superseded_by);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    content, title, content=documents, content_rowid=id,
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, content, title)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.title, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, content, title)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.title, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, content, title)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.title, ''));
    INSERT INTO documents_fts(rowid, content, title)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.title, ''));
END;
"""

# Migrate once per db_path per process (CREATE IF NOT EXISTS is idempotent, but no
# point re-running executescript on every document op).
_migrated: set[Path] = set()


def librarian_store(techne_db_path: Path) -> SqliteStore:
    """The Librarian's store (librarian.db, beside the given techne db), schema
    applied. The documents domain routes all persistence through here instead of
    techne.db. Reuses #98's SqliteStore (so the registry path is the same default).

    NOTE: honoring a config.stores dsn override (a non-default librarian path) is a
    follow-up — internal callers are db_path-shaped, so we derive the standard
    `<data_dir>/librarian.db` location, which equals the registry default.
    """
    db = Path(techne_db_path).parent / "librarian.db"
    store = SqliteStore("librarian", db)
    if db not in _migrated:
        store.migrate(LIBRARIAN_SCHEMA)
        _migrated.add(db)
    return store


def librarian_db(techne_db_path: Path) -> Path:
    """Path to the migrated librarian.db — what the low-level documents.py functions
    (still db_path-shaped) operate on."""
    return librarian_store(techne_db_path).db_path


def migrate_documents_to_librarian(techne_db_path: Path) -> dict:
    """One-time cutover: copy any `documents` rows still in techne.db (where they
    used to live) into librarian.db, PRESERVING ids so existing cards resolve, then
    drop the documents tables from techne.db. Idempotent — no documents table in
    techne.db → no-op. Verifies the copy before dropping."""
    from techne.core.database import connect

    src = connect(techne_db_path)
    try:
        has = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if not has:
            return {"moved": 0, "dropped": False, "note": "no documents table in techne.db"}
        rows = [dict(r) for r in src.execute("SELECT * FROM documents").fetchall()]
        src_active = src.execute(
            "SELECT COUNT(*) FROM documents WHERE superseded_by IS NULL"
        ).fetchone()[0]
    finally:
        src.close()

    store = librarian_store(techne_db_path)  # creates + migrates librarian.db
    dest = store.connect()
    try:
        # superseded_by self-references mean a row can point at a not-yet-copied id;
        # turn off FK enforcement for the bulk copy (the data is already consistent).
        dest.execute("PRAGMA foreign_keys=OFF")
        for d in rows:
            cols = ", ".join(d)
            ph = ", ".join("?" * len(d))
            dest.execute(
                f"INSERT OR IGNORE INTO documents ({cols}) VALUES ({ph})", tuple(d.values())
            )
        dest.commit()
        dest_active = dest.execute(
            "SELECT COUNT(*) FROM documents WHERE superseded_by IS NULL"
        ).fetchone()[0]
    finally:
        dest.close()

    # Only drop the source once the destination has at least as many active rows.
    if dest_active < src_active:
        return {"moved": len(rows), "dropped": False, "note": "verify failed — source kept"}

    src = connect(techne_db_path)
    try:
        src.executescript(
            "DROP TRIGGER IF EXISTS documents_ai; DROP TRIGGER IF EXISTS documents_ad; "
            "DROP TRIGGER IF EXISTS documents_au; DROP TABLE IF EXISTS documents_fts; "
            "DROP TABLE IF EXISTS documents;"
        )
        src.commit()
    finally:
        src.close()
    return {"moved": len(rows), "dropped": True, "active": dest_active}
