"""Go in codex (techne#1284): symbols, call-site lines, and the Go resolver.

The case that asked for it: on a Go service, `codex index` wrote nothing, and the
question an index could have answered was "list every call site of
`NewSubscriber`", whose calls crossed three packages, several of them inside one
function, one through a struct field (`clients.PubSub.NewSubscriber(...)`).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from techne.codex.go_resolve import resolve_go_edges
from techne.codex.import_resolve import resolve_edges_by_import, resolve_edges_by_scope
from techne.codex.source import (
    RepoSource,
    _extract_import_names,
    _walk_repo,
    go_default_package_name,
    index_file,
)
from techne.core.codegraph import callers, decode_call_sites, encode_call_sites
from techne.core.codex import CODEX_SCHEMA


def _has_go_grammar() -> bool:
    try:
        import tree_sitter_go  # noqa: F401

        return True
    except ImportError:
        return False


#: Only the tests that PARSE Go need the grammar. The migration, the pre-v7 read,
#: the encoding and the walk run without it, so an older `[codex]` install still
#: exercises them rather than skipping the whole file (Prudence, PR #1285 r1).
needs_go = pytest.mark.skipif(not _has_go_grammar(), reason="tree-sitter-go not installed")

GO_MOD = "module example.com/papi\n\ngo 1.22\n"


def _write(root, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _db(tmp_path, files: dict[str, str]):
    _write(tmp_path, files)
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in files:
        if rel.endswith(".go"):
            index_file(db, tmp_path / rel, tmp_path)
    return db


def _resolve(db, root):
    conn = sqlite3.connect(db)
    try:
        return resolve_go_edges(conn, repo_root=root)
    finally:
        conn.close()


def _call(db, caller: str, to_name: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            """SELECT e.resolution_status, e.to_symbol, e.call_sites
                 FROM edges e JOIN symbols s ON s.id = e.from_symbol
                WHERE e.kind = 'call' AND s.name = ? AND e.to_name = ?""",
            (caller, to_name),
        ).fetchone()
    finally:
        conn.close()


def _symbols(db):
    conn = sqlite3.connect(db)
    try:
        return {
            (r[0], r[1]): r[2]
            for r in conn.execute("SELECT name, kind, signature FROM symbols")
        }
    finally:
        conn.close()


def _import_node(src: str):
    import tree_sitter_go as tsg
    from tree_sitter import Language, Parser

    tree = Parser(Language(tsg.language())).parse(src.encode())
    found = []

    def walk(n):
        if n.type == "import_spec":
            found.append(n)
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    return found


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@needs_go
@pytest.mark.parametrize(
    "spec,expected",
    [
        ('"example.com/papi/store"', [("store", "example.com/papi/store")]),
        ('st "example.com/papi/store"', [("st", "example.com/papi/store")]),
        ("`net/http`", [("http", "net/http")]),
        # A blank import binds nothing; a dot import binds names with no
        # qualifier, which this resolver cannot follow. Both record nothing.
        ('_ "example.com/driver"', []),
        ('. "example.com/dsl"', []),
    ],
)
def test_go_import_binds_the_package_name_and_records_the_path(spec, expected):
    (node,) = _import_node(f"package p\nimport {spec}\n")
    assert _extract_import_names(node, "go") == expected


@pytest.mark.parametrize(
    "path,name",
    [
        ("fmt", "fmt"),
        ("net/http", "http"),
        ("github.com/redis/go-redis/v9", "go-redis"),
        ("gopkg.in/yaml.v3", "yaml"),
        ("example.com/papi/v2/store", "store"),
    ],
)
def test_default_package_name_follows_the_convention(path, name):
    assert go_default_package_name(path) == name


@needs_go
def test_every_go_kind_is_indexed_with_a_go_signature(tmp_path):
    db = _db(
        tmp_path,
        {
            "m/m.go": (
                "package messaging\n\n"
                "type Client struct{}\n\n"
                "type Subscriber interface{ Receive() error }\n\n"
                "func NewSubscriber(name string) *Client { return &Client{} }\n\n"
                "func (c *Client) Close() error { return nil }\n"
            )
        },
    )
    syms = _symbols(db)
    assert syms[("<module>", "module")] == "package messaging"
    assert syms[("Client", "class")] == "type Client struct"
    assert syms[("Subscriber", "interface")] == "interface Subscriber"
    assert syms[("NewSubscriber", "function")] == "func NewSubscriber(...)"
    # The receiver is what tells two same-named methods apart in a search.
    assert syms[("Close", "method")] == "func (*Client) Close(...)"


@needs_go
def test_every_call_site_keeps_its_line_and_qualifier(tmp_path):
    """Seven calls inside one function were one edge with no line before v7."""
    db = _db(
        tmp_path,
        {
            "s/s.go": (
                "package server\n\n"
                'import "example.com/papi/messaging"\n\n'
                "func Setup(c Clients) {\n"
                '\tmessaging.NewSubscriber("a")\n'  # line 6
                '\tmessaging.NewSubscriber("b")\n'  # line 7
                '\tc.PubSub.NewSubscriber("c")\n'  # line 8
                '\tc.NewSubscriber("d")\n'  # line 9
                '\tNewSubscriber("e")\n'  # line 10
                "}\n"
            )
        },
    )
    status, _target, raw = _call(db, "Setup", "NewSubscriber")
    assert decode_call_sites(raw) == [
        (6, "messaging"),
        (7, "messaging"),
        (8, "*"),
        (9, "c"),
        (10, None),
    ]
    assert status == "unresolved"


def test_call_sites_encode_deterministically():
    """The same sites always give the same string (incremental == rebuild, #1055)."""
    a = encode_call_sites({(9, "x"), (3, None), (9, None)})
    b = encode_call_sites([(9, None), (9, "x"), (3, None)])
    assert a == b == json.dumps([[3, None], [9, None], [9, "x"]], separators=(",", ":"))


def test_python_and_typescript_calls_carry_sites_too(tmp_path):
    db = _db(tmp_path, {})
    _write(tmp_path, {"a.py": "import os\n\ndef f():\n    os.getcwd()\n    g()\n"})
    index_file(db, tmp_path / "a.py", tmp_path)
    assert decode_call_sites(_call(db, "f", "getcwd")[2]) == [(4, "os")]
    assert decode_call_sites(_call(db, "f", "g")[2]) == [(5, None)]


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def test_vendor_and_testdata_are_skipped_for_go_only(tmp_path):
    """`vendor/` in a Python repo can be real code; v3's purge tests rely on it."""
    _write(
        tmp_path,
        {
            "main.go": "package main\n",
            "vendor/x/x.go": "package x\n",
            "pkg/testdata/bad.go": "package bad\nfunc F( {\n",
            "vendor/lib.py": "def f(): pass\n",
        },
    )
    walked = {str(p.relative_to(tmp_path)) for p in _walk_repo(tmp_path, {"python", "go"})}
    assert walked == {"main.go", "vendor/lib.py"}


@needs_go
def test_go_is_in_the_default_languages(tmp_path):
    _write(tmp_path, {"main.go": "package main\n\nfunc main() {}\n"})
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.close()
    assert RepoSource(db, tmp_path).initial_walk() >= 2


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

PAPI = {
    "go.mod": GO_MOD,
    "main.go": (
        "package main\n\n"
        'import (\n\t"fmt"\n\t"example.com/papi/server"\n)\n\n'
        "func main() {\n"
        '\tfmt.Println("start")\n'
        "\tserver.SetupStorage()\n"
        "\trun()\n"
        "}\n"
    ),
    "run.go": 'package main\n\nfunc run() { _ = len("x") }\n',
    "server/server.go": (
        "package server\n\n"
        'import msg "example.com/papi/messaging"\n\n'
        'func SetupStorage() { msg.NewSubscriber("a") }\n'
    ),
    "messaging/messaging.go": (
        "package messaging\n\n"
        "type Client struct{}\n\n"
        "func NewSubscriber(name string) *Client { return &Client{} }\n\n"
        "func (c *Client) NewSubscriber(name string) *Client { return c }\n"
    ),
}


@needs_go
def test_a_bare_call_resolves_across_files_of_one_package(tmp_path):
    db = _db(tmp_path, PAPI)
    _resolve(db, tmp_path)
    status, target, _ = _call(db, "main", "run")
    assert (status, target) == ("scope_resolved", "run.go::run::function::3")


@needs_go
def test_a_qualified_call_resolves_through_go_mod(tmp_path):
    db = _db(tmp_path, PAPI)
    _resolve(db, tmp_path)
    assert _call(db, "main", "SetupStorage")[:2] == (
        "import_resolved",
        "server/server.go::SetupStorage::function::5",
    )
    # Through an alias, and to the FUNCTION, never the same-named method.
    assert _call(db, "SetupStorage", "NewSubscriber")[:2] == (
        "import_resolved",
        "messaging/messaging.go::NewSubscriber::function::5",
    )


@needs_go
def test_stdlib_and_predeclared_names_are_external(tmp_path):
    db = _db(tmp_path, PAPI)
    _resolve(db, tmp_path)
    assert _call(db, "main", "Println")[0] == "external"
    assert _call(db, "run", "len")[0] == "external"


@needs_go
def test_a_package_that_shadows_a_predeclared_name_binds_it(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "a.go": "package a\n\nfunc len(s string) int { return 0 }\n",
            "b.go": 'package a\n\nfunc f() { len("x") }\n',
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "f", "len")[:2] == ("scope_resolved", "a.go::len::function::3")


@needs_go
def test_a_module_outside_the_repo_is_external(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "a.go": 'package a\n\nimport "github.com/other/lib"\n\nfunc f() { lib.Do() }\n',
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "f", "Do")[0] == "external"


@needs_go
def test_without_go_mod_only_the_stdlib_is_ruled_external(tmp_path):
    """No module path to compare against is no evidence that a path is foreign."""
    db = _db(
        tmp_path,
        {
            "a.go": (
                "package a\n\n"
                'import (\n\t"strings"\n\t"github.com/other/lib"\n)\n\n'
                'func f() { strings.ToUpper("x"); lib.Do() }\n'
            )
        },
    )
    report = _resolve(db, tmp_path)
    assert _call(db, "f", "ToUpper")[0] == "external"
    assert _call(db, "f", "Do")[0] == "unresolved"
    assert report["unplaced"] == 1


@needs_go
def test_a_call_through_a_field_or_value_stays_unresolved(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "m/m.go": "package m\n\nfunc Get() {}\n",
            "a.go": (
                "package a\n\n"
                'import "example.com/papi/m"\n\n'
                "func f(c C) { c.Store.Get() }\n\n"
                "func g(s S) { s.Get() }\n\n"
                "func h() { m.Get(); Get() }\n"
            ),
        },
    )
    report = _resolve(db, tmp_path)
    assert _call(db, "f", "Get")[0] == "unresolved"  # "*": needs types (#324)
    assert _call(db, "g", "Get")[0] == "unresolved"  # `s` is not an import
    assert _call(db, "h", "Get")[0] == "unresolved"  # bare + qualified on one row
    assert report["mixed_or_typed"] == 2
    assert report["not_a_package"] == 1


