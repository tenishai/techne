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


def archivist_db(config) -> Path:
    """Path to the base Archivist store (techne schema), created if absent.

    ``config.archivist_db_path`` overrides; default ``<data_dir>/archivist.db``.
    A full techne-schema db so federation recall (techne#222) can read it directly
    as a ``team`` connection — no bespoke schema at the read edge.
    """
    from techne.core.database import init_db

    p = (
        Path(config.archivist_db_path)
        if config.archivist_db_path
        else Path(config.data_dir) / "archivist.db"
    )
    init_db(p)
    return p


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
    from techne.config import Config
    from techne.core.state import add_observation, ensure_session

    config = Config.load()
    db = archivist_db(config)
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
    description="Base Archivist — writes pushed team memory into archivist.db (techne#346).",
)


def register_base_archivist(config=None) -> None:
    """Register ``sink:archivist`` (idempotent). Called before enqueue when a
    non-standalone instance routes thread summaries to the Archivist lane."""
    from techne.sync.sinks import register_sink

    register_sink(ARCHIVIST_SINK)
