"""Documents library — external reference corpora stored in a techne db.

This is the Librarian domain. Documents are a separate layer from knowledge:
no cross-table FK, provenance is carried via `source_url` (a plain string
that works across separate dbs). The library db is intended to be its own
techne db (with empty knowledge/observations/threads tables), configured via
a distinct `db_path` — that separation is a deployment convention, not
enforced here.

Core CRUD:
  store_document    — insert or supersede-on-content-change
  get_document      — fetch one document by id
  list_documents    — list active (non-superseded) documents with filters

Deferred (note in-module so reviewers can track scope):
  - document_chunks + per-chunk vec tables (chunking for long papers/PRs)
  - synthesis operation: documents-db → knowledge-db with source_url provenance
  - additional corpus adapters: arXiv, Jira, Confluence, runbooks
  - load_source_adapter() factory + entry-point registration
  - CLI commands: `techne library ingest`, `techne library list`, etc.
  - The "library as its own techne db" deployment is config (separate db_path),
    not new code — a caller passes the library db path, nothing else changes.
"""

import hashlib
from pathlib import Path

from techne._log import get_logger

# Plain connection (no core schema) — documents live in their own domain db
# (librarian.db); core.database.connect would core-initialize it (techne#99).
from techne.core.stores import plain_connect as connect

log = get_logger(__name__)


def _content_hash(content: str) -> str:
    """SHA-256 prefix over document content — used to detect changes on re-fetch."""
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def store_document(
    db_path: Path,
    *,
    source_type: str,
    source_url: str,
    content: str,
    title: str | None = None,
    meta: str | None = None,
    owned_by: str = "librarian",
) -> tuple[int, bool]:
    """Insert a document, or supersede the prior row if content changed.

    Dedup key is `source_url`. Three outcomes:
      - No prior active row for this URL → INSERT, return (new_id, True).
      - Prior row exists, content unchanged → no-op, return (existing_id, False).
      - Prior row exists, content changed → supersede old row, INSERT new,
        return (new_id, True).

    Returns:
        (id, stored): id of the active row, stored=True iff a new row was written.
    """
    conn = connect(db_path)
    try:
        # BEGIN IMMEDIATE serializes writers so the SELECT→INSERT below is atomic:
        # without it two concurrent callers both see "no active row" and both INSERT,
        # leaving TWO active rows for one source_url (the invariant this function
        # exists to keep). A partial UNIQUE(source_url) WHERE superseded_by IS NULL
        # can't be used here because the insert-first supersede path is transiently
        # two-active, which such an index would reject. (techne#170)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT id, content FROM documents
               WHERE source_url = ? AND superseded_by IS NULL""",
            (source_url,),
        ).fetchone()

        if existing is not None:
            if _content_hash(existing["content"]) == _content_hash(content):
                log.debug(
                    "document_unchanged",
                    source_url=source_url,
                    id=existing["id"],
                )
                conn.commit()  # release the write lock; nothing changed
                return existing["id"], False

            # Content changed — supersede the old row.
            # Insert the new revision first, then mark the old row as superseded
            # (mirrors knowledge.supersede()). Safe under the held write lock.
            old_id = existing["id"]
            cur = conn.execute(
                """INSERT INTO documents
                       (source_type, source_url, title, content, meta, owned_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_type, source_url, title, content, meta, owned_by),
            )
            new_id = cur.lastrowid
            conn.execute(
                """UPDATE documents
                   SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_id, old_id),
            )
            conn.commit()
            log.info(
                "document_superseded",
                source_url=source_url,
                old_id=old_id,
                new_id=new_id,
            )
            return new_id, True

        # No prior row — fresh insert.
        cur = conn.execute(
            """INSERT INTO documents
                   (source_type, source_url, title, content, meta, owned_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_type, source_url, title, content, meta, owned_by),
        )
        new_id = cur.lastrowid
        conn.commit()
        log.info(
            "document_stored",
            source_url=source_url,
            id=new_id,
            source_type=source_type,
        )
        return new_id, True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search_documents(db_path: Path, query: str, *, limit: int = 20) -> list[dict]:
    """FTS over ACTIVE documents (superseded excluded) — the Librarian's domain
    search (db-per-domain #99). Personal recall is the gist cards in techne.db;
    this searches the full content in librarian.db."""
    from techne.core.search import prepare_fts_query

    fts = prepare_fts_query(query)  # None when empty/all-punctuation → MATCH '' crash (#143)
    if fts is None:
        return []
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT d.id, d.source_type, d.source_url, d.title, d.content, d.meta,
                      d.updated_at, bm25(documents_fts) AS rank
               FROM documents_fts f JOIN documents d ON d.id = f.rowid
               WHERE documents_fts MATCH ? AND d.superseded_by IS NULL
               ORDER BY rank LIMIT ?""",
            (fts, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_document(db_path: Path, doc_id: int) -> dict | None:
    """Fetch one document by id (any revision, including superseded)."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT id, source_type, source_url, title, content, meta,
                      owned_by, fetched_at, created_at, updated_at, superseded_by
               FROM documents WHERE id = ?""",
            (doc_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_documents(
    db_path: Path,
    source_type: str | None = None,
    limit: int = 50,
    include_superseded: bool = False,
) -> list[dict]:
    """List documents with optional filters.

    By default returns only active (non-superseded) rows ordered by
    updated_at DESC.
    """
    conn = connect(db_path)
    try:
        query = (
            "SELECT id, source_type, source_url, title, meta, owned_by, "
            "fetched_at, created_at, updated_at, superseded_by "
            "FROM documents WHERE 1=1"
        )
        params: list = []

        if not include_superseded:
            query += " AND superseded_by IS NULL"
        if source_type is not None:
            query += " AND source_type = ?"
            params.append(source_type)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