@needs_go
def test_two_names_for_one_import_path_are_one_target(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "m/m.go": "package m\n\nfunc Get() {}\n",
            "a.go": (
                "package a\n\n"
                'import (\n\t"example.com/papi/m"\n\tmm "example.com/papi/m"\n)\n\n'
                "func f() { m.Get(); mm.Get() }\n"
            ),
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "f", "Get")[:2] == ("import_resolved", "m/m.go::Get::function::3")


@needs_go
def test_the_package_clause_beats_the_path_convention(tmp_path):
    """`.../go-foo` holding `package foo`: the unaliased import binds `foo`."""
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "go-foo/foo.go": "package foo\n\nfunc Bar() {}\n",
            "a.go": 'package a\n\nimport "example.com/papi/go-foo"\n\nfunc f() { foo.Bar() }\n',
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "f", "Bar")[:2] == ("import_resolved", "go-foo/foo.go::Bar::function::3")


@needs_go
def test_two_definitions_in_one_package_are_ambiguous(tmp_path):
    """Build-tagged twins (`x_linux.go`, `x_darwin.go`) cannot be ranked."""
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "x_linux.go": "//go:build linux\n\npackage a\n\nfunc open() {}\n",
            "x_darwin.go": "//go:build darwin\n\npackage a\n\nfunc open() {}\n",
            "a.go": "package a\n\nfunc f() { open() }\n",
        },
    )
    report = _resolve(db, tmp_path)
    assert _call(db, "f", "open")[0] == "unresolved"
    assert report["ambiguous"] == 1


