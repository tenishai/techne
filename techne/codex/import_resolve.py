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
#: Node's builtin modules — the TypeScript analogue of `sys.stdlib_module_names`
#: (Prudence r4 Important 3). `import { x } from 'util'` in `web/app.ts` is a BARE
#: specifier: node and `moduleResolution: node` look in `web/node_modules`, then
#: `node_modules`, then upward, and never at `web/util/`. Reaching a nested repo
#: directory from a bare specifier needs an explicit `baseUrl`/`paths` in
#: tsconfig, which this resolver does not read. And these names — `util`, `path`,
#: `types`, `stream`, `events` — are among the most ordinary directory names in a
#: TS repo, so the collision is likely rather than exotic. Hardcoded rather than
#: imported because there is no `sys.stdlib_module_names` for node; the list is
#: node 22's, and a name missing from it costs a false resolution, not a crash.
_NODE_BUILTINS = frozenset(
    """assert async_hooks buffer child_process cluster console constants crypto
    dgram diagnostics_channel dns domain events fs http http2 https inspector
    module net os path perf_hooks process punycode querystring readline repl
    stream string_decoder sys timers tls trace_events tty url util v8 vm wasi
    worker_threads zlib""".split()
)

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


def _module_root(module: str, language: str | None) -> str:
    """The first segment of a module specifier, in that language's own spelling.

    ONE DEFINITION, THREE CALL SITES, AND THAT IS THE WHOLE POINT (Prudence r6
    Important 2). This concept had three literal spellings in this file — the
    Python guard split on ".", the TypeScript guard split on "/" after stripping
    `node:`, and the evidence gate split on "." for BOTH languages. Each round
    fixed the root in one place and left it wrong in another, three rounds
    running.

    The gate was the one that never got fixed, and its failure was silent rather
    than wrong-looking: `_in_repo_names` returns path segments and file stems, so
    NO entry can ever contain a "/". Splitting a TypeScript specifier on "." left
    `ui/button` whole, which nothing in that set can equal — a test that cannot be
    satisfied rather than one that fails. Every unplaceable TS subpath fell
    through to `external`, asserting as fact that no symbol here can be a callee
    that was sitting in the index.

    This is the redundant-declaration shape this file has now deleted twice on
    its own account (the second language gate in `_module_candidates`, the dead
    `ambiguous` flag in `_match_paths`). A single function makes the disagreement
    impossible instead of merely corrected.
    """
    if language == "python":
        return module.split(".", 1)[0]
    return module.removeprefix("node:").split("/", 1)[0]


