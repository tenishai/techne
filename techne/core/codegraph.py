"""Code-graph data layer over codex.db (techne#89).

Symbols + edges CRUD, FTS over signatures, and graph queries (callers/callees/
blast-radius) as hand-rolled recursive CTEs — graph reachability WITHOUT a graph DB
(the v1 plan; graphqlite Cypher/algorithms is an optional upgrade later). plain_connect
keeps codex.db code-only (no techne core tables).
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
) -> str:
    """Upsert a symbol by id. Returns the id."""
    conn = plain_connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO symbols (id, file, name, kind, signature, span, language)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (id, file, name, kind, signature, span, language),
        )
        conn.commit()
        return id
    finally:
        conn.close()


def add_edge(db_path: Path, from_symbol: str, to_symbol: str, kind: str) -> bool:
    """Add a directed edge (call|import|def-use). Idempotent (PK). Returns True if new."""
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO edges (from_symbol, to_symbol, kind) VALUES (?, ?, ?)",
            (from_symbol, to_symbol, kind),
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


def _neighbors(db_path: Path, symbol_id: str, *, incoming: bool, kind: str | None) -> list[dict]:
    on, where = ("from_symbol", "to_symbol") if incoming else ("to_symbol", "from_symbol")
    sql = f"SELECT s.* FROM edges e JOIN symbols s ON s.id = e.{on} WHERE e.{where} = ?"
    params: list = [symbol_id]
    if kind:
        sql += " AND e.kind = ?"
        params.append(kind)
    conn = plain_connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def callers(db_path: Path, symbol_id: str) -> list[dict]:
    """Direct callers — who calls this symbol."""
    return _neighbors(db_path, symbol_id, incoming=True, kind="call")


def callees(db_path: Path, symbol_id: str) -> list[dict]:
    """Direct callees — what this symbol calls."""
    return _neighbors(db_path, symbol_id, incoming=False, kind="call")


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

    direction='out' follows from→to (dependencies / what this reaches);
    direction='in' follows to→from (dependents / what reaches this). Each result
    carries its minimum `depth` from the anchor. The anchor itself is excluded.
    """
    if direction == "out":
        step = "SELECT e.to_symbol, r.depth + 1 FROM edges e JOIN reach r ON e.from_symbol = r.id"
    else:
        step = "SELECT e.from_symbol, r.depth + 1 FROM edges e JOIN reach r ON e.to_symbol = r.id"

    params: list = [symbol_id, max_depth]
    kind_filter = ""
    if kinds:
        kind_filter = " AND e.kind IN (%s)" % ", ".join("?" * len(kinds))
        params.extend(kinds)
    params.extend([symbol_id, limit])

    sql = f"""
        WITH RECURSIVE reach(id, depth) AS (
            SELECT ?, 0
            UNION
            {step}
            WHERE r.depth < ?{kind_filter}
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
