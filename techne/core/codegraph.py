"""Code-graph data layer over codex.db (techne#89).

Symbols + edges CRUD, FTS over signatures, and graph queries (callers/callees/
blast-radius) as hand-rolled recursive CTEs — graph reachability WITHOUT a graph DB
(the v1 plan; graphqlite Cypher/algorithms is an optional upgrade later).

v1 edge model: edges are name-based (to_name = callee name string, always known;
to_symbol = resolved id, NULL until a future resolver pass). Traversal matches
callee symbols by name (prefer to_symbol join when set, else match symbols.name =
edges.to_name). plain_connect keeps codex.db code-only (no techne core tables).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from techne.core.stores import plain_connect


def add_symbol(
    db_path: Path,
    *,
    id: str,
    file: str,
    name: str,
    kind: str,
    signature: str | None = None,
    span: str | None = None,
    language: str | None = None,
    file_hash: str | None = None,
) -> str:
    """Upsert a symbol by id. Returns the id."""
    conn = plain_connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO symbols
               (id, file, name, kind, signature, span, language, file_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, file, name, kind, signature, span, language, file_hash),
        )
        conn.commit()
        return id
    finally:
        conn.close()


def add_edge(
    db_path: Path,
    from_symbol: str,
    to_name: str,
    kind: str,
    *,
    to_symbol: str | None = None,
    src_file_hash: str | None = None,
    resolution_status: str = "unresolved",
) -> bool:
    """Add a directed name-based edge (call|import|def-use). Idempotent (PK).

    to_name is the callee/imported name string — always required in v1.
    to_symbol is the resolved symbol id; NULL until a future resolver pass.
    Returns True if the row was newly inserted.
    """
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO edges
               (from_symbol, to_name, to_symbol, kind, src_file_hash, resolution_status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (from_symbol, to_name, to_symbol, kind, src_file_hash, resolution_status),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_symbol(db_path: Path, symbol_id: str) -> dict | None:
    conn = plain_connect(db_path)
    try:
        row = conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def search_symbols(db_path: Path, query: str, *, limit: int = 20) -> list[dict]:
    """FTS over symbol name + signature."""
    from techne.core.search import prepare_fts_query

    fts = prepare_fts_query(query)  # None when empty/all-punctuation → MATCH '' crash (#143)
    if fts is None:
        return []
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.id, s.file, s.name, s.kind, s.signature, s.file_hash,
                      bm25(symbols_fts) AS rank
               FROM symbols_fts f JOIN symbols s ON s.rowid = f.rowid
               WHERE symbols_fts MATCH ? ORDER BY rank LIMIT ?""",
            (fts, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def callers(db_path: Path, symbol_id: str) -> list[dict]:
    """Direct callers — which symbols call this one.

    Finds edges where to_symbol = symbol_id (resolved) OR
    to_name = symbol.name (unresolved name match).
    """
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.*
               FROM edges e
               JOIN symbols s ON s.id = e.from_symbol
               WHERE e.kind = 'call'
                 AND (
                     e.to_symbol = ?
                     OR (e.to_symbol IS NULL
                         AND e.to_name = (SELECT name FROM symbols WHERE id = ?))
                 )""",
            (symbol_id, symbol_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def callees(db_path: Path, symbol_id: str) -> list[dict]:
    """Direct callees — which symbols this one calls.

    Resolves targets by to_symbol when set, else by symbols.name = to_name.
    Multiple symbols may share a name; all are returned.
    """
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.*
               FROM edges e
               JOIN symbols s ON (
                   s.id = e.to_symbol
                   OR (e.to_symbol IS NULL AND s.name = e.to_name)
               )
               WHERE e.from_symbol = ?
                 AND e.kind = 'call'""",
            (symbol_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reachable(
    db_path: Path,
    symbol_id: str,
    *,
    direction: str = "out",
    kinds: list[str] | None = None,
    max_depth: int = 5,
    limit: int = 500,
) -> list[dict]:
    """Transitive reachability via a recursive CTE — the blast-radius query, dep-free.

    direction='out' follows from→to (dependencies / what this reaches).
    direction='in'  follows to→from (dependents / what reaches this).

    Name-based traversal: when to_symbol IS NULL, resolves callee by matching
    symbols.name = edges.to_name. Each result carries its minimum `depth` from
    the anchor. The anchor itself is excluded.

    UNION (not UNION ALL) deduplicates (id, depth) pairs, which naturally
    terminates cycles without requiring explicit visited-set tracking.
    """
    params: list = [symbol_id, max_depth]
    kind_filter = ""
    if kinds:
        kind_filter = " AND e.kind IN (%s)" % ", ".join("?" * len(kinds))
        params.extend(kinds)

    if direction == "out":
        # Step: from anchor id, follow edges out, resolve to_name → symbol id.
        step = f"""
            SELECT COALESCE(e.to_symbol, s2.id), r.depth + 1
            FROM reach r
            JOIN edges e ON e.from_symbol = r.id
            LEFT JOIN symbols s2 ON s2.name = e.to_name AND e.to_symbol IS NULL
            WHERE r.depth < ?{kind_filter}
              AND COALESCE(e.to_symbol, s2.id) IS NOT NULL
        """
    else:
        # Step: from anchor id, walk edges backwards, caller is from_symbol.
        step = f"""
            SELECT e.from_symbol, r.depth + 1
            FROM reach r
            JOIN symbols rs ON rs.id = r.id
            JOIN edges e ON (
                e.to_symbol = r.id
                OR (e.to_symbol IS NULL AND e.to_name = rs.name)
            )
            WHERE r.depth < ?{kind_filter}
        """

    params.extend([symbol_id, limit])

    sql = f"""
        WITH RECURSIVE reach(id, depth) AS (
            SELECT ?, 0
            UNION
            {step}
        )
        SELECT s.*, MIN(r.depth) AS depth
        FROM reach r JOIN symbols s ON s.id = r.id
        WHERE r.id != ?
        GROUP BY s.id ORDER BY depth, s.name LIMIT ?
    """
    conn = plain_connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    conn = plain_connect(db_path)
    try:
        return {
            "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "files": conn.execute("SELECT COUNT(DISTINCT file) FROM symbols").fetchone()[0],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Staleness annotation (techne#89 slice)
# ---------------------------------------------------------------------------

# Freshness states mirror anchor.py's spirit but scoped to codex symbols:
#   fresh   — file on disk, bytes match the indexed file_hash
#   stale   — file on disk, bytes differ (re-index this file)
#   missing — file not found on disk (deleted or moved)
#   unknown — can't determine: no repo_root for a relative path, or no file_hash


def _sha256_bytes(path: Path) -> str:
    """SHA-256 hex digest of *path*'s bytes. Caller must catch OSError."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def annotate_freshness(
    db_path: Path,
    symbols: list[dict],
    *,
    repo_root: Path | str | None = None,
) -> list[dict]:
    """Return a copy of *symbols* with a ``freshness`` key added to each dict.

    Freshness states:
      fresh   — file present, bytes match the indexed ``file_hash``
      stale   — file present, bytes differ (re-index recommended)
      missing — file not found on disk
      unknown — cannot determine (relative path + no repo_root, or no file_hash)

    ``repo_root`` can be passed explicitly or is read from ``codex_meta``
    (written by ``RepoSource.initial_walk()``). Symbols that store an absolute
    path in ``file`` are resolved without a root.

    File hashing is deduplicated: multiple symbols in the same file share one
    ``os.stat`` + one SHA-256 computation. This makes the call cheap (one stat +
    hash per DISTINCT file) even on large result sets.

    The function does not mutate the input dicts — it returns new dicts.
    """
    # Resolve repo_root from arg or codex_meta.
    if repo_root is None:
        if db_path is not None:
            from techne.core.codex import get_meta

            root_str = get_meta(db_path, "repo_root")
            repo_root = Path(root_str) if root_str else None
        # else: no db_path and no explicit repo_root — relative paths → unknown
    elif not isinstance(repo_root, Path):
        repo_root = Path(repo_root)

    # Per-file cache: file_str → freshness state string. Deduplicates disk access
    # for symbols sharing the same file. Keyed on file_str ALONE (not file_hash):
    # every symbol of a given file carries that file's one file_hash, so file_str
    # uniquely determines freshness — the hash is not part of the cache key.
    _file_freshness: dict[str, str] = {}

    def _classify_file(file_str: str, file_hash: str | None) -> str:
        """Return the freshness state for one file (may be cached)."""
        if file_str in _file_freshness:
            return _file_freshness[file_str]

        # Resolve to an absolute path.
        candidate = Path(file_str)
        if candidate.is_absolute():
            abs_path: Path | None = candidate
        elif repo_root is not None:
            abs_path = repo_root / candidate
        else:
            # Relative path with no repo_root — unknown.
            state = "unknown"
            _file_freshness[file_str] = state
            return state

        if not abs_path.exists():
            state = "missing"
        elif file_hash is None:
            # Can't compare — stored hash absent (older row).
            state = "unknown"
        else:
            try:
                current_hash = _sha256_bytes(abs_path)
                state = "fresh" if current_hash == file_hash else "stale"
            except OSError:
                state = "unknown"

        _file_freshness[file_str] = state
        return state

    result: list[dict] = []
    for sym in symbols:
        annotated = dict(sym)
        file_str = sym.get("file")
        if not file_str:
            annotated["freshness"] = "unknown"
        else:
            annotated["freshness"] = _classify_file(file_str, sym.get("file_hash"))
        result.append(annotated)

    return result
