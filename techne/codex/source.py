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
import threading
from pathlib import Path
from typing import Iterator

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


def _extract_import_names(node, language: str) -> list[str]:
    """Pull the leaf imported names out of an import node."""
    names: list[str] = []
    if language == "python":
        node_type = node.type
        if node_type == "import_statement":
            # import foo, import foo as bar, import a.b.c
            for child in node.children:
                if child.type == "dotted_name":
                    # last identifier is the usable name
                    idents = [c for c in child.children if c.type == "identifier"]
                    if idents:
                        names.append(idents[-1].text.decode("utf-8", errors="replace"))
                elif child.type == "aliased_import":
                    # import json as j → the alias (j) is what's in scope
                    alias_node = child.child_by_field_name("alias")
                    if alias_node:
                        names.append(alias_node.text.decode("utf-8", errors="replace"))
                    else:
                        # no alias — use the module's last component
                        dotted = child.child_by_field_name("name")
                        if dotted:
                            idents = [c for c in dotted.children if c.type == "identifier"]
                            if idents:
                                names.append(idents[-1].text.decode("utf-8", errors="replace"))
        elif node_type == "import_from_statement":
            # from foo import bar, baz
            in_names = False
            for child in node.children:
                if child.type == "import":
                    in_names = True
                    continue
                if in_names and child.type == "dotted_name":
                    idents = [c for c in child.children if c.type == "identifier"]
                    if idents:
                        names.append(idents[-1].text.decode("utf-8", errors="replace"))
                elif in_names and child.type == "aliased_import":
                    alias_node = child.child_by_field_name("alias")
                    if alias_node:
                        names.append(alias_node.text.decode("utf-8", errors="replace"))
                    else:
                        dotted = child.child_by_field_name("name")
                        if dotted:
                            idents = [c for c in dotted.children if c.type == "identifier"]
                            if idents:
                                names.append(idents[-1].text.decode("utf-8", errors="replace"))
    elif language == "typescript":
        # import { X, Y } from 'z'; import X from 'z'; import * as X from 'z'
        for child in node.children:
            if child.type == "import_clause":
                for sub in child.children:
                    if sub.type == "identifier":
                        names.append(sub.text.decode("utf-8", errors="replace"))
                    elif sub.type == "named_imports":
                        for spec in sub.children:
                            if spec.type == "import_specifier":
                                alias = spec.child_by_field_name("alias")
                                nm = spec.child_by_field_name("name")
                                target = alias or nm
                                if target:
                                    names.append(target.text.decode("utf-8", errors="replace"))
                    elif sub.type == "namespace_import":
                        ident = next((c for c in sub.children if c.type == "identifier"), None)
                        if ident:
                            names.append(ident.text.decode("utf-8", errors="replace"))
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
            for name in _extract_import_names(imp_node, language):
                key = (module_sym_id, name, "import")
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(
                        {
                            "from_symbol": module_sym_id,
                            "to_name": name,
                            "kind": "import",
                            "src_file_hash": file_hash,
                        }
                    )

    return symbols, edges


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
            conn.executemany(
                """INSERT OR IGNORE INTO edges
                   (from_symbol, to_name, kind, src_file_hash, resolution_status)
                   VALUES (:from_symbol, :to_name, :kind, :src_file_hash, 'unresolved')""",
                edges,
            )
        conn.commit()
        log.debug("codex_indexed file=%s symbols=%d edges=%d", file_str, len(symbols), len(edges))
        return len(symbols)

    except Exception:
        log.exception("codex_index_failed file=%s", file_path)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        conn.close()


def _walk_repo(root: Path, languages: set[str]) -> Iterator[Path]:
    """Yield source files under *root*, skipping ignored directories."""
    exts = {ext for ext, lang in _EXT_LANGUAGE.items() if lang in languages}
    for path in root.rglob("*"):
        # Skip ignored dirs by the path RELATIVE to the repo root. Testing
        # path.parts would also match the repo's own ancestors (a repo under
        # ~/build/ or a monorepo under target/), silently yielding zero files.
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in exts:
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


def index_file(db_path: Path, file_path: Path, repo_root: Path) -> int:
    """Index a single file into a repo's codex db; returns the symbol count
    (incremental — an unchanged file is a no-op via its stored hash)."""
    return _index_file(db_path, file_path, repo_root)


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
        for path in _walk_repo(self._root, self._languages):
            files += 1
            total += _index_file(self._db_path, path, self._root)

        # Enforce the CURRENT skip list against what is already stored (#613).
        # The walk above cannot do this on its own: it only visits files it is
        # willing to index, so rows written when the skip list was shorter are
        # never revisited. Runs on every full index, which is what makes a
        # skip-list change self-healing rather than needing a migration each time.
        self.last_swept = sweep_excluded_rows(self._db_path, self._root)
        log.info(
            "codex_initial_walk root=%s files=%d symbols=%d swept=%d",
            self._root,
            files,
            total,
            self.last_swept,
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
                n = _index_file(db_path, path, root)
                if n:
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
