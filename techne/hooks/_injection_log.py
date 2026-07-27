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
  injected item actually used" (the counterfactual — deferred, techne#416).
- **Ordering-signal recorded** (techne#413) — ``hit_ids`` alone said WHAT was
  injected but not WHY. Each hit also carries the value that decided its
  position, the scale that value is on, and which channel produced it, so
  retrieval quality is scorable offline and a relevance floor (techne#414)
  has something to threshold on.
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
    approx_tokens   INTEGER NOT NULL,
    total_hits      INTEGER,
    native_scores   TEXT,
    score_kinds     TEXT,
    match_sources   TEXT,
    k_requested     INTEGER
)
"""

# Columns added AFTER context_injections first shipped, in order. This table is
# created lazily here (not via the core schema-reconcile path), so a db written
# before a given column existed won't have it.
_LATER_COLUMNS = {
    "total_hits": "INTEGER",  # techne#343
    "native_scores": "TEXT",  # techne#413 ↓
    "score_kinds": "TEXT",
    "match_sources": "TEXT",
    "k_requested": "INTEGER",
}


def _ensure_columns(conn) -> None:
    """Idempotent self-migration for every post-ship column (techne#343, #413).

    One PRAGMA read, then ADD COLUMN only for what's genuinely absent — a cheap
    no-op once a db is current. SQLite has no ADD COLUMN IF NOT EXISTS, hence the
    guard. Generalized from the single-column #343 version because #413 made this
    the second lazy migration of this table; the behavior per column is identical.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(context_injections)").fetchall()}
    for name, decl in _LATER_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE context_injections ADD COLUMN {name} {decl}")


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
    """The FTS BM25 signal that contributed to ordering, when present.

    Since techne#401 the hybrid path is a vec-first union with no single fused
    score. We deliberately do NOT fold a vector ``distance`` (lower = nearer)
    into the same column as BM25 ``rank`` (more-negative = better) — mixing two
    opposite-signed scales would make this column non-comparable across rows
    (#402 review). So report only the comparable FTS signal: ``rrf_score`` if a
    legacy/federation path fused one, else BM25 ``rank``. Vector-only rows (no
    ``rank``) and budget-selected session-start rows report None — their
    inclusion isn't an FTS score, and that's honest.
    """
    for key in ("rrf_score", "rank"):
        v = _field(r, key)
        if isinstance(v, (int, float)):
            return round(v, 4)
    return None


def _ordering_signal(r: Any) -> tuple[float | None, str | None]:
    """The value that actually decided this row's POSITION, and the scale it's on.

    Complement to ``_inclusion_score`` (techne#413). That function reports only the
    comparable FTS signal and is therefore ``None`` for vector rows — honest, but
    since #401/#402 made search a vec-first union, most hits ARE vector rows, so
    the column that records "why was this here" is mostly empty exactly where the
    rail does its work.

    The fix is not to merge the two scales into one column — that's what the #402
    review correctly rejected, since BM25 ``rank`` (more-negative = better) and vec
    ``distance`` (lower = nearer) are opposite-signed and would be silently
    incomparable. Instead: record the raw value AND a ``kind`` discriminator, so a
    consumer must acknowledge the scale before comparing.

    Which signal placed the row follows ``search()``'s union: the vec channel is
    taken first (already sorted by ascending distance), so a ``vec`` or ``fts+vec``
    row was positioned by its distance — an overlap row keeps its VECTOR position
    and merely carries ``rank`` along as provenance. FTS-only rows were positioned
    by BM25. Anything else (budget-selected session-start rows, edit-rail rows) has
    no retrieval score at all and reports ``(None, None)``.

    NB on reading ``match_sources`` back: ``search()`` stamps ``match_source`` only
    on the union path. Its two early returns (``use_vectors=False``, and vectors
    requested but unavailable/empty) hand back FTS rows unstamped, so the channel
    reads ``None`` there even though the hits are genuinely FTS. ``score_kinds``
    disambiguates — ``"bm25"`` with a null channel is the FTS-only fallback, while
    null kind AND null channel means no retrieval score existed at all.
    """
    if _field(r, "match_source") in ("vec", "fts+vec"):
        d = _field(r, "distance")
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            return round(d, 4), "vec_distance"
    for key, kind in (("rrf_score", "rrf"), ("rank", "bm25")):
        v = _field(r, key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(v, 4), kind
    return None, None


def log_context_injection(
    db_path: Any,
    *,
    source: str,
    session_id: str | None,
    trigger: str | None,
    rows: list,
    injected_text: str,
    total_hits: int | None = None,
    k_requested: int | None = None,
) -> None:
    """Record one context-injection event. Best-effort — never raises.

    ``source`` is ``"per_turn_recall"`` / ``"session_start"`` / ``"codex_edit_rail"``.
    ``rows`` is what was INJECTED (possibly empty — an injection path that fired and
    selected nothing is itself signal); ``hit_count`` is ``len(rows)``. ``total_hits``
    is the pre-cap count of candidates the injection concerns when it differs from
    what was shown (e.g. the edit rail's full blast-radius vs. the capped display,
    techne#343) — ``None`` when the source doesn't cap. ``injected_text`` is the
    rendered block actually added to context; ``trigger`` is the recall prompt (or
    the edited symbol id), ``None`` for the untriggered session-start path.

    ``k_requested`` is the limit the source ASKED for (techne#413) — recorded so a
    full result set is distinguishable from a coincidence: ``hit_count == k`` may
    mean "the cap bound the result" or "that's all there was", and after the fact
    those are indistinguishable without k. ``None`` where a source doesn't retrieve
    by k (session-start selects against a token budget, not a count).

    Per-hit ordering provenance (techne#413) is written as three JSON arrays
    positionally aligned with ``hit_ids``: ``native_scores`` (the value that placed
    the row), ``score_kinds`` (which scale that value is on — never compare across
    kinds), and ``match_sources`` (which channel produced it: vec / fts / fts+vec).
    """
    try:
        hit_ids = [_field(r, "id") for r in rows]
        scores = [_inclusion_score(r) for r in rows]
        ordering = [_ordering_signal(r) for r in rows]
        native_scores = [s for s, _ in ordering]
        score_kinds = [k for _, k in ordering]
        match_sources = [_field(r, "match_source") for r in rows]
        conn = sqlite3.connect(str(db_path))
        try:
            # Parity with every other writer to this db (database.py:471,
            # stores.py:103): WAL serializes writers, so without this a concurrent
            # write makes our INSERT hit SQLITE_BUSY immediately and the
            # best-effort except silently drops the row — gaps in the very audit
            # data this table exists to produce.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_DDL)
            _ensure_columns(conn)  # self-migrate pre-#343 / pre-#413 tables
            conn.execute(
                "INSERT INTO context_injections "
                "(source, session_id, trigger_snippet, hit_count, hit_ids, scores, "
                "approx_tokens, total_hits, native_scores, score_kinds, "
                "match_sources, k_requested) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    session_id,
                    (trigger[:200] if trigger else None),
                    len(rows),
                    json.dumps(hit_ids),
                    json.dumps(scores),
                    _approx_tokens(injected_text),
                    total_hits,
                    json.dumps(native_scores),
                    json.dumps(score_kinds),
                    json.dumps(match_sources),
                    k_requested,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — instrumentation must never break the hook
        pass
