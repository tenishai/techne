"""RepoSource — tree-sitter SourceAdapter for Codex repo indexing (techne#89).

Walks a repository, parses Python and TypeScript files with tree-sitter, and
writes symbols + unresolved call/import edges to codex.db. Implements the
SourceAdapter Protocol from techne.sync.adapter.

Key design decisions
--------------------
* File-hash-scoped incremental:  sha256(bytes) is stored on every symbol row.
  On re-index, any file whose hash hasn't changed is skipped.  On change, stale
  rows are deleted scoped by FILE (not by content hash — two files with identical
  bytes share a SHA, so a hash-keyed edge delete wipes the other file's edges,
  techne#170):
  DELETE edges WHERE from_symbol IN (SELECT id FROM symbols WHERE file = ?);
  DELETE symbols WHERE file = ?;  re-parse and INSERT fresh rows.

* Unresolved edges:  v1 writes to_name (the callee name string, always known)
  and leaves to_symbol NULL + resolution_status = 'unresolved'.  A future
  resolver pass can fill those in without re-parsing any source.

* Module symbol:  every file gets a synthetic module-scope symbol
  (id = "<file>::<module>::module::0") as the from_symbol for calls/imports
  that appear outside any function or class definition.

* Lazy grammar imports:  tree_sitter_python / tree_sitter_typescript are
  optional extras (techne[codex]).  Missing grammars degrade gracefully:
  files for that language are silently skipped.

* Watch mode:  mirrors JsonlTranscriptSource — a watchdog Observer calls
  _index_file() on each changed file through a Debouncer.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

# Extensions → language tag
_EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    # .tsx deferred to a follow-up: it needs tree-sitter's `language_tsx()`
    # grammar. The plain typescript grammar parses JSX into ERROR nodes, which
    # silently swallow surrounding symbol/call captures — worse than honestly
    # not supporting it.
}

# Directories to skip during repo walk (covers .git, node_modules, venvs, caches).
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "venv",
        ".venv",
        "env",
        ".env",
        # THE NAMES ABOVE ARE THE VENV DIRECTORY; THESE TWO ARE ITS CONTENTS, and
        # that difference is the whole fix (techne#604). A venv called anything
        # else — this repo's CI environments are `.venv-ci-3.12` / `.venv-ci-3.13`
        # — sails past the exact-name list and gets indexed in full.
        #
        # MEASURED on tenishai/techne after a re-index: 114,412 venv symbols
        # against 5,137 repo symbols, i.e. 96% of the graph was vendored code.
        # `codex_blast_radius` on a hook function returned 500 dependents whose
        # nearest were `_pytest/pytester.py`, `aiohttp/web.py` and
        # `huggingface_hub/cli/hf.py`; `codex_search` ranked a standard library
        # against ours. The failure is silent — a plausible symbol from
        # site-packages reads exactly like one of ours.
        #
        # Skipping the CONTENTS rather than the container catches every
        # virtualenv regardless of what its directory is called, which is
        # precisely the case that broke. Of those 114,412 symbols, 114,410 were
        # under `site-packages`.
        #
        # THE RESIDUAL IS TWO SYMBOLS and is left alone deliberately: each venv's
        # `bin/activate_this.py`. Adding prefix matching for `venv*`/`.venv*`
        # would be new machinery serving 0.002% of the measured problem, and
        # naming `.venv-ci-3.12` explicitly would be the hand-kept list this repo
        # has rejected repeatedly (techne#1022).
        "site-packages",
        "dist-packages",
        "build",
        "dist",
        "target",
        ".idea",
        ".vscode",
    }
)


# ---------------------------------------------------------------------------
# Grammar / parser cache
# ---------------------------------------------------------------------------


class _GrammarCache:
    """Lazy-load and cache tree-sitter Language + Parser objects per language."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._langs: dict[str, object] = {}
        self._parsers: dict[str, object] = {}
        self._queries: dict[str, object] = {}

    def get(self, language: str):
        """Return (Language, Parser, Query) for *language*, or None if unavailable."""
        with self._lock:
            if language not in self._langs:
                self._load(language)
            lang = self._langs.get(language)
            if lang is None:
                return None
            return lang, self._parsers[language], self._queries[language]

    def _load(self, language: str) -> None:
        try:
            from tree_sitter import Language, Parser, Query, QueryCursor  # noqa: F401

            if language == "python":
                import tree_sitter_python as tsp

                lang = Language(tsp.language())
            elif language == "typescript":
                import tree_sitter_typescript as tsts

                lang = Language(tsts.language_typescript())
            else:
                self._langs[language] = None
                return

            # Load .scm file
            scm_path = Path(__file__).parent / "queries" / f"{language}.scm"
            pattern = scm_path.read_text(encoding="utf-8")
            # Strip comment lines before passing to the query compiler.
            clean = "\n".join(
                line for line in pattern.splitlines() if not line.strip().startswith(";")
            )

            # tree-sitter 0.26 removed the deprecated Language.query(); the
            # Query(language, source) constructor is the forward-compatible API
            # (works on 0.25 and 0.26). See #263 follow-up / CI red on 0.26.0.
            query = Query(lang, clean)

            parser = Parser(lang)
            self._langs[language] = lang
            self._parsers[language] = parser
            self._queries[language] = query
            log.debug("codex_grammar_loaded language=%s", language)
        except ImportError:
            log.debug("codex_grammar_unavailable language=%s (install techne[codex])", language)
            self._langs[language] = None
        except Exception:
            log.exception("codex_grammar_load_failed language=%s", language)
            self._langs[language] = None