def _is_builtin(module: str, language: str | None) -> bool:
    """Does this specifier name a language builtin — stdlib, or a node builtin.

    A BUILTIN NAME IS POSITIVE EVIDENCE OF OUT-OF-REPO, and that is why this is a
    predicate the CALLER consults rather than an early return inside the matcher.
    An earlier shape returned no candidates for a builtin and let the evidence
    gate decide, which produced `unresolved` whenever the repo happened to hold a
    same-named directory — precisely the collision the guard exists for. A repo
    with a top-level `util/` would have had `import { promisify } from 'util'`
    reported as "we could not tell", when in fact we can: nothing in this repo is
    the node `util` module.

    Splitting it out also stops the rule being spelled twice, once in the matcher
    and once in the gate, which is the shape this file has now removed three
    times (the second language gate, the dead `ambiguous` flag, and three
    spellings of the module root).
    """
    root = _module_root(module, language)
    if language == "python":
        return root in sys.stdlib_module_names
    return root in _NODE_BUILTINS


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
    than a false one. Measured on this repo: it costs none of the 593.
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

    # THE BUILTIN TEST RUNS BEFORE THE EXACT BRANCH FOR TYPESCRIPT, AND AFTER IT
    # FOR PYTHON (Prudence r6 Important 1). The exact-match carve-out below is
    # defended by an argument that is Python's and only Python's: a repo-root
    # `logging.py` IS what `import logging` resolves to when the root is on
    # `sys.path`. THERE IS NO `sys.path` IN NODE. A bare specifier resolves
    # through `node_modules` and never against a repo-relative path, and builtins
    # outrank `node_modules` besides — so for TypeScript the exact branch is not
    # the plausible answer, it is the impossible one.
    #
    # r5 fixed this guard for the suffix branch and the exact branch kept
    # returning above it, so `import { promisify } from 'util'` bound to a
    # TOP-LEVEL `util/index.ts` — the likeliest form of exactly the collision the
    # guard exists to stop, since `util`, `path`, `types` and `stream` are among
    # the most ordinary top-level directory names in a TS repo. Both shipped
    # tests used a NESTED shadow, so the top-level shape was uncovered in both
    # directions, the same way the subpath shape was one round earlier.
    if language != "python" and _is_builtin(module, language):
        memo[key] = []
        return memo[key]

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
    builtin = (
        _is_builtin(module, language)
        if language == "python"
        # THE ROOT, NOT THE WHOLE SPECIFIER (Prudence r5 Important 1). `_NODE_BUILTINS`
        # holds bare roots, so `fs/promises` — about as ordinary as node TypeScript
        # gets — matched nothing and bound to a same-named `web/fs/promises.ts`, a
        # false `import_resolved`. `timers/promises`, `stream/web`, `util/types` and
        # `path/posix` are the same shape. The Python branch had taken the root since
        # r3; the TS branch got r4's argument without r4's implementation.
        # The `node:` prefix is stripped BEFORE splitting, and only once: a bare
        # `module in _NODE_BUILTINS` disjunct was dead, since no member carries the
        # prefix (r5 nit 4).
        else False  # already returned above for TS
    )
    if builtin:
        # A NESTED FILE NEVER SHADOWS THE STDLIB (Prudence r3 Important 2).
        # `myapp/app.py` doing `from logging import getLogger` means the stdlib:
        # `myapp` is a package, not a `sys.path` entry, so Python could not reach
        # `myapp/logging.py` that way. Suffix matching has no notion of stdlib
        # and happily ended at `/logging.py`. A module that wraps the stdlib
        # module it shadows is the archetype, and `logging.py`, `json.py`,
        # `types.py` and `csv.py` are ordinary file names.
        #
        # THE SAME RULE ON THE TYPESCRIPT SIDE, SCOPED TO BUILTINS AND NO WIDER
        # (Prudence r5 Important 3). An earlier version of this comment argued
        # that a bare specifier can never reach a nested repo directory, which is
        # false and dangerously so: pnpm/npm/yarn WORKSPACES symlink
        # `node_modules/ui` to `packages/ui` with no tsconfig at all, so
        # `import { Button } from 'ui'` resolving to `packages/ui/index.ts` is
        # CORRECT — and it is the monorepo case this card advertises. Acting on
        # the broader rule would delete that. What is guessed is only the case
        # where the specifier names a BUILTIN, because nothing in the repo can be
        # `fs` or `util`. This repo has no TypeScript at all, so no measurement
        # here can ever see any of it — the same blind spot that hid r1's
        # Criticals, one language over.
        #
        # THIS BLOCK IS NOW PYTHON-ONLY, AND SAYING SO IS THE POINT. It is reached
        # only for `language == "python"`: TypeScript returns at the early guard
        # ~50 lines above, before the exact branch. An earlier version of this
        # paragraph opened "ONLY THE SUFFIX BRANCH IS GUARDED" and described, in
        # the present tense, a TypeScript path that r6 had already made
        # unreachable — dead prose contradicting the live comment 50 lines up and
        # the architecture card, left behind because the control flow was fixed
        # and its old justification was not.
        #
        # FOR PYTHON only the suffix branch is guarded, and an EXACT match is left
        # to resolve: a repo-root `logging.py` IS what `import logging` resolves
        # to when the root is on `sys.path` (running from the root, or from
        # tests; an installed wheel puts site-packages AFTER the stdlib, so a
        # shipped top-level `logging.py` would not shadow — the guard fails
        # toward the plausible answer under both readings). This is the mirror of
        # the vendoring boundary below: there, a deliberate vendored copy is what
        # the reader wants; here, nothing was vendored and a name merely
        # collided.
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


