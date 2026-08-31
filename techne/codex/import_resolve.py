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
import sys

log = logging.getLogger(__name__)

#: Statuses this pass will overwrite. An `lsp`-resolved edge is strictly better
#: evidence than an import binding and must survive a re-run of this pass, so it
#: is absent from this set — the ordering is a claim about evidence quality, not
#: an implementation detail, and inverting it would silently downgrade the graph.
_OVERWRITABLE = ("unresolved",)


#: Candidate file shapes per language. THE LANGUAGE IS PART OF THE QUESTION
#: (Prudence r2 Important 2): offering `.py` and `.ts` shapes to every module
#: regardless of the importing file let a Python call bind to a TypeScript
#: symbol and assert it `import_resolved` — the status that survives
#: `include_inferred=False`. Measured zero times on this repo, because it needs
#: a module name absent in language A and present in language B, which `api`,
#: `utils`, `types` and `config` make ordinary in a full-stack repo indexed with
#: the default `--language python,typescript`.
_CANDIDATE_SHAPES: dict[str, tuple[str, ...]] = {
    "python": ("{stem}.py", "{stem}/__init__.py"),
    "typescript": ("{stem}.ts", "{stem}/index.ts"),
}


def _module_candidates(dotted: str, language: str | None = None) -> tuple[str, ...]:
    """Repo-relative paths a dotted module could live at, for one language.

    `a.b.c` is either `a/b/c.py` (a module) or `a/b/c/__init__.py` (a package).
    Both are checked because the indexer stores whichever the file actually is,
    and a package's `__init__.py` is where its re-exports live.

    AN UNKNOWN LANGUAGE REFUSES rather than widening to every shape. This is the
    ONLY gate against a cross-language binding — an earlier version also carried
    an `AND language = ?` clause on the symbol lookup, and the two were
    redundant: removing either left the other, so neither could be shown to be
    load-bearing and drift in one would be silently covered by the other. That
    is the declared-twice shape #852 fixed in the recall cap. Widening here would
    reopen the hole for exactly the rows whose language we cannot name, so it
    returns nothing. NOTE WHAT THAT DOES AND DOES NOT DO: with no candidates the
    row reaches the evidence gate with no lookup attempted, so it can still be
    written `external` if its module root names nothing in the repo — it does not
    become `unresolved` by default. That is correct for a genuine stdlib import
    and is unreachable today anyway: `_EXT_LANGUAGE` maps exactly two extensions
    and `_parse_file` always writes one, so the only NULL-language route is
    `codegraph.add_symbol`'s default, which has no production caller. `.tsx` is
    the language that would first walk into it. No indexed symbol
    in any live codex db has a NULL language (measured 2026-08-31 across techne,
    liminal and cabal), so this costs nothing today and cannot rot into a bypass.
    """
    shapes = _CANDIDATE_SHAPES.get(language or "")
    if not shapes:
        return ()
    stem = dotted.replace(".", "/")
    return tuple(sh.format(stem=stem) for sh in shapes)


def _in_repo_names(known_files) -> frozenset[str]:
    """Every name a module root could legitimately be, anywhere in the tree.

    This is the POSITIVE EVIDENCE that makes an `external` verdict sound: if a
    module's root names nothing in this repo at all, no layout could place it
    here and "the callee is not in this repo" is a fact rather than a failure
    to look.

    SEGMENTS, NOT SEGMENT ZERO (Prudence r2 Important 1). The first version took
    `f.split("/", 1)[0]`, which is the repo's TOP-level names — correct only for
    a flat layout. Under `src/` those are {'src', 'tests'} and never a package
    name, so `mypkg` could not match and every unplaceable in-repo module fell
    straight through to a false `external`: the exact failure r1 raised, one
    layout further out, and invisible here because techne is flat. A pnpm
    monorepo (`packages/foo/src/index.ts` imported as `'foo'`) is the same shape.

    Directory segments AND file stems, because both can be a module root: `mypkg`
    in `src/mypkg/app.py`, and `utils` in a top-level `utils.py`.

    The cost is real and is the trade r1 already asked for: a stdlib name that
    collides with an in-repo directory name (a repo with its own `types/`) stops
    being a true `external` and becomes `unresolved`. A lost fact is cheaper
    than a false one. Measured on this repo: it costs none of the 597.
    """
    names: set[str] = set()
    for f in known_files:
        parts = f.split("/")
        names.update(parts[:-1])
        names.add(parts[-1].rsplit(".", 1)[0])
    return frozenset(names)


