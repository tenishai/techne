"""Local outbox queue for team sync.

Every write destined for a team backend goes through the outbox — never
directly to the network. The flush cycle calls the configured SyncAdapter
at startup and session end, fast-fails if adapter.health() returns false,
and defers gracefully.

Sink-based delivery (flush_sinks / enqueue with deliver=True) is a parallel
path for per-target typed delivery — see techne/sync/sinks.py.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from techne.core.database import TIMESTAMP_FORMAT, connect
from techne.sync.adapter import OutboxEntry, SyncAdapter

logger = logging.getLogger(__name__)

# Every outbox target declares its delivery lane via a namespace prefix. The
# prefix is a permanent property of the row, so which flush path owns it never
# depends on the sink registry's state at flush time — that's what closes the
# flush()/flush_sinks race (#51). flush(adapter) drains `sync:%`; flush_sinks
# drains `sink:%`; the two sets are disjoint by construction.
SYNC_PREFIX = "sync:"  # adapter-bound — drained by flush(adapter) to the team backend
SINK_PREFIX = "sink:"  # sink-bound — delivered by flush_sinks via a registered OutboxSink


def content_hash_for(data: dict) -> str:
    """The outbox's dedup key for *data*.

    EXPOSED SO CALLERS CAN PRE-FILTER WITHOUT RE-DERIVING IT. A bulk producer
    that wants to skip rows this lane already holds has to compute the same hash
    the outbox would, and a second copy of the formula silently stops matching
    the moment either side changes — the same drift the REDIRECTED_TYPES filter
    was rewritten to avoid (#450). One definition, two callers. (techne#475.)
    """
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def enqueue(
    db_path: Path,
    target: str,
    data: dict,
    content_hash: str | None = None,
    *,
    deliver: bool = True,
):
    """Add an entry to the outbox. Deduplicates by content_hash.

    The *target* must declare its delivery lane via a namespace prefix (#51):

    - ``sink:<name>`` — delivered by ``flush_sinks`` through a registered
      OutboxSink. The sink must be registered (raises UnknownSinkError) and the
      payload is validated against its schema (InvalidPayloadError on mismatch).
      ``deliver=True`` (default) attempts immediate synchronous delivery
      (transactional-outbox); failures leave the row pending for the next
      ``flush_sinks`` cycle.
    - ``sync:<name>`` — drained by ``flush(adapter)`` to the team backend. No
      sink, no payload schema, no immediate delivery — the row waits for a flush.

    An un-namespaced target is rejected (no orphan rows). ``deliver=False`` skips
    immediate delivery for sink targets (tests / bulk import).
    """
    from techne.sync.sinks import (
        InvalidPayloadError,
        UnknownSinkError,
        get_sink,
        list_sinks,
    )

    sink = None
    if target.startswith(SINK_PREFIX):
        sink = get_sink(target)
        if sink is None:
            available = [s.name for s in list_sinks()]
            raise UnknownSinkError(
                f"No sink registered for target={target!r}. Available: {available}"
            )
        try:
            sink.payload_schema(**data)
        except TypeError as e:
            raise InvalidPayloadError(target, str(e)) from e
    elif not target.startswith(SYNC_PREFIX):
        raise ValueError(
            f"outbox target must be namespaced {SINK_PREFIX!r} (sink-delivered) or "
            f"{SYNC_PREFIX!r} (adapter-delivered); got {target!r}"
        )

    if content_hash is None:
        content_hash = content_hash_for(data)

    conn = connect(db_path)
    try:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO outbox (target, data_json, content_hash)
               VALUES (?, ?, ?)""",
            (target, json.dumps(data), content_hash),
        )
        conn.commit()
        inserted_id = cursor.lastrowid if cursor.rowcount else None
    finally:
        conn.close()

    if deliver and inserted_id and sink is not None:
        _try_deliver_now(db_path, sink, inserted_id, data)


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
               WHERE status = 'pending' AND target LIKE 'sync:%'
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

        now = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
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
                err = (
                    errors[i]
                    if i < len(errors)
                    else "(adapter reported failure without an error message)"
                )
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
        row = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'dead'").fetchone()
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
        pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
        synced = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'synced'").fetchone()[0]
        dead = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'dead'").fetchone()[0]
        return {"total": total, "pending": pending, "synced": synced, "dead": dead}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sink-based delivery helpers
# ---------------------------------------------------------------------------


def _try_deliver_now(db_path: Path, sink, row_id: int, data: dict) -> None:
    """Best-effort immediate delivery after enqueue.

    Failures leave the row pending for the next flush_sinks cycle.
    The producing caller sees nothing of this.
    """
    try:
        result = sink.handler(data)
        if not result.ok:
            logger.warning(
                "_try_deliver_now: handler rejected delivery target=%r error=%r",
                sink.name,
                result.error,
            )
        _update_row_status(db_path, row_id, result)
    except Exception:
        logger.exception(
            "_try_deliver_now: unexpected error for target=%r row_id=%d",
            sink.name,
            row_id,
        )


def _update_row_status(db_path: Path, row_id: int, result) -> None:
    now = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
    conn = connect(db_path)
    try:
        if result.ok:
            conn.execute(
                "UPDATE outbox SET status = 'synced', synced_at = ? WHERE id = ?",
                (now, row_id),
            )
        else:
            conn.execute(
                "UPDATE outbox SET error_message = ? WHERE id = ?",
                (result.error, row_id),
            )
        conn.commit()
    finally:
        conn.close()


def _load_pending_grouped_by_target(conn) -> dict[str, list]:
    rows = conn.execute(
        """SELECT id, target, data_json FROM outbox
           WHERE status = 'pending' AND target LIKE 'sink:%'
           ORDER BY created_at"""
    ).fetchall()

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["target"], []).append(row)
    return grouped


