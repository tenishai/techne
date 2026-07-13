"""Per-turn recall visibility log (techne#329).

The auto-recall rail (``user_prompt_submit``) injects memory into context on
every turn but left NO trace — the utilization audit that motivated this could
not see the READ path at all, even though it is the primary way memory reaches
the reasoner. This records, per recall attempt, WHAT was injected (knowledge
ids + scores), how many hits, and the approximate token cost, into a dedicated
``recall_injections`` table so the rail's contribution becomes auditable.

Design:
- **Dedicated table, no FK** — deliberately decoupled from ``observations``
  (which enforces a session FK and feeds threads/FTS). This is instrumentation,
  not memory; it must never fail because a session row doesn't exist yet.
- **Best-effort** — every failure is swallowed. Observability must never break
  the turn (same contract as the rail itself).
- **Attribution-ready** — ``hit_ids`` is recorded so a later pass can join
  "was the injected item actually used this turn" (the counterfactual is
  deferred; this makes it *possible*).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS recall_injections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    timestamp     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    prompt_snippet TEXT,
    hit_count     INTEGER NOT NULL,
    hit_ids       TEXT,
    scores        TEXT,
    approx_tokens INTEGER NOT NULL
)
"""


def _approx_tokens(text: str) -> int:
    """Rough token estimate for cost tracking (~4 chars/token). Cheap on purpose."""
    return (len(text) + 3) // 4 if text else 0


def _inclusion_score(r: dict) -> float | None:
    """The signal that actually determined inclusion/order.

    Prefer ``rrf_score`` (the fused score when search ran the hybrid vector path,
    ``use_vectors=True``); fall back to the raw FTS BM25 ``rank``. The per-turn
    rail is FTS-only today, so ``rank`` is what's present — but preferring the
    fused score keeps the log faithful if the rail ever enables vectors.
    """
    v = r.get("rrf_score")
    if v is None:
        v = r.get("rank")
    return round(v, 4) if isinstance(v, (int, float)) else None


def log_recall_injection(
    db_path: Any,
    *,
    session_id: str | None,
    prompt: str,
    rows: list[dict],
    injected_text: str,
) -> None:
    """Record one per-turn recall attempt. Best-effort — never raises.

    ``rows`` is the search result (possibly empty — a recall that fired and
    matched nothing is itself signal). ``injected_text`` is the rendered block
    that was actually added to context ("" when there were no hits).
    """
    try:
        hit_ids = [r.get("id") for r in rows]
        scores = [_inclusion_score(r) for r in rows]
        conn = sqlite3.connect(str(db_path))
        try:
            # Parity with every other writer to this db (database.py:471,
            # stores.py:103): the db is WAL, which serializes writers, so without
            # this a concurrent write (post_tool_use / reflect / federation) makes
            # our INSERT hit SQLITE_BUSY immediately and the best-effort except
            # would silently drop the row — punching gaps in the very audit data
            # this table exists to produce.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_DDL)
            conn.execute(
                "INSERT INTO recall_injections "
                "(session_id, prompt_snippet, hit_count, hit_ids, scores, approx_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    (prompt or "")[:200],
                    len(rows),
                    json.dumps(hit_ids),
                    json.dumps(scores),
                    _approx_tokens(injected_text),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — instrumentation must never break the turn
        pass
