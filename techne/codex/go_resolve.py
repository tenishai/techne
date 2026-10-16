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
- `external`: the import path is outside every in-repo module (Go import paths
  are absolute, so this is a fact, not a guess), or a bare call names a
  predeclared identifier (`len`, `append`, `string`) that the package does not
  shadow.

LEFT UNRESOLVED, deliberately: calls through a value or a field (`s.db.Get()`,
`clients.PubSub.NewSubscriber()`), which need types and are issue #324's job;
rows mixing bare and qualified sites, or two qualifiers; calls through a dot
import; and names rebound by a local variable or parameter, which this table
cannot see (the same accepted gap as the Python scope pass). A local variable
that shadows an imported package name is the one case that can bind wrongly; Go
linters flag it, and it is recorded here rather than guessed around.
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


def _module_roots(repo_root: Path | None, go_dirs: set[str]) -> list[tuple[str, str]]:
    """(module path, repo-relative dir) for every go.mod governing an indexed Go dir.

    Found by walking up from each directory that holds indexed Go files, so a
    repo with several modules (a go.work workspace, or nested modules) maps
    each import to the right one. Longest module path first, so a nested
    module wins over the module that contains it.
    """
    if repo_root is None:
        return []
    found: dict[str, str] = {}
    seen: set[str] = set()
    for d in go_dirs:
        cur = d
        while cur not in seen:
            seen.add(cur)
            mod = _parse_go_mod(repo_root / cur / "go.mod")
            if mod is not None:
                found.setdefault(mod, cur)
                break
            if cur in ("", "."):
                break
            cur = posixpath.dirname(cur)
    return sorted(found.items(), key=lambda kv: len(kv[0]), reverse=True)


def _import_dir(import_path: str, modules: list[tuple[str, str]]) -> str | None:
    """The repo-relative directory an import path names, or None if it is not in the repo."""
    for mod, mod_dir in modules:
        if import_path == mod:
            return mod_dir
        if import_path.startswith(mod + "/"):
            rest = import_path[len(mod) + 1 :]
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

    modules = _module_roots(repo_root, {d for d, _ in pkg_of.values()})

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
            d = _import_dir(path, modules)
            names = packages_in.get(d, set()) if d is not None else set()
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
        d = _import_dir(path, modules)
        if d is None:
            # Outside every in-repo module is positive evidence only when some
            # module was found to compare against; with no go.mod in reach, only
            # the standard library can be ruled out.
            if modules or _is_stdlib(path):
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
