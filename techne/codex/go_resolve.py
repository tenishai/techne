"""Resolve Go call edges without a toolchain (techne#1284).

Go needs its own pass because both assumptions behind `import_resolve` fail:

- A package is a DIRECTORY, not a file. A bare `helper()` reaches any function
  in any file of the caller's package, so the same-file scope pass misses most
  of them.
- An import binds a PACKAGE, not the called name. `store.Get()` is an edge to
  `Get` whose binding lives in the call's qualifier (`store`), which the
  (file, name) lookup in `resolve_edges_by_import` never sees. The qualifier is
  read from `edges.call_sites` (schema v7).

Three outcomes, each written only on positive evidence, never on a failed lookup:

- `scope_resolved`: every site is a bare call, and the caller's package (same
  directory, same `package` clause) holds exactly one module-level function or
  struct of that name.
- `import_resolved`: every site is qualified by the same identifier, the file
  imports a package under that name, the import path maps through a `go.mod`
  module path to an indexed directory, and that package holds exactly one
  module-level function or struct of that name.
- `external`: the caller's own directory is governed by a found `go.mod` and
  the import path is outside every in-repo module (Go import paths are
  absolute, so that is a fact), or the path is standard-library shaped, or a
  bare call names a predeclared identifier (`len`, `append`, `string`) that the
  package does not shadow.

LEFT UNRESOLVED, deliberately: calls through a value or a field (`s.db.Get()`,
`clients.PubSub.NewSubscriber()`), which need types and are issue #324's job;
rows mixing bare and qualified sites, or two names for different paths; calls
through a dot import; and build-tagged twins, which are ambiguous.

NOT PROVEN, AND IT CAN WRITE A FALSE FACT: this table records no local
bindings, so a local variable or parameter that reuses a name misleads every
outcome above. Three shapes, all accepted as the Python scope pass accepts its
own (card, v5 paragraph):

- a local shadowing an imported package name (`store := x; store.Get()`)
  binds `import_resolved` to that package's `Get`;
- a parameter or local shadowing a package-level function
  (`func run(helper func()) { helper() }`) binds `scope_resolved` to the
  package's `helper`;
- a local shadowing a predeclared name (`close := func() {}; close()`, or a
  parameter named `min`) is written `external`.

Go linters flag the first two. Ruling them out needs scopes, which is gopls
(issue #324).
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

from techne.codex.import_resolve import _OVERWRITABLE, _nested_symbol_ids
from techne.codex.source import go_default_package_name
from techne.core.codegraph import decode_call_sites

log = logging.getLogger(__name__)

#: Go's predeclared functions and types, which a bare call can name without any
#: declaration (a conversion `string(b)` parses as a call). A package may
#: shadow any of them, so each is `external` only when the package does not.
GO_PREDECLARED = frozenset(
    """append cap clear close complex copy delete imag len make max min new panic
    print println real recover any bool byte comparable complex64 complex128 error
    float32 float64 int int8 int16 int32 int64 rune string uint uint8 uint16 uint32
    uint64 uintptr""".split()
)


def _parse_go_mod(path: Path) -> str | None:
    """The module path declared in a go.mod file, or None if it has none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip()
        if line.startswith("module ") or line.startswith("module\t"):
            return line[len("module") :].strip().strip('"`') or None
    return None


#: `_Modules.dir_for` could not choose between modules declaring one path.
AMBIGUOUS = "<ambiguous>"