_GRAMMAR = _GrammarCache()


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _is_method_python(fn_node) -> bool:
    """True if a Python function_definition node is a method (inside a class body).

    A decorated method's parent is `decorated_definition` (whose parent is the
    class `block`), so hop through it — else @property/@staticmethod methods get
    mis-classified as plain functions.
    """
    p = fn_node.parent
    if p is not None and p.type == "decorated_definition":
        p = p.parent
    return (
        p is not None
        and p.type == "block"
        and p.parent is not None
        and p.parent.type == "class_definition"
    )


def _find_enclosing_symbol(byte_pos: int, spans: list[tuple[int, int, str]]) -> str | None:
    """Return the id of the innermost symbol span that contains *byte_pos*."""
    best: str | None = None
    best_size = float("inf")
    for start, end, sid in spans:
        if start <= byte_pos <= end:
            size = end - start
            if size < best_size:
                best_size = size
                best = sid
    return best


#: Prefixes that mark a TypeScript specifier as UNRESOLVABLE BY THIS PARSER.
#: Each names a path this resolver cannot follow, for the same reason:
#:   "."  relative to the IMPORTING FILE, whose position the parser lacks
#:   "@"  a scoped package, or a tsconfig `paths` alias
#:   "$"  a Vite / SvelteKit alias — `$lib/queries` maps to `./src/lib/queries`
#:        via tsconfig `paths` and vite `resolve.alias`, neither of which is read
#:
#: `$` WAS MISSING AND IT COST A FALSE FACT ON REAL CODE. Found 2026-08-31 by
#: indexing tenishai/ladon — the first repo with real TypeScript this resolver
#: had ever run on — where `ui/src/main.ts` importing `setApiClient` from
#: `$lib/queries` was asserted `external`, i.e. "no symbol in this repo can be
#: it", about `ui/src/lib/queries.ts:23`. Seven rounds of hand-built fixtures
#: never produced it because nobody thought of `$`, which is exactly what a
#: fixture cannot do: it can only contain the cases its author already knows.
#: A bare specifier is deliberately NOT in this set — that one resolves through
#: node_modules and is handled by the builtin guard and the workspace cost.
_TS_ALIAS_SIGILS = (".", "@", "$")


