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
import time
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
    supersede_key: str | None = None,
):
    """Add an entry to the outbox. Deduplicates by content_hash.

    ``supersede_key`` names a payload field (e.g. ``thread_id``). When given, any
    still-PENDING row for the same target whose payload has the same value for that
    field is deleted first, so the outbox holds at most one pending revision of each
    keyed item (techne#1187). Delivered rows are never touched.

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
        if supersede_key is not None:
            # Coalesce (techne#1187): a still-pending row for the same key is an
            # older revision of this one. Nobody has received it yet, so replacing
            # it loses nothing, and keeping it would ship two versions of one thing.
            conn.execute(
                """DELETE FROM outbox
                    WHERE target = ? AND status = 'pending'
                      AND json_extract(data_json, '$.' || ?) = ?""",
                (target, supersede_key, data.get(supersede_key)),
            )
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


def flush_sinks(db_path: Path, budget_s: float | None = None) -> dict[str, tuple[int, int]]:
    """Flush pending rows to registered sinks, grouped by target.

    Rows whose target has no registered sink are left pending — they'll be
    delivered if the sink is registered later, or drained by prune_orphans.

    *budget_s* bounds the time spent delivering (#1298): once it is used up, the
    remaining rows are left pending, untouched and with no error recorded, for the
    next flush. At least one row is always attempted, so a slow start cannot stall a
    backlog forever. None or 0 means unbounded.

    Returns {target: (synced, failed)} for diagnostics.
    """
    from techne.sync.sinks import DeliveryResult, get_sink

    deadline = time.monotonic() + budget_s if budget_s else None
    attempted = False

    def out_of_time() -> bool:
        return attempted and deadline is not None and time.monotonic() >= deadline

    conn = connect(db_path)
    try:
        rows_by_target = _load_pending_grouped_by_target(conn)

        summary: dict[str, tuple[int, int]] = {}
        for target, rows in rows_by_target.items():
            if out_of_time():
                break
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
                if out_of_time():
                    logger.debug("flush_sinks: budget of %ss used, rest left pending", budget_s)
                    break
                attempted = True
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


# The two row sets the sweep acts on, defined ONCE and shared by the preview and
# the delete. A dry run that computes its own copy of the predicate is worse than
# no dry run: it reports on a scope the apply path may not share, and the
# divergence is invisible precisely when it matters. Both take the ledger-target
# placeholders as their leading parameters.
_SWEEP_COLLAPSE = """
    SELECT id, target FROM (
        SELECT id, target, ROW_NUMBER() OVER (
            PARTITION BY target, content_hash
            ORDER BY (status = 'dead') ASC, id DESC
        ) AS rn
        FROM outbox
        WHERE target IN ({ph})
          AND status != 'pending'
          AND content_hash IS NOT NULL
    ) WHERE rn > 1
"""

# ALWAYS KEEP THE NEWEST ROW PER TARGET — the tombstone that stops a swept lane
# from presenting as a lane that never existed.
#
# `check_outbox_targets` (checks.py:1254-1256) reads outbox history per target:
#   SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending'), MAX(created_at)
#   FROM outbox GROUP BY target
# Empty a quiet lane completely and its target drops out of that GROUP BY, so the
# check falls into its `_declared_outbox_targets - seen` branch and reports
# "declared lane has NEVER enqueued anything" — false, and it replaces the very
# STALE diagnosis (the eight-week-dead thread_summary incident) that check exists
# to make. The evidence would be destroyed at the exact moment it became
# actionable, because `retain_days` defaults to 7 and `_PRODUCER_SILENT_AFTER` is
# also 7 days: STALE fires precisely in the window the sweep had already emptied.
#
# One retained row per target keeps COUNT/MAX honest and costs a few hundred bytes.
# This is cheaper and more robust than coupling the two constants, because it
# holds for ANY retain_days an operator picks — including the one that motivated
# them to lower it.
_SWEEP_AGED = """
    SELECT id, target FROM outbox
     WHERE status = 'synced'
       AND target NOT IN ({ph})
       AND created_at < ?
       AND id NOT IN (SELECT MAX(id) FROM outbox GROUP BY target)
       -- FIRST TOMBSTONE: the newest row per target, whatever its status.
       -- ITS IN-PACKAGE JUSTIFICATION IS UNREACHABLE, AND THAT IS NOT WHY IT
       -- STAYS (#528 r6). The earlier argument was that it protects a `synced`
       -- row carrying a NULL synced_at. No code here can produce that: every
       -- assignment of status='synced' sets synced_at in the SAME statement
       -- (outbox.py:156, :262, :291), and database.py:668's backfill is itself
       -- gated on `synced_at IS NOT NULL`. Grepped exhaustively for
       -- `status = 'synced'` across techne/ to say that.
       -- SO THE HONEST REASON IS OUT-OF-BAND WRITES: a manual UPDATE against the
       -- db, a future migration, a third-party tool. This table is a plain
       -- SQLite file an operator can and does open. The clause costs one row per
       -- target and is the only thing standing between such a write and a lane
       -- that reads as never-synced.
       -- Pinned by test_the_first_tombstone_survives_an_out_of_band_write, which
       -- creates exactly that state directly. Mutating this clause to `1=1` left
       -- the whole suite green before that test existed — a clause defended only
       -- by an argument, and by an argument that could not happen.
       -- SECOND TOMBSTONE: the newest SYNCED row per target.
       -- The first one preserves PRESENCE; it does not preserve SYNC HISTORY,
       -- and check_outbox_targets reads MAX(synced_at) from the same GROUP BY.
       -- When a lane has a backlog its newest row is PENDING, so that row takes
       -- the first tombstone slot and every synced row older than the boundary
       -- is deleted — leaving MAX(synced_at) NULL. With pending > 0 the check
       -- falls to temporal_verdict(last_synced, 14d), and temporal_verdict(None)
       -- is UNOBSERVABLE (checks.py:288-291). So a healthy, actively-draining
       -- lane reports "never synced".
       -- Reachable on defaults, not exotic: retain_days=7 and a last successful
       -- sync 7-14 days old — precisely a lane that has just started backing up.
       -- Same class as the bug the first tombstone fixes, caught on one axis and
       -- not the other: an aggregate the check reads, destroyed by retention.
       -- TIE-BROKEN BY id, and that is load-bearing (#528 r7, found by #531).
       -- This was `GROUP BY target HAVING synced_at = MAX(synced_at)`, which is
       -- ambiguous when two rows share a synced_at: SQLite then returns an
       -- ARBITRARY row's id, and it returned the OLDER one — protecting the row
       -- the sweep should remove and leaving the newest unprotected.
       -- It never showed up because `synced_at` used to be written with
       -- .isoformat(), whose MICROSECONDS made ties practically impossible. #531
       -- normalised every timestamp to second precision, so two rows synced in
       -- the same second now collide routinely — and they do, because a drain
       -- stamps a whole batch with one `now`. The clause was depending on
       -- precision it never declared, supplied by a different module.
       -- ONLY A ROW WITH A synced_at CAN HOLD THIS SLOT (#547). For a NULL
       -- synced_at the NOT EXISTS below is vacuously true (nothing compares
       -- greater than NULL), so without `o.synced_at IS NOT NULL` every NULL row
       -- was protected forever. It protected nothing worth keeping: this slot
       -- exists for MAX(synced_at), and MAX ignores NULL. Presence is the first
       -- tombstone's job, and a NULL row that is its target's newest row still
       -- survives through it. Pinned by
       -- test_null_synced_at_rows_are_sweepable_but_the_lane_keeps_its_tombstones.
       AND id NOT IN (
             SELECT o.id FROM outbox o
              WHERE o.status = 'synced'
                AND o.synced_at IS NOT NULL
                AND NOT EXISTS (
                      SELECT 1 FROM outbox n
                       WHERE n.target = o.target AND n.status = 'synced'
                         AND (n.synced_at > o.synced_at
                              OR (n.synced_at = o.synced_at AND n.id > o.id))))
"""


def _sweep_boundary(conn, retain_days: int, cutoff: str | None) -> str:
    if cutoff is not None:
        return cutoff
    return conn.execute("SELECT datetime('now', ?)", (f"-{int(retain_days)} days",)).fetchone()[0]


def delivered_hashes(db_path: Path, target: str) -> set[str]:
    """The content hashes already enqueued for *target* — the dedup pre-filter (#518).

    THE DECLARED ACCESSOR. Reading enqueue history back makes those rows
    load-bearing state rather than a log, so the sweep must not age them out.
    That invariant used to live in a regex over the source tree, and a regex
    could not hold it: it was blind to a consumer reaching the table THROUGH
    this module (the idiomatic way, since every other outbox query lives here),
    and it produced false positives on files that merely mention both tokens —
    `core/state.py` has a `COUNT(*) FROM outbox` stat with no target and no dedup
    semantics anywhere near it. Its precision rested on where semicolons happened
    to fall.

    So the enforceable half lives HERE instead. WHAT THIS GUARANTEES, stated so it
    is falsifiable: IF YOU CALL `delivered_hashes`, YOU MUST DECLARE YOUR LANE.

    WHAT IT DOES NOT: it cannot stop a pre-filter that queries the table
    directly. `connect` is imported by ~40 modules across the package — most of
    core — so nothing prevents another from inlining the same SELECT with an
    undeclared target. (An earlier version of this sentence said "three modules",
    which invited the reader to picture a small surveyable set; the argument is
    correct and in fact stronger than that stated it, and core/database.py
    DEFINES connect rather than importing it.) An earlier
    version of this docstring claimed "a new pre-filter cannot be written that
    the retention policy does not know about" — that was false, and a confident
    sentence over a weaker mechanism is worse than no sentence, because it
    recruits the next reader into trusting a guarantee that is not there.

    `test_no_undeclared_inlined_prefilter` is a leaky detector for the inlined
    case, and says so; between them they cover the two ways this gets written.
    """
    from techne.core.archivist import LEDGER_TARGETS

    if target not in LEDGER_TARGETS:
        raise ValueError(
            f"{target!r} reads outbox enqueue history but is not in LEDGER_TARGETS. "
            f"Those rows are a dedup ledger, not a log — the sweep would age them "
            f"out and the next push would re-enqueue everything. Declare the lane "
            f"in techne/core/archivist.py, or do not read history back."
        )
    conn = connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT content_hash FROM outbox WHERE target = ? AND status != 'dead'",
                (target,),
            )
        }
    finally:
        conn.close()


def sweep_preview(db_path: Path, retain_days: int = 7, cutoff: str | None = None) -> dict:
    """What ``sweep_delivered`` WOULD delete, computed from the same predicates.

    Returns ``{"collapsed", "aged_out", "by_target", "retained", "boundary"}``.
    Read-only.

    ``boundary`` is returned so a caller can pass it BACK to ``sweep_delivered``
    as *cutoff*. Otherwise each computes ``datetime('now', ...)`` independently
    and an --apply run uses a boundary a moment later than the dry run the
    operator read. It cannot matter at day granularity — but the whole design
    rests on the dry run being unable to describe a different row set than the
    apply, and threading it makes that unconditional rather than true-in-practice.
    """
    from techne.core.archivist import LEDGER_TARGETS

    conn = connect(db_path)
    try:
        ph = ",".join("?" for _ in LEDGER_TARGETS)
        targets = tuple(LEDGER_TARGETS)
        boundary = _sweep_boundary(conn, retain_days, cutoff)

        collapse = conn.execute(_SWEEP_COLLAPSE.format(ph=ph), targets).fetchall()
        aged = conn.execute(_SWEEP_AGED.format(ph=ph), (*targets, boundary)).fetchall()

        by_target: dict[str, int] = {}
        for _id, target in [*collapse, *aged]:
            by_target[target] = by_target.get(target, 0) + 1

        total = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return {
            "collapsed": len(collapse),
            "aged_out": len(aged),
            "by_target": by_target,
            "retained": total - len(collapse) - len(aged),
            "boundary": boundary,
        }
    finally:
        conn.close()


def sweep_delivered(
    db_path: Path, retain_days: int = 7, cutoff: str | None = None
) -> dict[str, int]:
    """Bound outbox growth by dropping delivered rows nothing will read again (#518).

    Returns ``{"collapsed": n, "aged_out": n}``. NEVER touches ``pending`` — those
    rows are undelivered work.

    ``dead`` rows are kept by the AGE-OUT (they are diagnostic; ``list_dead``
    reads them) but a ledger lane's COLLAPSE can remove one: the partition spans
    statuses, so a dead duplicate of a hash that also has a live row is dropped as
    redundant. That is intended — the surviving row carries the same hash, which
    is the only thing the pre-filter reads — but "never touches dead" was too
    strong, so it is stated precisely rather than reassuringly.

    TWO POLICIES, because the lanes differ in kind and the difference is real:

    * LEDGER TARGETS (``archivist.LEDGER_TARGETS``) — COLLAPSE, DO NOT AGE OUT.
      Their rows are consulted as a dedup ledger, so the SET of content hashes is
      load-bearing while the LOG is not. Keeps one row per hash and drops the
      redundant copies, which is why this needs no age condition: a duplicate hash
      is waste the moment it lands. Historically 126,160 rows -> ~772.

      READ THE NUMBERS IN THIS MODULE AS DATED OBSERVATIONS, NOT AS CONSTANTS.
      Three different totals appear across this PR's surfaces — 126,160 here,
      128,200 at `archivist.py:132` and below, 130,266 in #518's title — and they
      are NOT the same measurement of different populations. I could not
      reconcile them and did not invent a reconciliation: they are consistent
      with one growing table sampled on three days, and the distinct-hash
      figures beside them (~772 vs 761) move in the opposite direction to the
      totals, which no single-population story explains. The state they describe
      is GONE — a hand-run sweep on 2026-08-07 took the table down, and the live
      store today holds 5,303 outbox rows (838 on this ledger lane), re-derived
      through `sweep_preview` on a copy. Do not subtract these from each other,
      and do not treat any of them as a current fact about the store.
    * EVERY OTHER TARGET — AGE OUT. Nothing reads these back (verified: the only
      reader of ``outbox.content_hash`` outside this module is the pre-filter in
      ``core/archivist.py``), and their rows are unique events rather than repeats
      of a bounded set, so collapsing reclaims almost nothing and age is the only
      bound that works.

    Applying either policy to the other lane fails: age-sweeping the ledger breaks
    the pre-filter, and collapsing observations reclaims ~21% instead of ~99%.

    PREFERS A NON-DEAD SURVIVOR when collapsing. The pre-filter's query excludes
    ``status = 'dead'``, so keeping a dead row over a synced one with the same hash
    would drop that hash from the set and re-enqueue it — the exact bug this
    function exists to avoid, reintroduced by the fix for it.

    *cutoff* overrides the computed age boundary (tests pass an explicit timestamp
    rather than sleeping). ``outbox.created_at`` is uniformly space-separated, so
    it is safe to compare lexically here — verified across 128,200 rows; this is
    NOT true of ``observations.timestamp`` (#515), so do not generalise it.
    """
    from techne.core.archivist import LEDGER_TARGETS

    conn = connect(db_path)
    try:
        ph = ",".join("?" for _ in LEDGER_TARGETS)
        targets = tuple(LEDGER_TARGETS)
        boundary = _sweep_boundary(conn, retain_days, cutoff)

        # Both deletes select through the SHARED predicates above, so the dry run
        # cannot describe a different row set than the apply.
        collapsed = conn.execute(
            f"DELETE FROM outbox WHERE id IN (SELECT id FROM ({_SWEEP_COLLAPSE.format(ph=ph)}))",
            targets,
        ).rowcount
        aged_out = conn.execute(
            f"DELETE FROM outbox WHERE id IN (SELECT id FROM ({_SWEEP_AGED.format(ph=ph)}))",
            (*targets, boundary),
        ).rowcount

        conn.commit()
        logger.debug(
            "swept outbox: collapsed=%d aged_out=%d (retain_days=%d, boundary=%s)",
            collapsed,
            aged_out,
            retain_days,
            boundary,
        )
        return {"collapsed": collapsed, "aged_out": aged_out}
    finally:
        conn.close()


def prune_orphans(db_path: Path, targets: list[str] | None = None) -> int:
    """Mark pending ``sink:`` rows whose sink isn't registered as dead.

    ``targets`` restricts it to rows for those targets (techne#1172): the operator
    naming a target is what resolves the ambiguity a refused entry point leaves.

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

        orphan_ids = [
            r["id"]
            for r in rows
            if get_sink(r["target"]) is None and (targets is None or r["target"] in targets)
        ]
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
