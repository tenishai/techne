"""Curated memory drain — persist CC's memory/*.md files into techne.db (techne#107 PR2).

CC authors ``~/.claude/projects/<slug>/memory/*.md`` (the curated SPOOL); this module
mirrors them faithfully into the ``curated_memory`` table, content-hash deduped and
superseding on change.  The same 3-outcome dance as documents.store_document and
knowledge.store:

  - No prior active row for source_path  -> INSERT, return (new_id, True).
  - Prior row exists, content unchanged  -> no-op, return (existing_id, False).
  - Prior row exists, content changed    -> supersede old, INSERT new,
                                           return (new_id, True).

This is the read foundation for PR3 (claim-gate).

Faithfulness caveat: the mirror is full-content but redaction-NORMALIZED —
``redact_credentials`` runs before hashing/storing, so a secret in the source file is
masked here (storing it raw would be worse). A consequence: if redaction rules ever
change, an otherwise-unchanged file re-hashes and spuriously supersedes once.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from techne._log import get_logger
from techne.core.database import connect
from techne.core.redaction import redact_credentials

log = get_logger(__name__)


def _content_hash(content: str) -> str:
    """SHA-256 prefix over content -- used to detect changes on re-ingest.

    Identical contract to documents._content_hash.
    """
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def ingest_memory_file(
    db_path: Path,
    source_path: Path | str,
    *,
    content: str | None = None,
    slug: str | None = None,
    session_id: str | None = None,
    provenance: str = "disk_walk",
) -> tuple[int, bool]:
    """Ingest one curated memory file into the ``curated_memory`` table.

    Dedup key is ``source_path``.  Three outcomes:
      - No prior active row for this path  -> INSERT, return (new_id, True).
      - Prior row exists, content unchanged -> no-op, return (existing_id, False).
      - Prior row exists, content changed  -> supersede old, INSERT new,
        return (new_id, True).

    Args:
        db_path:     Path to the techne SQLite database.
        source_path: Absolute path to the memory file on disk.
        content:     File text.  If None the file is read from ``source_path``;
                     on OSError the function returns (-1, False) and logs a warning.
        slug:        CC project slug for scoping queries.
        session_id:  Optional originating session.
        provenance:  Provenance label (default ``'disk_walk'``).

    Returns:
        (id, stored): id of the active row; stored=True iff a new row was written.
        Returns (-1, False) when the file cannot be read from disk.
    """
    source_path = Path(source_path)

    if content is None:
        try:
            content = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("curated_memory_read_error", path=str(source_path), error=str(exc))
            return -1, False

    content = redact_credentials(content)
    title = source_path.name

    # Compact meta: mtime + size when available, else None.
    meta: str | None = None
    try:
        st = os.stat(source_path)
        meta = json.dumps({"mtime": st.st_mtime, "size": st.st_size}, separators=(",", ":"))
    except OSError:
        pass

    new_hash = _content_hash(content)
    source_str = str(source_path)

    conn = connect(db_path)
    try:
        # Take the write lock BEFORE the read (BEGIN IMMEDIATE): with the default
        # isolation a bare SELECT opens no transaction, so two concurrent ingests of
        # the same changed path could both capture the same stale `old_id` and leave
        # TWO live rows — breaking the one-live-row-per-source_path invariant the whole
        # table rests on (the read→write TOCTOU; cf. techne#3). The second writer now
        # blocks here and re-reads the freshly-superseded row. (Unreachable today —
        # sync is single-threaded — but start_watch will eventually overlap.)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT id, content_hash FROM curated_memory
               WHERE source_path = ? AND superseded_by IS NULL""",
            (source_str,),
        ).fetchone()

        if existing is not None:
            if existing["content_hash"] == new_hash:
                conn.commit()  # release the lock; nothing changed
                log.debug(
                    "curated_memory_unchanged",
                    path=source_str,
                    id=existing["id"],
                )
                return existing["id"], False

            # Content changed -- supersede the old row then insert the new one.
            old_id = existing["id"]
            cur = conn.execute(
                """INSERT INTO curated_memory
                       (source_path, slug, title, content, content_hash,
                        provenance, session_id, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_str, slug, title, content, new_hash, provenance, session_id, meta),
            )
            new_id = cur.lastrowid
            conn.execute(
                """UPDATE curated_memory
                   SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_id, old_id),
            )
            conn.commit()
            log.info(
                "curated_memory_superseded",
                path=source_str,
                old_id=old_id,
                new_id=new_id,
            )
            return new_id, True

        # No prior row -- fresh insert.
        cur = conn.execute(
            """INSERT INTO curated_memory
                   (source_path, slug, title, content, content_hash,
                    provenance, session_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_str, slug, title, content, new_hash, provenance, session_id, meta),
        )
        new_id = cur.lastrowid
        conn.commit()
        log.info(
            "curated_memory_stored",
            path=source_str,
            id=new_id,
            slug=slug,
        )
        return new_id, True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


#: Provenance of a tombstone row: the file it supersedes is gone from disk (techne#1290).
RETIRED_PROVENANCE = "retired"
#: Never a real `_content_hash` (those are hex), so a file that comes back with any
#: content, even empty, supersedes the tombstone instead of matching it.
_RETIRED_HASH = "retired"


def retire_missing_memory(db_path: Path, *, slug: str, present: set[str]) -> int:
    """Retire live rows for *slug* whose ``source_path`` is not in *present*.

    A deleted memory file kept its live row forever, and memory_claims kept judging
    it as current memory (techne#1290). Retiring follows the supersede chain the
    rest of this module uses: a tombstone row (provenance ``'retired'``, empty
    content) is inserted and the old row points at it, so history stays and a file
    that reappears supersedes the tombstone through `ingest_memory_file` as usual.
    ``live_curated_memory`` leaves tombstones out.

    Scoped to one slug, so one project's walk never retires another's rows. The
    caller must not call this for an empty walk: a missing or unmounted directory
    would otherwise retire everything. Returns the number of rows retired.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT id, source_path, title FROM curated_memory
               WHERE superseded_by IS NULL AND slug = ? AND provenance != ?""",
            (slug, RETIRED_PROVENANCE),
        ).fetchall()
        retired = 0
        for r in rows:
            if r["source_path"] in present:
                continue
            cur = conn.execute(
                """INSERT INTO curated_memory
                       (source_path, slug, title, content, content_hash, provenance)
                   VALUES (?, ?, ?, '', ?, ?)""",
                (
                    r["source_path"],
                    slug,
                    redact_credentials(r["title"] or ""),
                    _RETIRED_HASH,
                    RETIRED_PROVENANCE,
                ),
            )
            conn.execute(
                """UPDATE curated_memory
                   SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (cur.lastrowid, r["id"]),
            )
            retired += 1
            log.info(
                "curated_memory_retired",
                path=r["source_path"],
                old_id=r["id"],
                tombstone_id=cur.lastrowid,
            )
        conn.commit()
        return retired
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def live_curated_memory(
    db_path: Path,
    *,
    slug: str | None = None,
) -> list[dict]:
    """Return current (non-superseded) curated memory rows, optionally scoped to a slug.

    Tombstones (``provenance = 'retired'``, a deleted file) are not memory and are
    left out (techne#1290).

    Used by the PR3 claim-gate and any other consumer that needs the active drain
    snapshot.  Returns rows as plain dicts ordered by ``source_path``.
    """
    conn = connect(db_path)
    try:
        if slug is not None:
            rows = conn.execute(
                """SELECT id, source_path, slug, title, content, content_hash,
                          provenance, session_id, meta, captured_at, created_at, updated_at
                   FROM curated_memory
                   WHERE superseded_by IS NULL AND slug = ? AND provenance != ?
                   ORDER BY source_path""",
                (slug, RETIRED_PROVENANCE),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, source_path, slug, title, content, content_hash,
                          provenance, session_id, meta, captured_at, created_at, updated_at
                   FROM curated_memory
                   WHERE superseded_by IS NULL AND provenance != ?
                   ORDER BY source_path""",
                (RETIRED_PROVENANCE,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
