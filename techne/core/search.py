"""Search across all techne data layers — FTS5 and optional vector search."""

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from techne.core.database import connect


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
    Embeddings are lazily computed via fastembed (same model as #19 inference)
    and cached per candidate in ``_emb`` so no row is embedded twice per query.

    When fastembed is not installed the function returns candidates[:limit] in
    relevance order — identical to the pre-MMR behaviour (graceful degradation).

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
        model = _get_embedding_model()
    except Exception:
        # fastembed not installed or model unavailable — no-op.
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
) -> list[dict]:
    """Two-stage reranker: (1) recency × BM25 combined score, then (2) optional MMR.

    Stage 1 — BM25 from FTS5 is negative (more negative = better match).
    Normalise to [0..1] across the candidate set so it can be linearly combined
    with recency_score.  score = recency_weight * recency + bm25_weight * norm_bm25.

    Stage 2 — Greedy MMR selection from the stage-1 ordered pool.  When
    mmr_lambda=0 (default) MMR is skipped and pure relevance ordering is kept.
    When fastembed is unavailable MMR degrades silently to relevance ordering.
    """
    if not candidates:
        return candidates

    # BM25 is negative; most-negative = best match.
    raw_ranks = [r["rank"] for r in candidates]
    min_r, max_r = min(raw_ranks), max(raw_ranks)
    spread = max_r - min_r  # >= 0

    for r in candidates:
        # norm_bm25=1 for best BM25 match (most negative rank = min_r), 0 for worst.
        r["_norm_bm25"] = (max_r - r["rank"]) / spread if spread else 1.0

    for r in candidates:
        # Preferred order: event_at (real-world subject-event time) →
        # updated_at (last modification) → timestamp (ingestion time).
        ts = r.get("event_at") or r.get("updated_at") or r.get("timestamp")
        r["_recency"] = _recency_score(ts)
        r["_combined"] = recency_weight * r["_recency"] + bm25_weight * r["_norm_bm25"]

    candidates.sort(key=lambda x: -x["_combined"])

    # Stage 2: MMR diversity pass (no-op when mmr_lambda=0 or fastembed absent).
    result = _mmr_reorder(candidates, limit, mmr_lambda=mmr_lambda, relevance_key="_combined")

    # Clean scratch keys before returning.
    for r in result:
        r.pop("_norm_bm25", None)
        r.pop("_recency", None)
        r.pop("_combined", None)

    return result