def _extract_import_names(node, language: str) -> list[tuple[str, str | None]]:
    """Pull the imported names out of an import node, each with its SOURCE MODULE.

    Returns ``(name, module)`` pairs (techne#679). ``module`` is the dotted path a
    name was imported FROM, and is ``None`` for a plain ``import x``:

        import os                  -> ("os",   None)
        import a.b.c               -> ("c",    None)
        import json as j           -> ("j",    None)
        from pathlib import Path   -> ("Path", "pathlib")
        from a.b import c as d     -> ("d",    "a.b")

    The ``None`` is load-bearing, not a placeholder: it is what distinguishes a
    module brought into scope from a name brought out of one. Before this pair
    the two were both a bare string, so 890 distinct ``to_name`` values on the
    live index mixed ``os`` and ``techne`` (modules) with ``Config`` and ``Enum``
    (names) with nothing to tell them apart — and a call to ``Config`` could not
    be tied back to the module that supplied it.

    The NAME is the binding as it appears in the importing scope, so an alias
    wins: after ``import json as j`` the callable in scope is ``j``, and
    recording ``json`` would not match any call site.

    BUT AN ALIASED from-IMPORT RECORDS NO MODULE. The name and the module are
    correct individually and wrong together: `resolve_edges_by_import` looks the
    BOUND name up inside the SOURCE module, and after
    ``from a.b import real as helper`` module ``a.b`` has no ``helper``. The
    original name is stored nowhere, so the pair cannot be resolved truthfully
    and the honest binding is none at all.
    """
    names: list[tuple[str, str | None]] = []
    if language == "python":
        node_type = node.type
        if node_type == "import_statement":
            # import foo, import foo as bar, import a.b.c
            for child in node.children:
                if child.type == "dotted_name":
                    # last identifier is the usable name
                    idents = [c for c in child.children if c.type == "identifier"]
                    if idents:
                        names.append((idents[-1].text.decode("utf-8", errors="replace"), None))
                elif child.type == "aliased_import":
                    # import json as j → the alias (j) is what's in scope
                    alias_node = child.child_by_field_name("alias")
                    if alias_node:
                        names.append((alias_node.text.decode("utf-8", errors="replace"), None))
                    else:
                        # no alias — use the module's last component
                        dotted = child.child_by_field_name("name")
                        if dotted:
                            idents = [c for c in dotted.children if c.type == "identifier"]
                            if idents:
                                names.append(
                                    (idents[-1].text.decode("utf-8", errors="replace"), None)
                                )
        elif node_type == "import_from_statement":
            # from foo.bar import baz, qux
            # The module is the dotted_name BEFORE the `import` keyword; every
            # name after it is bound FROM that module. The `in_names` flip is
            # what separates the two, so the module capture must read the same
            # children in the same pass rather than re-walking.
            in_names = False
            module: str | None = None
            for child in node.children:
                if child.type == "import":
                    in_names = True
                    continue
                if not in_names:
                    # `from . import x` / `from .. import y` — a relative import
                    # has no dotted_name here, only import_prefix dots. Left as
                    # None: a relative module cannot be resolved to a repo path
                    # without the importing file's own package position, which
                    # this function does not have. Deliberately not guessed.
                    if child.type == "dotted_name":
                        module = child.text.decode("utf-8", errors="replace")
                    continue
                if child.type == "dotted_name":
                    idents = [c for c in child.children if c.type == "identifier"]
                    if idents:
                        names.append((idents[-1].text.decode("utf-8", errors="replace"), module))
                elif child.type == "aliased_import":
                    alias_node = child.child_by_field_name("alias")
                    if alias_node:
                        # A RENAMING ALIAS LOSES THE MODULE (techne#679, Prudence
                        # r3 Critical 1). `to_name` is the name in the IMPORTING
                        # scope, which is what a call site matches — but the
                        # resolver then asks the source module for a symbol by
                        # that same name, and after `from a.b import real as
                        # helper` module `a.b` has no `helper`. Usually that
                        # misses and a miss is free; when it HITS it binds the
                        # call to an unrelated symbol and stamps it
                        # `import_resolved`, the status that survives
                        # `include_inferred=False`.
                        #
                        # `X as X` IS NOT A RENAME AND KEEPS ITS MODULE. The
                        # re-export idiom (`from .m import Thing as Thing`, which
                        # is how a module marks a name as public re-export for
                        # type checkers) binds the SAME name it imported, so the
                        # lookup asks the right question and the binding is a
                        # true fact. Dropping it cost 4 resolutions on this repo
                        # across 62 such imports — measured by A/B on one tree
                        # with this one line toggled, after an earlier version of
                        # this fix refused every alias indiscriminately. (An
                        # earlier annotation here said 58 resolutions; that was a
                        # regex count of imports, not an A/B of resolutions, and
                        # it disagreed with the card in the same commit.)
                        alias = alias_node.text.decode("utf-8", errors="replace")
                        orig_node = child.child_by_field_name("name")
                        orig = None
                        if orig_node is not None:
                            idents = [c for c in orig_node.children if c.type == "identifier"]
                            orig = (
                                idents[-1].text.decode("utf-8", errors="replace")
                                if idents
                                else orig_node.text.decode("utf-8", errors="replace")
                            )
                        names.append((alias, module if alias == orig else None))
                    else:
                        dotted = child.child_by_field_name("name")
                        if dotted:
                            idents = [c for c in dotted.children if c.type == "identifier"]
                            if idents:
                                names.append(
                                    (idents[-1].text.decode("utf-8", errors="replace"), module)
                                )
    elif language == "typescript":
        # import { X, Y } from 'z'; import X from 'z'; import * as X from 'z'
        # EVERY TS import form carries its source, unlike Python — there is no
        # module-only form. The source is a quoted string node; the quotes are
        # stripped so a BARE specifier ('react', 'pkg/sub') matches the dotted
        # shape Python produces and one resolver can read both.
        #
        # A RELATIVE OR ALIASED SPECIFIER IS LEFT UNBOUND, deliberately, and for
        # the identical reason `from . import x` is (techne#679, Prudence r1
        # Critical 2). './util' is a path relative to the IMPORTING FILE, and
        # this parser does not have that file's position; '@/x' is a tsconfig
        # path alias whose meaning lives in a config this parser does not read.
        # Passing either through as a module is worse than passing nothing:
        # `_module_candidates` maps '.' to '/', so './util' becomes '//util.ts',
        # which matches no repo-relative path and lands the edge in `external` —
        # a row asserting as FACT that no symbol in this repo can be the callee,
        # about a file sitting in the index. Relative specifiers are how
        # essentially every in-repo TS import is written, so that mistake is the
        # common case, not the edge case. NULL here leaves the edge honestly
        # `unresolved` until something can resolve it properly.
        module = None
        src = node.child_by_field_name("source")
        if src is not None:
            spec = src.text.decode("utf-8", errors="replace").strip("\"'`")
            module = None if spec.startswith(_TS_ALIAS_SIGILS) else spec
        for child in node.children:
            if child.type == "import_clause":
                for sub in child.children:
                    if sub.type == "identifier":
                        names.append((sub.text.decode("utf-8", errors="replace"), module))
                    elif sub.type == "named_imports":
                        for spec in sub.children:
                            if spec.type == "import_specifier":
                                alias = spec.child_by_field_name("alias")
                                nm = spec.child_by_field_name("name")
                                target = alias or nm
                                if target:
                                    # A RENAMING alias drops the module; `X as X`
                                    # keeps it. Same rule as Python above.
                                    bound = target.text.decode("utf-8", errors="replace")
                                    orig = (
                                        nm.text.decode("utf-8", errors="replace")
                                        if nm is not None
                                        else bound
                                    )
                                    names.append((bound, module if bound == orig else None))
                    elif sub.type == "namespace_import":
                        ident = next((c for c in sub.children if c.type == "identifier"), None)
                        if ident:
                            names.append((ident.text.decode("utf-8", errors="replace"), module))
    return names