def _nested_symbol_ids(conn) -> frozenset[str]:
    """Symbols enclosed by another symbol in the same file — i.e. NOT module-level.

    THE PROMISE THIS KEEPS (Prudence r4 Important 2). The module docstring says a
    callee that is "simply not a top-level symbol" stays `unresolved`, and the
    kind allow-list only expresses half of that: a method is excluded, but a
    function defined INSIDE another function is `kind='function'` too, because
    `_is_method_python` looks for a class body and a closure has none. So
    `from pkg.util import helper` bound to a `helper` nested in `outer` — which
    raises ImportError in Python — was stamped `import_resolved`, and reported
    by `include_inferred=False` as `exact`. Zero occurrences on this repo, like
    the stdlib case; it is the residual of the Critical r3 closed.

    KIND IS NOT THE DISCRIMINATOR HERE, NESTING IS, and nesting is already in the
    index: `span` is "start:end" line numbers, so a symbol is module-level when no
    other non-module symbol in its file encloses it. No schema change needed.

    Identical spans exclude each other, deliberately: two symbols claiming the
    same lines is a state this cannot rank, and refusing both fails toward
    silence, which is the trade this file makes everywhere else. NOTE that TS
    sibling declarators are RIVALS ONLY BY ACCIDENT of the `.scm` — it captures
    the `lexical_declaration`, so `export const a = …, b = …` gives both names
    the parent's span and both are refused. Filed rather than worked around
    here, because the fix belongs in the query.

    A NULL OR MALFORMED SPAN FAILS TOWARD SPEECH, and it is the one place here
    that does (Prudence r5 finding 8): such a row is dropped, so it is neither
    refusable nor able to shield what it encloses. Unreachable today —
    `_parse_file` always writes a span and `codegraph.add_symbol` has no
    production caller — which is the same footing as the NULL-language case
    above.
    """
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for sid, file, span in conn.execute(
        "SELECT id, file, span FROM symbols WHERE kind != 'module' AND span IS NOT NULL"
    ):
        head, _, tail = str(span).partition(":")
        try:
            by_file.setdefault(file, []).append((int(head), int(tail), sid))
        except ValueError:
            continue
    nested: set[str] = set()
    for rows in by_file.values():
        # Quadratic per FILE, not per repo; files hold tens of symbols.
        for a, b, sid in rows:
            for a2, b2, sid2 in rows:
                if sid2 != sid and a2 <= a and b2 >= b:
                    nested.add(sid)
                    break
    return frozenset(nested)


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
    nested_ids = _nested_symbol_ids(conn)
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
            # A BUILTIN SHORT-CIRCUITS THE NAME TEST. `_in_repo_names` would
            # otherwise veto the verdict whenever the repo holds a same-named
            # directory — a top-level `util/` turning `import … from 'util'`
            # into "we could not tell", when we can tell exactly.
            if not _is_builtin(module, language) and (
                _module_root(module, language) in in_repo_names
            ):
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
        # `interface` IS NOT IN THE LIST: an interface is not callable or
        # constructible, so no call or import-bound call can land on one. The
        # indexer does emit the kind since issue #859; binding a call to it would
        # invent an edge.
        candidates = conn.execute(
            "SELECT id FROM symbols WHERE file IN (%s) AND name = ? "
            "AND kind IN ('function', 'class')"
            % ",".join("?" * len(paths)),
            (*paths, to_name),
        ).fetchall()
        hit = next((row for row in candidates if row[0] not in nested_ids), None)
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