class _Modules:
    """The go.mod files governing the indexed Go dirs, and how an import maps through them.

    Found by walking up from each directory that holds indexed Go files, so a
    repo with several modules (a go.work workspace, or nested modules) maps each
    import to the module Go would use: the LONGEST module path the import path
    falls under, so a nested module wins over its parent.

    THE GOVERNED SET IS WHAT MAKES `external` A FACT (Prudence, PR #1285 r1).
    "Outside every found module" is evidence only for a caller that a found
    module governs: a module-less directory beside a nested `go.mod` imports by
    some other root, and ruling its in-repo imports external asserted that a
    callee sitting in the index could not be one.

    TWO go.mod FILES MAY DECLARE ONE PATH (Prudence, PR #1285 r2): several
    `examples/*/` each made with `go mod init example`, or a copied module kept
    beside its original. Keying modules by path alone kept whichever directory
    the set walk reached first, so a call bound into the OTHER module's package,
    and two runs over one tree could disagree. Every directory is kept now: the
    caller's own module wins when it is one of them, and otherwise the import
    is AMBIGUOUS, which the resolver counts as unplaced. The path stays known
    either way, so a governed caller's import of it is never ruled external.
    """

    def __init__(self, repo_root: Path | None, go_dirs: set[str]) -> None:
        self._dirs_by_path: dict[str, set[str]] = {}
        #: dir -> the directory of the go.mod governing it, or None.
        self._owner: dict[str, str | None] = {}
        self._path_of_mod_dir: dict[str, str] = {}
        if repo_root is not None:
            for d in sorted(go_dirs):
                self._walk_up(repo_root, d)
        self._paths = sorted(self._dirs_by_path, key=len, reverse=True)

    def _walk_up(self, repo_root: Path, d: str) -> None:
        chain = []
        cur = d
        owner: str | None = None
        while True:
            if cur in self._owner:
                owner = self._owner[cur]
                break
            chain.append(cur)
            mod = _parse_go_mod(repo_root / cur / "go.mod")
            if mod is not None:
                self._dirs_by_path.setdefault(mod, set()).add(cur)
                self._path_of_mod_dir[cur] = mod
                owner = cur
                break
            if cur in ("", "."):
                break
            cur = posixpath.dirname(cur)
        for c in chain:
            self._owner[c] = owner

    def governs(self, caller_dir: str) -> bool:
        return self._owner.get(caller_dir) is not None

    def dir_for(self, import_path: str, caller_dir: str) -> str | None:
        """The repo-relative directory *import_path* names from *caller_dir*.

        None when it falls under no found module, AMBIGUOUS when two
        directories declare the module it falls under and neither governs the
        caller.
        """
        for mod in self._paths:
            if import_path != mod and not import_path.startswith(mod + "/"):
                continue
            dirs = self._dirs_by_path[mod]
            own = self._owner.get(caller_dir)
            if len(dirs) == 1:
                mod_dir = next(iter(dirs))
            elif own in dirs:
                mod_dir = own
            else:
                return AMBIGUOUS
            rest = import_path[len(mod) + 1 :]
            if not rest:
                return mod_dir
            return posixpath.normpath(posixpath.join(mod_dir, rest)) if mod_dir else rest
        return None


def _is_stdlib(import_path: str) -> bool:
    """A standard-library path: its first element has no dot (`net/http`, `fmt`)."""
    return "." not in import_path.split("/", 1)[0]


