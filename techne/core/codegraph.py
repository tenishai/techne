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

# Symbols under tests/fixtures/** are deliberately-isolated sample code used to
# exercise the indexer. Because v1 edges are name-based (techne#325), their common
# names (execute/run/add/…) collide with real callees and inject phantom nodes into
# a blast radius. Excluded by default from callers/callees/reachable results;
# opt back in with exclude_fixtures=False.
#
# Two known limitations of this heuristic (both dissolve once the LSP resolver in
# techne#319 gives edges real to_symbol ids, ending name-collision traversal):
#   1. PROJECTION-ONLY. The filter drops fixture rows from the final result, but the
#      recursive traversal still walks THROUGH fixture nodes. So a real symbol
#      reachable *only* via a fixture's collision edge (anchor→fixture→real) still
#      surfaces — as an orphan, minus the fixture breadcrumb that flagged it as
#      noise. Strictly better than before (the fixture node itself is gone), but
#      deep leaks persist. Leaf collisions (the common case) are fully filtered.
#   2. PATH-CONVENTION-LOCKED. Matches this repo's POSIX `tests/fixtures/` layout;
#      a `tests\fixtures\` (Windows) or `test/fixtures` (singular) path would leak.
#: An `external` edge is one `import_resolve` RULED OUT of this repo: the callee
#: is bound to a module this index does not contain, so no symbol here can be it
#: (techne#679). Name inference must therefore not arm on it — matching `e.to_name`
#: against a local symbol would invent precisely the hop the status proves
#: impossible. Measured on the index of the day this clause landed (b539c6d):
#: 1 of 599 external edges name-matched a local symbol. On the current index it
#: is 0 of 593 — later narrowings moved both the count and that edge — so the
#: cost is smaller than the original note implies and the contradiction is not:
#: an edge ruled OUT of this repo must not name-match into it. The 599 is kept
#: DATED rather than refreshed, because it is the measurement that justified the
#: clause and a re-measurement after the fix cannot re-justify it.
#: Declared ONCE and interpolated into all four fallbacks, because four copies of
#: a rule is how they drift (Prudence r2 nit 3).
_NOT_EXTERNAL = "e.resolution_status != 'external'"

_FIXTURE_LIKE = "%tests/fixtures/%"


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


