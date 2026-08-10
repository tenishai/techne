"""Base Archivist — a single techne-schema SQLite db that IS the team memory (techne#346).

`memory-system-architecture.md` §4: team memory is "the same techne schema, shared";
for solo/small-team a single techne instance is the endorsed store. This is the
techne-native, cabal-free Archivist — `sink:archivist` writes pushed team memory into
`archivist.db`, which federation recall (techne#222) reads back read-only.

PR 1 (this file): the write target + the `sink:archivist` intake for thread summaries
(stored raw, per §3 "the Archivist holds raw for durability"). The knowledge-egress +
federation read-back loop that closes the recall side is PR 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from techne.sync.sinks import DeliveryResult, OutboxSink

log = logging.getLogger(__name__)

# The name under which the local base Archivist appears in recall's federation
# fan-out (techne#346 PR2). A non-standalone instance recalls over its own
# archivist.db as a `team` connection.
ARCHIVIST_CONNECTION_NAME = "archivist"


def _resolve_archivist_path(config) -> Path:
    """Resolve the base Archivist db path WITHOUT creating it. ``archivist_db_path``
    overrides; default ``<data_dir>/archivist.db``.

    The base Archivist is a SEPARATE team store. If misconfigured to the personal db,
    team intake would pollute personal memory (and federation would read the
    instance's own rows back) — refuse loudly rather than silently corrupt (#346)."""
    p = (
        Path(config.archivist_db_path)
        if config.archivist_db_path
        else Path(config.data_dir) / "archivist.db"
    )
    if p.resolve() == Path(config.db_path).resolve():
        raise ValueError(
            "archivist_db_path must not be the personal techne db (config.db_path); "
            "the base Archivist is a separate team store (techne#346)."
        )
    return p


def archivist_db(config) -> Path:
    """Path to the base Archivist store (techne schema), created if absent.

    A full techne-schema db so federation recall (techne#222) reads it directly as
    a ``team`` connection — no bespoke schema at the read edge."""
    from techne.core.database import init_db

    p = _resolve_archivist_path(config)
    init_db(p)
    return p


# The archivist db path the SINK HANDLERS write to. The OutboxSink API passes only
# the payload (no config), so a handler otherwise has to Config.load() — which reads
# config.yaml and diverges from a caller's in-memory/overridden config, misdirecting
# writes away from where the read path federates. ensure_registered_for() sets this
# from the CALLER's config so read and write always agree; Config.load() is only the
# fallback for a bare flush with no prior registration. Single-process by design
# (hooks/CLI), mirroring the sink registry itself.
_HANDLER_DB: Path | None = None


def _handler_db() -> Path:
    if _HANDLER_DB is not None:
        return _HANDLER_DB
    from techne.config import Config

    return archivist_db(Config.load())


@dataclass
class ThreadSummaryPayload:
    """Schema for a thread summary pushed to ``sink:archivist`` — mirrors the enqueue
    payload at the lifecycle thread-summary sites. Validated at enqueue time."""

    session_id: str
    thread_id: str
    thread_label: str
    summary: str
    resume_points: str
    agent_identity: str = ""


def _archivist_handler(payload: dict) -> DeliveryResult:
    """Write a pushed thread summary into the base Archivist as a raw observation
    (§3), keyed by the SOURCE agent's session + identity so team provenance is kept.

    Loads config internally so the sink stays a stable module singleton — that keeps
    ``register_sink`` idempotent (same object) rather than minting a new closure each
    call. Exceptions here are caught by the outbox and become ok=False (row retried).
    """
    from techne.core.state import add_observation, ensure_session

    db = _handler_db()
    sid = payload.get("session_id") or "archivist-intake"
    ensure_session(db, sid, agent_identity=payload.get("agent_identity", ""))
    label = payload.get("thread_label", "")
    summary = payload.get("summary", "")
    resume = payload.get("resume_points", "")
    content = f"[{label}] {summary}\nResume: {resume}".strip()
    add_observation(db, sid, "thread_summary", content)
    return DeliveryResult(ok=True)


# Module singleton so register_base_archivist is idempotent (register_sink compares
# handler identity). The full prefixed name matches the outbox target string.
ARCHIVIST_SINK = OutboxSink(
    name="sink:archivist",
    payload_schema=ThreadSummaryPayload,
    handler=_archivist_handler,
    description="Base Archivist — writes pushed thread summaries (raw) into archivist.db (#346).",
)

ARCHIVIST_KNOWLEDGE_TARGET = "sink:archivist_knowledge"

# TARGETS WHOSE DELIVERED OUTBOX ROWS ARE LOAD-BEARING STATE, NOT A LOG (#518).
#
# `push_knowledge_to_archivist` reads this lane's rows back as a dedup pre-filter
# (see the query below), so those rows ARE the de-facto export ledger. Retention
# must preserve the SET of content hashes here; deleting them makes the next
# teardown re-enqueue the entire knowledge base — correctly, because as far as the
# pre-filter can tell the rows really are gone.
#
# DEMONSTRATED, not theorised: a hand-run sweep of 128,200 rows on 2026-08-07
# completed at 20:13 and 761 distinct rows re-enqueued at 20:16:01-06.
#
# DECLARED HERE, NEXT TO THE CONSUMER, ON PURPOSE. `techne.sync.outbox` imports
# this rather than carrying its own copy of the target name: a literal typed next
# to the thing it describes is a copy, not a mechanism, and it drifts the moment
# either side moves (techne#1022, and the #508 review that found the same shape
# five times). `sync.outbox.delivered_hashes` is what BINDS this set: it refuses a
# target that is not in it, so any caller that reads enqueue history through the
# accessor must declare its lane. That is the enforceable half — a pre-filter
# that queries the table directly, without the accessor, is NOT prevented by
# anything here (see delivered_hashes' docstring for what it does and does not
# guarantee).
LEDGER_TARGETS = frozenset({ARCHIVIST_KNOWLEDGE_TARGET})


@dataclass
class KnowledgePayload:
    """Schema for a durable knowledge entry pushed to the Archivist (techne#346 PR2).
    This is the DISTILLED half — what federation recall reads back as team memory."""

    content: str
    type: str = "fact"
    tags: str = ""
    confidence: str = "medium"


def _archivist_knowledge_handler(payload: dict) -> DeliveryResult:
    """Write a pushed knowledge entry into the Archivist's ``knowledge`` table so
    recall federates over it (techne#346 PR2). source='team' marks provenance.

    Idempotent: skips a fact already live in the Archivist. The outbox only dedups
    PENDING rows (partial unique index), so a re-push after a prior sync would
    otherwise duplicate; the efficient per-sink export ledger that avoids even
    re-enqueuing is techne#223. Content is redacted to match how store() persists,
    so the dedup compares like-for-like."""
    from techne.core.knowledge import store
    from techne.core.redaction import redact_credentials
    from techne.core.stores import plain_connect

    db = _handler_db()
    content = redact_credentials(payload.get("content", ""))
    conn = plain_connect(db)
    try:
        exists = conn.execute(
            "SELECT 1 FROM knowledge WHERE content = ? AND superseded_by IS NULL LIMIT 1",
            (content,),
        ).fetchone()
    finally:
        conn.close()
    if exists:
        return DeliveryResult(ok=True)  # already present — idempotent no-op

    store(
        db,
        content=content,
        type=payload.get("type", "fact"),
        tags=payload.get("tags", ""),
        confidence=payload.get("confidence", "medium"),
        source="team",
    )
    return DeliveryResult(ok=True)


ARCHIVIST_KNOWLEDGE_SINK = OutboxSink(
    name=ARCHIVIST_KNOWLEDGE_TARGET,
    payload_schema=KnowledgePayload,
    handler=_archivist_knowledge_handler,
    description="Base Archivist — writes pushed distilled knowledge into archivist.db (#346).",
)


def register_base_archivist(config=None) -> None:
    """Register both Archivist sinks — ``sink:archivist`` (raw summaries) and
    ``sink:archivist_knowledge`` (distilled knowledge). Idempotent. When *config* is
    given, pins the handlers' write target to that config's archivist db so a
    delivery writes where the read path federates (see _HANDLER_DB)."""
    global _HANDLER_DB
    from techne.sync.sinks import register_sink

    if config is not None:
        _HANDLER_DB = archivist_db(config)
    register_sink(ARCHIVIST_SINK)
    register_sink(ARCHIVIST_KNOWLEDGE_SINK)


def ensure_registered_for(config) -> None:
    """Register the Archivist sinks iff this instance routes to them (``standalone``
    is false). Idempotent and cheap — call at EVERY entry point that enqueues OR
    drains sink rows (the session-end sites, the session-end hook's ``flush_sinks``,
    and ``techne outbox flush-sinks``), so a row that failed immediate delivery is
    still deliverable from a fresh CLI/hook process (techne#346 review). No-op when
    standalone (the sink lane is unused)."""
    if not config.standalone:
        register_base_archivist(config)


def push_knowledge_to_archivist(config) -> int:
    """Egress live durable knowledge from the personal store to the Archivist
    (techne#346 PR2). Enqueues each live entry (``superseded_by IS NULL``) to
    ``sink:archivist_knowledge``. Returns the number enqueued. No-op when standalone.

    The sink handler skips a fact already live in the Archivist, so re-pushing the
    same fact doesn't duplicate it. Two KNOWN GAPS in this v1, both requiring the
    export ledger / reconciliation in techne#223:

    1. STALE REVISIONS PERSIST. When a fact is superseded locally (a new revision),
       this pushes the NEW version but never supersedes/removes the OLD one already
       in the Archivist — so both revisions stay live there and recall can surface
       the stale one. v1 is append-only to the team store; propagating supersession
       needs a ledger that maps local ids → archivist rows.
    2. STALE-REVISION LEDGER still absent (see 1) — the full fix in techne#223.

    RE-ENQUEUE COST — FIXED (techne#475), and it was not a cost, it was the
    dominant term in session teardown. This re-enqueued ALL live knowledge on
    EVERY call, and ``enqueue`` opens, commits and closes its own SQLite
    connection per row: 760 enqueues, 2,306 connections, **5.15s of a 5.52s
    teardown — 93%**, against a 5.0s hook budget that then aborted teardown
    mid-flight (measured on a 380MB store; profile in the issue). The cost scaled
    with the size of the KNOWLEDGE BASE, not the session, which is why it looked
    flat against session length and why it would get monotonically worse as
    memory grows — 116,174 accumulated rows in this lane on the operator's store,
    from ~772 facts.

    The pre-filter below asks the lane ONCE what it already holds. It is not a
    substitute for the ledger: it does not fix (1), and a DELETED outbox row makes
    it re-enqueue (correctly — the row really is gone).

    3. NO BACK CATALOGUE FOR A REPOINTED ARCHIVIST. If `archivist_db_path` is
       moved or the team store is reset, this now sends nothing: those rows are
       legitimately `synced`, so the pre-filter skips them and the new store never
       receives history. The OLD re-enqueue-everything behaviour repopulated it by
       accident — that accident is worth naming rather than losing silently. A
       status filter cannot fix this one; it needs #223's reconcile path, which
       compares against what the SINK holds rather than what we enqueued."""
    if config.standalone:
        return 0
    from techne.core.database import connect
    from techne.core.knowledge import REDIRECTED_TYPES
    from techne.core.knowledge_export import query_live_knowledge
    from techne.sync.outbox import content_hash_for, delivered_hashes, enqueue

    ensure_registered_for(config)

    # ONE query for what this lane already holds, instead of N round trips to
    # rediscover it.
    #
    # `status != 'dead'` IS LOAD-BEARING AND WAS MISSING. The schema's vocabulary
    # is 'pending' | 'synced' | 'dead' (database.py) — there is no 'failed', which
    # an earlier version of this comment assumed. `dead` means PERMANENTLY NOT
    # DELIVERED, which is the opposite of "already enqueued" for a pre-filter's
    # purposes, so sweeping those hashes in here silently drops the row forever.
    #
    # Not hypothetical: `techne outbox prune-orphans` does not call
    # `ensure_registered_for` (unlike `flush-sinks` directly above it in cli.py),
    # so in that fresh process every sink:archivist_knowledge row looks like an
    # orphan and the whole pending backlog is marked dead. Measured on a 3-fact
    # store: BEFORE this pre-filter existed, the next teardown re-enqueued all 3
    # and the archivist recovered; with the pre-filter and no status clause, it
    # re-enqueued 0, permanently. `INSERT OR IGNORE` cannot save it — the filter
    # `continue`s before enqueue is ever reached. (Prudence, #482 review.)
    #
    # The general shape, worth carrying to any "stop repeating this work" change:
    # AN OUTBOX IS A LOG OF ENQUEUES, NOT A RECORD OF WHAT THE SINK HOLDS, and the
    # redundancy being removed was quietly doubling as a reconcile path.
    # THROUGH THE DECLARED ACCESSOR, not inlined SQL (#518). delivered_hashes()
    # raises unless the target is in LEDGER_TARGETS, so a lane whose enqueue
    # history is read back cannot exist without the retention policy knowing —
    # the invariant is enforced by the call, not by a regex over the source.
    already = delivered_hashes(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET)

    pushed = 0
    skipped = 0
    unchanged = 0
    for k in query_live_knowledge(config.db_path):
        # SKIP REDIRECTED TYPES AT THE SOURCE (#450). These live in another store
        # (techne#137 moved patterns to architect.db) and the sink handler rejects
        # them outright with a ValueError. Enqueuing them anyway produced a
        # PERMANENT PENDING LOOP: delivery fails, the row stays pending, and
        # because this function re-enqueues all live knowledge on every call
        # (KNOWN GAP 2 above), the next teardown re-enqueues the same rows.
        #
        # TEST AGAINST REDIRECTED_TYPES, NOT A LITERAL. That dict is the single
        # definition of this boundary — `store()` raises straight off it — so a
        # literal `"pattern"` re-implements the membership test and drifts the
        # moment a type is added. Demonstrated rather than assumed: adding
        # `document` to REDIRECTED_TYPES reproduces the identical permanent-
        # pending loop against a literal filter. Pinned by
        # test_redirected_types_filter_follows_the_dict_not_a_literal.
        #
        # Filtered HERE rather than in query_live_knowledge, which is shared with
        # the export path and must keep returning the full set.
        if k["type"] in REDIRECTED_TYPES:
            skipped += 1
            continue
        data = {
            "content": k["content"],
            "type": k["type"],
            "tags": k.get("tags") or "",
            "confidence": k.get("confidence") or "medium",
        }
        # Hash computed by the OUTBOX's own helper, never re-derived here — a
        # second copy of the formula stops matching the moment either side
        # changes, and a silently non-matching pre-filter degrades to exactly the
        # behaviour this removed.
        digest = content_hash_for(data)
        if digest in already:
            unchanged += 1
            continue
        enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, data, content_hash=digest)
        already.add(digest)
        pushed += 1
    if skipped:
        # debug, not warning: a permanent data condition on installs carrying
        # pre-#137 rows, so a louder level would fire on every teardown forever.
        # Worth recording at all because this function returns the POST-filter
        # count — without it, a shrinking `knowledge_pushed` is unanswerable
        # from outside.
        log.debug("archivist push: skipped %d redirected-type row(s)", skipped)
    return pushed


def archivist_connection(config):
    """The base Archivist as a federation connection for recall, or None when
    standalone (techne#346 PR2). Points at the archivist db PATH without creating
    it — fan_out_search opens it read-only and skips a missing/empty store."""
    if config.standalone:
        return None
    from techne.config import FederationConnectionSpec

    return FederationConnectionSpec(
        role="team",
        db_path=str(_resolve_archivist_path(config)),
        label="base archivist",
    )
