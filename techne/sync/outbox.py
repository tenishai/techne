"""Local outbox queue for team sync.

Every write destined for a team backend goes through the outbox — never
directly to the network. The flush cycle calls the configured SyncAdapter
at startup and session end, fast-fails if adapter.health() returns false,
and defers gracefully.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from techne.core.database import connect
from techne.sync.adapter import OutboxEntry, PushResult, SyncAdapter


def enqueue(
    db_path: Path,
    target: str,
    data: dict,
    content_hash: str | None = None,
):
    """Add an entry to the outbox. Deduplicates by content_hash."""
    if content_hash is None:
        content_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO outbox (target, data_json, content_hash)
               VALUES (?, ?, ?)""",
            (target, json.dumps(data), content_hash),
        )
        conn.commit()
    finally:
        conn.close()


def flush(db_path: Path, adapter: SyncAdapter) -> tuple[int, int]:
    """Flush pending outbox entries to the team backend.

    Returns (synced_count, failed_count).
    """
    if not adapter.health():
        return 0, 0

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, target, data_json, content_hash FROM outbox
               WHERE status = 'pending'
               ORDER BY created_at LIMIT 100"""
        ).fetchall()

        if not rows:
            return 0, 0

        entries = [
            OutboxEntry(
                id=r["id"],
                target=r["target"],
                data=json.loads(r["data_json"]),
                content_hash=r["content_hash"],
            )
            for r in rows
        ]

        result = adapter.push(entries)

        if result.synced > len(entries):
            raise ValueError(
                f"Adapter reported {result.synced} synced but only {len(entries)} "
                f"were in the batch — this violates the PushResult contract"
            )

        now = datetime.now(timezone.utc).isoformat()
        synced_ids = [e.id for e in entries[: result.synced]]
        failed_ids = [e.id for e in entries[result.synced : result.synced + result.failed]]

        if synced_ids:
            placeholders = ",".join("?" for _ in synced_ids)
            conn.execute(
                f"UPDATE outbox SET status = 'synced', synced_at = ? WHERE id IN ({placeholders})",
                [now] + synced_ids,
            )

        if failed_ids:
            # If the adapter reported more failures than it provided error
            # messages for, the extra entries previously slipped through
            # without status update and stayed pending forever. Use a
            # placeholder so every failed_id gets marked dead.
            errors = list(result.errors or [])
            for i, fid in enumerate(failed_ids):
                err = errors[i] if i < len(errors) else "(adapter reported failure without an error message)"
                conn.execute(
                    "UPDATE outbox SET status = 'dead', error_message = ? WHERE id = ?",
                    (err, fid),
                )

        conn.commit()
        return result.synced, result.failed

    finally:
        conn.close()


def pending_count(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()
        return row[0]
    finally:
        conn.close()


def dead_count(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'dead'"
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def list_dead(db_path: Path, limit: int = 50) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, target, data_json, content_hash, error_message, created_at
               FROM outbox WHERE status = 'dead'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    conn = connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
        ).fetchone()[0]
        synced = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'synced'"
        ).fetchone()[0]
        dead = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'dead'"
        ).fetchone()[0]
        return {"total": total, "pending": pending, "synced": synced, "dead": dead}
    finally:
        conn.close()