@needs_go
def test_an_external_test_package_is_its_own_scope(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "a.go": "package a\n\nfunc helper() {}\n",
            "a_test.go": "package a_test\n\nfunc TestX() { helper() }\n",
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "TestX", "helper")[0] == "unresolved"


@needs_go
def test_a_nested_module_wins_over_its_parent(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "tools/go.mod": "module example.com/tools\n",
            "tools/gen/gen.go": "package gen\n\nfunc Run() {}\n",
            "a.go": 'package a\n\nimport "example.com/tools/gen"\n\nfunc f() { gen.Run() }\n',
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "f", "Run")[:2] == ("import_resolved", "tools/gen/gen.go::Run::function::3")


@needs_go
def test_a_module_less_caller_beside_a_nested_module_is_not_ruled_external(tmp_path):
    """Outside every found module is evidence only for a caller a found module governs.

    Prudence, PR #1285 r1: `tools/go.mod` plus a module-less `app/` importing its
    own `app/store` by a GOPATH-style path wrote `external` over a callee sitting
    in the index.
    """
    db = _db(
        tmp_path,
        {
            "tools/go.mod": "module example.com/tools\n",
            "tools/gen/gen.go": "package gen\n\nfunc Run() {}\n",
            "app/store/s.go": "package store\n\nfunc Get() {}\n",
            "app/main.go": (
                "package main\n\n"
                'import "github.com/me/repo/app/store"\n\n'
                "func main() { store.Get() }\n"
            ),
        },
    )
    report = _resolve(db, tmp_path)
    assert _call(db, "main", "Get")[0] == "unresolved"
    assert report["unplaced"] == 1