def _parse_file(
    file_path: Path,
    language: str,
    file_str: str,
    file_hash: str,
    source_bytes: bytes,
) -> tuple[list[dict], list[dict]]:
    """Parse *file_path* and return (symbols, edges).

    symbols: list of dicts ready for codegraph.add_symbol
    edges:   list of dicts ready for codegraph.add_edge
    """
    result = _GRAMMAR.get(language)
    if result is None:
        log.debug("codex_skip_no_grammar file=%s language=%s", file_path, language)
        return [], []

    lang, parser, query = result

    from tree_sitter import QueryCursor

    tree = parser.parse(source_bytes)
    root = tree.root_node

    total_lines = source_bytes.count(b"\n") + 1
    module_sym_id = f"{file_str}::<module>::module::0"

    symbols: list[dict] = []
    edges: list[dict] = []

    # Always emit the module-level symbol (anchor for module-scope calls/imports).
    symbols.append(
        {
            "id": module_sym_id,
            "file": file_str,
            "name": "<module>",
            "kind": "module",
            "signature": None,
            "span": f"0:{total_lines}",
            "language": language,
            "file_hash": file_hash,
        }
    )

    # Run the combined query; categorize matches by capture name.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cursor = QueryCursor(query)
        matches = cursor.matches(root)

    # Collect symbol definitions: build spans for enclosing-symbol lookup.
    # spans: list of (start_byte, end_byte, symbol_id)
    spans: list[tuple[int, int, str]] = [(root.start_byte, root.end_byte, module_sym_id)]

    call_matches: list[tuple] = []  # deferred for after symbols are collected
    import_matches: list[tuple] = []  # deferred for after symbols are collected

    for _pattern_idx, caps in matches:
        # Symbol definition
        if "symbol.function" in caps or "symbol.class" in caps or "symbol.method" in caps:
            kind_key = next(
                (
                    k
                    for k in (
                        "symbol.function",
                        "symbol.class",
                        "symbol.method",
                        "symbol.interface",
                    )
                    if k in caps
                ),
                None,
            )
            if kind_key is None:
                continue
            def_nodes = caps[kind_key]
            name_nodes = caps.get("symbol.name", [])
            for def_node, name_node in zip(def_nodes, name_nodes):
                name_str = name_node.text.decode("utf-8", errors="replace")
                start_line = def_node.start_point[0] + 1  # 1-indexed
                end_line = def_node.end_point[0] + 1

                # Determine kind
                if kind_key == "symbol.function":
                    is_method = language == "python" and _is_method_python(def_node)
                    kind = "method" if is_method else "function"
                elif kind_key == "symbol.class":
                    kind = "class"
                elif kind_key == "symbol.method":
                    kind = "method"
                elif kind_key == "symbol.interface":
                    kind = "interface"
                else:
                    kind = "function"

                sym_id = f"{file_str}::{name_str}::{kind}::{start_line}"
                if language == "python":
                    sig = f"def {name_str}(...)"
                else:
                    sig = f"function {name_str}(...)"
                symbols.append(
                    {
                        "id": sym_id,
                        "file": file_str,
                        "name": name_str,
                        "kind": kind,
                        "signature": sig,
                        "span": f"{start_line}:{end_line}",
                        "language": language,
                        "file_hash": file_hash,
                    }
                )
                spans.append((def_node.start_byte, def_node.end_byte, sym_id))

        # Calls — defer until symbols are all collected.
        elif "call.callee" in caps:
            call_matches.append(caps)

        # Imports — defer similarly.
        elif "import.range" in caps:
            import_matches.append(caps)

    # Emit call edges.
    seen_edges: set[tuple[str, str, str]] = set()
    for caps in call_matches:
        callee_nodes = caps.get("call.callee", [])
        range_nodes = caps.get("call.range", [])
        for callee_node, call_node in zip(callee_nodes, range_nodes):
            callee_name = callee_node.text.decode("utf-8", errors="replace")
            enclosing = _find_enclosing_symbol(call_node.start_byte, spans)
            from_sym = enclosing or module_sym_id
            key = (from_sym, callee_name, "call")
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append(
                    {
                        "from_symbol": from_sym,
                        "to_name": callee_name,
                        "kind": "call",
                        "src_file_hash": file_hash,
                    }
                )

    # Emit import edges (from the module symbol).
    for caps in import_matches:
        imp_nodes = caps.get("import.range", [])
        for imp_node in imp_nodes:
            for name, to_module in _extract_import_names(imp_node, language):
                # KEY EXCLUDES to_module ON PURPOSE — it mirrors the table's
                # PRIMARY KEY (from_symbol, to_name, kind). Two imports binding
                # the same name in one module is a shadow, not two edges: after
                # `from a import x` then `from b import x`, the name `x` refers
                # to b's, so first-write-wins here would record the wrong one.
                # Later bindings therefore REPLACE the module rather than being
                # skipped, matching the language's own scoping.
                key = (module_sym_id, name, "import")
                if key in seen_edges:
                    for e in edges:
                        if (
                            e["kind"] == "import"
                            and e["from_symbol"] == module_sym_id
                            and e["to_name"] == name
                        ):
                            e["to_module"] = to_module
                            break
                    continue
                seen_edges.add(key)
                edges.append(
                    {
                        "from_symbol": module_sym_id,
                        "to_name": name,
                        "kind": "import",
                        "src_file_hash": file_hash,
                        "to_module": to_module,
                    }
                )

    return symbols, edges