def _match_paths(module: str, language: str | None, known_files, memo: dict) -> list[str]:
    """Indexed files a dotted module could be, exact match first.

    SUFFIX MATCHING EXISTS FOR `src/` LAYOUTS (techne#679, Prudence r1 Critical
    3). The resolver originally assumed dotted module path == repo-relative
    path, which holds for a flat layout like techne's own and fails for the
    other half of the Python ecosystem: `from mypkg.util import helper` in a
    repo that stores it at `src/mypkg/util.py` matched nothing and the edge was
    asserted `external` while the file sat in the index.

    AMBIGUITY IS REFUSED, NOT GUESSED, IN BOTH BRANCHES. If two indexed files
    match, this repo has two plausible callees and picking one is the
    name-matching behaviour the `external`/`unresolved` split exists to avoid.

    KNOWN BOUNDARY, measured rather than assumed: a suffix match will bind
    `from requests.models import Response` to a HAND-VENDORED
    `vendor/requests/models.py` if the repo has one. `_SKIP_DIRS` already
    excludes every conventional vendoring location — `node_modules`,
    `site-packages`, `dist-packages`, `venv`, `.tox`, `build` — so this needs a
    non-conventional directory, and in a repo that deliberately vendors there,
    binding to the vendored copy is the answer the reader wants. Recorded
    because it is a consequence of suffix matching that exact matching did not
    have, not because it is known to be wrong.

    NO `ambiguous` FLAG ANY MORE. It existed so two matches would not fall
    through to `external`, and at the time that was load-bearing. Once
    `_in_repo_names` learned about nested layouts the flag became PROVABLY dead:
    every candidate is the module path with dots turned to slashes, so any file
    matching one necessarily carries the module's root as a directory segment or
    its own stem — which is exactly `_in_repo_names`' membership test. Two
    matches cannot occur without the root already being in that set, so the
    caller's `or` could never change the outcome. Keeping it would be the
    redundant-gate shape this file removed one finding earlier.
    """
    key = (module, language)
    if key in memo:
        return memo[key]
    cands = _module_candidates(module, language)
    exact = [p for p in cands if p in known_files]
    if exact:
        # THIS BRANCH REFUSES TOO (Prudence r2 Important 2). It previously handed
        # every exact hit to a `LIMIT 1` with no ORDER BY, so a repo holding both
        # `api.py` and `api/__init__.py` resolved to whichever row SQLite
        # returned first — right by luck of row order, not by rule. The suffix
        # branch already refused this; the two must answer alike.
        out = exact if len(exact) == 1 else []
        memo[key] = out
        return out
    if language == "python" and module.split(".", 1)[0] in sys.stdlib_module_names:
        # A NESTED FILE NEVER SHADOWS THE STDLIB (Prudence r3 Important 2).
        # `myapp/app.py` doing `from logging import getLogger` means the stdlib:
        # `myapp` is a package, not a `sys.path` entry, so Python could not reach
        # `myapp/logging.py` that way. Suffix matching has no notion of stdlib
        # and happily ended at `/logging.py`. A module that wraps the stdlib
        # module it shadows is the archetype, and `logging.py`, `json.py`,
        # `types.py` and `csv.py` are ordinary file names.
        #
        # ONLY THE SUFFIX BRANCH IS GUARDED. An EXACT match is a genuine
        # top-level shadow — a repo-root `logging.py` IS what `import logging`
        # resolves to when the root is on `sys.path` — so it is left to resolve
        # above. This is the mirror of the vendoring boundary below: there, a
        # deliberate vendored copy is what the reader wants; here, nothing was
        # vendored and a name merely collided.
        memo[key] = []
        return memo[key]
    hits = {f for c in cands for f in known_files if f.endswith("/" + c)}
    # ONE, OR NOTHING. Two matches used to be reported to the caller as an
    # `ambiguous` flag so it would not fall through to `external`; that flag is
    # gone (Prudence r3 Important 3) because `_in_repo_names` made it provably
    # dead — see its docstring.
    out = list(hits) if len(hits) == 1 else []
    memo[key] = out
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
    in_repo_names = _in_repo_names(known_files)
    path_memo: dict[tuple[str, str | None], list[str]] = {}

    rows = conn.execute(
        f"""SELECT e.rowid, s.file, e.to_name, s.language
              FROM edges e JOIN symbols s ON s.id = e.from_symbol
             WHERE e.kind = 'call'
               AND e.to_symbol IS NULL
               AND e.resolution_status IN ({",".join("?" * len(_OVERWRITABLE))})""",
        _OVERWRITABLE,
    ).fetchall()

    for rowid, file, to_name, language in rows:
        module = bindings.get((file, to_name))
        if module is None:
            unresolved_no_binding += 1
            continue

        paths = _match_paths(module, language, known_files, path_memo)
        if not paths:
            # `external` IS A FACT AND NEEDS POSITIVE EVIDENCE (Prudence r1
            # Critical 2 & 3). Not finding a path is not evidence — the
            # module-path-to-file-path mapping can fail for reasons of LAYOUT,
            # and asserting `external` on that failure produced rows claiming
            # no symbol in this repo could be a callee that was sitting in the
            # index. The evidence is the module's ROOT NAME, tested against every
            # name in the tree — directory segment at any depth or file stem, NOT
            # just the top level, which is the flat-layout mistake r2 caught here
            # (see `_in_repo_names`). If this repo has nothing by that name
            # anywhere, the callee genuinely is not here.
            # Anything in-repo-shaped that we simply could not place stays
            # `unresolved`, which is the honest word for "we could not tell".
            if module.split(".", 1)[0] in in_repo_names:
                unresolved_in_repo_no_path += 1
                continue
            conn.execute(
                "UPDATE edges SET resolution_status = 'external' WHERE rowid = ?", (rowid,)
            )
            external += 1
            continue

        # AN ALLOW-LIST, NOT `kind != 'module'` (Prudence r1 nit 8, corrected in
        # r3 Critical 1: that clause was not too defensive, it was too weak). A
        # module-level from-import binds a module-level object, never a METHOD
        # hanging off a class in that file — in either language. Accepting one
        # produced four false `import_resolved` edges on this repo: documents.py
        # does `from techne.core.stores import plain_connect as connect`, and
        # `stores.py` has no module-level `connect` but does have
        # `DomainStore.connect`, so four calls to `plain_connect` were bound to a
        # method none of them calls — and `include_inferred=False` labelled them
        # `exact`, which is 52 symbols of "proven" blast radius reached through
        # edges that do not exist.
        #
        # This is also the case the module docstring already promises: a callee
        # that is re-exported or is not a top-level symbol stays `unresolved`.
        hit = conn.execute(
            "SELECT id FROM symbols WHERE file IN (%s) AND name = ? "
            "AND kind IN ('function', 'class', 'interface') LIMIT 1"
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