@needs_go
def test_two_modules_declaring_one_path_resolve_within_the_callers_own(tmp_path):
    """Prudence, PR #1285 r2: keyed by path alone, the first directory walked won.

    Several `examples/*` made with `go mod init example` is the ordinary case.
    A caller inside one of them binds into its own module; a caller in neither
    cannot choose, and stays unresolved rather than being ruled external.
    """
    db = _db(
        tmp_path,
        {
            "ex1/go.mod": "module example\n",
            "ex1/util/u.go": "package util\n\nfunc Get() {}\n",
            "ex2/go.mod": "module example\n",
            "ex2/util/u.go": "package util\n\nfunc Get() {}\n",
            "ex2/main.go": (
                'package main\n\nimport "example/util"\n\nfunc main() { util.Get() }\n'
            ),
            "other/go.mod": "module example.com/other\n",
            "other/o.go": (
                'package other\n\nimport "example/util"\n\nfunc run() { util.Get() }\n'
            ),
        },
    )
    report = _resolve(db, tmp_path)
    assert _call(db, "main", "Get")[:2] == ("import_resolved", "ex2/util/u.go::Get::function::3")
    assert _call(db, "run", "Get")[0] == "unresolved"
    assert report["unplaced"] == 1


def test_a_nested_module_in_one_copy_of_a_duplicated_path_does_not_capture_the_other(
    tmp_path,
):
    """Prudence, PR #1285 r3: the longest match ran before the caller's-own rule.

    `ex1/sub/go.mod` declares `example/sub`; a caller in `ex2` importing
    `example/sub` means its own `ex2/sub`, and was bound into `ex1/sub`. Reads
    real go.mod files and needs no grammar.
    """
    from techne.codex.go_resolve import AMBIGUOUS, _Modules

    _write(
        tmp_path,
        {
            "ex1/go.mod": "module example\n",
            "ex1/sub/go.mod": "module example/sub\n",
            "ex2/go.mod": "module example\n",
        },
    )
    m = _Modules(tmp_path, {"ex1", "ex1/sub", "ex2", "ex2/sub"})
    assert m.dir_for("example/sub", "ex2") == AMBIGUOUS
    assert m.dir_for("example/sub", "ex2/sub") == AMBIGUOUS
    # A caller inside the nested module still resolves within it.
    assert m.dir_for("example/sub/x", "ex1/sub") == "ex1/sub/x"
    # And an unduplicated path is untouched.
    _write(tmp_path, {"solo/go.mod": "module solo.example\n"})
    m = _Modules(tmp_path, {"solo", "solo/a"})
    assert m.dir_for("solo.example/a", "solo") == "solo/a"


@needs_go
def test_a_shadowing_parameter_is_a_known_false_binding(tmp_path):
    """PINNED COST, not a goal: the table records no local bindings (card, v7).

    `helper` here is the parameter, and the resolver binds the package's
    `helper`. If this starts failing because scopes became visible, delete it.
    """
    db = _db(
        tmp_path,
        {
            "a.go": (
                "package a\n\nfunc helper() {}\n\n"
                "func run(helper func()) { helper() }\n"
            )
        },
    )
    _resolve(db, tmp_path)
    assert _call(db, "run", "helper")[:2] == ("scope_resolved", "a.go::helper::function::3")