class IndexFailure(Exception):
    """Raised by _index_file_checked when one file could not be indexed.

    Exists because the old contract could not express failure: _index_file
    returns a symbol count, and 0 means BOTH "unchanged, nothing to do" and
    "blew up and rolled back". 461 files failed to index in a single run of this
    repo while the run exited 0 (techne#610). A caller that wants to report
    honestly needs the two apart.
    """


def _index_file(db_path: Path, file_path: Path, repo_root: Path) -> int:
    """Parse and index *file_path* into codex.db. Returns symbol count (0 = skipped).

    Incremental: reads the current file hash from the symbols table.  If the
    file is unchanged, returns 0 immediately.  On change or first-time index:
    deletes old symbols + edges for this file, inserts fresh rows.
    """
    from techne.core.stores import plain_connect

    language = _EXT_LANGUAGE.get(file_path.suffix.lower())
    if language is None:
        return 0

    try:
        source_bytes = file_path.read_bytes()
    except OSError as exc:
        log.warning("codex_read_error file=%s err=%s", file_path, exc)
        return 0

    file_hash = hashlib.sha256(source_bytes).hexdigest()

    try:
        file_str = str(file_path.relative_to(repo_root))
    except ValueError:
        file_str = str(file_path)

    conn = plain_connect(db_path)
    try:
        # Fast path: file unchanged.
        existing = conn.execute(
            "SELECT file_hash FROM symbols WHERE file = ? LIMIT 1", (file_str,)
        ).fetchone()
        if existing and existing[0] == file_hash:
            return 0

        # Delete stale data for THIS file. Scope edge deletion by the file's symbols,
        # NOT by content hash: two files with identical bytes share a SHA, so a
        # hash-keyed delete would wipe the OTHER file's edges (techne#170). An edge's
        # owning file is its from_symbol's file, so delete edges whose from_symbol is
        # one of this file's (about-to-be-replaced) symbols — before the symbols go.
        if existing:
            conn.execute(
                "DELETE FROM edges WHERE from_symbol IN (SELECT id FROM symbols WHERE file = ?)",
                (file_str,),
            )
            # INBOUND edges are the other half, and leaving them out is what made a
            # re-index fail with FOREIGN KEY on any file something RESOLVED calls into
            # (techne#610). `edges.to_symbol` is also REFERENCES symbols(id), and those
            # rows belong to OTHER files, so they are not ours to delete — the call is
            # still a true fact, only its resolved target is being replaced. Unresolve
            # them and let the re-insert re-resolve, mirroring what the v3 migration
            # does for the same asymmetry (#605).
            #
            # A plain walk never hits this: fresh edges leave to_symbol NULL and NULL
            # does not constrain. It needs a resolver pass to have populated it — and
            # then it selects for exactly the symbols that matter, because a file fails
            # to re-index PRECISELY BECAUSE something calls into it. The better
            # connected a module is, the more certainly it went stale.
            conn.execute(
                "UPDATE edges SET to_symbol = NULL, resolution_status = 'unresolved' "
                "WHERE to_symbol IN (SELECT id FROM symbols WHERE file = ?)",
                (file_str,),
            )
        conn.execute("DELETE FROM symbols WHERE file = ?", (file_str,))
        # NB: deliberately no commit here — delete + re-insert share one
        # transaction. A parse that yields nothing (or raises) rolls back the
        # deletes via finally/close, so the file keeps its prior good rows
        # rather than briefly "vanishing" from the graph.

        # Parse.
        symbols, edges = _parse_file(file_path, language, file_str, file_hash, source_bytes)
        if not symbols:
            return 0

        # Bulk-insert symbols.
        conn.executemany(
            """INSERT OR REPLACE INTO symbols
               (id, file, name, kind, signature, span, language, file_hash)
               VALUES (:id, :file, :name, :kind, :signature, :span, :language, :file_hash)""",
            symbols,
        )
        # Bulk-insert edges.
        if edges:
            # NORMALISED HERE, NOT AT EVERY EMISSION SITE (techne#679). Only
            # import edges carry `to_module`; call and def-use edges never do,
            # and `executemany` with named parameters raises on the first dict
            # missing a key rather than substituting NULL. Defaulting once at
            # the boundary means a new edge kind added later cannot break the
            # insert by forgetting a field that does not apply to it.
            for e in edges:
                e.setdefault("to_module", None)
            conn.executemany(
                """INSERT OR IGNORE INTO edges
                   (from_symbol, to_name, kind, src_file_hash, resolution_status, to_module)
                   VALUES (:from_symbol, :to_name, :kind, :src_file_hash, 'unresolved',
                           :to_module)""",
                edges,
            )
        conn.commit()
        log.debug("codex_indexed file=%s symbols=%d edges=%d", file_str, len(symbols), len(edges))
        return len(symbols)

    except Exception as exc:
        log.exception("codex_index_failed file=%s", file_path)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        # Best-effort per file is still the right posture — one unparseable file
        # must not abort a whole-repo walk. What changes is that the failure is now
        # RECOVERABLE INFORMATION rather than an indistinguishable 0: the rollback
        # leaves the file's PREVIOUS rows in place, so a silent 0 here means the
        # edit never entered the graph while the run reported success.
        raise IndexFailure(str(file_path)) from exc
    finally:
        conn.close()


