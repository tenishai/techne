"""Search across all techne data layers — FTS5 and optional vector search."""

import sqlite3
from pathlib import Path
from typing import Literal

from techne.core.database import connect

SearchTable = Literal["threads", "observations", "knowledge", "all"]


def search_fts(
    db_path: Path,
    query: str,
    table: SearchTable = "all",
    limit: int = 20,
) -> list[dict]:
    """Full-text search across techne tables.

    Returns results ranked by FTS5 relevance (bm25).
    """
    if not query.strip():
        return []

    fts_query = _sanitize_fts_query(query)
    conn = connect(db_path)
    results = []

    try:
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
                (fts_query, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "type": "thread",
                    "id": r["id"],
                    "label": r["label"],
                    "summary": r["summary"][:500],
                    "resume_points": r["resume_points"],
                    "status": r["status"],
                    "session_id": r["session_id"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                })

        if table in ("observations", "all"):
            rows = conn.execute(
                """SELECT o.id, o.content, o.tool_name, o.session_id,
                          o.timestamp, o.tier,
                          bm25(observations_fts) AS rank
                   FROM observations_fts f
                   JOIN observations o ON o.id = f.rowid
                   WHERE observations_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "type": "observation",
                    "id": r["id"],
                    "content": r["content"][:500],
                    "tool_name": r["tool_name"],
                    "session_id": r["session_id"],
                    "timestamp": r["timestamp"],
                    "tier": r["tier"],
                    "rank": r["rank"],
                })

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
                (fts_query, limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "type": "knowledge",
                    "id": r["id"],
                    "knowledge_type": r["type"],
                    "content": r["content"][:500],
                    "tags": r["tags"],
                    "confidence": r["confidence"],
                    "source": r["source"],
                    "updated_at": r["updated_at"],
                    "rank": r["rank"],
                })

        if table == "all":
            results.sort(key=lambda x: x.get("rank", 0))
            results = results[:limit]

    finally:
        conn.close()

    return results


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
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

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
                        results.append({
                            "type": "thread",
                            "id": thread[0],
                            "label": thread[1],
                            "summary": thread[2][:500],
                            "status": thread[3],
                            "distance": row[1],
                        })

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
                        results.append({
                            "type": "observation",
                            "id": obs[0],
                            "content": obs[1][:500],
                            "tool_name": obs[2],
                            "session_id": obs[3],
                            "timestamp": obs[4],
                            "distance": row[1],
                        })

        if table in ("knowledge", "all"):
            _exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
            ).fetchone()
            if _exists:
                rows = conn.execute(
                    """SELECT v.rowid, v.distance
                       FROM knowledge_vec v
                       WHERE v.embedding MATCH ? AND k = ?""",
                    (q_bytes, limit),
                ).fetchall()
                for row in rows:
                    k = conn.execute(
                        """SELECT id, type, content, tags, confidence
                           FROM knowledge WHERE id = ? AND superseded_by IS NULL""",
                        (row[0],),
                    ).fetchone()
                    if k:
                        results.append({
                            "type": "knowledge",
                            "id": k[0],
                            "knowledge_type": k[1],
                            "content": k[2][:500],
                            "tags": k[3],
                            "confidence": k[4],
                            "distance": row[1],
                        })

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
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    counts = {"threads": 0, "observations": 0, "knowledge": 0}

    try:
        for tbl, content_col, vec_tbl in [
            ("threads", "summary", "threads_vec"),
            ("observations", "content", "observations_vec"),
            ("knowledge", "content", "knowledge_vec"),
        ]:
            _ensure_vec_table(conn, vec_tbl)
            id_col = "rowid" if tbl == "threads" else "id"
            rows = conn.execute(
                f"""SELECT t.{id_col}, t.{content_col} FROM {tbl} t
                    WHERE t.{id_col} NOT IN (SELECT rowid FROM {vec_tbl})
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


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a query string for FTS5. Wraps bare terms in quotes if needed.

    `-` is FTS5's NOT operator; bare queries like `foo -bar` either throw a
    syntax error or silently exclude `bar` from results depending on
    placement. Treat it the same as the other operator chars: detection
    triggers per-term quoting so the query is interpreted as literal phrases.
    """
    special = set('(){}[]"*:^~-')
    if any(c in query for c in special):
        terms = query.split()
        # Escape embedded double-quotes by doubling them (FTS5 phrase literal rule)
        return " ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in terms if t.strip())
    return query
