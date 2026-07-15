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

from dataclasses import dataclass
from pathlib import Path

from techne.sync.sinks import DeliveryResult, OutboxSink

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

    Idempotent at the ARCHIVIST: the sink handler skips a fact already live there,
    so a fact isn't duplicated across re-pushes. v1 re-enqueues all live knowledge
    each call (cheap deliveries that no-op on dup); the per-sink export ledger that
    avoids re-enqueuing entirely is techne#223."""
    if config.standalone:
        return 0
    from techne.core.knowledge_export import query_live_knowledge
    from techne.sync.outbox import enqueue

    ensure_registered_for(config)
    pushed = 0
    for k in query_live_knowledge(config.db_path):
        enqueue(
            config.db_path,
            ARCHIVIST_KNOWLEDGE_TARGET,
            {
                "content": k["content"],
                "type": k["type"],
                "tags": k.get("tags") or "",
                "confidence": k.get("confidence") or "medium",
            },
        )
        pushed += 1
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
