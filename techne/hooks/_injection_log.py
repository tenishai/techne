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
  injected item actually used". #416 shipped the behavioral PROXY over this and
  deferred the judge; the true counterfactual is techne#682.
- **Retrieval signal recorded** (techne#413) — ``hit_ids`` alone said WHAT was
  injected but not WHY. Each hit also carries its raw retrieval score, the scale
  that score is on, and which channel produced it, so retrieval quality is
  scorable offline and a relevance floor (techne#414) has something to threshold
  on. That score is the row's PLACEMENT value only where ``score_kinds`` is
  ``"vec_distance"``; where it is ``"bm25"`` the score is an input to placement,
  not placement itself — see ``_retrieval_signal``, and do not build a floor
  without reading it.
- **Bounded** (techne#1073) — every write also deletes a bounded batch of rows
  older than ``RETENTION_DAYS``. See that constant for why the window is 180 days.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from typing import Any

from techne.core.tokens import approx_tokens as _approx_tokens  # shared helper (#386)

log = logging.getLogger(__name__)

#: Rows older than this are deleted (techne#1073); every write prunes. 180 days
#: outlasts every reader (re-send suppression, `techne audit`, usefulness_audit
#: (#995); #1024 when built); the per-reader reasoning is in PR #1139. A constant, like the other
#: retention windows, not a config key.
RETENTION_DAYS = 180

#: Most rows one prune looks at. The prune runs on every write, so it must cost
#: the same on a store with a million rows as on an empty one. A store upgrading
#: with a long backlog drains it BATCH rows per injection.
PRUNE_BATCH = 500

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
    k_requested     INTEGER,
    rows_truncated  INTEGER,
    chars_dropped   INTEGER
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
    "rows_truncated": "INTEGER",  # techne#853 ↓
    "chars_dropped": "INTEGER",
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


def _inclusion_score(r: Any) -> float | None:
    """The comparable FTS BM25 signal for this row, when present.

    Deliberately NOT "the signal that contributed to ordering" (#419 review): this
    returns ``rank`` for ``fts+vec`` overlap rows too, and those were placed by
    distance, not by BM25. The pre-#413 wording conflated having a BM25 score with
    having been ordered by one — the same conflation ``_retrieval_signal`` exists to
    undo. (Wording arrived with #402/177d95d, corrected here in passing.)

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
        # `not isinstance(v, bool)` — bool is a subclass of int, so a stray True
        # would otherwise log as the score 1.0. Matches _retrieval_signal (#419).
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(v, 4)
    return None


def _retrieval_signal(r: Any) -> tuple[float | None, str | None]:
    """This row's raw retrieval score on its own scale, and which scale that is.

    NOT "the value that placed the row" — that framing is only true for the vector
    channel, and #419 review (Prudence) caught it. Be precise, because techne#414's
    relevance floor gets built from this contract:

    - **vec rows**: ``distance`` IS the placement value — ``search_vec`` sorts by
      ascending distance and the union preserves that order.
    - **FTS rows**: ``rank`` is an INPUT to placement, not placement itself.
      ``_rerank`` orders by ``_combined`` = a weighted blend of normalized BM25,
      recency, and a confidence-decay tiebreak, then POPS ``_combined`` (and
      ``_norm_bm25`` / ``_recency``) before returning — so the actual ordering value
      is not observable from a returned row at all. BM25 merely carries the largest
      default weight (0.7 vs 0.3 recency, 0.1 decay); all three are operator-tunable
      parameters, so even "dominant" isn't guaranteed. A second MMR stage can
      reorder again — a no-op because every live caller leaves ``search_fts``'s
      ``mmr_lambda`` at its ``0.0`` default. That function parameter is now the
      ONLY place the knob exists: the ``SearchConfig`` field was removed in
      techne#420, where it had defaulted to 0.3 while never being plumbed.
      ``search()`` says as much at the union: FTS-only hits are appended "in
      ``search_fts``'s own recency/BM25/decay order."

    Storing the raw rank anyway is the deliberate choice: ``_norm_bm25`` is a
    per-type min–max over whatever candidate pool THAT query produced, so the blend
    is not comparable across queries — useless as a floor. Raw BM25 is stable.

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

    Which signal to attribute follows ``search()``'s union: the vec channel is taken
    first (already sorted by ascending distance), so a ``vec`` or ``fts+vec`` row is
    reported on the distance scale — an overlap row keeps its VECTOR position and
    merely carries ``rank`` along as provenance, so attributing it to BM25 would
    misreport it. FTS-only rows report BM25, with the input-vs-placement caveat
    above. Anything else (budget-selected session-start rows, edit-rail rows) has no
    retrieval score at all and reports ``(None, None)``.

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
    # `rrf` has no in-tree producer today (#401 replaced RRF fusion with the
    # vec-first union; test_search_rrf.py asserts `rrf_score`'s absence on every
    # path). BOTH readers of it are dead code — `_inclusion_score`'s legacy/
    # federation branch too, not just this one (#419 review). Kept deliberately as a
    # matched pair: if a federation path ever reintroduces `rrf_score`, one reader
    # reporting it while the other returns None for the same row would be a silent
    # disagreement between two columns that are supposed to describe one hit.
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
    rows_truncated: int | None = None,
    chars_dropped: int | None = None,
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

    Per-hit retrieval provenance (techne#413) is written as three JSON arrays
    positionally aligned with ``hit_ids``: ``native_scores`` (the row's raw
    retrieval score), ``score_kinds`` (which scale that score is on — never compare
    across kinds), and ``match_sources`` (which channel produced it: vec / fts /
    fts+vec).

    ``rows_truncated`` / ``chars_dropped`` (techne#853) record what the reader did
    NOT receive: how many of ``hit_count`` were cut by the source's char cap, and
    how many characters of content were withheld across the payload. Both ``None``
    for sources that do not truncate. The pair matters more than either alone —
    forty rows cut by five characters and forty cut by four hundred are different
    worlds, and a count cannot tell them apart. Recorded because the cap is a tuning
    knob whose effect was previously unobservable: this table said what was injected
    and never what was severed, so raising or lowering the cap could not be
    evaluated from data (techne#826, #852).

    ``native_scores`` is the row's PLACEMENT value only where ``score_kinds`` is
    ``"vec_distance"`` — stated in terms of the column a floor actually joins on,
    which correctly includes ``fts+vec`` overlap rows (#419 review). Where the kind
    is ``"bm25"`` the score is an input to placement, and the real ordering value is
    unobservable — ``_retrieval_signal`` explains why, and why the raw score is
    still what we store. Read that before building a floor on this column (#414).
    """
    try:
        hit_ids = [_field(r, "id") for r in rows]
        scores = [_inclusion_score(r) for r in rows]
        signals = [_retrieval_signal(r) for r in rows]
        native_scores = [s for s, _ in signals]
        score_kinds = [k for _, k in signals]
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
                "match_sources, k_requested, rows_truncated, chars_dropped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    rows_truncated,
                    chars_dropped,
                ),
            )
            conn.commit()
            # After the commit and in its own guard, so a failed prune never costs
            # the row just written.
            try:
                prune_context_injections(conn)
            except Exception as e:  # noqa: BLE001 — retention must never break the hook
                print(f"techne: context_injections prune skipped: {e}", file=sys.stderr)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — instrumentation must never break the hook
        pass


def prune_context_injections(
    conn: sqlite3.Connection,
    *,
    retention_days: int | None = None,
    batch: int | None = None,
) -> int:
    """Delete up to *batch* rows older than *retention_days*. Returns the count.

    Idempotent, and bounded: it examines only the *batch* lowest ids, so a store
    with nothing to prune pays a short rowid walk rather than a scan of the whole
    table. That relies on ids and timestamps rising together, which holds because
    ``timestamp`` is ``CURRENT_TIMESTAMP`` at insert and no writer supplies it.
    Commits its own work.
    """
    # Defaults resolve at call time so a test can move the module constants.
    retention_days = RETENTION_DAYS if retention_days is None else retention_days
    batch = PRUNE_BATCH if batch is None else batch
    cur = conn.execute(
        "DELETE FROM context_injections"
        " WHERE id IN (SELECT id FROM context_injections ORDER BY id LIMIT ?)"
        "   AND timestamp < datetime('now', ?)",
        (int(batch), f"-{int(retention_days)} days"),
    )
    conn.commit()
    n = cur.rowcount
    if n:
        log.debug("context_injections: pruned %d row(s) older than %d days", n, retention_days)
    return n