def resolve_edges_by_scope(conn) -> dict:
    """Resolve bare calls to a module-level symbol defined in the SAME file (techne#940).

    `resolve_edges_by_import` answers "which module did this file import `f` from?",
    and a call to a function defined in the caller's own file has no import to
    answer it with. So every intra-file call stayed `unresolved`, and
    `blast-radius --only-proven` printed "(nothing reachable)" for a function whose
    only callers sat next to it — the empty answer that reads as "safe to change".

    Binds `f` to the file's module-level `f` only when all of these hold, and leaves
    the edge `unresolved` otherwise:

      - at least one call site behind the edge is a BARE call (`bare_call = 1`).
        `f()` and `x.f()` share one edge row, and `x.f()` is not a call to the
        local `f`. Rows indexed before schema v5 are NULL and are not touched.
      - no import edge in the file binds the name. An import shadows or rebinds
        it, and `resolve_edges_by_import` owns those rows.
      - exactly ONE non-nested function or class of that name in the file. Two is
        a conditional or repeated definition this cannot rank.
      - no NESTED function or class of that name in the file. A nested `def f`
        can shadow the module-level one inside its enclosing scope, and spans
        alone do not say which caller sits inside it. Methods do not block: a
        bare call cannot reach a method in Python or TypeScript.

    NOT PROVEN, AND NOT ATTEMPTED — the same lexical scope as the import pass: a
    parameter, local variable or module-level assignment of the same name, `global`
    rebinding, and `import a.b` binding `a` (the edge records `b`). Those shadow
    the function at runtime and are invisible to this table.

    Writes `resolution_status = 'scope_resolved'`. Anything with `to_symbol` set is
    proven to `reachable(include_inferred=False)`, so no reader needs to know the
    new status. Only `_OVERWRITABLE` rows are touched, so `lsp` and
    `import_resolved` rows survive, and a re-run on an unchanged index is a no-op.
    """
    nested_ids = _nested_symbol_ids(conn)

    by_file_name: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for sid, file, name, kind in conn.execute(
        "SELECT id, file, name, kind FROM symbols WHERE kind IN ('function', 'class', 'method')"
    ):
        by_file_name.setdefault((file, name), []).append((sid, kind))

    imported = {
        (file, to_name)
        for file, to_name in conn.execute(
            """SELECT s.file, e.to_name
                 FROM edges e JOIN symbols s ON s.id = e.from_symbol
                WHERE e.kind = 'import'"""
        )
    }

    rows = conn.execute(
        f"""SELECT e.rowid, s.file, e.to_name
              FROM edges e JOIN symbols s ON s.id = e.from_symbol
             WHERE e.kind = 'call'
               AND e.to_symbol IS NULL
               AND e.bare_call = 1
               AND e.resolution_status IN ({",".join("?" * len(_OVERWRITABLE))})""",
        _OVERWRITABLE,
    ).fetchall()

    resolved = no_local = imported_name = ambiguous = 0
    for rowid, file, to_name in rows:
        local = by_file_name.get((file, to_name), [])
        if not local:
            no_local += 1
            continue
        if (file, to_name) in imported:
            imported_name += 1
            continue
        top = [sid for sid, kind in local if kind != "method" and sid not in nested_ids]
        shadows = [sid for sid, kind in local if kind != "method" and sid in nested_ids]
        if len(top) != 1 or shadows:
            ambiguous += 1
            continue
        conn.execute(
            "UPDATE edges SET to_symbol = ?, resolution_status = 'scope_resolved' WHERE rowid = ?",
            (top[0], rowid),
        )
        resolved += 1

    conn.commit()
    report = {
        "bare_call_edges_examined": len(rows),
        "resolved": resolved,
        "no_local_symbol": no_local,
        "name_imported": imported_name,
        "ambiguous": ambiguous,
    }
    log.debug("codex_scope_resolve %s", report)
    return report