@needs_go
def test_the_generic_passes_leave_go_rows_alone(tmp_path):
    """One owner per language, so two passes cannot disagree about a row."""
    db = _db(tmp_path, {"go.mod": GO_MOD, "a.go": "package a\n\nfunc g() {}\n\nfunc f() { g() }\n"})
    conn = sqlite3.connect(db)
    try:
        resolve_edges_by_import(conn)
        resolve_edges_by_scope(conn)
    finally:
        conn.close()
    assert _call(db, "f", "g")[0] == "unresolved"
    _resolve(db, tmp_path)
    assert _call(db, "f", "g")[0] == "scope_resolved"


@needs_go
def test_resolution_is_idempotent(tmp_path):
    db = _db(tmp_path, PAPI)
    first = _resolve(db, tmp_path)
    again = _resolve(db, tmp_path)
    assert first["import_resolved"] + first["scope_resolved"] > 0
    assert again["import_resolved"] == again["scope_resolved"] == again["external"] == 0


@needs_go
def test_repo_root_defaults_to_codex_meta(tmp_path):
    db = _db(tmp_path, PAPI)
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO codex_meta VALUES ('repo_root', ?)", (str(tmp_path),))
        conn.commit()
        resolve_go_edges(conn)
    finally:
        conn.close()
    assert _call(db, "main", "SetupStorage")[0] == "import_resolved"


# --------------------------------------------------------------------------
# What callers see
# --------------------------------------------------------------------------


@needs_go
def test_callers_report_every_call_site_line(tmp_path):
    db = _db(
        tmp_path,
        {
            "go.mod": GO_MOD,
            "messaging/m.go": "package messaging\n\nfunc NewSubscriber() {}\n",
            "server/s.go": (
                "package server\n\n"
                'import "example.com/papi/messaging"\n\n'
                "func Setup(c C) {\n"
                "\tmessaging.NewSubscriber()\n"
                "\tfor {\n\t\tmessaging.NewSubscriber()\n\t}\n"
                "}\n\n"
                "func Other(c C) { c.PubSub.NewSubscriber() }\n"
            ),
        },
    )
    _resolve(db, tmp_path)
    rows = {
        r["name"]: r
        for r in callers(db, "messaging/m.go::NewSubscriber::function::3")
    }
    assert rows["Setup"]["call_lines"] == [6, 8]
    assert rows["Setup"]["resolution"] == "exact"
    # Through a struct field: listed, and labelled as a name match.
    assert rows["Other"]["call_lines"] == [12]
    assert rows["Other"]["resolution"] == "name-inferred"


def test_callers_read_a_pre_v7_index(tmp_path):
    """The read path does not migrate, so a v6 db has no call_sites column."""
    db = _db(tmp_path, {})
    _write(tmp_path, {"a.py": "def g():\n    pass\n\n\ndef f():\n    g()\n"})
    index_file(db, tmp_path / "a.py", tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE edges DROP COLUMN call_sites")
        conn.commit()
    finally:
        conn.close()
    (row,) = callers(db, "a.py::g::function::1")
    assert row["name"] == "f" and row["call_lines"] == []


def test_a_v6_database_gains_call_sites_and_owes_a_reparse(tmp_path):
    from techne.core import migrator
    from techne.core.codex import CODEX_VERSION, _apply_codex_migration

    db = _db(tmp_path, {})
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO symbols (id, file, name, kind) "
            "VALUES ('a.go::f::function::3', 'a.go', 'f', 'function')"
        )
        conn.execute("ALTER TABLE edges DROP COLUMN call_sites")
        migrator.user_version_io.write(conn, 6)
        conn.commit()
    finally:
        conn.close()
    _apply_codex_migration(db)
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
        owed = conn.execute(
            "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
        ).fetchone()
        version = migrator.user_version_io.read(conn)
    finally:
        conn.close()
    assert "call_sites" in cols
    assert owed == ("7",)
    assert version == CODEX_VERSION == 7


@needs_go
def test_cli_index_and_callers_on_a_go_repo(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, PAPI)
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    assert runner.invoke(main, ["init"]).exit_code == 0
    out = runner.invoke(main, ["codex", "index", str(repo)])
    assert out.exit_code == 0, out.output
    assert "same-package" in out.output
    out = runner.invoke(
        main, ["codex", "callers", "messaging/messaging.go::NewSubscriber::function::5",
               "--repo", str(repo)]
    )
    assert out.exit_code == 0, out.output
    assert "server/server.go::SetupStorage::function::5" in out.output
    assert "line 5" in out.output