def resolve_go_edges(conn, repo_root: Path | None = None) -> dict:
    """Bind Go call edges by package scope and by import. Returns a report.

    *repo_root* defaults to the index's recorded `codex_meta.repo_root`, which
    is where the go.mod files are read from. Without one, only same-package
    calls, predeclared names and standard-library imports are resolved.
    Idempotent: it only touches `_OVERWRITABLE` rows, as the other passes do.
    """
    if repo_root is None:
        row = conn.execute("SELECT value FROM codex_meta WHERE key = 'repo_root'").fetchone()
        repo_root = Path(row[0]) if row and row[0] else None

    # file -> (dir, package name), from the module symbol's `package X` signature.
    pkg_of: dict[str, tuple[str, str | None]] = {}
    for file, sig in conn.execute(
        "SELECT file, signature FROM symbols WHERE kind = 'module' AND language = 'go'"
    ):
        name = sig[len("package ") :] if sig and sig.startswith("package ") else None
        pkg_of[file] = (posixpath.dirname(file), name)
    if not pkg_of:
        return {"call_edges_examined": 0, "scope_resolved": 0, "import_resolved": 0,
                "external": 0}

    nested = _nested_symbol_ids(conn)
    # (dir, package) -> name -> module-level function/struct ids. Methods are
    # left out: neither a bare call nor `pkg.F()` can reach one.
    members: dict[tuple[str, str | None], dict[str, list[str]]] = {}
    for sid, file, name in conn.execute(
        "SELECT id, file, name FROM symbols "
        "WHERE language = 'go' AND kind IN ('function', 'class')"
    ):
        if sid in nested or file not in pkg_of:
            continue
        members.setdefault(pkg_of[file], {}).setdefault(name, []).append(sid)

    # dir -> the non-test package names declared there.
    packages_in: dict[str, set[str]] = {}
    for d, name in pkg_of.values():
        if name and not name.endswith("_test"):
            packages_in.setdefault(d, set()).add(name)

    modules = _Modules(repo_root, {d for d, _ in pkg_of.values()})

    # file -> qualifier -> import path. An unaliased import of an in-repo package
    # also binds the name in that package's clause, which the path-based guess
    # in the parser can get wrong (`.../go-foo` holding `package foo`).
    imports: dict[str, dict[str, str]] = {}
    for file, to_name, path in conn.execute(
        """SELECT s.file, e.to_name, e.to_module
             FROM edges e JOIN symbols s ON s.id = e.from_symbol
            WHERE e.kind = 'import' AND s.language = 'go' AND e.to_module IS NOT NULL"""
    ):
        binds = imports.setdefault(file, {})
        binds[to_name] = path
        if to_name == go_default_package_name(path):
            d = modules.dir_for(path, posixpath.dirname(file))
            names = packages_in.get(d, set()) if d not in (None, AMBIGUOUS) else set()
            if len(names) == 1:
                binds.setdefault(next(iter(names)), path)

    rows = conn.execute(
        f"""SELECT e.rowid, s.file, e.to_name, e.call_sites
              FROM edges e JOIN symbols s ON s.id = e.from_symbol
             WHERE e.kind = 'call'
               AND s.language = 'go'
               AND e.to_symbol IS NULL
               AND e.resolution_status IN ({",".join("?" * len(_OVERWRITABLE))})""",
        _OVERWRITABLE,
    ).fetchall()

    counts: dict[str, int] = dict.fromkeys(
        ("scope_resolved", "import_resolved", "external", "no_sites", "no_local_symbol",
         "ambiguous", "not_a_package", "in_repo_no_symbol", "unplaced", "mixed_or_typed"),
        0,
    )

    def bind(rowid: int, target: str, status: str) -> None:
        conn.execute(
            "UPDATE edges SET to_symbol = ?, resolution_status = ? WHERE rowid = ?",
            (target, status, rowid),
        )
        counts[status] += 1

    def mark_external(rowid: int) -> None:
        conn.execute("UPDATE edges SET resolution_status = 'external' WHERE rowid = ?", (rowid,))
        counts["external"] += 1

    for rowid, file, name, raw_sites in rows:
        sites = decode_call_sites(raw_sites)
        if not sites:
            counts["no_sites"] += 1  # indexed before v7; a re-parse fills it
            continue
        quals = {q for _, q in sites}

        if quals == {None}:
            local = members.get(pkg_of.get(file, ("", None)), {}).get(name, [])
            if len(local) == 1:
                bind(rowid, local[0], "scope_resolved")
            elif not local and name in GO_PREDECLARED:
                mark_external(rowid)
            elif not local:
                counts["no_local_symbol"] += 1
            else:
                counts["ambiguous"] += 1
            continue

        if None in quals or "*" in quals:
            counts["mixed_or_typed"] += 1
            continue

        # Two names for one import path (`messaging.F()` and `msg.F()` after
        # `msg "x/messaging"`) are one target, so the test is on the PATH.
        binds = imports.get(file, {})
        found = [binds.get(q) for q in quals]
        paths = {p for p in found if p is not None}
        if None in found:
            counts["not_a_package"] += 1  # a variable or receiver: needs types (#324)
            continue
        if len(paths) != 1:
            counts["mixed_or_typed"] += 1
            continue
        path = next(iter(paths))
        d = modules.dir_for(path, pkg_of[file][0])
        if d == AMBIGUOUS:
            counts["unplaced"] += 1
            continue
        if d is None:
            # Outside every in-repo module is positive evidence only when a
            # found go.mod governs THIS caller; otherwise only the standard
            # library can be ruled out.
            if _is_stdlib(path) or modules.governs(pkg_of[file][0]):
                mark_external(rowid)
            else:
                counts["unplaced"] += 1
            continue
        names = packages_in.get(d, set())
        if len(names) > 1:
            names = {n for n in names if n in quals}
        if len(names) != 1:
            counts["unplaced"] += 1
            continue
        target = members.get((d, next(iter(names))), {}).get(name, [])
        if len(target) == 1:
            bind(rowid, target[0], "import_resolved")
        elif not target:
            counts["in_repo_no_symbol"] += 1
        else:
            counts["ambiguous"] += 1

    conn.commit()
    report = {"call_edges_examined": len(rows), **counts}
    log.debug("codex_go_resolve %s", report)
    return report