def _mark_synced(conn, row_id: int) -> None:
    now = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
    conn.execute(
        "UPDATE outbox SET status = 'synced', synced_at = ? WHERE id = ?",
        (now, row_id),
    )


def _mark_pending_with_error(conn, row_id: int, error: str | None) -> None:
    conn.execute(
        "UPDATE outbox SET error_message = ? WHERE id = ?",
        (error, row_id),
    )


def flush_sinks(db_path: Path) -> dict[str, tuple[int, int]]:
    """Flush pending rows to registered sinks, grouped by target.

    Rows whose target has no registered sink are left pending — they'll be
    delivered if the sink is registered later, or drained by prune_orphans.

    Returns {target: (synced, failed)} for diagnostics.
    """
    from techne.sync.sinks import DeliveryResult, get_sink

    conn = connect(db_path)
    try:
        rows_by_target = _load_pending_grouped_by_target(conn)

        summary: dict[str, tuple[int, int]] = {}
        for target, rows in rows_by_target.items():
            sink = get_sink(target)
            if sink is None:
                logger.debug(
                    "flush_sinks: no sink for target=%r (%d rows left pending)",
                    target,
                    len(rows),
                )
                summary[target] = (0, 0)
                continue

            synced = failed = 0
            for row in rows:
                try:
                    result = sink.handler(json.loads(row["data_json"]))
                except Exception as e:
                    result = DeliveryResult(ok=False, error=repr(e))

                if result.ok:
                    _mark_synced(conn, row["id"])
                    synced += 1
                else:
                    _mark_pending_with_error(conn, row["id"], result.error)
                    failed += 1

            conn.commit()
            summary[target] = (synced, failed)

        return summary
    finally:
        conn.close()


def purge_pending(db_path: Path, target: str | None = None) -> int:
    """Delete pending outbox rows. Intentional, CLI-only.

    Unlike prune_orphans (which only touches rows whose *target* has no sink),
    this drains rows that are pending because no consumer exists to take them —
    e.g. thread_summary rows under the noop adapter, which the sink stub never
    delivers and no team backend drains. These rows are regenerable: the next
    session_end re-enqueues current threads once a real adapter is configured,
    so deleting the stale backlog loses nothing.

    Pass *target* to restrict to one target; None purges all pending rows.
    Returns the number of rows deleted.
    """
    conn = connect(db_path)
    try:
        if target is None:
            cursor = conn.execute("DELETE FROM outbox WHERE status = 'pending'")
        else:
            cursor = conn.execute(
                "DELETE FROM outbox WHERE status = 'pending' AND target = ?", (target,)
            )
        conn.commit()
        logger.debug("purged %d pending outbox rows (target=%r)", cursor.rowcount, target)
        return cursor.rowcount
    finally:
        conn.close()


def prune_orphans(db_path: Path) -> int:
    """Mark pending ``sink:`` rows whose sink isn't registered as dead.

    Only ``sink:`` targets can orphan — a ``sync:`` row belongs to the adapter
    by construction (its lane is in the target prefix, not the registry), so it
    is never an orphan even when no adapter is configured. Only call this
    intentionally (e.g. via CLI); leaves-pending is the safe default. Returns
    the number of rows pruned.
    """
    from techne.sync.sinks import get_sink

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, target FROM outbox WHERE status = 'pending' AND target LIKE 'sink:%'"
        ).fetchall()

        orphan_ids = [r["id"] for r in rows if get_sink(r["target"]) is None]
        if not orphan_ids:
            return 0

        placeholders = ",".join("?" for _ in orphan_ids)
        conn.execute(
            f"UPDATE outbox SET status = 'dead', error_message = 'no sink registered for target' "
            f"WHERE id IN ({placeholders})",
            orphan_ids,
        )
        conn.commit()
        logger.debug("pruned %d orphan outbox rows", len(orphan_ids))
        return len(orphan_ids)
    finally:
        conn.close()