def _walk_repo(
    root: Path,
    languages: set[str],
    *,
    onerror: Callable[[OSError], None] | None = None,
    on_symlink_dir: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    """Yield source files under *root*, skipping ignored directories.

    THE CALLBACKS EXIST BECAUSE THE OLD WALK COULD NOT FAIL OUT LOUD (techne#599).
    This was `root.rglob("*")`, and `rglob` swallows per-directory errors
    internally: an unreadable directory simply DROPS OUT of the walk — no
    exception, no signal. `_unindexed_count` guarded it with `except OSError` and
    documented a degraded result on failure; that handler was dead code, so a
    `chmod 000` directory holding the sought symbol vanished from enumeration
    while `coverage` still said "complete" and the caller asserted "This is
    genuine absence" with the symbol physically on disk.

    `onerror` receives per-directory OSErrors (unreadable/vanished directories);
    `on_symlink_dir` receives symlinked directories, which are NOT descended into
    — the same as before, but now observable rather than an unrecorded hole.
    Indexing ignores both by default, which keeps its long-standing best-effort
    behaviour; only callers that make a POSITIVE claim about completeness need to
    care, and now they can.

    os.walk rather than rglob also prunes `_SKIP_DIRS` before descending instead
    of walking a venv and discarding it afterwards.
    """
    exts = {ext for ext, lang in _EXT_LANGUAGE.items() if lang in languages}
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        here = Path(dirpath)
        # Skip ignored dirs by the path RELATIVE to the repo root. Testing
        # path.parts would also match the repo's own ancestors (a repo under
        # ~/build/ or a monorepo under target/), silently yielding zero files.
        try:
            rel_parts = here.relative_to(root).parts
        except ValueError:  # pragma: no cover — os.walk stays under root
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            dirnames[:] = []
            continue
        # Prune in place so os.walk never descends into them at all.
        kept = []
        for d in dirnames:
            if d in _SKIP_DIRS:
                continue
            if (here / d).is_symlink():
                # Not followed — unchanged from rglob — but no longer silent.
                if on_symlink_dir is not None:
                    on_symlink_dir(here / d)
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            path = here / name
            if path.suffix.lower() in exts and path.is_file():
                yield path


def _is_excluded_stored_path(file_str: str, repo_root: Path) -> bool:
    """True if a STORED ``symbols.file`` names a path `_walk_repo` would now skip.

    Mirrors the walk's rule rather than restating it: relativise first, then match
    exact case-sensitive segments. `_index_file` stores repo-relative paths and
    falls back to absolute when `relative_to` fails, so both shapes turn up here.
    An absolute path outside the repo is declined rather than guessed at — it is
    not this walker's to judge.
    """
    path = Path(file_str)
    if path.is_absolute():
        try:
            path = path.relative_to(repo_root)
        except ValueError:
            return False
    return any(part in _SKIP_DIRS for part in path.parts)


def sweep_excluded_rows(db_path: Path, repo_root: Path) -> int:
    """Delete stored rows the CURRENT skip list excludes. Returns the count (#613).

    THIS IS WHERE SKIP-LIST ENFORCEMENT BELONGS, and putting it in a migration was
    the mistake. Indexing is incremental and its only DELETE is scoped to the file
    being re-indexed, so rows for files the walker no longer visits are never
    revisited: nothing walks them, so nothing removes them. `_codex_v2_to_v3` fixed
    that ONCE, for the entries `_SKIP_DIRS` held at v3 — but a migration step runs
    exactly once, when `user_version` moves 2→3.

    MEASURED, NOT CONSTRUCTED (#613): a db stamped v3 by a review worktree running
    the PR code, then re-indexed from a tree that predated the skip-list change,
    came back holding 114,410 vendored symbols with `user_version = 3`. The purge
    can never fire again, and `index_freshness` reports `stale: 0, coverage:
    "complete"` over those rows because the files genuinely are present and
    unchanged on disk. They are not stale. They should not be there. That is the
    self-certifying state of #604 reached by a second route.

    Running the check on every full index makes a skip-list change self-healing:
    `_SKIP_DIRS` has already changed twice, and under the old design each change
    needed its own migration.

    DELIBERATELY NOT FAIL-SAFE. Every other optional step in `initial_walk` (the
    `repo_root` persist) degrades to a warning, and this one does not, because a
    sweep that fails quietly returns the caller to precisely the silent pollution
    it exists to end — a db that looks healed and is not. Loud beats tidy here.
    """
    from techne.core.codex import purge_symbol_ids
    from techne.core.stores import plain_connect

    conn = plain_connect(db_path)
    try:
        doomed = [
            row[0]
            for row in conn.execute("SELECT id, file FROM symbols")
            if _is_excluded_stored_path(row[1], repo_root)
        ]
        if not doomed:
            return 0
        purge_symbol_ids(conn, doomed)
        conn.commit()
        log.info("codex_swept_excluded root=%s symbols=%d", repo_root, len(doomed))
        return len(doomed)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public single-file surface — for external callers (e.g. the reindex-on-edit
# freshness path, techne#301) that need one file indexed without a RepoSource.
# Depend on these, NOT the leading-underscore internals.
# ---------------------------------------------------------------------------


def _index_file_safe(db_path: Path, file_path: Path, repo_root: Path) -> tuple[int, bool]:
    """(symbol_count, failed). Best-effort per file — one bad file must never abort
    a whole-repo walk — but the failure is COUNTABLE instead of an anonymous 0."""
    try:
        return _index_file(db_path, file_path, repo_root), False
    except IndexFailure:
        return 0, True


def index_file(db_path: Path, file_path: Path, repo_root: Path) -> int:
    """Index a single file into a repo's codex db; returns the symbol count
    (incremental — an unchanged file is a no-op via its stored hash).

    Stays best-effort and int-returning for its existing callers (the
    reindex-on-edit rail, techne#301); the traceback is already logged by
    _index_file. Callers that must distinguish "unchanged" from "failed" should
    use _index_file_safe or catch IndexFailure directly."""
    count, _failed = _index_file_safe(db_path, file_path, repo_root)
    return count


def is_indexable_path(file_path: Path) -> bool:
    """True if this file's extension maps to a codex-indexable language."""
    return file_path.suffix.lower() in _EXT_LANGUAGE


# ---------------------------------------------------------------------------
# RepoSource — the SourceAdapter
# ---------------------------------------------------------------------------


class RepoSource:
    """SourceAdapter that indexes a repository's Python + TypeScript symbols.

    Satisfies the SourceAdapter Protocol (techne.sync.adapter.SourceAdapter).
    Drives codex.db via the tree-sitter extraction layer; no core techne.db
    tables are touched — codex is a fully separate domain store.

    Usage (from the CLI / tests):
        src = RepoSource(codex_db_path, Path("/path/to/repo"))
        src.initial_walk()   # one-shot full index
        src.start_watch()    # continuous file-change mode
        src.stop_watch()
    """

    def __init__(
        self,
        db_path: Path,
        repo_root: Path,
        *,
        languages: list[str] | None = None,
        debounce_ms: int = 300,
    ) -> None:
        self._db_path = db_path
        self._root = repo_root
        self._languages: set[str] = set(languages or ["python", "typescript"])
        self._debounce_ms = debounce_ms
        self._observer = None
        self._debouncer = None
        self._lock = threading.Lock()
        #: Rows removed by the last `initial_walk`'s skip-list sweep (#613). Read
        #: by the CLI so a heal is REPORTED rather than inferred from a shrinking
        #: total — a purge nobody is told about is how #604 stayed invisible.
        self.last_swept = 0
        #: Files the last initial_walk could not index. Non-zero means the graph is
        #: stale for those paths and the run must not be read as a success (#610).
        self.last_failed = 0

    # ------------------------------------------------------------------
    # SourceAdapter Protocol
    # ------------------------------------------------------------------

    def initial_walk(self) -> int:
        """Walk the repo, index all source files. Returns total symbol count.

        Also persists the repo_root absolute path in codex_meta so query-time
        staleness checks can resolve relative file paths without caller input.
        """
        total = 0
        files = 0
        failed = 0
        for path in _walk_repo(self._root, self._languages):
            files += 1
            n, did_fail = _index_file_safe(self._db_path, path, self._root)
            total += n
            failed += did_fail
        # A run that failed to index 461 files must SAY SO rather than exit 0
        # (techne#610). Each failure rolls back, so the file silently keeps its
        # previous rows: the graph is stale exactly where the run looked fine.
        self.last_failed = failed

        # Enforce the CURRENT skip list against what is already stored (#613).
        # The walk above cannot do this on its own: it only visits files it is
        # willing to index, so rows written when the skip list was shorter are
        # never revisited. Runs on every full index, which is what makes a
        # skip-list change self-healing rather than needing a migration each time.
        self.last_swept = sweep_excluded_rows(self._db_path, self._root)
        log.info(
            "codex_initial_walk root=%s files=%d symbols=%d swept=%d failed=%d",
            self._root,
            files,
            total,
            self.last_swept,
            failed,
        )
        if failed:
            log.warning(
                "codex_index_incomplete root=%s failed=%d of %d files — those files kept "
                "their PREVIOUS rows, so the graph is stale where this run looked clean "
                "(tenishai/techne#610)",
                self._root,
                failed,
                files,
            )

        # Persist repo_root so annotate_freshness can resolve relative paths.
        try:
            from techne.core.codex import set_meta

            set_meta(self._db_path, "repo_root", str(self._root.resolve()))
        except Exception:  # noqa: BLE001
            log.warning("codex: failed to persist repo_root in codex_meta", exc_info=True)

        # No-silent-failures guard: a non-empty repo that produces an empty graph
        # means grammars are missing or the layout is unsupported. total==0 alone
        # is normal on a fully-unchanged re-index, so check the store itself.
        if files and total == 0:
            from techne.core.stores import plain_connect

            conn = plain_connect(self._db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            finally:
                conn.close()
            if count == 0:
                log.warning(
                    "codex: walked %d source file(s) under %s but produced an empty "
                    "graph — tree-sitter grammars missing (`pip install 'techne[codex]'`) "
                    "or an unsupported repo layout?",
                    files,
                    self._root,
                )
        return total

    def start_watch(self) -> None:
        """Start a watchdog observer for continuous re-indexing. Non-blocking."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise RuntimeError(
                "watchdog is not installed; run `pip install techne[codex]`"
            ) from exc

        from techne.watch import Debouncer

        valid_exts = {ext for ext, lang in _EXT_LANGUAGE.items() if lang in self._languages}
        db_path = self._db_path
        root = self._root

        def _on_change(path: Path) -> None:
            if path.suffix.lower() not in valid_exts:
                return
            with self._lock:
                n, did_fail = _index_file_safe(db_path, path, root)
                if did_fail:
                    log.warning(
                        "codex_watch_reindex_failed file=%s — kept its previous rows (#610)",
                        path,
                    )
                elif n:
                    log.info("codex_watch_reindex file=%s symbols=%d", path, n)

        self._debouncer = Debouncer(_on_change, self._debounce_ms)
        debouncer = self._debouncer

        class _Handler(FileSystemEventHandler):
            def on_created(self, event) -> None:
                if not event.is_directory:
                    debouncer.schedule(Path(event.src_path))

            def on_modified(self, event) -> None:
                if not event.is_directory:
                    debouncer.schedule(Path(event.src_path))

        observer = Observer()
        observer.schedule(_Handler(), str(self._root), recursive=True)
        self._observer = observer
        observer.start()
        log.info("codex_watch_started root=%s", self._root)

    def stop_watch(self) -> None:
        """Stop the watchdog observer and flush pending debounced events."""
        if self._debouncer:
            self._debouncer.flush()
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        log.info("codex_watch_stopped")

    def health(self) -> bool:
        """True if the repo root exists."""
        return self._root.exists()
