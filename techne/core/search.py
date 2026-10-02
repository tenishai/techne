"""Search across all techne data layers — FTS5 and optional vector search."""

import json
import logging
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Hashable
from typing import Literal

from techne.core import embeddings as embeddings_mod
from techne.core.database import connect
from techne.core.embeddings import _get_embedding_model
from techne.core.knowledge import decay_score

logger = logging.getLogger(__name__)


#: THE RESULT-ROW CONTRACT (techne#579), and why `type` is deliberately ABSENT.
#:
#: A search row carries TWO different notions of kind and they were once the same
#: key, which made the wrong reading the obvious one:
#:
#:   source_table    — which table the row came from: thread | observation | knowledge
#:   knowledge_type  — the knowledge row's OWN kind: fact | decision | procedure |
#:                     evaluation | document_card | pattern. Knowledge rows only.
#:
#: `type` used to hold the FIRST of those, and it cost two live bugs. #565: the
#: per-turn pull-only guard was written `[r for r in rows if r.get("type") not in
#: PULL_ONLY_TYPES]` — it passed nothing and blocked nothing, because `type` was
#: always the literal "knowledge". Only a fail-without-fix test caught it; review
#: did not. #566: `_build_context` rendered `[{r.get("type")}]`, so EVERY injected
#: line read `[knowledge]` regardless of kind, collapsing decision-vs-evaluation
#: exactly where the agent decides how much to trust the line.
#:
#: `type` is REMOVED rather than kept as an alias. An alias preserves the trap for
#: whoever reaches for the obvious name first, and the natural code being the
#: broken code is the whole defect. Now a stale consumer raises KeyError at its own
#: call site — loud, and at the right place — instead of silently comparing against
#: a plausible wrong string.


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two array-like objects. Returns 0.0 on zero vectors."""
    import numpy as np

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _mmr_reorder(
    candidates: list[dict],
    limit: int,
    mmr_lambda: float,
    relevance_key: str = "_combined",
) -> list[dict]:
    """Greedy MMR second pass over a pre-scored candidate list.

    Each candidate must already have ``relevance_key`` set (higher = better).
    Embeddings are lazily computed via the active embedding backend (model2vec
    or fastembed — techne#601/#602, same singleton as #19 inference) and
    cached per candidate in ``_emb`` so no row is embedded twice per query.

    When no embedding backend is installed the function returns
    candidates[:limit] in relevance order — identical to the pre-MMR
    behaviour (graceful degradation).

    Args:
        candidates: Pre-sorted (desc relevance) candidate dicts.
        limit:      Number of items to return.
        mmr_lambda: Diversity weight λ ∈ [0, 1].
                    0 → pure relevance (no reordering).
                    1 → pure diversity.
        relevance_key: Dict key holding each candidate's relevance score.
    """
    if not candidates or limit <= 0:
        return candidates[:limit]

    # λ=0 → pure relevance; skip embedding pass entirely.
    if mmr_lambda == 0.0:
        return candidates[:limit]

    try:
        import numpy as np
    except ImportError:
        return candidates[:limit]

    try:
        # allow_download=False for the same reason as search_vec (techne#708).
        # This call site is UNREACHABLE today — the mmr_lambda == 0.0
        # short-circuit above returns before it and no live caller raises
        # lambda (the config knob was removed; see docs/arch/search-embeddings.md
        # "MMR — CODE EXISTS, NO CONFIG SURFACE"). Guarded anyway so it cannot
        # become a cold-cache hang the day someone re-plumbs the knob.
        model = _get_embedding_model(allow_download=False)
    except Exception:
        # No embedding backend installed, or the model is not cached — no-op.
        return candidates[:limit]

    # Embed all candidates that don't already have a cached embedding.
    needs_embed = [c for c in candidates if "_emb" not in c]
    if needs_embed:
        texts = [c.get("content") or c.get("summary") or c.get("label") or "" for c in needs_embed]
        try:
            embeddings = list(model.embed(texts))
            for c, emb in zip(needs_embed, embeddings):
                c["_emb"] = np.array(emb, dtype="float32")
        except Exception:
            # Embedding failed mid-query — degrade to relevance order.
            for c in candidates:
                c.pop("_emb", None)
            return candidates[:limit]

    selected: list[dict] = []
    remaining = list(candidates)

    while remaining and len(selected) < limit:
        if not selected:
            # First pick: highest relevance (list is already sorted desc).
            best = remaining[0]
        else:
            # MMR score = (1-λ)*relevance - λ*max_similarity_to_selected
            selected_embs = [s["_emb"] for s in selected if "_emb" in s]
            best = None
            best_score = float("-inf")
            for c in remaining:
                rel = c.get(relevance_key, 0.0)
                if selected_embs and "_emb" in c:
                    max_sim = max(_cosine_similarity(c["_emb"], s) for s in selected_embs)
                else:
                    max_sim = 0.0
                score = (1.0 - mmr_lambda) * rel - mmr_lambda * max_sim
                if score > best_score:
                    best_score = score
                    best = c

        selected.append(best)
        remaining.remove(best)

    # Clean MMR scratch keys.
    for c in selected + remaining:
        c.pop("_emb", None)

    return selected


SearchTable = Literal["threads", "observations", "knowledge", "all"]


def _knowledge_decay_score(
    row: dict,
    half_life_days: float,
    now: datetime | None = None,
) -> float:
    """Decay-adjusted confidence score (0..1) for a knowledge row dict.

    Used as a small additive tiebreak weight in ``_rerank`` (techne#214).
    Delegates to ``knowledge.decay_score()`` — the single canonical formula
    (techne#249).  Only knowledge rows carry ``confidence_numeric`` — for
    threads and observations the function returns 1.0 so the decay weight has
    no effect.

    ``half_life_days=0`` disables time decay and returns ``confidence_numeric``
    directly (the uncertain penalty still applies).

    Returns 1.0 when ``confidence_numeric`` is absent from the row dict so
    that manually-constructed test candidates (and non-knowledge rows) are not
    penalised.
    """
    if "confidence_numeric" not in row:
        return 1.0  # No metadata → neutral; no effect on threads/observations.
    return decay_score(
        row["confidence_numeric"],
        row.get("last_confirmed_at"),
        half_life_days,
        row.get("status"),
        now=now,
    )


def _recency_score(ts_str: str | None, half_life_days: float = 30.0) -> float:
    """Exponential decay from [0..1] where 1 = now, 0 = very old.

    Uses ``COALESCE(event_at, updated_at, created_at, timestamp)`` logic —
    caller passes whichever timestamp field is available for the row type.
    Returns 0.5 on parse failure (neutral, so BM25 still drives ordering).
    """
    if not ts_str:
        return 0.5
    try:
        # SQLite timestamps come back as strings: "YYYY-MM-DD HH:MM:SS"
        dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        return math.exp(-age_days * math.log(2) / half_life_days)
    except (ValueError, OSError):
        return 0.5


def _rerank(
    candidates: list[dict],
    limit: int,
    recency_weight: float,
    bm25_weight: float,
    mmr_lambda: float = 0.0,
    confidence_decay_weight: float = 0.1,
    half_life_days: float = 30.0,
) -> list[dict]:
    """Two-stage reranker: (1) recency × BM25 + decay combined score, then (2) optional MMR.

    Stage 1 — BM25 from FTS5 is negative (more negative = better match).
    Normalise to [0..1] across the candidate set so it can be linearly combined
    with recency_score.  score = recency_weight * recency + bm25_weight * norm_bm25
    + confidence_decay_weight * effective_confidence.

    The ``confidence_decay_weight`` term (techne#214) is deliberately modest
    (default 0.1) — a tiebreak/penalty, not a primary signal. A deeply-decayed
    'high' entry (e.g. unconfirmed for 2+ years) will score near zero on this
    term while a fresh 'medium' scores ~0.06, allowing the fresh entry to
    outrank the stale one when BM25/recency are otherwise equal.  Entries are
    NEVER excluded by decay — this is purely an additive weight.  Rows without
    ``confidence_numeric`` (threads, observations, manually-constructed test
    dicts) receive a neutral 1.0, so the weight adds uniformly and does not
    change relative ordering among non-knowledge rows.

    Stage 2 — Greedy MMR selection from the stage-1 ordered pool.  When
    mmr_lambda=0 (default) MMR is skipped and pure relevance ordering is kept.
    When no embedding backend is available MMR degrades silently to relevance
    ordering.
    """
    if not candidates:
        return candidates

    # BM25 is negative; most-negative = best match. Normalise PER SOURCE TABLE:
    # bm25() scores are NOT comparable across fts tables — threads/observations/
    # knowledge differ in doc count, average length, and term stats, so their BM25
    # magnitudes differ. Normalising a pooled multi-table set over one global spread
    # distorts cross-table ranking: the best match in a small-magnitude table gets
    # buried under another table's mid-pack rows (techne#56). Group by `type` and
    # normalise within each table's own [min, max]. A single-table search has one
    # group, so its behaviour is unchanged.
    spread_by_type: dict[str, tuple[float, float]] = {}
    ranks_by_type: dict[str, list[float]] = {}
    for r in candidates:
        # A row may lack `rank`: a vector-only hit carries `distance`, not BM25
        # `rank`. Federation's merge used to rerank vec rows here; since
        # techne#1089 it does not, but the guard stays for any caller that does.
        # Skip it here so it neither crashes nor distorts the FTS rows' per-type
        # min-max spread. (techne#401 / #402 review — Prudence.)
        rank = r.get("rank")
        if rank is not None:
            ranks_by_type.setdefault(r["source_table"], []).append(rank)
    for t, ranks in ranks_by_type.items():
        mn, mx = min(ranks), max(ranks)
        spread_by_type[t] = (mx, mx - mn)  # (max_r, spread); spread >= 0

    for r in candidates:
        rank = r.get("rank")
        if rank is None or r["source_table"] not in spread_by_type:
            # No BM25 signal (a pure vector-only hit reaching the merge path).
            # 0.0 is the per-type FLOOR on the [0,1] norm scale, not a midpoint,
            # so such a row leans entirely on recency/decay and can be demoted
            # below rank-bearing peers — and, since the merge truncates to
            # limit, possibly below the cut. Accepted: `distance` and BM25
            # `rank` aren't foldable onto one scale, and this is the rare
            # vec-only-on-federation case; the common fts+vec overlap keeps its
            # FTS rank (Layer 1). (techne#401 / #402 review — Prudence.)
            r["_norm_bm25"] = 0.0
            continue
        max_r, spread = spread_by_type[r["source_table"]]
        # norm_bm25=1 for this table's best match (most-negative rank), 0 for worst.
        r["_norm_bm25"] = (max_r - rank) / spread if spread else 1.0

    for r in candidates:
        # Preferred order: event_at (real-world subject-event time) →
        # updated_at (last modification) → timestamp (ingestion time).
        ts = r.get("event_at") or r.get("updated_at") or r.get("timestamp")
        r["_recency"] = _recency_score(ts)
        r["_combined"] = recency_weight * r["_recency"] + bm25_weight * r["_norm_bm25"]
        # Confidence decay tiebreak (techne#214): add a small additive bonus for
        # freshly-confirmed knowledge entries. Deliberately modest so decay never
        # dominates — see _knowledge_decay_score() docstring. Non-knowledge rows
        # (no confidence_numeric) receive a neutral 1.0, adding uniformly so
        # ordering among non-knowledge rows is unchanged.
        if confidence_decay_weight > 0:
            r["_combined"] += confidence_decay_weight * _knowledge_decay_score(r, half_life_days)

    candidates.sort(key=lambda x: -x["_combined"])

    # Stage 2: MMR diversity pass (no-op when mmr_lambda=0 or no embedding backend).
    result = _mmr_reorder(candidates, limit, mmr_lambda=mmr_lambda, relevance_key="_combined")

    # Clean scratch keys before returning.
    for r in result:
        r.pop("_norm_bm25", None)
        r.pop("_recency", None)
        r.pop("_combined", None)

    return result


#: Per FTS sub-table: (FTS table, base table, base column equal to the FTS rowid,
#: base columns the result row is built from). Allow-listed names only — every SQL
#: string below interpolates these, never caller input.
_FTS_SUB_TABLES: dict[str, tuple[str, str, str, str]] = {
    "threads": (
        "threads_fts",
        "threads",
        "rowid",
        "b.id, b.label, b.summary, b.resume_points, b.status, b.session_id, b.updated_at",
    ),
    "observations": (
        "observations_fts",
        "observations",
        "id",
        "b.id, b.content, b.tool_name, b.session_id, b.timestamp, b.event_at, b.tier",
    ),
    "knowledge": (
        "knowledge_fts",
        "knowledge",
        "id",
        "b.id, b.type, b.content, b.tags, b.confidence, b.confidence_numeric, "
        "b.last_confirmed_at, b.status, b.source, b.updated_at",
    ),
}


#: The most one clamped term can add to a row's bm25: FTS5's clamped idf (1e-6) times
#: its fixed ``k1 + 1`` (k1 = 1.2), the limit of the term-frequency factor
#: (techne#1093). Bounds how far dropping a term can move any row's score.
_CLAMPED_TERM_MAX_BM25 = 1e-6 * 2.2


def _drop_clamped_or_terms(conn: sqlite3.Connection, fts_table: str, or_query: str) -> str:
    """Drop the terms of an OR-of-terms MATCH that occur in more than half of
    ``fts_table``'s rows (techne#1093). Returns ``or_query`` unchanged when no
    term qualifies, and when EVERY term does (the query is never emptied).

    THE 50% IS FIXED BY FTS5, NOT TUNED. FTS5's bm25 computes
    ``idf = log((N - df + 0.5) / (df + 0.5))`` and clamps ``idf <= 0`` to 1e-6,
    which happens exactly when ``df >= N / 2``. Such a term adds at most
    ``_CLAMPED_TERM_MAX_BM25`` to any row's score, so dropping it can only
    reorder rows whose scores on the kept terms are within that amount (two
    rows tying on every kept term can swap), and it removes rows that match
    NOTHING BUT clamped terms, which rank below every row whose kept-term
    score exceeds the slack. :func:`_fts_match_rows` guards the LIMIT
    boundary against both. At 20% the prune was measured lossy (FTS-only
    pages 26/49 identical), so do not lower it. bm25's ``N`` is FTS5's
    ``nTotalRow`` (the averages record in
    ``<fts>_data``); ``<fts>_docsize`` has one row per indexed document and
    equals it while the index is consistent. It is the whole index, not a
    filtered count.

    Each term costs one probe bounded at ``N // 2 + 1`` rowids. ``or_query`` is
    :func:`build_or_fts_query`'s output: phrase literals with no whitespace
    inside them, joined by ``" OR "``, so splitting on that separator is exact.
    """
    terms = or_query.split(" OR ")
    if len(terms) < 2:
        return or_query
    half = conn.execute(f"SELECT count(*) FROM {fts_table}_docsize").fetchone()[0] // 2
    probe = f"SELECT count(*) FROM (SELECT 1 FROM {fts_table} WHERE {fts_table} MATCH ? LIMIT ?)"
    keep = [t for t in terms if conn.execute(probe, (t, half + 1)).fetchone()[0] <= half]
    return " OR ".join(keep) if keep else or_query


def _fts_sub_table_rows(
    conn: sqlite3.Connection,
    sub_table: str,
    fts_query: str,
    candidate_limit: int,
    filter_sql: str,
) -> list[sqlite3.Row]:
    """The top ``candidate_limit`` base rows of one FTS sub-table, best bm25
    first, each carrying its ``rank`` — RANK FIRST, THEN FETCH (techne#1092).

    The single-statement shape (``FTS JOIN base ... ORDER BY rank LIMIT``)
    fetched and sorted EVERY matching row with its content before the LIMIT
    applied, and the OR pass matches most of a table (p50 47,399 of 59,836
    observations). So step 1 ranks ``(rowid, bm25)`` pairs alone and keeps the
    top ``candidate_limit``; step 2 fetches at most that many base rows by id.

    FILTERS APPLY BEFORE THE LIMIT, IN STEP 1. ``filter_sql`` (the knowledge
    superseded/archived/uncertain clauses) is on base columns, so when it is
    non-empty step 1 joins the base table to evaluate it — still sorting only
    ``(rowid, rank)``. Filtering after the LIMIT would return a short page
    whenever a filtered row ranked inside it. Step 2 repeats the filter, so a
    row changed or deleted between the two statements shows up as a MISSING
    row, and any missing row sends the whole sub-table through the original
    single-statement query instead. The page is therefore always exactly what
    that query returns, with the same ``bm25`` values.
    """
    fts, base, key, cols = _FTS_SUB_TABLES[sub_table]
    if filter_sql:
        rank_sql = (
            f"SELECT f.rowid, bm25({fts}) AS rank FROM {fts} f "
            f"JOIN {base} b ON b.{key} = f.rowid "
            f"WHERE {fts} MATCH ?{filter_sql} ORDER BY rank LIMIT ?"
        )
    else:
        rank_sql = (
            f"SELECT rowid, bm25({fts}) AS rank FROM {fts} "
            f"WHERE {fts} MATCH ? ORDER BY rank LIMIT ?"
        )
    ranked = conn.execute(rank_sql, (fts_query, candidate_limit)).fetchall()
    if not ranked:
        return []
    fetched = {
        r["_fts_rowid"]: r
        for r in conn.execute(
            f"SELECT b.{key} AS _fts_rowid, {cols} FROM {base} b "
            f"WHERE b.{key} IN (SELECT value FROM json_each(?)){filter_sql}",
            (json.dumps([r[0] for r in ranked]),),
        )
    }
    if len(fetched) == len(ranked):
        return [dict(fetched[rowid], rank=rank) for rowid, rank in ranked]
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT {cols}, bm25({fts}) AS rank FROM {fts} f "
            f"JOIN {base} b ON b.{key} = f.rowid "
            f"WHERE {fts} MATCH ?{filter_sql} ORDER BY rank LIMIT ?",
            (fts_query, candidate_limit),
        )
    ]


def _fts_match_rows(
    conn: sqlite3.Connection,
    fts_query: str,
    table: SearchTable,
    candidate_limit: int,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
    drop_clamped_or_terms: bool = False,
) -> list[dict]:
    """Run the per-table FTS5 MATCH queries for ``fts_query`` and return raw
    candidate rows (pre-rerank). Shared by the implicit-AND pass and the #70
    OR fallback so both issue identical SQL; each table ranks before it
    fetches (see :func:`_fts_sub_table_rows`).

    ``include_archived`` (techne#216): when False, soft-archived knowledge rows
    (``status = 'archived'``) are filtered out of the knowledge FTS query.
    Default True — explicit search (techne_search) returns archived entries;
    only DEFAULT recall (techne_recall) passes False. Affects the knowledge
    table only; threads/observations have no archive lifecycle.

    ``drop_clamped_or_terms`` (techne#1093): ``fts_query`` is an OR-of-terms
    query; drop, per table, the terms bm25 clamps to ~zero weight (see
    :func:`_drop_clamped_or_terms`). The pruned result is kept only when it
    provably has the full query's top ``candidate_limit`` rows: it must be full
    (otherwise rows matching only dropped terms would fill the rest), and the
    bm25 gap at the LIMIT boundary must exceed the most the dropped terms can
    add to a score (otherwise a clamped term decides a tie for the last slot).
    Failing either, that table re-runs the full query. Order WITHIN the kept
    rows can still differ from the full query's where two rows tie on every
    kept term, and ``rank`` differs by the dropped terms' clamped
    contributions. Only the OR pass sets this; the AND pass never drops a term.
    """
    # The caller's cap wins for KNOWLEDGE rows; the ledger tables keep
    # `_ROW_CHAR_CAP` regardless (techne#842).
    _cap = row_chars if row_chars is not None else _ROW_CHAR_CAP
    results: list[dict] = []

    def _rows(sub_table: str, filter_sql: str = "") -> list[dict]:
        if drop_clamped_or_terms:
            pruned = _drop_clamped_or_terms(conn, _FTS_SUB_TABLES[sub_table][0], fts_query)
            if pruned != fts_query:
                rows = _fts_sub_table_rows(conn, sub_table, pruned, candidate_limit + 1, filter_sql)
                # The pruned top `candidate_limit` is the full query's top set only if
                # the score gap at the LIMIT boundary exceeds what the dropped terms
                # could add (a row past it can gain at most `slack`). A short result,
                # or a boundary tie within `slack`, re-runs the full query: the rows
                # matching only dropped terms, or a clamped term breaking the tie,
                # would decide the last slot.
                slack = (
                    len(fts_query.split(" OR ")) - len(pruned.split(" OR "))
                ) * _CLAMPED_TERM_MAX_BM25
                if (
                    len(rows) > candidate_limit
                    and rows[candidate_limit]["rank"] - rows[candidate_limit - 1]["rank"] > slack
                ):
                    return rows[:candidate_limit]
        return _fts_sub_table_rows(conn, sub_table, fts_query, candidate_limit, filter_sql)

    if table in ("threads", "all"):
        for r in _rows("threads"):
            results.append(
                {
                    "source_table": "thread",
                    "id": r["id"],
                    "label": r["label"],
                    "summary": r["summary"][:_ROW_CHAR_CAP],
                    "resume_points": r["resume_points"],
                    "status": r["status"],
                    "session_id": r["session_id"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                }
            )

    if table in ("observations", "all"):
        for r in _rows("observations"):
            results.append(
                {
                    "source_table": "observation",
                    "id": r["id"],
                    "content": r["content"][:_ROW_CHAR_CAP],
                    "tool_name": r["tool_name"],
                    "session_id": r["session_id"],
                    "timestamp": r["timestamp"],
                    "event_at": r["event_at"],
                    "tier": r["tier"],
                    "rank": r["rank"],
                }
            )

    if table in ("knowledge", "all"):
        # techne#216: DEFAULT recall (include_archived=False) drops soft-archived
        # rows; explicit search (default True) keeps them. Interpolating a fixed
        # clause here — not a bound param — because it's a boolean-selected literal
        # SQL fragment, never user input.
        archived_clause = "" if include_archived else " AND b.status != 'archived'"
        # techne#501: `uncertain` is a TRUST STATE and now FILTERS, where it used to
        # only feed decay_score as "a tiebreak/penalty, not a primary signal"
        # (weight 0.1). A state answers "may this be acted on"; a confidence answers
        # "how sure" — and we already have `confidence` + `confidence_numeric` doing
        # the second job. Wiring the state to rank instead of to a filter meant an
        # entry we are on record as NOT BELIEVING still surfaced, still injected,
        # and still read to the agent as true, just slightly lower down.
        #
        # DEFAULT IS FAIL-CLOSED, unlike include_archived's permissive default, and
        # the asymmetry is deliberate: this one is free. Measured 2026-08-18 —
        # 1091 knowledge rows, ALL `current`, and knowledge_deps is empty, so
        # `_cascade_uncertain` has never fired in production. Filtering by default
        # changes nothing today and protects the moment #443 populates the deps
        # and the cascade starts flipping rows.
        uncertain_clause = "" if include_uncertain else " AND b.status != 'uncertain'"
        filter_sql = f" AND b.superseded_by IS NULL{archived_clause}{uncertain_clause}"
        for r in _rows("knowledge", filter_sql):
            results.append(
                {
                    "source_table": "knowledge",
                    "id": r["id"],
                    "knowledge_type": r["type"],
                    # KNOWLEDGE HONOURS THE CALLER'S CAP (techne#842).
                    "content": r["content"][:_cap],
                    # THE PRE-TRUNCATION LENGTH, so a caller can count what the
                    # reader LOST (techne#853, restored here by techne#842).
                    # Without it, a caller that only ever sees an already-cut row
                    # can measure the marker's width and nothing else.
                    "content_chars_total": len(r["content"]),
                    "tags": r["tags"],
                    "confidence": r["confidence"],
                    # Decay metadata (techne#214): used by _rerank to compute
                    # effective_confidence as a tiebreak signal.
                    "confidence_numeric": r["confidence_numeric"],
                    "last_confirmed_at": r["last_confirmed_at"],
                    "status": r["status"],
                    "source": r["source"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                }
            )

    # documents moved to librarian.db (db-per-domain, techne#99) — not searched
    # here; use the Librarian store (techne library search).

    return results


#: DEFAULT per-row character cap for search results. NOT A CEILING ANY MORE
#: (techne#842): callers that need whole rows pass `row_chars`, and the per-turn
#: recall rail passes its own `hooks.per_turn_recall_chars`, so nothing arrives
#: pre-cut below what the caller asked for.
#:
#: WHAT THE UNCONDITIONAL SLICE COST, measured rather than argued. A row arriving
#: at exactly 500 took `_truncate_for_injection`'s `len(content) <= limit` branch,
#: so `was_cut` stayed False and the row shipped severed mid-word with BOTH the
#: announcing marker and the header notice suppressed — the one configuration in
#: which that rail was less honest than before techne#826, whose entire point was
#: that a truncated row announces itself. And it hit the rows worth having:
#: knowledge#750 (1,359 chars, a review procedure) cut immediately before its
#: worked example, knowledge#673 (2,247 chars, a re-auth runbook) immediately
#: before the actual steps.
#:
#: OBSERVATIONS AND THREADS KEEP THE DEFAULT, deliberately. The observation table
#: is a tool ledger — mostly shell echoes — and truncating it costs nothing a
#: reader wanted. `knowledge` is the curated layer someone deliberately wrote.
#: That asymmetry is the whole content of this constant.
_ROW_CHAR_CAP = 500


def _and_or_pools(
    conn: sqlite3.Connection,
    query: str,
    table: SearchTable,
    candidate_limit: int,
    *,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Gather ``search_fts``'s AND pool and OR top-up pool, SEPARATELY, across
    every sub-table for ``table="all"`` (or the one table otherwise).

    THIS IS PER SUB-TABLE, NOT A POOLED TOTAL (techne#1079). ``table="all"``
    used to make ONE :func:`fts_and_or_split` call wrapping
    ``_fts_match_rows(..., "all", candidate_limit, ...)``, which internally
    runs three separate ``LIMIT candidate_limit`` queries (threads/
    observations/knowledge) and concatenates them — so the AND row count
    compared against ``want`` was the SUM across tables. That let one table's
    AND pass alone reaching ``candidate_limit`` (e.g. observations) satisfy
    the pooled comparison even while another table's AND pass (e.g.
    knowledge) returned nothing, silently skipping that table's top-up. Each
    sub-table now gets its own :func:`fts_and_or_split` call with its own
    ``want=candidate_limit``, so a table's top-up decision depends only on
    its own AND count, never on how much another table happened to fetch. A
    single-table caller (``table`` other than ``"all"``) was never affected
    by this — there was only ever one table to sum.

    Returns ``(and_pool, or_pool)``, NEITHER reranked NOR capped to any
    display ``limit`` — ``search_fts`` reranks and concatenates each block
    separately; see its docstring for why they must stay apart until then.

    Every row is tagged ``_match_pass: "and" | "or"`` (techne#1079 review
    Important 1). This is what lets a LATER step downstream —
    ``federation.merge_and_rank``, which merges pages from several stores
    (techne#1089) — tell the two populations apart, instead of recreating
    the #1081 bug one hop further out. Local, single-store consumers
    (``search_fts`` itself) don't need the
    tag for correctness — the block split already IS the two lists — but
    tagging here rather than in two duplicate places keeps one producer.
    """
    sub_tables = ("threads", "observations", "knowledge") if table == "all" else (table,)
    and_pool: list[dict] = []
    or_pool: list[dict] = []
    for sub_table in sub_tables:

        def _run(match: str, st: SearchTable = sub_table, drop: bool = False) -> list[dict]:
            return _fts_match_rows(
                conn,
                match,
                st,
                candidate_limit,
                include_archived=include_archived,
                include_uncertain=include_uncertain,
                row_chars=row_chars,
                drop_clamped_or_terms=drop,
            )

        and_rows, or_rows = fts_and_or_split(
            query,
            _run,
            want=candidate_limit,
            key=lambda r: (r["source_table"], r["id"]),
            # techne#1093: the OR pass drops terms bm25 clamps to ~zero weight.
            run_or=lambda match, run=_run: run(match, drop=True),
        )
        for r in and_rows:
            r["_match_pass"] = "and"
        for r in or_rows:
            r["_match_pass"] = "or"
        and_pool.extend(and_rows)
        or_pool.extend(or_rows)
    return and_pool, or_pool


def search_fts(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 20,
    recency_weight: float = 0.3,
    bm25_weight: float = 0.7,
    expansion_ratio: int = 2,
    mmr_lambda: float = 0.0,
    confidence_decay_weight: float = 0.1,
    half_life_days: float = 30.0,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Full-text search across techne tables.

    ``conn`` (techne#1094): run on the caller's connection and leave it open.
    ``search()`` passes the one connection it holds for the whole call. Without
    it this opens its own connection and closes it before returning.

    ``row_chars`` caps each KNOWLEDGE row's content, defaulting to
    ``_ROW_CHAR_CAP`` when None (techne#842). It does NOT move observation or
    thread rows: those are the tool ledger, and a caller raising its recall
    budget wants more of the curated row, not more transcript. A caller that
    renders a truncation marker should ask for one character MORE than it will
    show — at exactly the cap a severed row is indistinguishable from a whole
    one.

    Fetches ``expansion_ratio * limit`` BM25 candidates per table, then
    applies a two-stage reranker:
      Stage 1 — recency × BM25 + decay-confidence combined score.
      Stage 2 — optional MMR diversity pass (skipped when mmr_lambda=0).

    Args:
        recency_weight:          Weight for exponential-decay recency score (0..1).
        bm25_weight:             Weight for normalised BM25 score (0..1).
        expansion_ratio:         Candidate pool = expansion_ratio * limit per table.
        mmr_lambda:              MMR diversity weight λ ∈ [0, 1].  0 = pure relevance
                                 (default, matches legacy behaviour).  Requires an
                                 embedding backend (model2vec or fastembed);
                                 degrades silently to relevance order if neither
                                 is installed.
        confidence_decay_weight: Small additive weight for effective_confidence()
                                 tiebreak (techne#214).  Default 0.1 — deliberately
                                 modest, decay is operational not accuracy-driving.
                                 Set to 0.0 to disable decay entirely.
        half_life_days:          Half-life for confidence decay, passed from
                                 config.knowledge_confidence_half_life_days.
                                 Default 30.0; 0.0 disables time decay.
        include_archived:        When False, soft-archived knowledge rows (techne#216)
                                 are excluded. Default True — explicit search keeps
                                 archived entries; only DEFAULT recall passes False.
    """
    fts_query = prepare_fts_query(query)
    if fts_query is None:
        # Empty or all-punctuation query (e.g. "\") → no searchable terms; MATCH '' would
        # crash. Single guard shared with documents/patterns/codegraph (techne#143/#168).
        return []
    own_conn = conn is None
    if own_conn:
        conn = connect(db_path)
    candidate_limit = limit * expansion_ratio

    try:
        # #70: bare space-separated terms are FTS5 implicit-AND, so a multi-word
        # or natural-language query (recall topics especially) returns nothing
        # unless one row contains *every* term. When the AND pass falls short of
        # candidate_limit, top up with the OR-of-terms pass — see _and_or_pools
        # for why table="all" tops up PER SUB-TABLE, not on a pooled total.
        and_pool, or_pool = _and_or_pools(
            conn,
            query,
            table,
            candidate_limit,
            include_archived=include_archived,
            include_uncertain=include_uncertain,
            row_chars=row_chars,
        )

        # For multi-table ("all"), pre-sort each block by raw rank for a
        # deterministic input order; the reranker normalises BM25 PER TABLE
        # (techne#56) and then re-sorts by the combined recency×BM25 score.
        if table == "all":
            and_pool.sort(key=lambda x: x.get("rank", 0))
            or_pool.sort(key=lambda x: x.get("rank", 0))

        # AND ROWS AND OR ROWS ARE RERANKED IN SEPARATE BLOCKS, NEVER TOGETHER
        # (techne#1079 v2). #1081 put them in one pool and reranked once; on the
        # live store a short on-topic OR row's normalised BM25 + recency beat a
        # long exact-match AND row's, dropping identifier queries from rank 1 to
        # 2 or 3 and losing `reflect-drain` from the hybrid path entirely — that
        # PR was reverted (#1084) and this issue reopened over it. Each block is
        # reranked on its own (so an AND-only query is BYTE-IDENTICAL to the
        # pre-#1079 page), then concatenated AND-first: no OR row's score can
        # ever place it ahead of an AND row.
        and_ranked = _rerank(
            and_pool,
            limit,
            recency_weight,
            bm25_weight,
            mmr_lambda=mmr_lambda,
            confidence_decay_weight=confidence_decay_weight,
            half_life_days=half_life_days,
        )
        remaining = limit - len(and_ranked)
        or_ranked = (
            _rerank(
                or_pool,
                remaining,
                recency_weight,
                bm25_weight,
                mmr_lambda=mmr_lambda,
                confidence_decay_weight=confidence_decay_weight,
                half_life_days=half_life_days,
            )
            if remaining > 0 and or_pool
            else []
        )
        results = and_ranked + or_ranked

    finally:
        if own_conn:
            conn.close()

    return results


def search_knowledge_conn(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    half_life_days: float = 30.0,
    recency_weight: float = 0.3,
    bm25_weight: float = 0.7,
    include_event_at: bool = True,
) -> list[dict]:
    """Public read-only knowledge-layer search over a caller-supplied connection.

    Stable public entrypoint for the knowledge FTS + rerank path (techne#253).
    Encapsulates the AND/OR-fallback search, optional ``event_at`` enrichment, and
    ``_rerank`` so that callers (esp. federation's remote path) never reach into
    search.py private helpers.  Returning rows with ``event_at`` populated means
    the federation path no longer hand-backfills it — the temporal tiebreak for
    cross-store contradiction verdicts is now owned here.

    The caller owns the connection lifecycle (open/close).  This function never
    calls ``database.connect()`` so it is safe to use with read-only remote
    connections (?mode=ro) — calling ``connect()`` would write WAL + schema into
    the foreign directory.

    ``include_event_at`` (default True): when True, fetches ``event_at`` from the
    ``knowledge`` table for every result row.  Set to False only in unit tests that
    construct mock rows without a real SQLite connection.

    table='knowledge' only — this function is intentionally knowledge-scoped.
    For multi-table hybrid search use ``search()`` or ``search_fts()``.

    Every row is tagged ``_match_pass: "and" | "or"`` (techne#1079 review
    Important 1) — this is the remote side of federation's fix: `merge_and_rank`
    merges pages from several stores (techne#1089), and its tier rule needs to
    tell each store's AND rows from its OR rows to avoid recreating the #1081
    bug one hop further out.
    """
    candidate_limit = limit * 2  # expansion_ratio=2 (matches search_fts default)
    # #70: OR tops up when AND falls short of candidate_limit (same helper as
    # search_fts). AND and OR are reranked as SEPARATE blocks below and
    # concatenated AND-first — see search_fts for why (techne#1079 v2 /
    # #1081/#1084: reranking them together let a short OR row outrank a long
    # exact AND row).
    and_rows, or_rows = fts_and_or_split(
        query,
        lambda match: _fts_match_rows(
            conn, match, "knowledge", candidate_limit, include_archived=False
        ),
        want=candidate_limit,
        key=lambda r: (r["source_table"], r["id"]),
        # techne#1093: the OR pass drops terms bm25 clamps to ~zero weight.
        run_or=lambda match: _fts_match_rows(
            conn,
            match,
            "knowledge",
            candidate_limit,
            include_archived=False,
            drop_clamped_or_terms=True,
        ),
    )
    for r in and_rows:
        r["_match_pass"] = "and"
    for r in or_rows:
        r["_match_pass"] = "or"

    # Enrich with event_at from the base knowledge table, ONE query covering
    # both blocks. _fts_match_rows omits event_at from its knowledge SELECT
    # (adding it there would change recency ranking on the local path too —
    # out of scope). Federation needs event_at for the cross-store "later
    # event_at wins" tiebreak, so we fetch it here, scoped to the connection
    # (never touching the local db). IDs come from our own FTS result, never
    # user input — the interpolated IN-list is safe.
    all_rows = and_rows + or_rows
    if include_event_at and all_rows:
        ids = [r["id"] for r in all_rows]
        placeholders = ",".join("?" for _ in ids)
        event_by_id = {
            row["id"]: row["event_at"]
            for row in conn.execute(
                f"SELECT id, event_at FROM knowledge WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        for r in all_rows:
            r["event_at"] = event_by_id.get(r["id"])

    and_ranked = _rerank(
        and_rows, limit, recency_weight, bm25_weight, half_life_days=half_life_days
    )
    remaining = limit - len(and_ranked)
    or_ranked = (
        _rerank(or_rows, remaining, recency_weight, bm25_weight, half_life_days=half_life_days)
        if remaining > 0 and or_rows
        else []
    )
    return and_ranked + or_ranked


# Knowledge is the only vec table whose rows can be superseded after embedding,
# and search_vec drops superseded rows AFTER the ANN k-nearest cut — so a stale
# embedding eats a top-k slot and starves a live result. backfill_vectors purges
# stale embeddings, but nothing in this repo auto-runs it and a deployment that
# does (see the note in backfill_vectors, techne#704) still only purges at
# backfill time — so this query-time over-fetch is the guarantee that actually
# fires on every search: pull k×factor neighbours, then keep the closest `limit`
# LIVE ones. (techne#168)
_VEC_SUPERSEDE_OVERFETCH = 3


# The broad `except` below is correct — a hook must not raise — but before techne#708
# those exceptions PROPAGATED to user_prompt_submit's handler, which printed to stderr
# and made `_record` write outcome="failed". Returning [] silently would delete that
# signal and make a rail whose every search fails report LIVE, which is the exact shape
# _record's docstring exists to prevent. So keep the broad catch and narrow the SILENCE.
#
# TWO SIGNALS, BECAUSE ON THE PATH THAT MATTERS THE STDERR ONE REACHES NOBODY.
# `user_prompt_submit` is a `UserPromptSubmit` hook and it exits 0. Claude Code's hook
# documentation is explicit: "Stderr from a hook that exits 0 goes to the debug log
# only, never the transcript, and Claude never sees it." Only exit 2 surfaces stderr,
# and this hook must never exit 2 — that BLOCKS the prompt and erases it. So on the
# hook path the warning below is invisible to the operator AND to Claude, and the
# marker is the only signal that can exist. It is not the nice-to-have half.
# (Prudence, #728 r3 nit 5 asked whether that stderr surfaces at all; it does not.)
#
# ONCE PER PROCESS, WHICH IS NOT ONCE PER TURN ON THE HOOK PATH EITHER. The hook runs
# as a FRESH SUBPROCESS per prompt, so this flag starts False every turn and the line
# would print every turn if anyone could see it. The dedupe earns its keep only in a
# long-lived process — a CLI backfill loop, the MCP server.
_EMBED_UNAVAILABLE_WARNED = False

#: The last vector-channel failure, for the liveness marker. CONSUME-ONCE via
#: `take_vec_channel_error()` (techne#728 r3 Important 1).
#:
#: WHY A MODULE GLOBAL RATHER THAN A RETURN VALUE. Threading a per-channel status out
#: of `search()` is the right long-term shape and it is a signature change across every
#: caller — that is techne#739 and it stays there. This is the same mechanism sitting
#: four lines above, used for the same reason, and it closes the marker half here
#: rather than one release later.
#:
#: CONSUME-ONCE IS LOAD-BEARING, not tidiness. In a long-lived process a failure left
#: standing would mark a LATER, healthy turn as degraded — a stale-derived-fact defect,
#: which is precisely the class this whole change is about. Reading it clears it.
_VEC_CHANNEL_ERROR: str | None = None


def take_vec_channel_error() -> str | None:
    """Return the last vector-channel failure and CLEAR it. None when there was none.

    The vector channel degrades to FTS rather than raising (techne#708), so a caller
    that wants to record the degradation has nothing to catch. This is how it finds
    out. See `_VEC_CHANNEL_ERROR` for why it clears.
    """
    global _VEC_CHANNEL_ERROR
    err, _VEC_CHANNEL_ERROR = _VEC_CHANNEL_ERROR, None
    return err


def _query_embedding(query: str):
    """Embed ``query`` for the vector channel, or None when the channel is off.

    None covers every degrade exit that comes before the store is touched: no
    sqlite-vec, no embedding backend, or a model that cannot be built without a
    download. ``search()`` calls this ONCE per call and hands the vector to every
    ``search_vec`` it makes (techne#1094); ``search_vec`` calls it itself when no
    vector is passed. An exception from ``model.embed`` propagates, as it always has.
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return None
    if not embeddings_mod.backend_available():
        # Neither model2vec nor fastembed importable — degrade to FTS-only,
        # same as the pre-#601 fastembed-only probe (techne#601/#602).
        return None

    import numpy as np

    # allow_download=False IS LOAD-BEARING, not defensive (techne#708). This
    # function is reachable from hooks/user_prompt_submit.py -> search()
    # (use_vectors=True by default) -> here, i.e. from EVERY prompt submission.
    # On a cold HF cache the default would block on a Hub fetch, and a blocking
    # HTTP download is signal-deaf: bounded_hook's SIGALRM cannot preempt it, so
    # the turn hangs rather than degrading. backend_available() above does NOT
    # cover this — it is an IMPORT probe, so it passes with the package present
    # and the model absent, which is exactly the fresh-install state.
    try:
        model = _get_embedding_model(allow_download=False)
    except Exception as exc:
        # Degrade to FTS-only, the same contract as every other exit here. The
        # singleton is deliberately left unset on a failed allow_download=False
        # attempt, so `techne vectors backfill` (which may download) can still
        # warm the cache in the same process.
        #
        # LESS SILENT — AND ONLY HALF THE SIGNAL, WHICH THE PREVIOUS WORDING GOT
        # WRONG (Prudence, #728 r2). This catch is broad on purpose (a hook must not
        # raise) and therefore also swallows genuine construction failures: a typo'd
        # `search.embedding_model` reaches _build_backend unvalidated, as do a corrupt
        # cache and an incompatible weights file. Pre-#708 those propagated and
        # `_record` wrote outcome="failed".
        #
        # BOTH HALVES, NOW. An earlier round of this PR restored only the stderr half
        # and the rail still recorded outcome="ok" over a dead backend — MEASURED
        # end-to-end through user_prompt_submit.main():
        #     {"last_run": {"outcome": "ok", "detail": "1 rows"}, "last_error": null}
        # `_record`'s docstring, eight lines above where that happens, names exactly
        # that shape as the bug the module exists to surface. The guard was intact; the
        # failure walked around it, because post-#708 `search_vec` no longer raises.
        # `_VEC_CHANNEL_ERROR` is what user_prompt_submit reads to fill `degraded`.
        # (Prudence, #728 r3 Important 1.)
        global _EMBED_UNAVAILABLE_WARNED, _VEC_CHANNEL_ERROR

        _VEC_CHANNEL_ERROR = f"vector recall unavailable: {exc}"
        if not _EMBED_UNAVAILABLE_WARNED:
            _EMBED_UNAVAILABLE_WARNED = True
            print(
                "techne: vector recall unavailable — embedding model not cached "
                f"or not buildable ({exc}); run `techne vectors backfill`",
                file=sys.stderr,
            )
        return None
    return np.stack(list(model.embed([query])))[0]


def _ensure_sqlite_vec(conn: sqlite3.Connection, sqlite_vec) -> bool:
    """Load sqlite-vec into ``conn`` unless it is already there. False when it cannot load.

    The probe is what makes the load happen once per connection (techne#1094):
    ``search()`` shares one connection between its main vector query and the #946
    curated pool, and each goes through ``search_vec``.
    """
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.OperationalError:
        pass  # not loaded on this connection yet
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        # Interpreter built without loadable-extension support — degrade to FTS.
        return False
    return True


def search_vec(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 10,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    query_vec=None,
) -> list[dict]:
    """Semantic vector search. Requires sqlite-vec and an embedding backend
    (model2vec or fastembed — techne#601/#602; see core/embeddings.py).

    ``row_chars`` caps each KNOWLEDGE row's content, defaulting to
    ``_ROW_CHAR_CAP`` when None (techne#842). It does NOT move observation or
    thread rows: those are the tool ledger, and a caller raising its recall
    budget wants more of the curated row, not more transcript. A caller that
    renders a truncation marker should ask for one character MORE than it will
    show — at exactly the cap a severed row is indistinguishable from a whole
    one.

    Falls back gracefully if vector dependencies aren't installed.

    ``include_archived`` (techne#216): when False, soft-archived knowledge rows
    are excluded from the vector channel too, so search()'s DEFAULT-recall
    exclusion holds regardless of which channel surfaced the row.

    ``conn`` and ``query_vec`` (techne#1094) let ``search()`` reuse one
    connection and one query embedding across its vector calls. With ``conn``,
    sqlite-vec is loaded into it if it is not already, and the connection is left
    open. ``query_vec`` is the query's embedding (``_query_embedding``); without
    it the query is embedded here.
    """
    try:
        import sqlite_vec
    except ImportError:
        return []
    if query_vec is None:
        query_vec = _query_embedding(query)
        if query_vec is None:
            return []

    import numpy as np

    q_bytes = np.asarray(query_vec, dtype="float32").tobytes()

    own_conn = conn is None
    if own_conn:
        conn = connect(db_path)
    try:
        if not _ensure_sqlite_vec(conn, sqlite_vec):
            return []
        return _vec_channel_rows(
            conn,
            q_bytes,
            table,
            limit,
            include_archived=include_archived,
            include_uncertain=include_uncertain,
            row_chars=row_chars,
        )
    finally:
        if own_conn:
            conn.close()


def _vec_channel_rows(
    conn: sqlite3.Connection,
    q_bytes: bytes,
    table: SearchTable,
    limit: int,
    *,
    include_archived: bool,
    include_uncertain: bool,
    row_chars: int | None,
) -> list[dict]:
    """``search_vec``'s ANN queries on a connection with sqlite-vec loaded."""
    # The caller's cap wins for KNOWLEDGE rows; the ledger tables keep
    # `_ROW_CHAR_CAP` regardless (techne#842).
    _cap = row_chars if row_chars is not None else _ROW_CHAR_CAP
    results = []
    try:
        if table in ("threads", "all"):
            _exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='threads_vec'"
            ).fetchone()
            if _exists:
                rows = conn.execute(
                    """SELECT v.rowid, v.distance
                       FROM threads_vec v
                       WHERE v.embedding MATCH ? AND k = ?""",
                    (q_bytes, limit),
                ).fetchall()
                for row in rows:
                    thread = conn.execute(
                        "SELECT id, label, summary, status FROM threads WHERE rowid = ?",
                        (row[0],),
                    ).fetchone()
                    if thread:
                        results.append(
                            {
                                "source_table": "thread",
                                "id": thread[0],
                                "label": thread[1],
                                "summary": thread[2][:_ROW_CHAR_CAP],
                                "status": thread[3],
                                "distance": row[1],
                            }
                        )

        if table in ("observations", "all"):
            _exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='observations_vec'"
            ).fetchone()
            if _exists:
                rows = conn.execute(
                    """SELECT v.rowid, v.distance
                       FROM observations_vec v
                       WHERE v.embedding MATCH ? AND k = ?""",
                    (q_bytes, limit),
                ).fetchall()
                for row in rows:
                    obs = conn.execute(
                        "SELECT id, content, tool_name, session_id, timestamp"
                        " FROM observations WHERE id = ?",
                        (row[0],),
                    ).fetchone()
                    if obs:
                        results.append(
                            {
                                "source_table": "observation",
                                "id": obs[0],
                                "content": obs[1][:_ROW_CHAR_CAP],
                                "tool_name": obs[2],
                                "session_id": obs[3],
                                "timestamp": obs[4],
                                "distance": row[1],
                            }
                        )

        if table in ("knowledge", "all"):
            _exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
            ).fetchone()
            if _exists:
                # Over-fetch (k×factor) then keep the closest `limit` LIVE rows, so
                # superseded embeddings still in the index can't starve live results
                # (techne#168 — see _VEC_SUPERSEDE_OVERFETCH).
                rows = conn.execute(
                    """SELECT v.rowid, v.distance
                       FROM knowledge_vec v
                       WHERE v.embedding MATCH ? AND k = ?""",
                    (q_bytes, limit * _VEC_SUPERSEDE_OVERFETCH),
                ).fetchall()
                # techne#216: DEFAULT recall drops soft-archived rows on the vec
                # channel too. Fixed literal clause (boolean-selected), never user input.
                archived_clause = "" if include_archived else " AND status != 'archived'"
                # techne#501 — same trust-state filter on the vector channel. A
                # per-channel divergence here would mean an uncertain row is
                # reachable by vector and not by FTS, which is worse than either.
                uncertain_clause = "" if include_uncertain else " AND status != 'uncertain'"
                live = 0
                for row in rows:
                    k = conn.execute(
                        f"""SELECT id, type, content, tags, confidence, status
                           FROM knowledge WHERE id = ? AND superseded_by IS NULL{archived_clause}{uncertain_clause}""",
                        (row[0],),
                    ).fetchone()
                    if k:
                        results.append(
                            {
                                "source_table": "knowledge",
                                "id": k[0],
                                "knowledge_type": k[1],
                                # KNOWLEDGE HONOURS THE CALLER'S CAP
                                # (techne#842).
                                "content": k[2][:_cap],
                                # Pre-truncation length; see the FTS site.
                                "content_chars_total": len(k[2]),
                                "tags": k[3],
                                "confidence": k[4],
                                # techne#501: the FTS row has always carried `status`;
                                # this one did not. An explicit search that TAGS an
                                # uncertain row could otherwise return a vec-only hit
                                # with nothing to tag it by — the same per-channel
                                # divergence the filter above exists to prevent.
                                "status": k[5],
                                "distance": row[1],
                            }
                        )
                        live += 1
                        if live >= limit:
                            break

        # documents live with the Librarian store now (techne#99) — and there is NO
        # document vector search: librarian.db has one virtual table, `documents_fts`.
        # This comment previously said 'documents vector search lives with the
        # Librarian store', which asserted a `documents_vec` that has never existed.

        results.sort(key=lambda x: x.get("distance", 999))
        return results[:limit]

    except sqlite3.OperationalError:
        # A stale vec0 cache embedded by a *different* backend raises here on the
        # `embedding MATCH ?` — most importantly "Dimension mismatch: expected N
        # ... received M" when a DB embedded at 384-d (fastembed/MiniLM) is queried
        # after a py3.14 upgrade flips the active backend to 256-d potion. The
        # tables are a derived cache and the drop+re-embed migration
        # (_ensure_vec_backend_identity, run from backfill_vectors) is what
        # actually reconciles them; until an operator re-embeds we must NEVER crash
        # the top-level recall path — degrade to FTS-only, matching this module's
        # existing extension-load fallback in `_ensure_sqlite_vec`.
        return []


RRF_K = 60  # Cormack et al. 2009 — smoothing constant


def _rrf_fuse(
    channels: list[list[tuple]],
    k: int = RRF_K,
) -> list[tuple[tuple, float]]:
    """Reciprocal Rank Fusion across N ranked channels.

    score(d) = sum over channels of 1 / (k + rank_channel(d))

    Items appearing in multiple channels get the sum of their per-channel
    contributions, naturally boosting them above items appearing in only
    one channel. RRF is channel-agnostic — it does not require channel
    scores to be on the same scale, which is exactly why it works for
    BM25 (negative scores) fused with vector cosine distance (0..1).

    Args:
        channels: list of ``[(key, rank_position), ...]`` per channel.
            ``rank_position`` starts at 1 (best). Keys can be any hashable
            — typically (type, id) tuples so cross-table results don't
            collide.
        k: smoothing constant. 60 is the standard from Cormack et al.

    Returns:
        ``[(key, fused_score), ...]`` sorted by descending score.
    """
    scores: dict[object, float] = {}
    for channel in channels:
        for key, rank in channel:
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def _fts_floor(limit: int, n_fts_only: int) -> int:
    """How many TAIL slots of a page are reserved for lexical-only hits (#827).

    The vec-first union (#401) is right about ordering and was wrong about
    truncation: appending FTS-only hits after a full vector page and then
    slicing to ``limit`` means they are never returned. This reserves a slot
    so the lexical channel can reach the caller at all.

    THE SIZE IS ONE, AND THAT IS A MEASUREMENT, NOT A TASTE (Prudence, #829 r1).
    The first cut reserved ``limit // 3`` on the argument that a minority slice
    is cheap. It is not cheap. Running the eval leg the default suite deselects
    (``pytest -m eval_vec``, the gate at tests/eval/test_retrieval_gate_vec.py,
    which calls ``search(use_vectors=True, limit=10)`` directly), same machine,
    same pinned potion-base-8M, only this module swapped:

        floor at limit=10      paraphrase r@10    MRR
        0 (pre-#827)                 1.0          0.546
        1                            1.0          0.546
        2                            0.833        0.528
        3 (the first cut)            0.833        0.528

    ONLY THE limit=10 ARM CAN SHOW COST, AND THE OTHER TWO ARE CITED FOR WHAT
    THEY CANNOT MEASURE UNLESS THIS SAYS SO (Prudence, #829 r4 Important 1).
    The cost mechanism is `g385_dbpool` at vec-rank 9 being truncated off the
    head, so it can appear only where the head budget ``limit - floor`` drops
    below 9. Forced across the whole range on the pinned gold set:

        limit=5,  scored r@5   FLAT at every expressible floor (0.667 / 0.5) —
                               reserving 4 of 5 slots moves nothing, because the
                               gold is off the page at ANY floor. This arm
                               CANNOT show cost.
        limit=10, scored r@10  floors 0-1: 1.0 / 0.546; floors 2-4: 0.833 /
                               0.528; floors 5+: 0.667 / 0.5. THE ARM WITH TEETH.
        limit=15, scored r@10  FLAT through floor 6 (1.0 / 0.546); floor 7:
                               0.833 / 0.528 — head budget 8, below 9.

    So "identical at limit=5" and "identical at limit=15" are true and nearly
    uninformative: at 15, floors 3 and 6 are identical too. An earlier draft
    offered the 5-arm as evidence that "20% of a page is affordable" — the same
    PROPORTION that costs a gold at 10. That inference was unsound; the arm does
    not bite because it cannot, not because the proportion is cheap. (The label
    was Prudence's from #829 r2 and she corrected it herself; the unsound
    inference built on it was mine.)

    THE PER-TURN RAIL'S REAL PAGE IS 15, AND THE FLOOR USUALLY DOES NOT REACH
    THE SESSION THROUGH IT. `config.py` sets `per_turn_recall_limit = 5`, but
    `hooks/user_prompt_submit.py:640` passes `limit=k * _RESEND_OVERFETCH` with
    `_RESEND_OVERFETCH = 3`, so `search()` is called at 15 and the rail cuts to
    5 itself at line 656 after dedup and the relevance floor. Since this floor
    reserves the TAIL, the reserved row lands at index 14 of 15 and the rail's
    own `[:k]` drops it — see test_the_rail_drops_the_reserved_lexical_row in
    tests/test_user_prompt_submit_hook.py, which drives the hook rather than
    asserting a rail property from a seam that cannot observe one.
    It is "usually not delivered" rather than "never": delivery needs 10+ of the
    14 head rows dropped between the two cuts, and only TWO steps can drop any —
    `_drop_pull_only` and session dedup. `_apply_relevance_floor` is inert by
    default (both `per_turn_recall_floor_*` default to None), so it thins
    nothing. Session dedup measures 46.1% across deliveries against the ~71% a
    single turn would need, so "usually not" is if anything generous toward
    delivery (Prudence, #829 r4). Neither of us has measured the rate directly.
    THE COST CLAIM IS UNAFFECTED — measured at limit=15, floor 1:
    paraphrase r@10 1.0 / MRR 0.546, exact/filter/negative 1.0. What is wrong is
    the DELIVERY claim, not the price. Fixing it means placing the reserved row
    inside the first k, or having the rail stop discarding it; both are changes
    to a caller's contract and belong with techne#831, not here.

    At 2 and above the gold `g385_dbpool` — sitting at vec-rank 9 — is
    truncated off the head and replaced, on a ZERO-TOKEN-OVERLAP paraphrase
    query, by lexically-matched distractors. The gate's 0.8 floor still passes
    at 0.833, but retrieval_gold.yaml:97-99 reserves that margin for
    cross-machine BLAS drift; spending it deterministically leaves the next
    genuine wobble looking like flake. A floor of 1 delivers #827 — lexical-only
    hits reach the caller — at a measured cost of zero.

    SO THE FLOOR DOES NOT SCALE WITH ``limit``:

        limit=20 -> 1    limit=10 -> 1    limit=5 -> 1
        limit=3  -> 1    limit=2  -> 1    limit=1 -> 0

    A WIDER PAGE DOES AFFORD MORE, AND THAT IS A TRAP RATHER THAN A LICENCE.
    limit=15 tolerates a floor of 6; limit=10 does not tolerate 2. If you widen
    the floor and re-run "the ablation" at 15 — the page the rail actually uses,
    and therefore the one that looks production-relevant — you get a clean
    1.0 / 0.546 up to floor 6 and ship a regression that limit=10 would have
    caught. RE-RUN THE limit=10 ARM. It is the discriminating one, for the
    reason given above: it is where the head budget crosses the gold's rank.
    Record the numbers here when you do.

    THE OTHER HALF OF THE TRADE, so a future reader is not shown only the cost
    (Prudence, #829 r2). A wider floor DOES buy something measurable, and this
    is it: on the live store at floor 3, the queries "issue 814" and "ae219b5"
    reached knowledge #1810 — the entry recording that PR #815 merged as
    ae219b5 — and at floor 1 they do not; the single slot goes to a raw
    observation instead. So the choice here is not "1 is free and 3 is costly",
    it is "3 costs a gold on the paraphrase arm to buy a store-dependent
    lexical hit". The reason to refuse that trade is that the shortfall has a
    DIFFERENT cause and therefore a different fix. `_rerank` orders by
    `0.3 x recency + 0.7 x norm_bm25 + 0.1 x decay` — :279 PLUS :286 — and it is
    the third term, absent from the formula this docstring used to state, that
    decides the case. Each table is queried under its own `LIMIT
    candidate_limit` and `norm_bm25` is min-max normalised WITHIN each
    `source_table`, so on an identifier query the best observation and the best
    knowledge row commonly arrive tied at 1.0 on both of the first two terms;
    `_knowledge_decay_score` then hands the observation a flat 1.0 (it carries no
    `confidence_numeric`) against AT MOST 0.9 for a `high`-confidence knowledge
    row, and that margin is the whole story. THE TERM IS CONFIDENCE TIMES AGE,
    not confidence alone: `decay_score` is `confidence_numeric * 0.5 **
    (days_since_confirmed / half_life_days)` at a 30-day half-life, so a
    `high` row long unconfirmed decays toward 0 and the margin widens toward the
    full 0.1000 — a FRESH `medium` (0.0400) beats a 30-day-old `high` (0.0550),
    which is the point of the term. On THIS store the margin is 0.0100 and
    stable, because 2,823 of 2,914 live knowledge rows carry a NULL
    `last_confirmed_at` and take `decay_score`'s no-timestamp branch; see the
    schema note below. Measured on the live store over six identifier-shaped
    queries (Prudence, #934 r2): on four of six the BM25 and recency deltas are
    both exactly 0.0000. So which fts-only row wins rank 1 is
    decided by the reranker, not by how many slots it is handed. A wider floor
    buys more lottery tickets without changing the draw. That is techne#831, and
    it is where a fix for identifier recall belongs.

    At ``limit=1`` there is no room to reserve and the single slot goes to the
    vector channel, preserving #401's behaviour on a one-row page. At
    ``limit=2`` the reserved slot is exactly half the page — the floor is never
    MORE than half, which is a weaker claim than the one this docstring made
    before Prudence checked it against its own table.

    Returns 0 when there is nothing lexical-only to place, so the fused page
    is identical to the pre-#827 one whenever the two channels agree.

    ``n_fts_only`` IS A COUNT OF DISTINCT KEYS, AND AT THIS FLOOR THAT IS
    DORMANT SCAFFOLDING RATHER THAN A LIVE GUARD (Prudence, #829 r2 Important 1
    — do not read the green test as a witness). The tail loop dedups via
    `seen`, so an inflated count would reserve slots the tail cannot fill and
    return a SHORT page. But the guard above has already established
    ``n_fts_only > 0``, so ``min(n_fts_only, 1)`` is 1 for every positive count:
    duplicates take it from 1 to 4 and ``min(4, 1) == min(1, 1)``. The deduped
    and undeduped sizings are indistinguishable for EVERY possible input while
    the floor is 1, which is why mutating the caller back to ``len(fts_only)``
    leaves the whole test file green.

    AND IT IS NARROWER THAN "DORMANT" (Prudence, #829 r3 Important 2 — this
    clause has to be exactly right, because the paragraph invokes #565 to
    justify keeping the guard). NO PRODUCER REACHING THIS FUNCTION CAN EMIT A
    DUPLICATE KEY AT ALL, at any floor: ``fts_results`` comes only from
    ``search_fts``, which runs one query per table with each ``*_fts`` rowid
    joined 1:1 to its base table and each table's rows tagged with a distinct
    ``source_table``. Since techne#1079 the OR pass tops up a short AND pass
    (instead of only an empty one) and the two are reranked as SEPARATE
    blocks then concatenated AND-first (v2 — the joint-rerank version of this
    regressed identifier ranking, shipped as #1081, and was reverted as
    #1084), but ``fts_and_or_split`` dedups the OR block against the AND
    block by key BEFORE either is reranked, so no duplicate key reaches
    ``search_fts``'s output either way. Measured: 6 query shapes x 4 table
    scopes on a seeded store, including queries that fall through to the OR
    pass — 0 duplicates in all 24. So this is unreachable from today's
    producer, not merely inert at today's floor. It is kept to keep
    ``search()`` correct if a producer ever CAN duplicate — a second
    knowledge query, or a dedup key that collides across tables — which costs
    one set comprehension. (Under monkeypatch it does behave as described:
    sizing reverted with the floor at 3 fails the dupe test with an 8-row
    page. That is a property of the test harness, not of the system.)

    For the same reason ``min(n_fts_only, ...)`` below is forward-looking
    scaffolding, not an active constraint: at this floor it reduces to
    ``return 1``.
    """
    if limit <= 1 or n_fts_only <= 0:
        return 0
    return min(n_fts_only, 1)


#: Tables whose rows are the distilled answer rather than the raw trace of one.
#: `_curated_first` promotes these ahead of the rest of the lexical tail.
_CURATED_TABLES = frozenset({"knowledge"})


def _curated_first(fts_only: list[dict]) -> list[dict]:
    """Order the lexical tail so a curated row wins the reserved slot (#831).

    #827 made the lexical channel reachable; it did not make it USEFUL, because
    the curated row loses the slot on the CONFIDENCE-DECAY TIEBREAK. An earlier
    draft of this docstring blamed one shared BM25 pool and BM25's preference for
    the shorter document; Prudence measured both dead (#934 r1). There is no
    shared pool — `_fts_match_rows` queries each table separately under its own
    `LIMIT candidate_limit` — and `_rerank` min-max normalises `rank` within each
    `source_table`, so each table's best row reaches `_combined` at
    `_norm_bm25 = 1.0` and the shorter-document advantage is cancelled before the
    tables compete. The tiebreak then decides it: `_knowledge_decay_score`
    returns a flat 1.0 for a row carrying no `confidence_numeric`, so an
    observation collects the whole `confidence_decay_weight` and a
    `medium`-confidence knowledge row collects 0.6 of it — 1.1000 against 1.0600
    at equal recency, FRESHLY CONFIRMED. `medium` is only the default, and a
    `high`/0.9 row such as #1810 collects 0.9 of the weight, narrowing the margin
    to 0.0100 — but only while the row is fresh: the term is
    `confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)`, so age
    widens the margin back toward 0.1000 and a stale `high` sits WIDER than a
    fresh `medium`. On this store the 0.0100 holds rather than drifting, because
    2,823 of 2,914 live knowledge rows have a NULL `last_confirmed_at` and never
    reach the decay branch at all. The ~16:1 corpus ratio (measured 2026-09-15 on the live
    store: 46,489 observations, 2,904 live knowledge, 2,458 threads) acts through
    order statistics on top of that, not through a shared pool. So with
    `_fts_floor` pinned at 1, the single reserved slot lands on a command echo
    and the curated entry behind it never reaches the caller. Measured on six
    identifier-shaped queries through `search(table="all", limit=10)` the day
    this shipped: a curated row held the reserved slot in 1 of 6 before and 6 of
    6 after.

    THAT IS ONE MEASUREMENT AND AN EARLIER DRAFT CITED A DIFFERENT ONE HERE
    (Temperance, pre-pass on 6769d73). The draft said "rank 1 was a non-knowledge
    row in 4 of 6", which came from a separate probe — `search_fts` directly at
    limit=5, over a query set differing in one entry — and disagreed with the
    1/6 figure in the CHANGELOG, the arch card, the PR body and the regression
    test. Two probes of the same defect are fine; quoting one number in the
    docstring and another everywhere else is not. The `search()` probe is the
    one cited throughout, because it is the before/after that motivates the fix.

    WHY REORDER AND NOT FILTER. Dropping non-curated rows would also take them
    out of the SLACK the caller gets when the vector channel is thin — the
    `head + tail` composition deliberately lets the tail spend unused head
    budget. Reordering changes only who wins the SCARCE reserved slots and
    leaves the abundant case exactly as it was.

    WHY NOT WIDEN THE FLOOR INSTEAD. That trade was measured and rejected in
    #829: a floor of 2+ costs the #399/#401 paraphrase arm a gold (recall@10
    1.0 -> 0.833). This buys lexical precision without spending a head slot,
    which is why it can be done at all.

    The partition is STABLE on both sides, so BM25 order is preserved within
    curated rows and within the rest; this reorders the tail and never reranks.

    PARTITIONED WITHIN EACH `_match_pass` BLOCK, NOT ACROSS THEM (techne#1079
    review Nit 3). A plain curated/rest partition over the whole list would
    let a curated OR row (partial match) jump ahead of a non-curated AND row
    (all-terms match) — the same AND-precedence problem Important 1 fixed for
    `merge_and_rank`, showing up here as a smaller, tail-only case. `fts_only`
    is `search_fts`'s own output, already AND-block-then-OR-block and every
    row tagged, so splitting on `_match_pass` first and partitioning each
    half is enough; there is no untagged case to decide here the way
    `merge_and_rank` has one.
    """
    if not any(r.get("source_table") in _CURATED_TABLES for r in fts_only):
        return fts_only

    def _curated_partition(rows: list[dict]) -> list[dict]:
        curated = [r for r in rows if r.get("source_table") in _CURATED_TABLES]
        rest = [r for r in rows if r.get("source_table") not in _CURATED_TABLES]
        return curated + rest

    and_block = [r for r in fts_only if r.get("_match_pass") == "and"]
    or_block = [r for r in fts_only if r.get("_match_pass") != "and"]
    return _curated_partition(and_block) + _curated_partition(or_block)


#: Head slots a `table="all"` page reserves for curated rows arriving on the VECTOR
#: channel (techne#946). Small by default, and the smallness is the whole position —
#: see `_curated_head_reservation` for why no number here is derivable from theory.
CURATED_HEAD_SLOTS_DEFAULT = 3


def _curated_slots(slots: int | None) -> int:
    """Resolve the caller's `curated_head_slots` (None = the module default).

    This module takes tuning as ARGUMENTS and never reads `Config` — see its import
    list, which is deliberately free of `techne.config`. The config field
    (`SearchConfig.curated_head_slots`) is plumbed by the callers that already hold a
    Config, so a store can tune the reservation without search.py growing a dependency
    that would make it unimportable from the migration path.
    """
    return CURATED_HEAD_SLOTS_DEFAULT if slots is None else slots


def _curated_head_reservation(head_budget: int, slots: int) -> int:
    """How many HEAD slots are held for curated rows the vector channel ranked low.

    THE PROBLEM THIS SOLVES IS NOT THE ONE #827/#831 SOLVE (techne#946). Those two
    reserve a slot in the lexical TAIL, so they can only rescue a curated row the FTS
    channel found. A curated row that the VECTOR channel found but ranked below the
    head budget is invisible to both: `fts_only` excludes every vec key by
    construction, so `_curated_first` never sees it. Measured by an external evaluator
    on a 35k-observation store with 25 curated rows: three gold queries ranked their
    curated answer at vec-rank 1, 2 and 7 within `table="knowledge"` and lost it
    entirely at `table="all"`, because 35k observations supplied closer distances.

    WHY A RESERVATION AND NOT A SCORE PRIOR. Both are shipped patterns, and the
    architect store holds the argument between them. `Confidence-banded rerank`
    multiplies a provenance weight into the retrieval score; `Relevance and trust as
    two fields, never fused into one number` (ard-spec) answers that a fused number is
    undecomposable — you cannot afterwards tell whether a row placed low because it
    matched poorly or because its source is trusted less, so you can neither debug a
    ranking complaint nor move one policy without moving the other. A reserved slot is
    the decomposable form: a row is here because it filled slot N, and the page says
    so in `reserved_slot`. It is also the mechanism this file already uses at
    `_fts_floor`, so the page has one shape rather than two.

    THE SIZE IS MEASURED, NOT ARGUED — AND NOT BY US. A web survey found no published
    number for a minority collection's quota: Google Cloud Search ships a crowding cap
    of 3 per source with no tuning study attached, and the SIGIR '26 soft-quota work
    exists because STATIC quotas are the known failure mode, its contribution being the
    per-query calibration we have no demand signal for. The default therefore comes
    from the only real-corpus sweep of this defect that exists — the external
    evaluator's, on 35k observations with a fully embedded curated layer, their gold
    set, k=10, 14 scored queries:

        table="all" (status quo)        11/14   MRR 0.587
        table="knowledge" only          12/14   MRR 0.638
        knowledge-first, k=10 total     12/14   MRR 0.638
        knowledge-first, k=20 total     12/14   MRR 0.638
        reserved 2 of 10                11/14   MRR 0.631
        reserved 3 of 10                12/14   MRR 0.649   <- the knee
        reserved 5 of 10                12/14   MRR 0.645
        rank fusion (RRF k=60)          11/14   MRR 0.657
        reserved 3 THEN fused           12/14   MRR 0.649   (identical to 3 alone)

    Two is no better than the status quo on recall; five is no better than three and
    slightly worse on MRR. THAT SWEEP IS THEIRS, ON THEIR CORPUS, and is cited because
    it is the only one of its shape. Our own gated eval cannot reproduce it: its tasks
    query `table="knowledge"`, where this reservation is a no-op by construction.

    FUSION IS NOT THE ANSWER HERE, which the same sweep settles. RRF took the best MRR
    (0.657) on the worst recall tier (11/14) — it reorders what the page already holds
    and cannot surface what `all` never retrieved. Stacking it on top of the
    reservation measured IDENTICAL to the reservation alone, because fusion's MRR
    advantage WAS promoting curated rows, which the reservation does by construction.

    NEVER MORE THAN THE HEAD MINUS ONE. At `head_budget` 1 this returns 0: a
    single-slot head belongs to the vector channel, matching `_fts_floor`'s refusal to
    reserve at ``limit <= 1``. Without that clamp a one-row page could be handed to a
    curated row the vector channel ranked last, which is a different product, not a
    prior.

    Returns 0 for a non-positive ``slots``, which is how an operator turns the
    reservation off entirely.

    THE PROPORTIONAL CAP IS WHAT KEEPS A NARROW PAGE SANE. The measured knee is 3 of a
    10-row page, about a third; a flat 3 would be three quarters of the 4-slot head at
    ``limit=5``, a size nobody has measured and a different product. `_fts_floor`'s own
    docstring warns that a wider page affording more is a trap rather than a licence,
    and the mirror of that trap is a narrow page being handed the same absolute count.
    So the reservation is the smallest of three: what the caller asked for, one less
    than the head, and a third of the head. At ``head_budget`` 9 — the ``limit=10``
    page with the floor at 1 — that is exactly the measured 3.
    """
    if slots <= 0 or head_budget <= 1:
        return 0
    return min(slots, head_budget - 1, max(1, head_budget // 3))


#: Head slots held for rows matching a RARE identifier-shaped query token
#: (techne#1078). Measured on the 2026-09-28 snapshot (56,856 observations): the
#: worst case, `emit_max_chars 100000 config.yaml techne`, has two exact rows, and
#: 1 slot seats only one of them; 2 seats both. 3 moved no marker row against 2 on
#: the nine issue queries, so it would spend a vec slot for nothing measured.
IDENTIFIER_HEAD_SLOTS_DEFAULT = 2

#: A token is rare when fewer rows than this FRACTION of the searched scope match it
#: (techne#1078). Relative, not absolute, because the per-turn hook searches
#: `table="knowledge"` (3,426 rows) and an absolute 100 there made `config.yaml`
#: (70 knowledge rows) rare. At 0.2% the 2026-09-28 snapshot gives 127 for
#: `table="all"` (63,155 rows) and 7 for `knowledge`. Measured df at `all`:
#: must-trigger b2dcdd2 18, commentblockgate 40, reflect-drain 49,
#: check_staged_symlinks.sh 65, emit_max_chars 71, ae219b5 77; must-not
#: potion-base-8M 153, pre-commit 184, config.yaml 757, techne.db 2939.
IDENTIFIER_RARE_FRACTION_DEFAULT = 0.002

#: The threshold never drops below this, so a small store (the eval corpus, a fresh
#: install) still treats a token in a handful of rows as rare.
IDENTIFIER_MIN_DF = 5

#: An all-letter token this long is treated as a squashed compound identifier
#: (`commentblockgate` for `hooks.commentBlockGate`), since a query typed in lower
#: case carries no other shape signal. Rarity still has to hold, so a long English
#: word common in the store (`troubleshooting`) does not trigger.
_IDENTIFIER_LONG_TOKEN = 15

#: Surrounding punctuation stripped before the shape test, so `emit_max_chars:` or
#: `(b2dcdd2)` reads as the identifier. Internal punctuation is the signal and stays.
_IDENTIFIER_STRIP = "\"'`,;:()[]{}<>!?"


def _is_identifier_shaped(term: str) -> bool:
    """Does one query term look like an identifier rather than an English word?

    Shapes (techne#1078): an underscore, letters mixed with digits (which covers
    hex shas such as `b2dcdd2`), a leading `--flag`, camelCase, `.`/`-`/`/` joining
    two word characters (`techne.db`, `reflect-drain`), or an all-letter token of
    `_IDENTIFIER_LONG_TOKEN`+ characters. A bare run of digits does not count
    (`100000`, an issue number). Decimals, dates and versions DO, through the joiner
    rule (`0.5`, `2026-09-28`, `2.1.283`), as do ordinals (`3rd`).

    THIS ACCEPTS ORDINARY ENGLISH TOO, and nothing here pretends otherwise
    (Prudence, #1082): `well-known`, `and/or`, `e.g.`, `iPhone`, `straightforward`
    all pass. Shape cannot tell `well-known` from `reflect-drain`. Rarity is the
    gate, with a stricter bar for word-like terms (`IDENTIFIER_WORDLIKE_DIVISOR`).
    """
    t = term.strip(_IDENTIFIER_STRIP).rstrip(".")
    if len(t) < 3 or not any(c.isalnum() for c in t):
        return False
    if t.startswith("--") and len(t) > 2 and t[2].isalnum():
        return True
    has_alpha = any(c.isalpha() for c in t)
    has_digit = any(c.isdigit() for c in t)
    if "_" in t.strip("_") and has_alpha:
        return True
    if has_alpha and has_digit:
        return True
    for i in range(1, len(t) - 1):
        if t[i] in ".-/" and t[i - 1].isalnum() and t[i + 1].isalnum():
            return True
    if any(a.islower() and b.isupper() for a, b in zip(t, t[1:])):
        return True
    return t.isalpha() and len(t) >= _IDENTIFIER_LONG_TOKEN


#: Word-like identifier terms (no digit, no underscore: `reflect-drain`, `well-known`,
#: `commentblockgate`, `straightforward`) must clear a bar this many times stricter
#: than the scope threshold (techne#1082 review). Shape cannot tell `reflect-drain`
#: from `well-known`, so rarity has to do more of the work for them. Measured on
#: the 2026-09-28 snapshot at `table="all"` (bar 63): reflect-drain 49 and
#: commentblockgate 40 still trigger; well-known 96, self-contained 94, and/or 115,
#: troubleshooting 77 no longer do; straightforward 53 still does.
IDENTIFIER_WORDLIKE_DIVISOR = 2


def _has_code_mark(term: str) -> bool:
    """A digit or an underscore: marks that plain English rarely carries."""
    return any(c.isdigit() or c == "_" for c in term)


def _identifier_terms(query: str) -> list[str]:
    """The identifier-shaped terms of ``query``, as FTS phrase-literal bodies.

    Built on `_fts_terms`, the same split `search_fts` quotes into its MATCH, so a
    term counted here is exactly a phrase FTS matched on.
    """
    return [t for t in _fts_terms(query) if _is_identifier_shaped(t.replace('""', '"'))]


#: Per scope: (base table, the `source_table` tag `_fts_match_rows` gives its rows,
#: SQL returning a phrase MATCH's result-row ids). Allow-listed names only.
_IDENTIFIER_SCOPES = {
    "observations": (
        "observations",
        "observation",
        "SELECT rowid FROM observations_fts WHERE observations_fts MATCH ? LIMIT ?",
    ),
    "knowledge": (
        "knowledge",
        "knowledge",
        "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ? LIMIT ?",
    ),
    "threads": (
        "threads",
        "thread",
        "SELECT t.id FROM threads_fts f JOIN threads t ON t.rowid = f.rowid "
        "WHERE threads_fts MATCH ? LIMIT ?",
    ),
}


def _rare_identifier_terms(
    conn: sqlite3.Connection, query: str, table: SearchTable, fraction: float
) -> tuple[list[str], set[tuple[str, object]]]:
    """The identifier-shaped terms of ``query`` RARE in the searched scope, and the
    keys of every row matching one.

    Rare means at least one and fewer than ``bar`` rows match the term as an FTS
    phrase. ``max_df = max(IDENTIFIER_MIN_DF, ceil(rows * fraction))``, where
    ``rows`` counts the tables ``table`` searches. A term with a digit or an
    underscore uses ``bar = max_df``; a word-like term (neither) uses the stricter
    ``bar = max(2, max_df // IDENTIFIER_WORDLIKE_DIVISOR)``. Each lookup is bounded
    by ``bar``, so a common term like `techne.db` costs at most that many rowids per
    table, and the key set is at most bar-sized per rare term.
    """
    terms = _identifier_terms(query)
    if not terms or fraction <= 0:
        return [], set()
    names = _IDENTIFIER_SCOPES if table == "all" else (table,)
    scopes = [_IDENTIFIER_SCOPES[n] for n in names]
    n_rows = sum(
        conn.execute(f"SELECT count(*) FROM {base}").fetchone()[0] for base, _, _ in scopes
    )
    max_df = max(IDENTIFIER_MIN_DF, math.ceil(n_rows * fraction))
    rare: list[str] = []
    keys: set[tuple[str, object]] = set()
    for term in terms:
        bar = max_df if _has_code_mark(term) else max(2, max_df // IDENTIFIER_WORDLIKE_DIVISOR)
        found: set[tuple[str, object]] = set()
        for _, tag, sql in scopes:
            rows = conn.execute(sql, (f'"{term}"', bar - len(found))).fetchall()
            found.update((tag, r[0]) for r in rows)
            if len(found) >= bar:
                break
        if 0 < len(found) < bar:
            rare.append(term)
            keys |= found
    return rare, keys


def _identifier_pool(
    db_path: Path,
    query: str,
    table: SearchTable,
    slots: int,
    fraction: float,
    fts_results: list[dict],
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Rows to seat in the identifier head, best first (techne#1078).

    Two sources, in this order. First the rows of ``fts_results`` that hold a rare
    term, in its order: they went through `search_fts`'s full rerank, which scores
    every query term, so among exact rows the one matching more of the query leads.
    Ranking rare-term rows by BM25 alone seated two `reflect-drain` shell echoes
    ahead of the `reflect-drain --go` row the query named. Then a SCOPED query for
    the same terms, best BM25 first, for the #946 reason: an exact row has to survive
    that rerank into `search_fts`'s top ``limit`` to be in ``fts_results`` at all,
    and on the live store one sat at observation-rank 16-47. It MATCHes through
    `_fts_match_rows`, so the archived, uncertain and superseded filters are the
    ones every FTS read applies.

    Returns ``[]`` without opening the store when no term is identifier-shaped, and
    on any SQLite error: failing open to the ordinary vec-first page is the safe
    direction. ``conn`` is ``search()``'s shared connection (techne#1094), left
    open; without it the pool opens and closes its own.
    """
    if slots <= 0 or not _identifier_terms(query):
        return []
    own_conn = conn is None
    if own_conn:
        try:
            conn = connect(db_path)
        except sqlite3.Error:
            return []
    try:
        rare, keys = _rare_identifier_terms(conn, query, table, fraction)
        if not rare:
            return []
        scoped = _fts_match_rows(
            conn,
            " OR ".join(f'"{t}"' for t in rare),
            table,
            slots,
            include_archived=include_archived,
            include_uncertain=include_uncertain,
            row_chars=row_chars,
        )
    except sqlite3.Error:
        return []
    finally:
        if own_conn:
            conn.close()
    scoped.sort(key=lambda r: r["rank"])
    return [r for r in fts_results if (r["source_table"], r["id"]) in keys] + scoped


def _identifier_head_reservation(head_budget: int, slots: int) -> int:
    """How many HEAD slots the identifier reservation may take (techne#1078).

    Never more than the head minus one, so the vector channel always keeps a head
    slot — the same clamp `_curated_head_reservation` and `_fts_floor` apply. At
    ``limit=10`` (head 9) the default seats 2.
    """
    if slots <= 0 or head_budget <= 1:
        return 0
    return min(slots, head_budget - 1)


#: A query phrase counts as rare when fewer rows than this FRACTION of the searched
#: scope hold it (techne#1142), the #1078 rule applied to a span of query terms
#: instead of one token. CHOSEN ON A HELD-OUT SET, not on the issue's two queries:
#: 115 short queries, each quoting a phrase sampled (seeds 1142 and 11421) from a
#: row of the 2026-09-28 snapshot (63,155 rows at `table="all"`), gold = that
#: source row. Bars swept: df below 2, 4, 7, 13, 26, 61, 131. The rule, fixed
#: before the run: the tightest bar reaching the maximum source-row MRR@10 at
#: limit 10, ties to the tighter. MRR@10 was 0.478 with no seat, 0.523 at df <= 1,
#: and rose to 0.540 at df <= 130, the loosest bar swept, which the rule picked.
#: 0.00207 gives a bar of 131 there. Measured, not argued; the sweep's ceiling bounded
#: the pick, so a looser bar was not ruled out, only not measured.
PHRASE_RARE_FRACTION_DEFAULT = 0.00207

#: The phrase bar never drops below this, so a small store (the eval corpus, a
#: fresh install) still treats a phrase in a handful of rows as rare. Same floor as
#: `IDENTIFIER_MIN_DF`.
PHRASE_MIN_DF = 5

#: The phrase seat is skipped for a query with more FTS terms than this
#: (techne#1142). A long query is a description, not a quoted phrase.
PHRASE_SEAT_MAX_TERMS = 8

#: Words that cannot anchor a phrase span on their own (techne#1142). A bigram
#: needs both words outside this set, a longer span at least one, so `the extra`
#: or `does not` never counts as a phrase however rare the row holding it.
_PHRASE_STOP = frozenset(
    "a an the and or but if of to in on at by for with from as is are was were be been "
    "being do does did not no it its this that these those i we you he she they me us "
    "my our your so than then there here what why how when where which who can could "
    "should would will just all any some have has had into out up about over".split()
)

#: First words that make a query a question (techne#1142). A question is asked in
#: the searcher's own words, so a rare span in it is not a phrase they are quoting.
_QUESTION_WORDS = frozenset(
    "what why how when where which who whom whose is are was were do does did can "
    "could should would will has have had may might shall am isn't aren't doesn't "
    "don't didn't can't won't".split()
)


def _is_question(query: str) -> bool:
    """Question form: ends in ``?`` or opens with a wh-word or an auxiliary."""
    q = query.strip()
    if q.endswith("?"):
        return True
    words = q.split()
    return bool(words) and words[0].lower().strip(_IDENTIFIER_STRIP) in _QUESTION_WORDS


def _phrase_seat_row(
    conn: sqlite3.Connection,
    query: str,
    table: SearchTable,
    fraction: float,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
) -> dict | None:
    """The one row to seat for a RARE contiguous span of the query (techne#1142).

    A span is 2 or more consecutive query terms (`_fts_terms`, the split
    `search_fts` quotes into its MATCH), matched as one FTS5 phrase, so the
    store's tokenizer and porter stemming apply (`replicate` matches
    `replicates`). A bigram needs both words outside `_PHRASE_STOP`, a longer span
    at least one. Rare means at least one and fewer than
    ``bar = max(PHRASE_MIN_DF, ceil(rows * fraction))`` rows hold it, ``rows``
    counting the tables ``table`` searches, the #1078 arithmetic.

    Spans are probed bottom-up: an n-span only when both its (n-1)-subspans
    matched a row, so a query costs at most a few probes past its bigrams, each
    bounded by ``bar`` rowids. The row returned holds the LONGEST rare span, best
    BM25 first among rows holding a span of that length. Returns None for a
    question-form query, one of more than `PHRASE_SEAT_MAX_TERMS` terms, no rare
    span, or any SQLite error: the ordinary page is the safe direction.
    """
    terms = _fts_terms(query)
    if len(terms) < 2 or len(terms) > PHRASE_SEAT_MAX_TERMS or _is_question(query):
        return None
    names = _IDENTIFIER_SCOPES if table == "all" else (table,)
    scopes = [_IDENTIFIER_SCOPES[n] for n in names]
    words = [t.replace('""', '"').strip(_IDENTIFIER_STRIP).lower() for t in terms]
    try:
        n_rows = sum(
            conn.execute(f"SELECT count(*) FROM {base}").fetchone()[0] for base, _, _ in scopes
        )
        bar = max(PHRASE_MIN_DF, math.ceil(n_rows * fraction))

        def df(phrase: str) -> int:
            found = 0
            for _, _, sql in scopes:
                found += len(conn.execute(sql, (phrase, bar - found)).fetchall())
                if found >= bar:
                    break
            return found

        alive: set[tuple[int, int]] = set()
        rare: dict[int, list[str]] = {}
        for n in range(2, len(terms) + 1):
            for i in range(len(terms) - n + 1):
                if n > 2 and not ((i, n - 1) in alive and (i + 1, n - 1) in alive):
                    continue
                phrase = '"' + " ".join(terms[i : i + n]) + '"'
                d = df(phrase)
                if d:
                    alive.add((i, n))
                content = sum(1 for w in words[i : i + n] if w and w not in _PHRASE_STOP)
                if content >= (2 if n == 2 else 1) and 0 < d < bar:
                    rare.setdefault(n, []).append(phrase)
        for n in sorted(rare, reverse=True):
            # A span's rows can all be filtered out here (archived, uncertain,
            # superseded) though the df probe counted them; fall back a length.
            rows = _fts_match_rows(
                conn,
                " OR ".join(rare[n]),
                table,
                1,
                include_archived=include_archived,
                include_uncertain=include_uncertain,
                row_chars=row_chars,
            )
            if rows:
                best = dict(min(rows, key=lambda r: r["rank"]))
                # The MATCH that seated it, for `_window_seated_row`.
                best["_phrase_match"] = " OR ".join(rare[n])
                return best
    except sqlite3.Error:
        return None
    return None


#: Source table -> (FTS table, FTS column index, page field) for
#: `_window_seated_row`. Threads are left out: their page field is `summary`, and
#: a thread's span usually sits in `label` or `resume_points`, which the page
#: shows whole.
_SEAT_WINDOW_COLS = {
    "observation": ("observations_fts", 0, "content"),
    "knowledge": ("knowledge_fts", 0, "content"),
}


def _window_seated_row(conn: sqlite3.Connection, row: dict, match: str) -> None:
    """Show the seated row's matched span, in place (techne#1142).

    Search cuts each row to its head (`_ROW_CHAR_CAP` for observations, the
    caller's cap for knowledge). The phrase seat picks a row for a span that can
    sit past that cut: on the 2026-09-28 eval store it seated a 1,733-char
    Dockerfile echo whose `litestream replicates techne.db` starts near char 890,
    so the page showed 500 chars of shell and nothing the query asked for.

    When the span's end lies past what the row shows, the content becomes a
    window of the same width starting a quarter-width before the span, prefixed
    with `…` when it does not start at char 0. FTS5 `highlight()` finds the span,
    so porter stemming applies exactly as it did in the seat's MATCH. The
    original offsets go in `content_window` and the full length in
    `content_chars_total`. Any SQLite error, or a span in another column (tags),
    leaves the row as it was.
    """
    spec = _SEAT_WINDOW_COLS.get(row.get("source_table"))
    shown = row.get("content")
    if spec is None or not isinstance(shown, str) or not shown:
        return
    fts, col, field = spec
    try:
        hit = conn.execute(
            f"SELECT highlight({fts}, {col}, char(1), char(2)) FROM {fts} "
            f"WHERE {fts} MATCH ? AND rowid = ?",
            (match, row["id"]),
        ).fetchone()
    except sqlite3.Error:
        return
    if hit is None or hit[0] is None or "\x01" not in hit[0]:
        return
    marked = hit[0]
    start = marked.index("\x01")
    # Offsets into the unmarked text: the first span's markers are the only ones
    # before its closing marker.
    end = marked.index("\x02", start) - 1
    full = marked.replace("\x01", "").replace("\x02", "")
    width = len(shown)
    if end <= width:
        return
    lo = max(0, start - width // 4)
    window = full[lo : lo + width]
    row[field] = ("…" if lo else "") + window
    row["content_window"] = [lo, lo + len(window)]
    row["content_chars_total"] = len(full)
    logger.debug(
        "phrase seat: windowed %s %s to chars %d-%d of %d",
        row["source_table"],
        row["id"],
        lo,
        lo + len(window),
        len(full),
    )


def search(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 20,
    use_vectors: bool = True,
    half_life_days: float = 30.0,
    include_archived: bool = True,
    include_uncertain: bool = False,
    row_chars: int | None = None,
    curated_head_slots: int | None = None,
    identifier_head_slots: int | None = None,
    identifier_rare_fraction: float | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    phrase_seat: bool = False,
) -> list[dict]:
    """Hybrid search: BM25 (FTS5) + vector (cosine) combined by a vec-first
    union (techne#401).

    ``row_chars`` caps each KNOWLEDGE row's content, defaulting to
    ``_ROW_CHAR_CAP`` when None (techne#842). It does NOT move observation or
    thread rows: those are the tool ledger, and a caller raising its recall
    budget wants more of the curated row, not more transcript. A caller that
    renders a truncation marker should ask for one character MORE than it will
    show — at exactly the cap a severed row is indistinguishable from a whole
    one.

    With ``use_vectors=False``: returns BM25-only results (unchanged
    legacy behavior).

    With ``use_vectors=True`` and vector deps installed: runs both channels and
    returns the vector channel first (best-first by ascending distance), with a
    reserved tail slice for FTS-only hits (``_fts_floor``, techne#827). Rows
    present in BOTH channels keep their vector-channel position; agreement is
    recorded as provenance, not a rank boost. Vec-first replaced Reciprocal Rank
    Fusion because the #399 ablation showed RRF's rank-sum buries a strong
    single-channel (vector-only) hit beneath lexically-tempting FTS distractors,
    losing paraphrase recall@10 (0.667 vs 1.0) and MRR (0.144 vs 0.546) while
    only TYING on exact/filter/negative.

    THE TAIL FLOOR IS LOAD-BEARING (techne#827). Vec-first originally appended
    FTS-only hits after the whole vector channel and then sliced to ``limit``,
    which means a vector channel that fills the page — the normal case for any
    non-trivial store — silently discarded all of them: measured 108 of 108
    lexical-only hits dropped across 15 identifier-shaped queries on the live
    store. That made exact-identifier recall (PR numbers, commit shas, symbol
    names, config keys) impossible on every read path, since ``techne_recall``,
    ``techne_search`` and the per-turn injection hook all land here. The floor
    is ONE slot, sized by measurement rather than by argument: at that size the
    #399/#401 paraphrase arm holds at recall@10 1.0 / MRR 0.546, unchanged from
    pre-#827, while a floor of 2 or more drops it to 0.833 / 0.528. See
    ``_fts_floor`` for the ablation and for why this does not scale with limit.

    THE HEAD RESERVES SLOTS FOR CURATED ROWS (techne#946). The tail floor can only
    rescue a curated row the LEXICAL channel found; one the vector channel found but
    ranked below the head budget was invisible to it. ``curated_head_slots`` holds
    that many head positions for the best curated vec rows, displacing the weakest
    non-curated rows of the head one for one — so the page never grows and the tail
    floor is untouched. ``None`` takes ``CURATED_HEAD_SLOTS_DEFAULT``; 0 disables it
    and restores the pre-#946 page exactly. An unfilled reservation costs nothing: if
    the curated channel has fewer rows than the reservation, the remaining slots stay
    with the rows that earned them, the same way the tail spends unused head budget.

    AN EXACT IDENTIFIER LOOKUP GETS THE FIRST SLOTS (techne#1078). When the query
    carries an identifier-shaped token (`emit_max_chars`, `b2dcdd2`, `reflect-drain`)
    that fewer than ``identifier_rare_fraction`` of the searched rows match, up to
    ``identifier_head_slots`` rows holding it lead the page, from `_identifier_pool`.
    Vec-first had ordered them by distance: on the 2026-09-28 snapshot
    `emit_max_chars 100000 config.yaml techne` missed at limit 10 and 15. It is a
    displacement: the vec rows it pushes out refill a short page, the #827 floor
    keeps its slot, and the #946 reservation is sized on the head that is left.
    A query with no RARE identifier-shaped token gets a page byte-identical to the
    pre-#1078 one. Ordinary words can be identifier-shaped (`well-known`, a date), so
    that is narrower than "plain English is unaffected"; see `_is_identifier_shaped`.

    A RARE QUOTED PHRASE GETS THE SEAT AFTER THE TOP RESULT, ON REQUEST ONLY
    (techne#1142). With ``phrase_seat=True``, a query holding a contiguous span of
    two or more terms that fewer than `PHRASE_RARE_FRACTION_DEFAULT` of the
    searched rows contain seats ONE row holding it right after vec-first's first
    head row (after any identifier head), from `_phrase_seat_row`. Vec-first had
    left such rows off the page: `litestream replicate techne.db exec` never
    reached the top 200 on the 2026-09-28 snapshot. Skipped for a question-form
    query and one of more than `PHRASE_SEAT_MAX_TERMS` terms. A displacement like
    #1078's, sized the same way. DEFAULT OFF: the explicit surfaces
    (``techne_search``, ``techne search``, ``techne_recall``) turn it on; the
    per-turn and inbound recall rails do not, because in the issue's measurement
    phase none of the seats on real per-turn prompts beat the row it displaced.
    With it off, or when it finds no rare span, the page is byte-identical to the
    pre-#1142 one. At any limit the top result is the seat-off page's: while the
    seat holds a row, the #946 promotion may not displace vec #1.

    Returned rows include:

      - ``match_source``: ``"fts"``, ``"vec"``, or ``"fts+vec"``
      - ``fusion``: ``"vec_first"`` — how order was decided. Unchanged by the
        floor: the vector channel still decides the head of the page.
      - ``reserved_slot``: present ONLY on a row that is on the page because a
        reservation put it there — ``"identifier_head"`` (#1078),
        ``"phrase_head"`` (#1142), ``"curated_head"`` (#946) or ``"lexical_tail"``
        (#827). Absent means the row earned its place on
        rank. This is the field that makes a ranking complaint answerable without
        re-deriving arithmetic, which is the property a fused score prior would have
        destroyed.

    When vec is requested but no vec results came back (extras not
    installed, vec tables empty, etc.), falls through to FTS-only —
    same as the legacy behavior.

    ``include_archived`` (techne#216): when False, soft-archived knowledge rows
    are excluded from BOTH channels — this is the DEFAULT-recall path (techne_recall).
    Default True: EXPLICIT search (techne_search) still returns archived entries.

    ONE CONNECTION, ONE sqlite-vec LOAD, ONE QUERY EMBED PER CALL (techne#1094).
    The FTS channel, the vector channel, the #1078 identifier pool and the #946
    curated pool all read through the connection opened here, and the query is
    embedded once for both vector calls. Each channel used to open its own:
    3.08 connections, 1.73 extension loads and 1.73 embeds per ``table="all"``
    call (issue #1094's measurement, operator-store snapshot 2026-09-29 03:30;
    the arch card's 3.02 / 1.63 / 1.63 is PR #1100's run on the 2026-09-28
    16:51 snapshot), each connection starting with a cold page cache. The
    connection is closed on every path, including an exception.

    ``conn`` (techne#1095): run on the caller's connection and leave it open. The
    MCP server passes its long-lived read connection (``core/read_conn.py``) so
    the page cache and the sqlite-vec load survive across tool calls. The caller
    owns that connection's freshness: it must not hold a read transaction open
    between calls. Without ``conn`` this opens and closes its own, as before.
    """
    if not use_vectors and prepare_fts_query(query) is None:
        # Nothing to run: search_fts returns [] before touching the store, and
        # this keeps that true for the call as a whole.
        return []
    own_conn = conn is None
    if own_conn:
        conn = connect(db_path)
    try:
        return _search_on_conn(
            conn,
            db_path,
            query,
            table=table,
            limit=limit,
            use_vectors=use_vectors,
            half_life_days=half_life_days,
            include_archived=include_archived,
            include_uncertain=include_uncertain,
            row_chars=row_chars,
            curated_head_slots=curated_head_slots,
            identifier_head_slots=identifier_head_slots,
            identifier_rare_fraction=identifier_rare_fraction,
            phrase_seat=phrase_seat,
        )
    finally:
        if own_conn:
            conn.close()


def _search_on_conn(
    conn: sqlite3.Connection,
    db_path: Path,
    query: str,
    *,
    table: SearchTable,
    limit: int,
    use_vectors: bool,
    half_life_days: float,
    include_archived: bool,
    include_uncertain: bool,
    row_chars: int | None,
    curated_head_slots: int | None,
    identifier_head_slots: int | None,
    identifier_rare_fraction: float | None,
    phrase_seat: bool = False,
) -> list[dict]:
    """``search()``'s body, on the one connection ``search()`` owns (techne#1094)."""
    fts_results = search_fts(
        db_path,
        query,
        table=table,
        limit=limit,
        half_life_days=half_life_days,
        include_archived=include_archived,
        include_uncertain=include_uncertain,
        row_chars=row_chars,
        conn=conn,
    )

    if not use_vectors:
        return fts_results

    # Embedded once, for this call and the #946 curated pool below. None means the
    # channel is off here (no sqlite-vec, no backend, model not cached); search_vec
    # is still called, because it is the one place that decides the channel's
    # answer, and without a vector it resolves the model itself and returns [].
    query_vec = _query_embedding(query)
    vec_results = search_vec(
        db_path,
        query,
        table=table,
        limit=limit,
        include_archived=include_archived,
        include_uncertain=include_uncertain,
        row_chars=row_chars,
        conn=conn,
        query_vec=query_vec,
    )
    if not vec_results:
        return fts_results

    # Vec-first union with a reserved lexical floor (techne#401, floor #827).
    # The vector channel leads — it comes back already sorted best-first
    # (ascending distance; search_vec sorts before truncating) — and FTS-only
    # hits take a reserved slice of the page TAIL, in search_fts's own
    # recency/BM25/decay order. Dedup on (type, id), truncate to limit.
    # Cross-channel agreement is recorded as provenance (match_source="fts+vec")
    # but does not reweight order the way RRF's rank-sum did — that rank-sum
    # was what buried strong single-channel vector hits (#399 ablation).
    #
    # WHY THE FLOOR EXISTS (#827). Before it, this union appended the whole vec
    # channel, then the FTS-only hits, then truncated with `out[:limit]` — so a
    # vec channel that filled the page, which any non-trivial store does, threw
    # away every FTS-only hit before the caller saw it. Measured on the live
    # store: 108 of 108 lexical-only hits dropped across 15 identifier-shaped
    # queries, i.e. exact-identifier recall (PR numbers, commit shas, symbol
    # names, config keys) was answered by cosine distance alone, on all three
    # read paths (techne_recall, techne_search, per-turn injection).
    # Reserving the tail costs the head `floor` positions, and those positions
    # are not always pure-vec rows: an `fts+vec` row can be displaced too, and
    # it cannot be rescued into the tail because `fts_only` excludes every vec
    # key. That is why the floor is 1 and not a fraction of the page (#829 r1) —
    # at 1 the paraphrase arm measures exactly as it did pre-#827.
    # NB: _rrf_fuse / RRF_K are intentionally retained as a library primitive —
    # the #399 ablation's RRF arm references them; not the default.
    fts_by_key = {(r["source_table"], r["id"]): r for r in fts_results}
    vec_keys = {(r["source_table"], r["id"]) for r in vec_results}
    fts_only = [r for r in fts_results if (r["source_table"], r["id"]) not in vec_keys]

    # Size the floor off DISTINCT keys. UNREACHABLE, NOT MERELY DORMANT (#829
    # r3/r4): no producer that can reach this function emits a duplicate key at
    # ANY floor — search_fts joins each *_fts rowid 1:1 to its base table, tags a
    # distinct source_table per table, and since techne#1079 the OR pass tops
    # up a short AND pass but fts_and_or_split dedups the OR block against the
    # AND block by key before either is reranked, so no duplicate reaches here.
    # Widening the floor does NOT make this live; only a new producer would.
    # Kept so search() stays correct if one ever exists.
    # (An earlier version of this comment said "becomes load-bearing if the floor
    # widens". That was wrong and is the claim #829 r3 removed from _fts_floor —
    # this copy is the one r4 caught still carrying it.)
    floor = _fts_floor(limit, len({(r["source_table"], r["id"]) for r in fts_only}))
    seen: set[tuple] = set()
    head: list[dict] = []

    def _as_page_row(r: dict) -> dict:
        row = dict(r)  # copy — don't mutate the caller's rows
        fts_row = fts_by_key.get((r["source_table"], r["id"]))
        if fts_row is not None:
            row["match_source"] = "fts+vec"
            # Carry the FTS BM25 `rank` onto the overlap row. Vec rows have only
            # `distance`, but FTS-aware consumers read `rank` (federation's
            # merge_and_rank fed it to _rerank until techne#1089). Pre-#401 the
            # overlap row WAS the fts row, so it had `rank`; vec-first makes it a vec-row
            # copy, so restore `rank` explicitly. (#402 review — Prudence.)
            if "rank" in fts_row:
                row["rank"] = fts_row["rank"]
            # Same for `_match_pass` (techne#1079 review round 2/3): an
            # fts+vec overlap row went through the AND/OR split before the
            # vector channel found it too, and `merge_and_rank`'s tier rule
            # needs to know which pass it came from — NOT to sort an AND
            # overlap from a vec-only row (both are first tier either way),
            # but to tell an OR overlap (partial lexical match PLUS vector
            # agreement — more evidence, stays first tier) from an OR row
            # with no vector corroboration (second tier). Without the copy,
            # every fts+vec row read as untagged and merge_and_rank could
            # not draw that distinction at all.
            if "_match_pass" in fts_row:
                row["_match_pass"] = fts_row["_match_pass"]
        else:
            row["match_source"] = "vec"
        row["fusion"] = "vec_first"
        return row

    head_budget = limit - floor
    #: Rows the head reservation displaced. Appended after the tail so a page that
    #: gave up a head slot cannot come back short (Prudence, #966 r1).
    overflow: list[dict] = []

    # IDENTIFIER HEAD (techne#1078). A query carrying a RARE identifier-shaped token
    # (`emit_max_chars`, `b2dcdd2`, `reflect-drain`) is an exact lookup, and vec-first
    # ordered its exact rows by cosine distance: measured on the live store, anywhere
    # from rank 2 to off the page. Rows FTS matched on such a token take the first
    # slots, in FTS order. A DISPLACEMENT like #946: they come out of the head budget,
    # the vec rows they push out go to `overflow`, and the #827 tail floor is
    # untouched. A query with no RARE identifier-shaped token runs nothing below
    # differently, so its page is byte-identical to the pre-#1078 one. That is not
    # "every plain-English query": a rare hyphenated word or date qualifies.
    ident_head: list[dict] = []
    ident_cap = _identifier_head_reservation(
        head_budget,
        IDENTIFIER_HEAD_SLOTS_DEFAULT if identifier_head_slots is None else identifier_head_slots,
    )
    pool = _identifier_pool(
        db_path,
        query,
        table,
        ident_cap,
        IDENTIFIER_RARE_FRACTION_DEFAULT
        if identifier_rare_fraction is None
        else identifier_rare_fraction,
        fts_results,
        include_archived=include_archived,
        include_uncertain=include_uncertain,
        row_chars=row_chars,
        conn=conn,
    )
    if pool:
        vec_by_key = {(r["source_table"], r["id"]): r for r in vec_results}
        natural: list[tuple] = []  # the head's keys as vec-first would seat them
        for r in vec_results:
            k = (r["source_table"], r["id"])
            if k not in natural:
                natural.append(k)
        for r in pool:
            if len(ident_head) >= ident_cap:
                break
            key = (r["source_table"], r["id"])
            if key in seen:
                continue
            seen.add(key)
            if key in vec_by_key:
                row = _as_page_row(vec_by_key[key])
            else:
                row = dict(r)
                row["match_source"] = "fts"
                row["fusion"] = "vec_first"
            # Marked only when the reservation MOVED the row: one vec-first
            # already seated at this index is there on rank.
            pos = len(ident_head)
            if pos >= len(natural) or natural[pos] != key:
                row["reserved_slot"] = "identifier_head"
            ident_head.append(row)
    vec_budget = head_budget - len(ident_head)
    # Rows the reservation MOVED. One already at its vec-first index costs the page
    # nothing, so it must not shrink the curated budget or bump a vec row either;
    # counting it did both and changed a page that should have been identical.
    n_moved = sum(1 for r in ident_head if r.get("reserved_slot") == "identifier_head")

    # PHRASE SEAT (techne#1142), requested by the explicit surfaces only. ONE row
    # holding a rare contiguous span of the query, seated right AFTER the first vec
    # head row, so it never displaces the top result. In the issue's measurement
    # phase (2026-09-28 snapshot, a df <= 5 bigram bar) a seat in front changed the
    # top result on 5 of 40 real pages and one after vec #1 on none; at the shipped
    # bar the top result changes on none either. It takes a head slot, and keeps one
    # for the vector channel, as the identifier head does.
    phrase_row: dict | None = None
    if phrase_seat and vec_budget > 1:
        cand = _phrase_seat_row(
            conn,
            query,
            table,
            PHRASE_RARE_FRACTION_DEFAULT,
            include_archived=include_archived,
            include_uncertain=include_uncertain,
            row_chars=row_chars,
        )
        # The head as vec-first would fill it from here, without the seat.
        rest = []
        for r in vec_results:
            k = (r["source_table"], r["id"])
            if k not in seen and k not in rest:
                rest.append(k)
        rest = rest[:vec_budget]
        key = None if cand is None else (cand["source_table"], cand["id"])
        # Already seated by the identifier head, or already the top vec row: the
        # page gives it that place on its own, so there is nothing to move.
        if key is not None and key not in seen and rest[:1] != [key]:
            seen.add(key)
            vec_by_key = {(r["source_table"], r["id"]): r for r in vec_results}
            if key in vec_by_key:
                phrase_row = _as_page_row(vec_by_key[key])
            else:
                # The FTS channel's own copy when it has one: that carries the
                # reranked `rank` and `_match_pass` federation's tier rule reads.
                phrase_row = dict(fts_by_key.get(key, cand))
                phrase_row.pop("_phrase_match", None)
                phrase_row["match_source"] = "fts"
                phrase_row["fusion"] = "vec_first"
            if cand.get("_phrase_match"):
                _window_seated_row(conn, phrase_row, cand["_phrase_match"])
            # Tagged when the seat changed its place: a row vec-first already put
            # second is there on rank, the #1078 rule.
            if rest[1:2] != [key]:
                phrase_row["reserved_slot"] = "phrase_head"
            vec_budget -= 1
            # Counted as a displacement only when it came from OUTSIDE the natural
            # head. One moved up from inside it leaves the head's row set as it was,
            # so it must not bump a vec row or shrink the #946 curated budget.
            if key not in rest:
                n_moved += 1

    for r in vec_results:
        if len(head) >= vec_budget:
            break
        key = (r["source_table"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        head.append(_as_page_row(r))
    # The vec rows the identifier head pushed out, kept as overflow so a short tail
    # cannot shorten the page (the #966 r1 rule).
    bumped: list[dict] = []
    if n_moved:
        for r in vec_results:
            if len(bumped) >= n_moved:
                break
            key = (r["source_table"], r["id"])
            # `search_vec` returns distinct keys, so `seen` is the only check needed.
            if key in seen:
                continue
            bumped.append(_as_page_row(r))

    # CURATED HEAD RESERVATION (techne#946) — a DISPLACEMENT, not an extra slot, so
    # `head + tail` is still exactly `limit` and the #827 tail floor is untouched.
    # Only rows the natural fill left behind are promoted, so a page that already
    # carries its quota of curated rows is returned byte-identical to the pre-#946
    # one, and a store with no curated vec hits pays nothing at all.
    # SCOPED TO `table="all"`, AND THE EVAL GATE IS WHY THIS LINE EXISTS. Without the
    # scope check the reservation fired on a `table="observations"` or
    # `table="threads"` query too, injecting knowledge rows into a page the caller had
    # deliberately narrowed — a contract violation, not a ranking preference. The #385
    # filter tasks caught it immediately: four of six leaked a forbidden knowledge
    # distractor into a single-table page (fpr 0.0 -> 0.667). A single-table query has
    # no cross-table competition to correct, so there is nothing here to reserve
    # against.
    # Sized on what the identifier head left (techne#1078). On the full
    # `head_budget` the two stacked: `emit_max_chars` came back as 2 identifier + 3
    # curated + 1 tail, six of ten slots reserved, and #946's "a third of the head"
    # cap no longer held. Equal to `head_budget` when no identifier row was moved.
    reserve = (
        _curated_head_reservation(head_budget - n_moved, _curated_slots(curated_head_slots))
        if table == "all"
        else 0
    )
    if reserve:
        in_head = sum(
            1
            for r in ident_head + head + ([phrase_row] if phrase_row else [])
            if r.get("source_table") in _CURATED_TABLES
        )
        deficit = reserve - in_head
        if deficit > 0:
            # THE CANDIDATES COME FROM A SCOPED QUERY, NOT FROM `vec_results`, AND
            # THAT IS THE WHOLE MECHANISM. `search_vec` is itself capped at `limit`,
            # so the union's own results hold at most `limit` rows across every
            # table: on a store where observations crowd the page, the curated row
            # worth promoting was never fetched, and a reservation reading
            # `vec_results` would find nothing to seat. Measured on the live store
            # before this: 12 real queries, mean curated rows per page 4.00 -> 4.08,
            # i.e. one extra row in twelve pages — a reservation in name only.
            # One extra ANN query against the knowledge table is what makes the slots
            # real. IT FETCHES `deficit + in_head`, NOT `deficit` (Temperance, #966):
            # the pool is deduped against rows already seated in the head, so a pool
            # sized to the deficit alone can come back short by exactly the number of
            # curated rows that won on distance. The surplus is the dedup buffer.
            curated_pool = search_vec(
                db_path,
                query,
                table="knowledge",
                limit=deficit + in_head,
                include_archived=include_archived,
                include_uncertain=include_uncertain,
                row_chars=row_chars,
                conn=conn,
                query_vec=query_vec,
            )
            promoted: list[dict] = []
            for r in curated_pool:
                if len(promoted) >= deficit:
                    break
                key = (r["source_table"], r["id"])
                if key in seen:
                    continue
                seen.add(key)
                row = _as_page_row(r)
                row["reserved_slot"] = "curated_head"
                promoted.append(row)
            if promoted:
                # Displace from the BACK of the head, worst-ranked first, and only
                # non-curated rows — displacing a curated row to seat a curated row
                # would be a no-op that silently costs a result. The promoted rows
                # land at the end of the head: they are on the page by reservation,
                # not by distance, and the order says so.
                keep, dropped, displaced = [], 0, []
                # VEC #1 STAYS FIRST WHILE A PHRASE ROW IS HELD (techne#1142, Prudence
                # #1143 r1). The seat lowers `vec_budget`, so at limit 3 (or 2 with no
                # FTS-only row) `head` is vec #1 alone and this loop displaced it:
                # the top result moved on a page where the phrase row was already
                # second. Guarded only when the seat holds a row, so a seat-off page
                # runs this loop exactly as before.
                protected = head[0] if phrase_row is not None and head else None
                for row in reversed(head):
                    if (
                        dropped < len(promoted)
                        and row.get("source_table") not in _CURATED_TABLES
                        and row is not protected
                    ):
                        dropped += 1
                        # KEEP THE DISPLACED ROW AS OVERFLOW, do not drop it
                        # (Prudence, #966 r1). The promotion pool is not deduped
                        # against `fts_only`, so a curated row the vector channel
                        # never fetched but FTS matched can be promoted here AND be
                        # the tail floor's only candidate — `seen` then empties the
                        # tail, and a page that surrendered a head slot comes back
                        # one row SHORT for nothing, since `_curated_first` would
                        # have seated that same row in the floor slot anyway.
                        # Measured: 12 observations + 1 lexical-only curated row,
                        # limit 10 -> 9 rows. Appending after the tail lets the
                        # closing `[:limit]` decide: in the ordinary case the row
                        # falls off the end exactly as before, and in this case it
                        # refills the slot the empty tail left.
                        displaced.append(row)
                        seen.discard((row["source_table"], row["id"]))
                        continue
                    keep.append(row)
                head = list(reversed(keep)) + promoted[:dropped]
                overflow = list(reversed(displaced))
                for row in promoted[dropped:]:
                    # NOTHING WAS DISPLACEABLE. The cause named here used to be "an
                    # all-curated head", which the arithmetic above rules out
                    # (promotion needs `deficit > 0`, so `in_head < reserve`, and
                    # `reserve <= head_budget - 1`). The reachable cause is a SHORT
                    # head: a thin vector channel returns fewer rows than the budget
                    # and all of them are curated, so there is no non-curated row to
                    # displace while the scoped pool still finds a curated row the
                    # union never fetched. Put the key back so the tail may still
                    # reach it through the lexical channel (Prudence, #966 r2).
                    seen.discard((row["source_table"], row["id"]))

    tail: list[dict] = []
    for r in _curated_first(fts_only):
        key = (r["source_table"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        row = dict(r)  # copy — don't mutate the caller's rows
        row["match_source"] = "fts"
        row["fusion"] = "vec_first"
        # The first `floor` tail rows are the ones #827 reserved; anything after them
        # is slack the thin-vec case handed back. Marking only the reserved ones keeps
        # `reserved_slot` an honest answer to "why is this row here".
        # GATED ON THE HEAD ACTUALLY HAVING SPENT ITS BUDGET (Prudence, #966 r1):
        # with a thin vec channel the first tail row is on the page regardless, so
        # marking it reserved answers "why is this row here" with a reservation that
        # did nothing.
        if len(tail) < floor and len(head) >= vec_budget:
            row["reserved_slot"] = "lexical_tail"
        tail.append(row)

    # `head` may be shorter than its budget (a thin vec channel); the tail is
    # allowed to spend the slack rather than returning a short page. `overflow`
    # holds rows the head reservation displaced: last in line, so they refill a
    # short page rather than shrinking it (#966 r1).
    # `bumped` goes last, after the #946 overflow. A bumped curated row can come
    # back through the #946 promotion, but promoting it displaces a row into
    # `overflow`, which takes the page's last free slot first, so the duplicate is
    # always cut by `[:limit]`.
    # The phrase row joins the head only now, after vec #1 (techne#1142). Held apart
    # until here so the #946 promotion, which displaces from the back of `head`,
    # cannot evict it, and so the tail's `len(head) >= vec_budget` test reads the
    # vec rows alone.
    if phrase_row is not None:
        head.insert(min(1, len(head)), phrase_row)
    return (ident_head + head + tail + overflow + bumped)[:limit]


def backfill_vectors(
    db_path: Path, batch_size: int = 100, *, allow_download: bool = True
) -> dict[str, int]:
    """Embed un-embedded rows. Returns counts of newly embedded items.

    `allow_download=False` IS THE OPT-IN SURFACE the module docstring of
    `core/embeddings.py` asks for (techne#814). Until the SessionStart chain was
    owned by `register_hooks`, operator shell and hook chain invoked the
    identical `techne vectors backfill` string, so there was no signal to branch
    on and the permissive default was the only option. The hook now passes
    `--no-download`, which lands here: a cold model cache degrades to "swept,
    embedded nothing" in ~0.2s instead of blocking session start on a Hub fetch.
    The bare CLI keeps the permissive default, so README's cold-cache recovery
    still downloads.

    `orphans_swept` is present ONLY when the sweep actually ran (#408). Its
    ABSENCE means the function returned before reaching it — no numpy, no
    sqlite_vec, or an interpreter that cannot load extensions — and a caller must
    not report hygiene as having happened. An empty dict is ambiguous across
    three different outcomes; the key is the discriminator.
    """
    try:
        import numpy as np
        import sqlite_vec
    except ImportError:
        # numpy is only in the [vectors] extra, so `pip install techne` +
        # `pip install sqlite-vec` lands here with the CLI's own import guard
        # satisfied. No sweep runs; say nothing rather than claim it did.
        return {}
    have_backend = embeddings_mod.backend_available()

    conn = connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        # Interpreter built without loadable-extension support — degrade gracefully.
        conn.close()
        return {}

    # SWEEP ORPHANS BEFORE THE BACKEND GATE (techne#408). Deleting an embedding
    # whose parent row is gone is pure SQL: it needs sqlite_vec loaded to touch a
    # vec0 table, but NOT a model. Gating it behind `backend_available()` would
    # mean an install with sqlite-vec and no model2vec/fastembed accumulates
    # orphans forever while `vectors:coverage` reports dangling rows and names
    # `techne vectors backfill` as the remedy — a check whose remedy provably
    # does nothing, which is exactly the never-green defect #496 was just fixed
    # for. Found by running this against an interpreter with sqlite-vec but no
    # embedding backend; the wiring test failed and the early return was why.
    # PER-TABLE try, not one sum(). A generator inside sum() raises out of the
    # whole expression, so an unreadable threads_vec would leave observations_vec
    # and knowledge_vec unswept — and on the no-backend path there is no loop
    # below to cover for it, so one bad index would silently suppress the other
    # two. This is the ONLY sweep call site: an earlier version also swept inside
    # the per-table loop, which made "unwire the purge" an ambiguous mutation —
    # the in-loop copy alone satisfied the end-to-end test on a machine WITH a
    # backend, so that test pinned nothing about placement in the environment
    # that matters.
    swept = 0
    sweep_errors = 0
    for name in _VEC_PARENT:
        try:
            swept += _purge_orphaned_vec(conn, name)
        except sqlite3.OperationalError:
            # Present but unreadable (e.g. built at another dim). Not worth
            # failing a backfill over; _ensure_vec_backend_identity rebuilds it.
            # COUNTED, not just swallowed: `orphans_swept: 0` from a sweep that
            # FAILED on every index is otherwise indistinguishable from one that
            # found nothing to do, which is the same absent-vs-empty collapse the
            # ABSENCE of the key exists to avoid.
            sweep_errors += 1
    if swept:
        conn.commit()
    counts = {"orphans_swept": swept, "sweep_errors": sweep_errors}

    if not have_backend:
        # Neither model2vec nor fastembed importable — nothing to embed with, but
        # the orphan sweep above HAS run, and the key says so.
        conn.close()
        return counts

    try:
        model = _get_embedding_model(allow_download=allow_download)
    except Exception:
        if allow_download:
            # Unchanged behaviour for the default path: a failure here is a real
            # error and must not be swallowed into a counts dict that reads like
            # success — the #496 defect from the other side.
            raise
        # allow_download=False on a cold cache raises by design (fastembed's
        # local_files_only / model2vec's scoped HF_HUB_OFFLINE). Degrade exactly
        # like the no-backend path above: the sweep ran and `orphans_swept` says
        # so, `threads`/`observations`/`knowledge` are absent because nothing was
        # embedded. A traceback here would reach a hook's stderr on every single
        # session start.
        conn.close()
        return counts

    counts.update({"threads": 0, "observations": 0, "knowledge": 0})

    try:
        # techne#601/#602: detect a backend/model swap (e.g. upgrading to py3.14,
        # where fastembed can't install and model2vec takes over) BEFORE touching
        # any per-table vec data. A mismatch drops all three vec tables so the
        # per-table loop below recreates them at the new dim and the
        # "NOT IN (SELECT rowid FROM vec_tbl)" backfill query naturally re-embeds
        # every row against an empty table. See _ensure_vec_backend_identity.
        _ensure_vec_backend_identity(conn, model)

        for tbl, content_col, vec_tbl in [
            ("threads", "summary", "threads_vec"),
            ("observations", "content", "observations_vec"),
            ("knowledge", "content", "knowledge_vec"),
        ]:
            _ensure_vec_table(conn, vec_tbl, model.dim)
            # Superseded/merged knowledge must NOT have a live ANN embedding: search_vec
            # filters superseded rows only AFTER the k-nearest cut, so a stale embedding
            # occupies a top-k slot (techne#168). The audit suggested deleting from
            # _supersede_in_conn, but that connection can't touch a vec0 table (connect()
            # never loads sqlite_vec); backfill_vectors is the one place the extension is
            # loaded, so the index-side cleanup lives here: sweep existing stale embeddings
            # and skip superseded rows when embedding so they're never (re-)added.
            # NB: this cleanup fires only when backfill_vectors runs, which is NOT on
            # every write — the query-time over-fetch in search_vec
            # (_VEC_SUPERSEDE_OVERFETCH) is the always-on guard, sound up to that factor
            # of stale neighbours. That much is unchanged.
            # WHAT CHANGED (techne#704): this used to say "nothing auto-runs
            # backfill_vectors in production today". That is a claim about THIS REPO —
            # where cli.py's `techne vectors backfill` is the only non-test caller — and
            # it was wrongly stated as a claim about the deployed system. At least one
            # real deployment chained the CLI into its SessionStart hook as a shell
            # string, and deployment wiring lives in the host's settings.json, which no
            # amount of grepping this repo can see.
            #
            # SUPERSEDED AGAIN BY techne#814 — READ THIS BEFORE REASONING ABOUT CALLERS.
            # That hand-wired chain was the bug, not the design: `register_hooks` rewrites
            # settings.json wholesale, so the hand edit was deleted by every `techne init`
            # and one store drifted 883 observations behind. The segment now lives in
            # `register_hooks`' own SessionStart template, so THIS REPO ships the auto-run
            # caller. It is still not in the Python call graph — it is a shell string this
            # package writes — which is the durable lesson: check
            # platforms/claude_code/hooks.py, not just the import graph, before claiming
            # anything about who calls this in production.
            if tbl == "knowledge" and _purge_superseded_knowledge_vec(conn):
                conn.commit()
            id_col = "rowid" if tbl == "threads" else "id"
            extra_where = " AND t.superseded_by IS NULL" if tbl == "knowledge" else ""
            # techne#382: a knowledge row edited in place carries vec_stale=1, so
            # re-embed it alongside never-embedded rows. Knowledge is the only
            # table edited via a plain UPDATE (stable rowid → a stale embedding
            # lingers on a live row). observations are append-only; threads rotate
            # the rowid via INSERT OR REPLACE (old embedding orphaned on a dead
            # rowid, new summary re-embedded as missing), so neither needs a flag.
            missing = f"t.{id_col} NOT IN (SELECT rowid FROM {vec_tbl})"
            unembedded = f"({missing} OR t.vec_stale = 1)" if tbl == "knowledge" else missing
            # Loop until no un-embedded rows remain — one call must fully re-embed a
            # table, not just the newest `batch_size` rows. This matters most right
            # after a backend swap: _ensure_vec_backend_identity has just dropped and
            # recreated the table empty, so EVERY row needs (re-)embedding and a
            # single capped batch would silently leave older rows unembedded. Each
            # pass embeds rows NOT already in vec_tbl and inserts them, so the
            # NOT IN set strictly shrinks and the loop terminates.
            while True:
                rows = conn.execute(
                    f"""SELECT t.{id_col}, t.{content_col} FROM {tbl} t
                        WHERE {unembedded}{extra_where}
                        ORDER BY t.{id_col} DESC LIMIT ?""",
                    (batch_size,),
                ).fetchall()
                if not rows:
                    break
                ids, texts = zip(*rows)
                placeholders = ",".join("?" * len(ids))
                # A re-embedded (vec_stale) row is already present in vec_tbl; vec0
                # rejects a duplicate rowid, so clear any existing entry for these
                # ids before inserting. No-op for never-embedded rows. (techne#382)
                conn.execute(f"DELETE FROM {vec_tbl} WHERE rowid IN ({placeholders})", ids)
                embeddings = np.stack(list(model.embed(list(texts))))
                for row_id, emb in zip(ids, embeddings):
                    conn.execute(
                        f"INSERT INTO {vec_tbl}(rowid, embedding) VALUES (?, ?)",
                        (row_id, emb.astype("float32").tobytes()),
                    )
                if tbl == "knowledge":
                    # Embedding now matches content — clear the staleness flag so the
                    # row drops out of the re-embed set (loop-termination). techne#382
                    conn.execute(
                        f"UPDATE knowledge SET vec_stale = 0 WHERE id IN ({placeholders})",
                        ids,
                    )
                conn.commit()
                counts[tbl] += len(rows)

    finally:
        conn.close()

    return counts


def _purge_superseded_knowledge_vec(conn: sqlite3.Connection) -> int:
    """Delete ANN embeddings of superseded/merged knowledge rows (techne#168).

    search_vec filters superseded rows only AFTER the k-nearest cut, so a stale
    embedding occupies a top-k slot and starves a live result. Lives here (called
    from backfill_vectors, where sqlite_vec is loaded) rather than in supersede:
    connect() never loads sqlite_vec, so a plain supersede connection can't touch a
    vec0 table. No-op if knowledge_vec was never created. Returns rows removed."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
    ).fetchone()
    if not exists:
        return 0
    cur = conn.execute(
        "DELETE FROM knowledge_vec WHERE rowid IN "
        "(SELECT id FROM knowledge WHERE superseded_by IS NOT NULL)"
    )
    return cur.rowcount


# Each vec0 index and the table whose row ids it embeds. Used by the orphan sweep
# so the relationship is declared ONCE rather than restated at each call site.
_VEC_PARENT = {
    "threads_vec": ("threads", "rowid"),
    "observations_vec": ("observations", "id"),
    "knowledge_vec": ("knowledge", "id"),
}


def _purge_orphaned_vec(conn: sqlite3.Connection, vec_tbl: str) -> int:
    """Delete embeddings whose parent row is gone (techne#408). Returns rows removed.

    DISTINCT FROM `_purge_superseded_knowledge_vec`, and the difference is why
    that one cannot cover this: it deletes
    ``WHERE rowid IN (SELECT id FROM knowledge WHERE superseded_by IS NOT NULL)``
    — it selects FROM the parent, so a rowid with NO parent row behind it is
    never matched. `vectors:coverage` verified exactly that (purge removed 0 of
    2 in that state) and reports the count without failing, precisely because no
    remedy existed to clear it. This is that remedy.

    HOW THE ORPHANS ARISE, and it is not one bug:
    * threads — `thread_orchestrator` updates a summary via INSERT OR REPLACE,
      which ROTATES the rowid. The new summary is embedded fresh (it reads as
      missing) and the old rowid's embedding is stranded. Unbounded growth, one
      row per summary update.
    * knowledge — a HARD delete (`knowledge.delete`, the pattern cleanup) goes
      through a plain `connect()`, which cannot touch a vec0 table, so the
      embedding outlives the row.

    Recall stays CORRECT without this — `search_vec`'s `if row:` guard drops
    dead-rowid hits after the k-nearest cut — but each orphan can still occupy a
    top-k ANN slot before that filter runs, which is #168's failure shape bounded
    by `_VEC_SUPERSEDE_OVERFETCH`. So this is an index-hygiene fix, not a
    correctness one, and the PR says so rather than overclaiming.

    Lives here, like its sibling, because `backfill_vectors` is the one place
    sqlite_vec is loaded.
    """
    parent, id_col = _VEC_PARENT[vec_tbl]
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (vec_tbl,)
    ).fetchone()
    if not exists:
        return 0
    cur = conn.execute(f"DELETE FROM {vec_tbl} WHERE rowid NOT IN (SELECT {id_col} FROM {parent})")
    return cur.rowcount


_VEC_TABLE_NAMES = frozenset({"threads_vec", "observations_vec", "knowledge_vec"})

# extension_state row name for the embedding-backend identity stamp (techne#601/#602).
# Reuses the existing extension_state sidecar table (core/extension_state.py) rather
# than adding a bespoke meta table — it's already the "durable key-value sidecar"
# home for exactly this kind of small, versioned marker.
_VEC_BACKEND_STATE_NAME = "embedding_backend"


def _ensure_vec_table(conn: sqlite3.Connection, name: str, dim: int):
    # Defense in depth: name is f-string-interpolated into CREATE TABLE below.
    # Today callers only pass from the hardcoded list in backfill_vectors, but
    # this is a module-public-looking helper — assert at the SQL construction
    # site so a future caller can't smuggle in arbitrary identifiers.
    if name not in _VEC_TABLE_NAMES:
        raise ValueError(f"unknown vec table {name!r}; expected one of {sorted(_VEC_TABLE_NAMES)}")
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not exists:
        # dim comes from the active EmbeddingBackend (was hardcoded 384 for
        # fastembed/MiniLM pre-#601) — vec0's column width must match whatever
        # backend is about to write into it, see _ensure_vec_backend_identity
        # for what happens when the active backend's dim changes after tables
        # already exist at the old width.
        conn.execute(f"CREATE VIRTUAL TABLE {name} USING vec0(embedding float[{dim}])")


def _ensure_vec_backend_identity(conn: sqlite3.Connection, backend) -> bool:
    """Detect an embedding-backend swap and drop+restamp the vec tables if so.

    vec0 tables are fixed-width (`float[N]`) at CREATE time — there is no
    ALTER to widen/narrow an existing vec0 column, and even at a constant
    width, embeddings from two different models are not comparable (different
    semantic space entirely; L2 distance between a MiniLM vector and a
    potion vector is meaningless). So ANY backend or model change — not just
    a dim change — invalidates every existing embedding, not just the ones
    that no longer fit the column width.

    Identity is stamped as ``{"name": backend.name, "dim": backend.dim}`` in
    extension_state (see core/extension_state.py) under
    ``_VEC_BACKEND_STATE_NAME``. On mismatch (or no prior stamp — the vec
    tables may already exist from before this migration shipped, stamped
    implicitly as fastembed MiniLM/384 since that was the only backend that
    ever existed):
      1. DROP each of the three vec tables (allow-listed by _VEC_TABLE_NAMES —
         same guard _ensure_vec_table uses; no dynamic/user-controlled names).
      2. Restamp extension_state to the new identity.
      3. Return True so the caller knows a full re-embed is needed.

    Recreating the (now-empty) tables happens lazily in the normal
    _ensure_vec_table call right after this — an empty table's
    ``NOT IN (SELECT rowid FROM vec_tbl)`` selects every row, so
    backfill_vectors' existing batch loop re-embeds everything with zero
    special-casing needed there.

    A DB created before this migration shipped (no stamp yet) is treated as
    "stamped fastembed:sentence-transformers/all-MiniLM-L6-v2 @ 384" — the
    only backend/model that ever wrote those tables — so upgrading techne
    on an existing fastembed-embedded DB with no backend change is a no-op
    here (correctly does NOT trigger a needless re-embed).
    """
    from techne.core.extension_state import get_state, set_state

    state = get_state(conn, _VEC_BACKEND_STATE_NAME)
    if state is None:
        # No stamp yet. Any vec table already present must have been written
        # by the only backend that ever existed pre-#601 (fastembed MiniLM,
        # 384-d) — assume that identity rather than treating "unstamped" as
        # "different" (which would force a needless re-embed on every existing
        # install on first run after upgrading).
        any_vec_table_exists = any(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            for t in _VEC_TABLE_NAMES
        )
        assumed_name = "fastembed:sentence-transformers/all-MiniLM-L6-v2"
        assumed_dim = 384
        if any_vec_table_exists and (backend.name == assumed_name and backend.dim == assumed_dim):
            set_state(conn, _VEC_BACKEND_STATE_NAME, {"name": assumed_name, "dim": assumed_dim})
            return False
        if not any_vec_table_exists:
            # Fresh DB, nothing to migrate away from — just stamp current identity.
            set_state(conn, _VEC_BACKEND_STATE_NAME, {"name": backend.name, "dim": backend.dim})
            return False
        # Existing tables assumed-fastembed, but the active backend is something
        # else (e.g. this is a 3.14 upgrade where model2vec is now selected) —
        # fall through to the mismatch path below.
        state = {"name": assumed_name, "dim": assumed_dim}

    if state.get("name") == backend.name and state.get("dim") == backend.dim:
        return False  # unchanged — no-op, the common case on every call.

    # Mismatch: drop all three vec tables (allow-listed names only) so stale,
    # semantically-incomparable embeddings can never be queried against the
    # new backend, then restamp. Tables are recreated lazily by the
    # _ensure_vec_table call that follows in backfill_vectors.
    for name in sorted(_VEC_TABLE_NAMES):
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    set_state(conn, _VEC_BACKEND_STATE_NAME, {"name": backend.name, "dim": backend.dim})
    conn.commit()
    return True


def _fts_terms(query: str) -> list[str]:
    """Split ``query`` into FTS5-safe phrase-literal bodies (#74).

    Each returned string is ready to wrap in double quotes as an FTS5 phrase
    literal: embedded double-quotes are doubled (FTS5's only escape), and
    backslashes are turned into separators (FTS5 has no backslash escape — a
    lone ``\\`` raises ``fts5: syntax error`` even inside a phrase). Empty
    fragments are dropped.
    """
    cleaned = query.replace("\\", " ")
    return [t.replace('"', '""') for t in cleaned.split() if t.strip()]


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a query string for FTS5 by quoting *every* term as a phrase literal.

    Quoting unconditionally — rather than only when an operator char is present
    (the old behaviour) — means bare boolean keywords (``AND``/``OR``/``NOT``)
    and FTS5 operator chars (``- : ^ ~ *`` parens) are always read as literal
    text, never as operators with missing operands. That closes the #74 crash
    class: a search box can't raise ``fts5: syntax error`` on adversarial input.

    Semantics are unchanged for normal queries: a single-word phrase literal
    (``"database"``) matches identically to the bare term, and the space
    between terms is still FTS5 implicit-AND.
    """
    return " ".join(f'"{t}"' for t in _fts_terms(query))


def prepare_fts_query(query: str) -> str | None:
    """The single entry point for turning a raw user query into an FTS5 MATCH string.

    Returns the sanitized query, or ``None`` when it has NO searchable terms — either
    empty/whitespace, OR all-punctuation/escape input (e.g. a lone ``\\``) that
    ``_sanitize_fts_query`` strips to ``""``. ``MATCH ''`` raises ``fts5: syntax error``
    (#103), so EVERY FTS MATCH caller must funnel through this and treat ``None`` as
    "no results, skip the query". Centralizing both guards here is the #143 fix: the
    bare-``query.strip()``-then-sanitize shape had drifted across search/documents/
    patterns/codegraph, each missing the empty-AFTER-sanitize case (#168 fixed only the
    main recall path). One chokepoint, no drift.
    """
    if not query.strip():
        return None
    fts = _sanitize_fts_query(query)
    return fts or None


def has_direct_match(db_path: Path, query: str, table: SearchTable = "all") -> bool:
    """Does ANY row contain ALL of these terms? (techne#552 S5)

    THIS EXISTS BECAUSE NEITHER EXISTING SIGNAL COULD ANSWER IT, and both failures
    were measured rather than assumed:

    * **Distance cannot.** `search()` is hybrid (techne#401) and a nearest-neighbour
      query returns k rows however far away they are. On the live store the nonsense
      query "zzzqqxx" produced a NEARER top neighbour (0.961) than a real query about
      work done that same day (0.974). There is no cutoff between them, so any
      threshold would have been a number invented to make the feature look like it
      worked.
    * **`match_source` cannot.** It reports which channel a row came from, and
      `vec_first` fusion fills the page from the vector channel before appending
      FTS-only hits. Measured: "tier ladder recall archivist" returns 10 FTS rows on
      its own, yet 10/10 `vec` through the hybrid path — the lexical evidence is real
      and simply crowded out. `match_source` therefore measures fusion, not matching.

    So this asks the question directly: the implicit-AND FTS pass, with **no #70 OR
    fallback and no vector channel**. An empty result is a fact about the store — no
    row contains all these terms — which is exactly the "we have no direct experience
    of this" that S5 has to tell apart from "we know nothing" and "it is documented".

    Scoped by `table`, which defaults to `"all"` and therefore fans across every FTS
    table — callers wanting the S5 semantics must pass `table="knowledge"` explicitly.
    `_fts_match_rows` builds ordinary result rows: it ranks every match, then fetches
    the one best row (techne#1092).
    """
    fts_query = prepare_fts_query(query)
    if fts_query is None:
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        conn.row_factory = sqlite3.Row
        return bool(_fts_match_rows(conn, fts_query, table, 1))
    except sqlite3.Error:
        # A missing FTS table must not make recall claim "no experience" — that is a
        # louder, more misleading answer than declining to characterise the result.
        return False
    finally:
        conn.close()


def build_or_fts_query(query: str) -> str:
    """Build an OR-of-terms FTS5 query for the #70 empty-result fallback.

    Reuses :func:`_fts_terms` so the same phrase-literal escaping (quote
    doubling + backslash handling, #74) applies, then joins with ``OR``.

    Returns ``""`` when the query has fewer than two terms — a single-term
    query is identical under AND and OR, so the fallback would be a redundant
    re-query.
    """
    terms = _fts_terms(query)
    if len(terms) < 2:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def fts_and_or_split(
    query: str,
    run: Callable[[str], list],
    want: int | None = None,
    key: Callable[[dict], Hashable] = lambda r: r.get("id"),
    run_or: Callable[[str], list] | None = None,
) -> tuple[list, list]:
    """The #70 control flow (issue #559), returning the AND pass and the
    OR-of-terms top-up as TWO SEPARATE lists rather than one merged list
    (techne#1079 v2).

    THIS SPLIT EXISTS BECAUSE MERGING THEM WAS THE #1081/#1084 BUG. That PR
    appended OR rows to the AND rows and reranked the combined pool in one
    ``_rerank`` call; on the live store a short on-topic OR row's normalised
    BM25 + recency beat a long exact-match AND row's, so identifier queries
    that used to resolve at rank 1 dropped to rank 2 or 3, and the hybrid path
    lost `reflect-drain` entirely. It merged, reverted, and was reopened as
    #1079. Keeping the two pools apart lets ``search_fts`` rerank each block
    on its own terms and concatenate AND-first, so no OR row's score can ever
    place it ahead of an AND row — precedence is structural, not scored.

    Guard (#143): an empty or all-punctuation query returns ``([], [])``
    without calling ``run``. A single-term query never re-runs (AND == OR).

    The OR pass runs when the AND pass returned nothing, OR (when ``want`` is
    given) fewer than ``want`` rows. When the AND pass is completely empty
    there is nothing to dedup against, so the pre-#1079 behaviour applies
    as-is: the raw OR result is returned, uncalled ``key`` included — a
    caller whose rows are never dicts (as in the control-flow test) still
    works as long as it never exercises the top-up branch. When the AND pass
    is non-empty but short of ``want``, OR rows ARE deduped against the AND
    rows by ``key`` (default: the row's ``"id"``; a multi-table caller whose
    ids aren't globally unique MUST pass a wider key, e.g.
    ``lambda r: (r["source_table"], r["id"])``) — NOT against each other,
    since ``run`` is expected to return each row at most once per call, as
    every caller's SQL already does. ``want=None`` runs OR only on an empty
    AND pass, same as the pre-#1079 helper.

    ``run_or`` (default ``run``) runs the OR-of-terms query instead of ``run``
    — the AND pass always goes through ``run``. Search passes one that drops
    terms matching over half the table (techne#1093); codegraph and patterns
    don't.
    """
    fts = prepare_fts_query(query)
    if fts is None:
        return [], []
    if run_or is None:
        run_or = run
    and_rows = run(fts)
    if not and_rows:
        or_query = build_or_fts_query(query)
        return [], (run_or(or_query) if or_query else [])
    if want is None or len(and_rows) >= want:
        return and_rows, []
    or_query = build_or_fts_query(query)
    if not or_query:
        return and_rows, []
    seen = {key(r) for r in and_rows}
    topup = [r for r in run_or(or_query) if key(r) not in seen]
    return and_rows, topup


def fts_with_or_fallback(
    query: str,
    run: Callable[[str], list],
    want: int | None = None,
    key: Callable[[dict], Hashable] = lambda r: r.get("id"),
) -> list:
    """:func:`fts_and_or_split`, merged back into one AND-then-OR list and
    capped to ``want`` — and the cap is NOT a no-op (Prudence, #1088 review
    round 2 Nit 2 — an earlier draft of this sentence claimed it was, and was
    wrong). ``run``'s own SQL ``LIMIT`` is ``want`` for BOTH the AND pass and
    the OR pass, so when the AND pass returns even one row, the OR pass can
    still return up to ``want`` MORE — ``and_rows + topup`` can exceed
    ``want``, and the cap is what trims it back down to the caller's
    intended pool size, dropping real OR rows to do it.

    Safe for a caller that does NOT rerank its result afterward — AND rows
    precede OR rows in the returned list unconditionally, so precedence holds
    as long as the caller does not re-sort. A caller that DOES rerank
    (``search_fts``, ``search_knowledge_conn``) must call
    :func:`fts_and_or_split` directly and rerank each block separately
    instead — see that function's docstring for why. A caller that pools this
    function's OUTPUT with another list and reranks the pool (as
    ``federation.merge_and_rank`` used to) hits the same problem one hop
    further out — see its docstring.
    """
    and_rows, topup = fts_and_or_split(query, run, want=want, key=key)
    results = and_rows + topup
    return results[:want] if want is not None else results
