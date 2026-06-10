"""Knowledge store — durable facts, decisions, patterns, evaluations, procedures."""

import hashlib
from pathlib import Path

from techne.core.database import connect

VALID_TYPES = ("fact", "decision", "pattern", "evaluation", "procedure")
VALID_CONFIDENCE = ("high", "medium", "low")


def store(
    db_path: Path,
    content: str,
    type: str = "fact",
    tags: str = "",
    confidence: str = "medium",
    source: str = "session",
) -> int:
    """Store a knowledge entry. Returns the new entry's id."""
    if type not in VALID_TYPES:
        raise ValueError(f"Invalid knowledge type '{type}'. Must be one of: {VALID_TYPES}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence '{confidence}'. Must be one of: {VALID_CONFIDENCE}")

    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO knowledge (type, content, source, tags, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            (type, content, source, tags, confidence),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def supersede(db_path: Path, old_id: int, new_content: str, source: str = "session") -> int:
    """Replace an existing knowledge entry with updated content.

    Marks the old entry as superseded and creates a new one inheriting
    the old entry's type, tags, and confidence.
    """
    conn = connect(db_path)
    try:
        old = conn.execute(
            "SELECT type, tags, confidence FROM knowledge WHERE id = ?", (old_id,)
        ).fetchone()
        if old is None:
            raise ValueError(f"Knowledge entry {old_id} not found")

        cur = conn.execute(
            """INSERT INTO knowledge (type, content, source, tags, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            (old["type"], new_content, source, old["tags"], old["confidence"]),
        )
        new_id = cur.lastrowid

        conn.execute(
            "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_id, old_id),
        )
        conn.commit()
        return new_id
    finally:
        conn.close()


def list_entries(
    db_path: Path,
    type: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    include_superseded: bool = False,
) -> list[dict]:
    """List knowledge entries with optional filters."""
    conn = connect(db_path)
    try:
        query = "SELECT id, type, content, tags, confidence, source, created_at, updated_at FROM knowledge WHERE 1=1"
        params: list = []

        if not include_superseded:
            query += " AND superseded_by IS NULL"
        if type:
            query += " AND type = ?"
            params.append(type)
        if tags:
            for tag in tags.split(","):
                query += " AND tags LIKE ?"
                params.append(f"%{tag.strip()}%")

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get(db_path: Path, entry_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT id, type, content, tags, confidence, source,
                      superseded_by, created_at, updated_at
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete(db_path: Path, entry_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM knowledge WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def content_hash(content: str) -> str:
    """Generate a dedup hash for knowledge content."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]