class SymbolResolutionError(ValueError):
    """A user-supplied symbol argument could not be resolved to exactly one
    symbol id. ``candidates`` holds the matches for the ambiguous case."""

    def __init__(self, message: str, candidates: list[dict] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def resolve_symbol_id(db_path: Path, arg: str) -> str:
    """Resolve a symbol argument to a concrete symbol id, or raise.

    Accepts either a full symbol id (``path::name::kind::line``) or a bare
    name:

    - **full id** → returned if it exists in this repo's graph, else
      ``SymbolResolutionError``.
    - **bare name** → the single symbol with that name is returned; if several
      share the name it's ``SymbolResolutionError`` with ``candidates``; if
      none, ``SymbolResolutionError``.

    This exists so ``callers`` / ``blast-radius`` fail loudly on an
    unresolvable argument instead of returning an empty result that is
    indistinguishable from "no results" (techne#300). Bare-name convenience
    means callers no longer have to copy a full id out of ``search`` for the
    common unambiguous case.
    """
    # Heuristic: a full symbol id contains "::" (file::name::kind::line), and a
    # bare name cannot — the indexer handles only Python/TypeScript, whose
    # identifiers are "::"-free. If a "::"-using language (Rust/C++/Ruby, e.g.
    # Foo::bar) is ever indexed, tighten this to a full-id shape check so such a
    # name isn't misrouted into the id branch. See techne#300 review.
    if "::" in arg:
        if get_symbol(db_path, arg) is None:
            raise SymbolResolutionError(
                f"no symbol with id {arg!r} in this repo's graph "
                "(is the repo indexed? run `codex index`)"
            )
        return arg
    # Bare name — exact-name match (NOT the FTS search, which is fuzzy).
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, file, name, kind FROM symbols WHERE name = ? ORDER BY id",
            (arg,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise SymbolResolutionError(
            f"no symbol named {arg!r} in this repo's graph; try `codex search {arg}`"
        )
    if len(rows) > 1:
        raise SymbolResolutionError(
            f"{arg!r} is ambiguous — {len(rows)} symbols share that name; "
            "pass a full symbol id (path::name::kind::line):",
            candidates=[dict(r) for r in rows],
        )
    return rows[0]["id"]


def symbol_at(db_path: Path, file: str, line: int) -> dict | None:
    """Return the innermost symbol whose span contains *line* in *file*, or None.

    *file* must match ``symbols.file`` as stored (repo-relative). Spans are
    ``"start:end"`` 1-indexed line ranges (techne#89). "Innermost" = the symbol
    with the smallest span still containing the line, so a function wins over the
    class or module enclosing it — the symbol the agent actually edited (the
    edit-rail's anchor, techne#335). Rows with an absent/malformed span are
    skipped rather than raising.
    """
    conn = plain_connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM symbols WHERE file = ?", (file,)).fetchall()
    finally:
        conn.close()
    best: dict | None = None
    best_width: int | None = None
    for r in rows:
        span = r["span"]
        if not span or ":" not in span:
            continue
        try:
            start_s, end_s = span.split(":", 1)
            start, end = int(start_s), int(end_s)
        except ValueError:
            continue
        if start <= line <= end:
            width = end - start
            if best_width is None or width < best_width:
                best, best_width = dict(r), width
    return best


def search_symbols(db_path: Path, query: str, *, limit: int = 20) -> list[dict]:
    """FTS over symbol name + signature.

    Multi-term queries use the #70 AND-then-OR pattern (techne#392): an
    implicit-AND pass first — a symbol matching every term ranks highest —
    then an OR-of-terms pass tops up the pool when AND falls short of
    `limit`, appending OR rows not already present (techne#1079). Without the
    fallback a multi-term query (e.g. "PolicyStore ScopeGrant has_scope")
    ANDs into a single symbol's name+signature and either matches nothing or
    matches one row that crowds out every other match — either way the
    result reads as "the index is stale/missing" and drives a needless cold
    source-read. The fallback returns the ranked union of per-term matches
    instead. This function does no reranking, so AND-then-OR ordering is
    unconditional — unlike `search_fts`, there's no score that could put an
    OR row ahead of an AND row here.
    """
    from techne.core.search import fts_with_or_fallback, prepare_fts_query

    if prepare_fts_query(query) is None:  # nothing searchable: don't open the db (#143)
        return []
    conn = plain_connect(db_path)
    try:

        def _match(match_str: str) -> list[dict]:
            rows = conn.execute(
                """SELECT s.id, s.file, s.name, s.kind, s.signature, s.file_hash,
                          bm25(symbols_fts) AS rank
                   FROM symbols_fts f JOIN symbols s ON s.rowid = f.rowid
                   WHERE symbols_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match_str, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        # The AND pass, topped up by OR-of-terms when it falls short of `limit`
        # (#70, #392, techne#1079). Rows key on `id` — this table alone.
        return fts_with_or_fallback(query, _match, want=limit, key=lambda r: r["id"])
    finally:
        conn.close()


def callers(db_path: Path, symbol_id: str, *, exclude_fixtures: bool = True,
    include_inferred: bool = True) -> list[dict]:
    """Direct callers — which symbols call this one.

    Finds edges where to_symbol = symbol_id (resolved) OR
    to_name = symbol.name (unresolved name match).

    exclude_fixtures drops tests/fixtures/** noise by default (techne#325).
    
    THE NAME FALLBACK CAN INVENT A CALL, AND EVERY ROW NOW SAYS WHICH KIND IT IS
    (techne#606). When an edge has ``to_symbol IS NULL`` the target is matched by
    bare ``to_name`` against ``symbols.name`` — so a call into code we deliberately
    do not index (a vendored import) is silently reattributed to whatever local
    symbol happens to share the name. Reproduced on a FRESH index: `app/main.py`
    doing ``from aiohttp.web import get`` makes ``callers("app/util.py::get")``
    return ``handler``, which never called it.

    Each row carries ``resolution``: ``"exact"`` (matched on ``to_symbol``) or
    ``"name-inferred"`` (matched on the bare name, and therefore a guess). Pass
    ``include_inferred=False`` to get only edges the graph can actually prove.

    THE DEFAULT STAYS PERMISSIVE, and that is a deliberate, uncomfortable choice.
    Measured on this repo's live index 2026-08-19: 19,345 call edges, ALL with
    ``to_symbol IS NULL``, of which 11,114 (57%) name-match some symbol — and 99
    ambiguous names account for 4,177 of them. Defaulting to ``False`` would empty
    the call graph for every store that has never run a resolver pass, which is all
    of them today. So the fix here is HONESTY, not suppression: the caller can now
    tell a proven edge from a guessed one and choose.

    WHAT WOULD ACTUALLY FIX IT is qualifying the fallback by import context — and
    it is NOT achievable in the query layer, because the BINDING is not recorded.
    An import edge stores one bare string in ``to_name`` and nothing else: for
    ``import os`` that is the module `os`, and for ``from aiohttp.web import get``
    it is the name `get`. There is no column separating the two cases and no
    record of the SOURCE MODULE a from-imported name came from — so nothing here
    can know that ``get`` arrived from outside the repo rather than being defined
    in it. Closing that needs the INDEXER to record name→module bindings.
    Measured on the live index, not assumed: 3,049 import edges, 852 distinct
    ``to_name`` values mixing modules and imported names, 0 with ``to_symbol``
    set. Follow-up filed as techne#679.
    """
    fixture_clause = " AND s.file NOT LIKE ?" if exclude_fixtures else ""
    params: list = [symbol_id, symbol_id]
    if exclude_fixtures:
        params.append(_FIXTURE_LIKE)
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT s.*,
                      CASE WHEN e.to_symbol = ? THEN 'exact' ELSE 'name-inferred' END
                        AS resolution
               FROM edges e
               JOIN symbols s ON s.id = e.from_symbol
               WHERE e.kind = 'call'
                 AND (
                     e.to_symbol = ?
                     OR (e.to_symbol IS NULL AND {_NOT_EXTERNAL}
                         AND e.to_name = (SELECT name FROM symbols WHERE id = ?))
                 ){fixture_clause}""",
            [symbol_id, *params],
        ).fetchall()
        out = [dict(r) for r in rows]
        return out if include_inferred else [r for r in out if r["resolution"] == "exact"]
    finally:
        conn.close()


def callees(db_path: Path, symbol_id: str, *, exclude_fixtures: bool = True,
    include_inferred: bool = True) -> list[dict]:
    """Direct callees — which symbols this one calls.

    Resolves targets by to_symbol when set, else by symbols.name = to_name.
    Multiple symbols may share a name; all are returned.

    exclude_fixtures drops tests/fixtures/** noise by default (techne#325).
    
    THE NAME FALLBACK CAN INVENT A CALL, AND EVERY ROW NOW SAYS WHICH KIND IT IS
    (techne#606). When an edge has ``to_symbol IS NULL`` the target is matched by
    bare ``to_name`` against ``symbols.name`` — so a call into code we deliberately
    do not index (a vendored import) is silently reattributed to whatever local
    symbol happens to share the name. Reproduced on a FRESH index: `app/main.py`
    doing ``from aiohttp.web import get`` makes ``callers("app/util.py::get")``
    return ``handler``, which never called it.

    Each row carries ``resolution``: ``"exact"`` (matched on ``to_symbol``) or
    ``"name-inferred"`` (matched on the bare name, and therefore a guess). Pass
    ``include_inferred=False`` to get only edges the graph can actually prove.

    THE DEFAULT STAYS PERMISSIVE, and that is a deliberate, uncomfortable choice.
    Measured on this repo's live index 2026-08-19: 19,345 call edges, ALL with
    ``to_symbol IS NULL``, of which 11,114 (57%) name-match some symbol — and 99
    ambiguous names account for 4,177 of them. Defaulting to ``False`` would empty
    the call graph for every store that has never run a resolver pass, which is all
    of them today. So the fix here is HONESTY, not suppression: the caller can now
    tell a proven edge from a guessed one and choose.

    WHAT WOULD ACTUALLY FIX IT is qualifying the fallback by import context — and
    it is NOT achievable in the query layer, because the BINDING is not recorded.
    An import edge stores one bare string in ``to_name`` and nothing else: for
    ``import os`` that is the module `os`, and for ``from aiohttp.web import get``
    it is the name `get`. There is no column separating the two cases and no
    record of the SOURCE MODULE a from-imported name came from — so nothing here
    can know that ``get`` arrived from outside the repo rather than being defined
    in it. Closing that needs the INDEXER to record name→module bindings.
    Measured on the live index, not assumed: 3,049 import edges, 852 distinct
    ``to_name`` values mixing modules and imported names, 0 with ``to_symbol``
    set. Follow-up filed as techne#679.
    """
    fixture_clause = " AND s.file NOT LIKE ?" if exclude_fixtures else ""
    params: list = [symbol_id]
    if exclude_fixtures:
        params.append(_FIXTURE_LIKE)
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT s.*,
                      CASE WHEN s.id = e.to_symbol THEN 'exact' ELSE 'name-inferred' END
                        AS resolution
               FROM edges e
               JOIN symbols s ON (
                   s.id = e.to_symbol
                   OR (e.to_symbol IS NULL AND {_NOT_EXTERNAL} AND s.name = e.to_name)
               )
               WHERE e.from_symbol = ?
                 AND e.kind = 'call'{fixture_clause}""",
            params,
        ).fetchall()
        out = [dict(r) for r in rows]
        return out if include_inferred else [r for r in out if r["resolution"] == "exact"]
    finally:
        conn.close()


def reachable(
    db_path: Path,
    symbol_id: str,
    *,
    direction: str = "in",
    kinds: list[str] | None = None,
    max_depth: int = 5,
    limit: int = 500,
    exclude_fixtures: bool = True,
    include_inferred: bool = True,
) -> list[dict]:
    """Transitive reachability via a recursive CTE, dep-free.

    direction='in'  follows to→from (dependents / what reaches this) — the
      "what breaks if I change this" blast radius, and the default (techne#325).
    direction='out' follows from→to (dependencies / what this reaches).

    Name-based traversal: when to_symbol IS NULL, resolves callee by matching
    symbols.name = edges.to_name. Each result carries its minimum `depth` from
    the anchor. The anchor itself is excluded.

    exclude_fixtures drops tests/fixtures/** phantom nodes (name collisions in
    the v1 name-based edge model) from the RESULT by default (techne#325). This
    is a projection-level filter — see _FIXTURE_LIKE for its two known limits
    (deep leaks through fixtures; POSIX-path-locked), both retired by techne#319.

    UNION (not UNION ALL) deduplicates (id, depth) pairs, which naturally
    terminates cycles without requiring explicit visited-set tracking.

    include_inferred=False WALKS ONLY PROVEN EDGES (techne#606). The name-based
    traversal above can invent a hop: an edge with to_symbol IS NULL matches any
    symbol sharing the bare name, so a call into unindexed vendored code gets
    reattributed to an unrelated local function. Setting this False restricts the
    recursive step to edges resolved on to_symbol, so every node returned is
    reached by an ALL-EXACT path.

    Unlike callers()/callees(), rows here carry NO ``resolution`` field, and that
    is deliberate: a node can be reached by a proven path AND a guessed one at the
    same depth, so a per-row label would have to pick one and would lie about the
    other. The honest control at this layer is the path filter, not a tag.
    """
    params: list = [symbol_id, max_depth]
    # techne#606: drop the name-fallback arm entirely when only proven edges are
    # wanted. '0' (never true) is used rather than removing the OR arm so the two
    # SQL shapes stay textually parallel and diffable.
    inferred_clause_out = "" if include_inferred else " AND e.to_symbol IS NOT NULL"
    inferred_join_in = f"e.to_symbol IS NULL AND {_NOT_EXTERNAL}" if include_inferred else "0"
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
                                        AND {_NOT_EXTERNAL}
            WHERE r.depth < ?{kind_filter}
              AND COALESCE(e.to_symbol, s2.id) IS NOT NULL{inferred_clause_out}
        """
    else:
        # Step: from anchor id, walk edges backwards, caller is from_symbol.
        step = f"""
            SELECT e.from_symbol, r.depth + 1
            FROM reach r
            JOIN symbols rs ON rs.id = r.id
            JOIN edges e ON (
                e.to_symbol = r.id
                OR ({inferred_join_in} AND e.to_name = rs.name)
            )
            WHERE r.depth < ?{kind_filter}
        """

    fixture_clause = " AND s.file NOT LIKE ?" if exclude_fixtures else ""
    params.append(symbol_id)
    if exclude_fixtures:
        params.append(_FIXTURE_LIKE)
    params.append(limit)

    sql = f"""
        WITH RECURSIVE reach(id, depth) AS (
            SELECT ?, 0
            UNION
            {step}
        )
        SELECT s.*, MIN(r.depth) AS depth
        FROM reach r JOIN symbols s ON s.id = r.id
        WHERE r.id != ?{fixture_clause}
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


def index_freshness(db_path: Path, *, repo_root: Path | str | None = None) -> dict:
    """Freshness of the INDEX ITSELF, independent of any query's results.

    ``annotate_freshness`` can only flag files a query actually returned rows
    from. That leaves the one case that most needs a warning with none: a symbol
    ADDED since the last index has no row, so there is nothing to hang a flag on,
    and the query returns an empty list that reads as authoritative absence
    (techne#595). Measured instance — ``recall_attribution`` shipped 2026-08-13,
    was absent from the index three days later, and ``codex_search`` for it
    returned ``{"results": [], "count": 0}`` with no notice, while a SIBLING
    symbol from the same stale file was correctly flagged ``stale`` in the same
    session. The index knew; the query surface did not say.

    So this asks the question absence cannot: how much of the index is behind
    the working tree? Returns ``{files, fresh, stale, missing, unknown}``.

    Implemented by handing every DISTINCT (file, file_hash) pair to
    ``annotate_freshness`` rather than re-deriving the states here — a second
    classifier would be a second definition of "stale" and would drift from the
    one callers already see on their rows.

    COST, measured on techne (228 indexed files): ~21 ms for the full scan, one
    stat + one SHA-256 per distinct file. Cheap enough for the zero-result path,
    where the alternative is the caller doing a cold source read of the whole
    subsystem — but it is not free, so callers should reach for it when a result
    needs qualifying, not on every call.
    """
    conn = plain_connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT file, file_hash FROM symbols").fetchall()
    finally:
        conn.close()

    pseudo = [{"file": r["file"], "file_hash": r["file_hash"]} for r in rows]
    annotated = annotate_freshness(db_path, pseudo, repo_root=repo_root)

    counts = {"fresh": 0, "stale": 0, "missing": 0, "unknown": 0}
    for row in annotated:
        state = row.get("freshness", "unknown")
        counts[state] = counts.get(state, 0) + 1

    unindexed, coverage = _unindexed_count(rows, repo_root)
    return {"files": len(annotated), **counts, "unindexed": unindexed, "coverage": coverage}


def zero_result_notice(idx: dict) -> str:
    """Describe how much a zero result can be trusted (techne#595).

    ORDERED SO THE STRONGEST DOUBT WINS, and every branch reports the FULL
    breakdown rather than one ratio — "1 of 6 stale" implies the other five were
    assessed and were fine, which is false when they were never assessed at all.

    "Genuine absence" is asserted ONLY when the disk was enumerated, nothing is
    unindexed, and every indexed file is fresh. The first cut of this fix said it
    whenever the indexed files happened to be fresh — which certified absence
    using evidence that could not see a brand-new module, and an index of ONE
    untouched file declared itself current while the symbol sat beside it unseen.
    That is worse than the silence it replaced: it converts "no information" into
    a confident wrong answer.
    """
    files, stale, missing = idx["files"], idx["stale"], idx["missing"]
    unknown, unindexed, coverage = idx["unknown"], idx["unindexed"], idx["coverage"]
    tally = (
        f"index: {files} file(s) — {idx['fresh']} fresh, {stale} stale, "
        f"{missing} missing, {unknown} unclassifiable"
    )
    if coverage == "complete":
        tally += f", {unindexed} on disk never indexed"

    if files == 0:
        # An EMPTY index issuing a clean bill of health is the strongest possible
        # statement of trust from the weakest possible evidence. Reachable through
        # the shipping CLI: codex_db_for_repo creates the db before initial_walk,
        # so a docs-only repo, a Go/Rust repo, or an install without the `codex`
        # extra all leave a db with zero symbols.
        return (
            "NO MATCH, and THE INDEX IS EMPTY (0 files) — this says nothing about "
            "whether the symbol exists. Run `techne codex index <repo>`."
        )
    if coverage != "complete":
        return (
            f"NO MATCH, but the disk could not be enumerated, so files may exist that "
            f"were never indexed. Treat this as UNKNOWN, not absent. {tally}."
        )
    if unindexed:
        return (
            f"NO MATCH — but {unindexed} source file(s) on disk have never been indexed, "
            "so a symbol in a NEW module cannot appear here. This is likely a blind spot "
            f"rather than an absence. Re-run `techne codex index`. {tally}."
        )
    if stale or missing:
        return (
            f"NO MATCH — but {stale + missing} indexed file(s) are stale or missing, so a "
            "symbol added or moved since the last index cannot appear here. Likely a blind "
            f"spot rather than an absence. Re-run `techne codex index`. {tally}."
        )
    if unknown:
        return (
            f"NO MATCH, and {unknown} file(s) could not be classified, so the index's "
            f"freshness is not established. Treat this as unknown rather than absent. {tally}."
        )
    return (
        f"NO MATCH, and the index is complete and current — every file on disk is indexed "
        f"and unchanged. This is genuine absence. {tally}."
    )


def _unindexed_count(indexed_rows, repo_root) -> tuple[int | None, str]:
    """Source files ON DISK that the index has never seen. ``(count, coverage)``.

    THE HALF THE FRESHNESS CHECK STRUCTURALLY CANNOT SEE, and the reason it must
    exist: comparing indexed files against disk answers "did what I indexed
    change?" It cannot answer "is there something I never indexed?" — and a
    symbol added since the last index arrives in a NEW module at least as often
    as in an edited one. Without this, an index holding one untouched file
    reports itself perfectly fresh while a brand-new module sits beside it
    unseen, and any claim of "genuine absence" built on that is a confident lie
    (techne#595 round 1).

    Uses the INDEXER'S OWN discovery (`techne.codex.source._walk_repo`) rather
    than a local notion of "source file". A second definition of which files
    belong in the graph would drift from the one that populates it, and this
    function exists precisely to catch drift. That module's grammar imports are
    lazy, so this works without the optional `techne[codex]` extra — verified by
    importing it with `tree_sitter` blocked.

    ``coverage`` is the honest part:
      complete — the disk was enumerated; the counts below are a real answer.
      unknown  — no repo_root, the walker is unavailable, a directory could not
                 be read, or a symlinked directory was not followed. WE CANNOT
                 SEE THE DISK, so callers must not certify absence on this result.

    FOUR ROUTES REACH "genuine absence" WRONGLY, and all four are named here
    because two of them used to be undocumented (techne#599):
      1. An unreadable directory — now downgrades to ``unknown``.
      2. A symlinked directory, never descended into — now downgrades too.
      3. A genuinely NEW TOP-LEVEL component: no indexed sibling at that level,
         so the scoping rule below has no signal. STILL UNFIXED and accepted —
         it is the stated cost of the scoping, narrower than what it replaced.
      4. A root-only index plus any subdirectory: follows from the "." component
         rule, same accepted cost as 3.
    """
    if repo_root is None:
        return None, "unknown"
    try:
        from techne.codex.source import _EXT_LANGUAGE, _walk_repo
    except Exception:  # noqa: BLE001 — optional extra absent / import error
        return None, "unknown"

    root = Path(repo_root)
    if not root.is_dir():
        return None, "unknown"

    indexed = {str(r["file"]) for r in indexed_rows}

    # SCOPED BY TOP-LEVEL COMPONENT, and the scoping is not timidity — an
    # unscoped count is worse than none. HISTORICALLY (before techne#604 taught
    # `_walk_repo` to skip `site-packages`) the walk on this repo yielded 10,282
    # files, 10,042 of them inside `.venv-ci-3.12` / `.venv-ci-3.13`, and an
    # unscoped count reported "10,055 files never indexed" — a warning that fires
    # that often is one the reader learns to skip, i.e. back to the silence this
    # fix exists to remove, wearing a louder coat.
    #
    # CORRECTED (techne#600): an earlier version of this comment said such a
    # warning "fires on every repo with a venv", which is FALSE and worth
    # correcting because the reason is what a future reader acts on. `_SKIP_DIRS`
    # DOES exclude `venv`, `.venv`, `env`, `.env`. What it misses is the
    # `.venv-ci-*` spelling this repo uses for its CI environments — i.e.
    # SPELLINGS OUTSIDE the skip list, not venvs as a category. Someone trusting
    # the old sentence would have concluded `_SKIP_DIRS` was useless.
    #
    # THOSE NUMBERS ARE NOW 242 AND 2: #604 removed the noise at its source, so
    # the scoping is no longer load-bearing against VENVS specifically. It is kept
    # because it is load-bearing against the general case — any directory tree the
    # index does not cover, of which a venv was merely the loudest example, and
    # `_SKIP_DIRS` cannot enumerate every one of those.
    #
    # PER-DIRECTORY WAS THE WRONG SCOPE AND SHIPPED A CRITICAL (#595 r2). Asking
    # "does this file's own directory already hold indexed files?" is blind to
    # any NEW DIRECTORY, not merely a new top-level package — a new sub-package
    # under an already-indexed tree scored 0 unindexed and earned "genuine
    # absence" with the symbol sitting on disk. Worse in a per-module-directory
    # layout (`Foo/index.ts`, and this indexer handles `.ts`): EVERY new module
    # lands in a new directory, so the count is pinned at 0 forever while the
    # notice asserts certainty. The fix would have done nothing, loudly.
    #
    # Top-level component fixes both: a new directory anywhere under `techne/`
    # still belongs to `techne/`, which the index covers. Measured identical to
    # the per-directory rule on nine real indexes (techne 13, cabal 4, gojira 2)
    # with venv noise still excluded, so the correction costs no precision.
    #
    # "ANY ANCESTOR" WAS CHECKED AND REJECTED: the repo root is an ancestor of
    # everything, which re-admits all 10,055 venv files.
    #
    # THE REMAINING COST, stated because it is real: a genuinely NEW top-level
    # component is still invisible — no indexed sibling at that level, so no
    # signal. Narrower than what it replaces, not nothing.
    def _component(rel: Path) -> str:
        """Top-level component of a repo-relative path; "." for a root file."""
        return rel.parts[0] if len(rel.parts) > 1 else "."

    indexed_components = {_component(Path(f)) for f in indexed}
    unseen = 0
    # THE `except OSError` THAT USED TO WRAP THIS WAS DEAD CODE (techne#599).
    # `_walk_repo` was built on `Path.rglob`, which swallows per-directory errors
    # internally — an unreadable directory just DROPS OUT of the walk, no
    # exception. So a `chmod 000` directory holding the sought symbol vanished
    # from enumeration while this returned "complete" and the caller went on to
    # say "This is genuine absence" with the symbol physically on disk. That is
    # the exact sentence #595 exists to make trustworthy, and it was the FOURTH
    # failure-open found on that PR: every doubt-expressing mechanism in it
    # degraded into confidence.
    blind = False

    def _saw_error(_exc: OSError) -> None:
        nonlocal blind
        blind = True

    def _saw_symlink_dir(_path: Path) -> None:
        nonlocal blind
        blind = True

    try:
        for path in _walk_repo(
            root,
            set(_EXT_LANGUAGE.values()),
            onerror=_saw_error,
            on_symlink_dir=_saw_symlink_dir,
        ):
            rel = path.relative_to(root)
            if str(rel) in indexed or str(path) in indexed:
                continue
            if _component(rel) in indexed_components:
                unseen += 1
    except OSError:
        # KEPT, and it is NOT the dead branch #599 named. This catches the walker
        # ITSELF failing outright; the callbacks above catch the different case of
        # a walk that succeeds while quietly dropping a directory. Two failure
        # modes, two mechanisms — collapsing them is what left the second one
        # unhandled for so long.
        return None, "unknown"
    if blind:
        # A directory we could not read, or a symlinked one we did not follow,
        # may hold the very file being asked about. `None` rather than a partial
        # count, matching the raise path: a number here would read as an answer
        # when the enumeration never completed.
        return None, "unknown"
    return unseen, "complete"
