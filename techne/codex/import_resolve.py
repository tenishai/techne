"""Import-binding edge resolution for codex — the dependency-free resolver (techne#679).

The tree-sitter indexer records every call edge with `to_symbol = NULL`: it knows
the callee's *name* but not what that name binds to. `lsp_resolve` fills this in
semantically, and does it better — but it needs a language server per language,
and on 2026-08-31 the live corpus measured **33,081 call edges with 1 resolved
target**, that one in a demo shard. Every production repo had zero. A resolver
that needs a toolchain nobody runs resolves nothing.

This pass needs no toolchain. It reads the binding the indexer now records
(`edges.to_module`, added in schema v4) and answers a narrower question than the
LSP does:

    the file calls `Config` — which module did this file import `Config` FROM,
    and is that module inside this repo?

Three outcomes, and the third is the one worth having:

  in-repo module, symbol found  -> `to_symbol` set, status 'import_resolved'
  in-repo module, symbol absent -> left 'unresolved' (the import is real; the
                                   callee may be re-exported, conditionally
                                   defined, or simply not a top-level symbol —
                                   guessing here is how name-matching got its
                                   reputation)
  module outside the repo       -> status 'external'

**`external` is a fact, not a failure.** `unresolved` means "we could not tell";
`external` means "we checked, and the callee is `pathlib.Path`, so no symbol in
this repo can be it." That distinction is what lets a caller ask for a call graph
with the guesses removed and get something back — before this, `include_inferred=
False` returned an empty graph on every store, because nothing had ever set
`to_symbol`.

Scope, deliberately: a lexical binding table, not type inference. It cannot
follow a re-export, a `getattr`, or a name rebound at runtime, and it does not
try. It composes with `lsp_resolve` rather than competing: LSP rows are never
overwritten here.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Statuses this pass will overwrite. An `lsp`-resolved edge is strictly better
#: evidence than an import binding and must survive a re-run of this pass, so it
#: is absent from this set — the ordering is a claim about evidence quality, not
#: an implementation detail, and inverting it would silently downgrade the graph.
_OVERWRITABLE = ("unresolved",)


def _module_candidates(dotted: str) -> tuple[str, ...]:
    """Repo-relative paths a dotted module could live at.

    `a.b.c` is either `a/b/c.py` (a module) or `a/b/c/__init__.py` (a package).
    Both are checked because the indexer stores whichever the file actually is,
    and a package's `__init__.py` is where its re-exports live.
    """
    stem = dotted.replace(".", "/")
    return (f"{stem}.py", f"{stem}/__init__.py", f"{stem}.ts", f"{stem}/index.ts")


def _top_level_roots(known_files) -> frozenset[str]:
    """First path segment of every indexed file — the repo's top-level names.

    This is the POSITIVE EVIDENCE that makes an `external` verdict sound. If a
    module's root segment names nothing at the top of this repo, then no layout
    this resolver understands could place it here, and "the callee is not in
    this repo" is a fact rather than a failure to look.
    """
    return frozenset(f.split("/", 1)[0] for f in known_files)


def _match_paths(module: str, known_files, memo: dict) -> tuple[list[str], bool]:
    """Indexed files a dotted module could be, exact match first.

    SUFFIX MATCHING EXISTS FOR `src/` LAYOUTS (techne#679, Prudence r1 Critical
    3). The resolver originally assumed dotted module path == repo-relative
    path, which holds for a flat layout like techne's own and fails for the
    other half of the Python ecosystem: `from mypkg.util import helper` in a
    repo that stores it at `src/mypkg/util.py` matched nothing and the edge was
    asserted `external` while the file sat in the index.

    AMBIGUITY IS REFUSED, NOT GUESSED. If two indexed files both end with the
    candidate path, this repo has two plausible callees and picking one is the
    name-matching behaviour the `external`/`unresolved` split exists to avoid.

    Returns (paths, ambiguous). THE FLAG MATTERS AS MUCH AS THE PATHS: two
    matches mean the module IS in-repo-shaped, we simply cannot say which file —
    so the caller must not fall through to `external`. Collapsing both cases to
    an empty list made the ambiguous repo report a false `external`, which the
    ambiguity test caught.
    """
    if module in memo:
        return memo[module]
    cands = _module_candidates(module)
    exact = [p for p in cands if p in known_files]
    if exact:
        memo[module] = (exact, False)
        return memo[module]
    hits = {f for c in cands for f in known_files if f.endswith("/" + c)}
    out = (list(hits), False) if len(hits) == 1 else ([], len(hits) > 1)
    memo[module] = out
    return out


def resolve_edges_by_import(conn) -> dict:
    """Resolve call edges through recorded import bindings. Returns a report.

    Idempotent: re-running resolves nothing new on an unchanged index, because
    every edge it touches leaves `_OVERWRITABLE`.
    """
    # Which repo-relative files does this index actually contain? This is the
    # in-repo test, and it is exact rather than heuristic — a module resolves to
    # a path we have indexed, or it does not.
    known_files = {r[0] for r in conn.execute("SELECT DISTINCT file FROM symbols")}

    # binding lookup: (importing file, bound name) -> source module
    bindings: dict[tuple[str, str], str] = {}
    for file, to_name, to_module in conn.execute(
        """SELECT s.file, e.to_name, e.to_module
             FROM edges e JOIN symbols s ON s.id = e.from_symbol
            WHERE e.kind = 'import' AND e.to_module IS NOT NULL"""
    ):
        bindings[(file, to_name)] = to_module

    resolved = external = 0
    unresolved_no_binding = unresolved_in_repo_no_symbol = 0
    unresolved_in_repo_no_path = 0
    roots = _top_level_roots(known_files)
    path_memo: dict[str, list[str]] = {}

    rows = conn.execute(
        f"""SELECT e.rowid, s.file, e.to_name
              FROM edges e JOIN symbols s ON s.id = e.from_symbol
             WHERE e.kind = 'call'
               AND e.to_symbol IS NULL
               AND e.resolution_status IN ({",".join("?" * len(_OVERWRITABLE))})""",
        _OVERWRITABLE,
    ).fetchall()

    for rowid, file, to_name in rows:
        module = bindings.get((file, to_name))
        if module is None:
            unresolved_no_binding += 1
            continue

        paths, ambiguous = _match_paths(module, known_files, path_memo)
        if not paths:
            # `external` IS A FACT AND NEEDS POSITIVE EVIDENCE (Prudence r1
            # Critical 2 & 3). Not finding a path is not evidence — the
            # module-path-to-file-path mapping can fail for reasons of LAYOUT,
            # and asserting `external` on that failure produced rows claiming
            # no symbol in this repo could be a callee that was sitting in the
            # index. The evidence is the root segment: if this repo has nothing
            # at the top level by that name, the callee genuinely is not here.
            # Anything in-repo-shaped that we simply could not place stays
            # `unresolved`, which is the honest word for "we could not tell".
            if ambiguous or module.split(".", 1)[0] in roots:
                unresolved_in_repo_no_path += 1
                continue
            conn.execute(
                "UPDATE edges SET resolution_status = 'external' WHERE rowid = ?", (rowid,)
            )
            external += 1
            continue

        hit = conn.execute(
            "SELECT id FROM symbols WHERE file IN (%s) AND name = ? AND kind != 'module' LIMIT 1"
            % ",".join("?" * len(paths)),
            (*paths, to_name),
        ).fetchone()
        if hit is None:
            unresolved_in_repo_no_symbol += 1
            continue

        conn.execute(
            "UPDATE edges SET to_symbol = ?, resolution_status = 'import_resolved' WHERE rowid = ?",
            (hit[0], rowid),
        )
        resolved += 1

    conn.commit()
    report = {
        "call_edges_examined": len(rows),
        "resolved": resolved,
        "external": external,
        "unresolved_no_binding": unresolved_no_binding,
        "unresolved_in_repo_no_symbol": unresolved_in_repo_no_symbol,
        "unresolved_in_repo_no_path": unresolved_in_repo_no_path,
    }
    log.debug("codex_import_resolve %s", report)
    return report
