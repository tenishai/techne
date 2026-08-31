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

        paths = [p for p in _module_candidates(module) if p in known_files]
        if not paths:
            # Bound to something we have not indexed: stdlib, a dependency, or a
            # file excluded from the walk. Either way no symbol here can be it.
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
    }
    log.debug("codex_import_resolve %s", report)
    return report
