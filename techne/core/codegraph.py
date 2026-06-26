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
    from techne.core.search import _sanitize_fts_query

    if not query.strip():  # FTS5 MATCH '' is a syntax error (#103)
        return []
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.id, s.file, s.name, s.kind, s.signature, bm25(symbols_fts) AS rank
               FROM symbols_fts f JOIN symbols s ON s.rowid = f.rowid
               WHERE symbols_fts MATCH ? ORDER BY rank LIMIT ?""",
            (_sanitize_fts_query(query), limit),
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