def _fts_match_rows(
    conn: sqlite3.Connection,
    fts_query: str,
    table: SearchTable,
    candidate_limit: int,
) -> list[dict]:
    """Run the per-table FTS5 MATCH queries for ``fts_query`` and return raw
    candidate rows (pre-rerank). Shared by the implicit-AND pass and the #70
    OR fallback so both issue identical SQL.
    """
    results: list[dict] = []

    if table in ("threads", "all"):
        rows = conn.execute(
            """SELECT t.id, t.label, t.summary, t.resume_points, t.status,
                      t.session_id, t.updated_at,
                      bm25(threads_fts) AS rank
               FROM threads_fts f
               JOIN threads t ON t.rowid = f.rowid
               WHERE threads_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, candidate_limit),
        ).fetchall()
        for r in rows:
            results.append(
                {
                    "type": "thread",
                    "id": r["id"],
                    "label": r["label"],
                    "summary": r["summary"][:500],
                    "resume_points": r["resume_points"],
                    "status": r["status"],
                    "session_id": r["session_id"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                }
            )

    if table in ("observations", "all"):
        rows = conn.execute(
            """SELECT o.id, o.content, o.tool_name, o.session_id,
                      o.timestamp, o.event_at, o.tier,
                      bm25(observations_fts) AS rank
               FROM observations_fts f
               JOIN observations o ON o.id = f.rowid
               WHERE observations_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, candidate_limit),
        ).fetchall()
        for r in rows:
            results.append(
                {
                    "type": "observation",
                    "id": r["id"],
                    "content": r["content"][:500],
                    "tool_name": r["tool_name"],
                    "session_id": r["session_id"],
                    "timestamp": r["timestamp"],
                    "event_at": r["event_at"],
                    "tier": r["tier"],
                    "rank": r["rank"],
                }
            )

    if table in ("knowledge", "all"):
        rows = conn.execute(
            """SELECT k.id, k.type, k.content, k.tags, k.confidence,
                      k.source, k.updated_at,
                      bm25(knowledge_fts) AS rank
               FROM knowledge_fts f
               JOIN knowledge k ON k.id = f.rowid
               WHERE knowledge_fts MATCH ?
                 AND k.superseded_by IS NULL
               ORDER BY rank
               LIMIT ?""",
            (fts_query, candidate_limit),
        ).fetchall()
        for r in rows:
            results.append(
                {
                    "type": "knowledge",
                    "id": r["id"],
                    "knowledge_type": r["type"],
                    "content": r["content"][:500],
                    "tags": r["tags"],
                    "confidence": r["confidence"],
                    "source": r["source"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                }
            )

    # documents moved to librarian.db (db-per-domain, techne#99) — not searched
    # here; use the Librarian store (techne library search).

    return results


def search_fts(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 20,
    recency_weight: float = 0.3,
    bm25_weight: float = 0.7,
    expansion_ratio: int = 2,
    mmr_lambda: float = 0.0,
) -> list[dict]:
    """Full-text search across techne tables.

    Fetches ``expansion_ratio * limit`` BM25 candidates per table, then
    applies a two-stage reranker:
      Stage 1 — recency × BM25 combined score.
      Stage 2 — optional MMR diversity pass (skipped when mmr_lambda=0).

    Args:
        recency_weight: Weight for exponential-decay recency score (0..1).
        bm25_weight:    Weight for normalised BM25 score (0..1).
        expansion_ratio: Candidate pool = expansion_ratio * limit per table.
        mmr_lambda:      MMR diversity weight λ ∈ [0, 1].  0 = pure relevance
                         (default, matches legacy behaviour).  Requires
                         fastembed; degrades silently to relevance order if
                         the dep is not installed.
    """
    fts_query = prepare_fts_query(query)
    if fts_query is None:
        # Empty or all-punctuation query (e.g. "\") → no searchable terms; MATCH '' would
        # crash. Single guard shared with documents/patterns/codegraph (techne#143/#168).
        return []
    conn = connect(db_path)
    candidate_limit = limit * expansion_ratio

    try:
        results = _fts_match_rows(conn, fts_query, table, candidate_limit)

        # #70: bare space-separated terms are FTS5 implicit-AND, so a multi-word
        # or natural-language query (recall topics especially) returns nothing
        # unless one row contains *every* term. When the AND pass is empty, retry
        # with an OR-of-terms query; BM25 still ranks rows matching more (and
        # rarer) terms highest, so precision degrades gracefully.
        if not results:
            or_query = _build_or_fts_query(query)
            if or_query:
                results = _fts_match_rows(conn, or_query, table, candidate_limit)

        # For multi-table ("all"), sort combined pool first so BM25 ordering
        # is consistent before the reranker normalises across the whole set.
        if table == "all":
            results.sort(key=lambda x: x.get("rank", 0))

        results = _rerank(results, limit, recency_weight, bm25_weight, mmr_lambda=mmr_lambda)

    finally:
        conn.close()

    return results


# Knowledge is the only vec table whose rows can be superseded after embedding,
# and search_vec drops superseded rows AFTER the ANN k-nearest cut — so a stale
# embedding eats a top-k slot and starves a live result. backfill_vectors purges
# stale embeddings, but nothing auto-runs it in production, so this query-time
# over-fetch is the guarantee that actually fires: pull k×factor neighbours, then
# keep the closest `limit` LIVE ones. (techne#168)
_VEC_SUPERSEDE_OVERFETCH = 3


def search_vec(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 10,
) -> list[dict]:
    """Semantic vector search. Requires sqlite-vec and fastembed extras.

    Falls back gracefully if vector dependencies aren't installed.
    """
    try:
        import sqlite_vec
        from fastembed import TextEmbedding
    except ImportError:
        return []

    import numpy as np

    model = _get_embedding_model()
    q_emb = np.stack(list(model.embed([query])))[0]
    q_bytes = q_emb.astype("float32").tobytes()

    conn = connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        # Interpreter built without loadable-extension support — degrade to FTS.
        conn.close()
        return []

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
                                "type": "thread",
                                "id": thread[0],
                                "label": thread[1],
                                "summary": thread[2][:500],
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
                        "SELECT id, content, tool_name, session_id, timestamp FROM observations WHERE id = ?",
                        (row[0],),
                    ).fetchone()
                    if obs:
                        results.append(
                            {
                                "type": "observation",
                                "id": obs[0],
                                "content": obs[1][:500],
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
                live = 0
                for row in rows:
                    k = conn.execute(
                        """SELECT id, type, content, tags, confidence
                           FROM knowledge WHERE id = ? AND superseded_by IS NULL""",
                        (row[0],),
                    ).fetchone()
                    if k:
                        results.append(
                            {
                                "type": "knowledge",
                                "id": k[0],
                                "knowledge_type": k[1],
                                "content": k[2][:500],
                                "tags": k[3],
                                "confidence": k[4],
                                "distance": row[1],
                            }
                        )
                        live += 1
                        if live >= limit:
                            break

        # documents vector search lives with the Librarian store now (techne#99).

        results.sort(key=lambda x: x.get("distance", 999))
        return results[:limit]

    finally:
        conn.close()


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


def search(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 20,
    use_vectors: bool = True,
) -> list[dict]:
    """Hybrid search: BM25 (FTS5) + vector (cosine) fused via Reciprocal
    Rank Fusion.

    With ``use_vectors=False``: returns BM25-only results (unchanged
    legacy behavior).

    With ``use_vectors=True`` and vector deps installed: runs both
    channels and fuses with RRF. Items present in BOTH channels are
    boosted above items in just one (the desired property of fusion).
    Returned rows include:

      - ``match_source``: ``"fts"``, ``"vec"``, or ``"fts+vec"``
      - ``rrf_score``: the fused score (when fusion ran)

    When vec is requested but no vec results came back (extras not
    installed, vec tables empty, etc.), falls through to FTS-only —
    same as the legacy behavior.
    """
    fts_results = search_fts(db_path, query, table=table, limit=limit)

    if not use_vectors:
        return fts_results

    vec_results = search_vec(db_path, query, table=table, limit=limit)
    if not vec_results:
        return fts_results

    # Build (type, id) → result-row map and per-channel rank lists.
    # rank position is 1-based (1 = best); _rrf_fuse expects that shape.
    by_key: dict[tuple, dict] = {}
    fts_channel: list[tuple[tuple, int]] = []
    vec_channel: list[tuple[tuple, int]] = []

    for rank, r in enumerate(fts_results, start=1):
        key = (r["type"], r["id"])
        if key not in by_key:
            row = dict(r)  # copy — don't mutate the caller's rows
            row["match_source"] = "fts"
            by_key[key] = row
        fts_channel.append((key, rank))

    for rank, r in enumerate(vec_results, start=1):
        key = (r["type"], r["id"])
        if key not in by_key:
            row = dict(r)
            row["match_source"] = "vec"
            by_key[key] = row
        else:
            # Appeared in both channels — RRF will boost it; mark provenance.
            by_key[key]["match_source"] = "fts+vec"
        vec_channel.append((key, rank))

    fused = _rrf_fuse([fts_channel, vec_channel])

    out: list[dict] = []
    for key, score in fused[:limit]:
        row = by_key[key]
        row["rrf_score"] = score
        out.append(row)
    return out


def backfill_vectors(db_path: Path, batch_size: int = 100) -> dict[str, int]:
    """Embed un-embedded rows. Returns counts of newly embedded items."""
    try:
        import numpy as np
        import sqlite_vec
    except ImportError:
        return {}

    model = _get_embedding_model()
    conn = connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        # Interpreter built without loadable-extension support — degrade gracefully.
        conn.close()
        return {}

    counts = {"threads": 0, "observations": 0, "knowledge": 0}

    try:
        for tbl, content_col, vec_tbl in [
            ("threads", "summary", "threads_vec"),
            ("observations", "content", "observations_vec"),
            ("knowledge", "content", "knowledge_vec"),
        ]:
            _ensure_vec_table(conn, vec_tbl)
            # Superseded/merged knowledge must NOT have a live ANN embedding: search_vec
            # filters superseded rows only AFTER the k-nearest cut, so a stale embedding
            # occupies a top-k slot (techne#168). The audit suggested deleting from
            # _supersede_in_conn, but that connection can't touch a vec0 table (connect()
            # never loads sqlite_vec); backfill_vectors is the one place the extension is
            # loaded, so the index-side cleanup lives here: sweep existing stale embeddings
            # and skip superseded rows when embedding so they're never (re-)added.
            # NB: nothing auto-runs backfill_vectors in production today, so this cleanup
            # only fires on a manual/explicit backfill — the query-time over-fetch in
            # search_vec (_VEC_SUPERSEDE_OVERFETCH) is the always-on guard, sound up to
            # that factor of stale neighbours.
            if tbl == "knowledge" and _purge_superseded_knowledge_vec(conn):
                conn.commit()
            id_col = "rowid" if tbl == "threads" else "id"
            extra_where = " AND t.superseded_by IS NULL" if tbl == "knowledge" else ""
            rows = conn.execute(
                f"""SELECT t.{id_col}, t.{content_col} FROM {tbl} t
                    WHERE t.{id_col} NOT IN (SELECT rowid FROM {vec_tbl}){extra_where}
                    ORDER BY t.{id_col} DESC LIMIT ?""",
                (batch_size,),
            ).fetchall()

            if rows:
                ids, texts = zip(*rows)
                embeddings = np.stack(list(model.embed(list(texts))))
                for row_id, emb in zip(ids, embeddings):
                    conn.execute(
                        f"INSERT INTO {vec_tbl}(rowid, embedding) VALUES (?, ?)",
                        (row_id, emb.astype("float32").tobytes()),
                    )
                conn.commit()
                counts[tbl] = len(rows)

    finally:
        conn.close()

    return counts


_embedding_model = None


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding

        cache_dir = Path.home() / ".cache" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _embedding_model = TextEmbedding(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            cache_dir=str(cache_dir),
        )
    return _embedding_model


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


_VEC_TABLE_NAMES = frozenset({"threads_vec", "observations_vec", "knowledge_vec"})


def _ensure_vec_table(conn: sqlite3.Connection, name: str):
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
        conn.execute(f"CREATE VIRTUAL TABLE {name} USING vec0(embedding float[384])")


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


def _build_or_fts_query(query: str) -> str:
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
