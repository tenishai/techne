"""Context-injection visibility log (techne#329).

BOTH memory-injection paths write here so the audit can compare them:
  - ``source="per_turn_recall"`` — the UserPromptSubmit rail (techne#109),
    prompt-triggered, opt-in (default OFF).
  - ``source="session_start"`` — the SessionStart tier-0 knowledge block
    (techne#95), once per session — the path that's actually live by default.

The utilization audit that motivated this couldn't see EITHER read-path:
injected memory left no trace, so "is what we inject worthwhile?" was
unanswerable from data. This records, per injection, WHAT was injected
(knowledge ids + scores), how many items, and the approx token cost.

Design:
- **Dedicated table, no FK** — decoupled from ``observations`` (which enforces a
  session FK + feeds threads/FTS). Instrumentation, not memory; it must never
  fail because a session row doesn't exist yet.
- **Best-effort** — every failure swallowed. Observability must never break a
  hook (same contract as the hooks themselves).
- **Source-discriminated** — one table + a ``source`` column, so the two paths
  are comparable in a single query, not a cross-table UNION.
- **Row-shape agnostic** — recall rows are dicts (from ``search()``) and
  session-start rows are ``EgressRow`` (a ``TypedDict`` — also a dict at
  runtime), so ``_field`` reads both via ``.get``; its ``getattr`` fallback is
  defensive for any future non-dict row type.
- **Attribution-ready** — ``hit_ids`` recorded so a later pass can join "was the
  injected item actually used" (the counterfactual — deferred).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS context_injections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    session_id      TEXT,
    timestamp       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trigger_snippet TEXT,
    hit_count       INTEGER NOT NULL,
    hit_ids         TEXT,
    scores          TEXT,
    approx_tokens   INTEGER NOT NULL
)
"""


def _field(r: Any, key: str) -> Any:
    """Read a field from a row. Both current sources are dict-shaped (recall
    dicts; session-start ``EgressRow`` is a ``TypedDict``); the ``getattr``
    branch is a defensive fallback for any future non-dict row type."""
    if isinstance(r, dict):
        return r.get(key)
    return getattr(r, key, None)


def _approx_tokens(text: str) -> int:
    """Rough token estimate for cost tracking (~4 chars/token). Cheap on purpose."""
    return (len(text) + 3) // 4 if text else 0


def _inclusion_score(r: Any) -> float | None:
    """The signal that actually determined inclusion/order, when there is one.

    Prefer ``rrf_score`` (the fused score when search ran the hybrid vector path);
    fall back to the raw FTS BM25 ``rank``. Budget-selected session-start rows
    have neither → None (their inclusion isn't a query score, and that's honest).
    """
    v = _field(r, "rrf_score")
    if v is None:
        v = _field(r, "rank")
    return round(v, 4) if isinstance(v, (int, float)) else None


def log_context_injection(
    db_path: Any,
    *,
    source: str,
    session_id: str | None,
    trigger: str | None,
    rows: list,
    injected_text: str,
) -> None:
    """Record one context-injection event. Best-effort — never raises.

    ``source`` is ``"per_turn_recall"`` or ``"session_start"``. ``rows`` is what
    was selected (possibly empty — an injection path that fired and selected
    nothing is itself signal). ``injected_text`` is the rendered block actually
    added to context ("" when there were no rows). ``trigger`` is the prompt for
    recall, ``None`` for the (untriggered) session-start path.
    """
    try:
        hit_ids = [_field(r, "id") for r in rows]
        scores = [_inclusion_score(r) for r in rows]
        conn = sqlite3.connect(str(db_path))
        try:
            # Parity with every other writer to this db (database.py:471,
            # stores.py:103): WAL serializes writers, so without this a concurrent
            # write makes our INSERT hit SQLITE_BUSY immediately and the
            # best-effort except silently drops the row — gaps in the very audit
            # data this table exists to produce.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_DDL)
            conn.execute(
                "INSERT INTO context_injections "
                "(source, session_id, trigger_snippet, hit_count, hit_ids, scores, approx_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    session_id,
                    (trigger[:200] if trigger else None),
                    len(rows),
                    json.dumps(hit_ids),
                    json.dumps(scores),
                    _approx_tokens(injected_text),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — instrumentation must never break the hook
        pass
