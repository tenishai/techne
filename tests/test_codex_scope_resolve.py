"""Same-file call resolution (techne#940).

`resolve_edges_by_import` binds a call through the import that supplied the name, so
a call to a function defined in the caller's own file was never resolved, and
`codex blast-radius --only-proven` printed "(nothing reachable)" for a function
whose only callers sat beside it. Reported on `should_capture`, called from `main()`
in the same file; reproduced on this repo with `_cursor_key` in core/reflect.py.
"""

from __future__ import annotations

import sqlite3

from click.testing import CliRunner

from techne.codex.import_resolve import resolve_edges_by_import, resolve_edges_by_scope
from techne.codex.source import index_file
from techne.core.codegraph import reachable
from techne.core.codex import CODEX_SCHEMA


def _db(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in files:
        index_file(db, tmp_path / rel, tmp_path)
    return db


def _resolve(db):
    conn = sqlite3.connect(db)
    try:
        resolve_edges_by_import(conn)
        return resolve_edges_by_scope(conn)
    finally:
        conn.close()


def _call(db, caller: str, to_name: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            """SELECT e.resolution_status, e.to_symbol, e.bare_call
                 FROM edges e JOIN symbols s ON s.id = e.from_symbol
                WHERE e.kind = 'call' AND s.name = ? AND e.to_name = ?""",
            (caller, to_name),
        ).fetchone()
    finally:
        conn.close()


def _sym(db, name: str, kind: str = "function"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id FROM symbols WHERE name = ? AND kind = ?", (name, kind)
        ).fetchone()[0]
    finally:
        conn.close()


CAPTURE = """\
def should_capture(tool_name):
    return tool_name != "Read"


def main():
    tool_name = "Bash"
    if should_capture(tool_name):
        print("captured")
"""


def test_a_same_file_caller_is_proven(tmp_path):
    db = _db(tmp_path, {"capture.py": CAPTURE})
    _resolve(db)
    status, to_symbol, _bare = _call(db, "main", "should_capture")
    assert status == "scope_resolved"
    assert to_symbol == _sym(db, "should_capture")

    proven = reachable(db, _sym(db, "should_capture"), direction="in", include_inferred=False)
    assert [r["name"] for r in proven] == ["main"], (
        "the only real caller must survive --only-proven; before #940 this was empty"
    )


def test_an_attribute_call_is_not_a_call_to_the_local_function(tmp_path):
    """`requests.get()` is not a call to the file's own `get`. The two forms share an
    edge row, so this is the case that needs `bare_call`."""
    db = _db(
        tmp_path,
        {
            "client.py": (
                "import requests\n\n"
                "def get(url):\n    return url\n\n"
                "def fetch(url):\n    return requests.get(url)\n"
            )
        },
    )
    _resolve(db)
    status, to_symbol, bare = _call(db, "fetch", "get")
    assert bare == 0
    assert (status, to_symbol) == ("unresolved", None)


def test_a_bare_site_beside_an_attribute_site_still_proves_the_edge(tmp_path):
    db = _db(
        tmp_path,
        {
            "mixed.py": (
                "import requests\n\n"
                "def get(url):\n    return url\n\n"
                "def fetch(url):\n    requests.get(url)\n    return get(url)\n"
            )
        },
    )
    _resolve(db)
    status, _to, bare = _call(db, "fetch", "get")
    assert bare == 1
    assert status == "scope_resolved"


def test_a_nested_def_of_the_same_name_blocks_resolution(tmp_path):
    """A nested `helper` may be the one `outer` calls; spans cannot say which."""
    db = _db(
        tmp_path,
        {
            "shadow.py": (
                "def helper():\n    return 1\n\n"
                "def outer():\n"
                "    def helper():\n        return 2\n"
                "    return helper()\n"
            )
        },
    )
    _resolve(db)
    status, to_symbol, _bare = _call(db, "outer", "helper")
    assert (status, to_symbol) == ("unresolved", None)


def test_an_import_of_the_same_name_blocks_resolution(tmp_path):
    """The import rebinds the name after the local def. The import is IN-REPO and a
    re-export, so the import pass leaves the edge `unresolved` rather than
    `external` — which is the only way the row reaches the scope pass at all."""
    db = _db(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "def helper():\n    return 1\n",
            "pkg/other.py": "from pkg.base import helper\n",
            "pkg/app.py": (
                "def helper():\n    return 2\n\n"
                "from pkg.other import helper\n\n"
                "def run():\n    return helper()\n"
            ),
        },
    )
    conn = sqlite3.connect(db)
    try:
        resolve_edges_by_import(conn)
    finally:
        conn.close()
    assert _call(db, "run", "helper")[0] == "unresolved", (
        "precondition: the import pass must leave this row for the scope pass"
    )
    _resolve(db)
    status, to_symbol, _bare = _call(db, "run", "helper")
    assert (status, to_symbol) == ("unresolved", None)


def test_two_module_level_definitions_are_ambiguous(tmp_path):
    db = _db(
        tmp_path,
        {
            "compat.py": (
                "try:\n    import fast\n"
                "    def load():\n        return fast.load()\n"
                "except ImportError:\n"
                "    def load():\n        return None\n\n"
                "def run():\n    return load()\n"
            )
        },
    )
    _resolve(db)
    status, to_symbol, _bare = _call(db, "run", "load")
    assert (status, to_symbol) == ("unresolved", None)


def test_a_method_of_the_same_name_does_not_block(tmp_path):
    """A bare call cannot reach a method, so `Store.save` is not a rival for `save`."""
    db = _db(
        tmp_path,
        {
            "store.py": (
                "def save(x):\n    return x\n\n"
                "class Store:\n"
                "    def save(self, x):\n        return save(x)\n"
            )
        },
    )
    _resolve(db)
    status, to_symbol, _bare = _call(db, "save", "save")
    # The caller here is the method; its bare `save(x)` is the module-level function.
    assert status == "scope_resolved"
    assert to_symbol == _sym(db, "save", "function")


def test_typescript_same_file_call_is_proven(tmp_path):
    db = _db(
        tmp_path,
        {
            "util.ts": (
                "function slug(s: string): string {\n  return s.toLowerCase();\n}\n\n"
                "export function title(s: string): string {\n  return slug(s);\n}\n"
            )
        },
    )
    _resolve(db)
    status, to_symbol, _bare = _call(db, "title", "slug")
    assert status == "scope_resolved"
    assert to_symbol == _sym(db, "slug")


def test_a_lsp_row_is_not_overwritten(tmp_path):
    db = _db(tmp_path, {"capture.py": CAPTURE})
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE edges SET resolution_status = 'lsp' "
        "WHERE kind = 'call' AND to_name = 'should_capture'"
    )
    conn.commit()
    conn.close()
    report = _resolve(db)
    assert report["resolved"] == 0
    assert _call(db, "main", "should_capture")[0] == "lsp"


def test_re_running_is_a_no_op(tmp_path):
    db = _db(tmp_path, {"capture.py": CAPTURE})
    assert _resolve(db)["resolved"] == 1
    assert _resolve(db)["resolved"] == 0


def test_a_pre_v5_row_is_left_alone(tmp_path):
    """Rows indexed before schema v5 have bare_call NULL: nothing to prove with."""
    db = _db(tmp_path, {"capture.py": CAPTURE})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE edges SET bare_call = NULL")
    conn.commit()
    conn.close()
    assert _resolve(db)["resolved"] == 0


def test_migration_adds_the_column_and_records_reparse_debt(tmp_path):
    from techne.core.codex import CODEX_VERSION, _run_codex_steps

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.execute("ALTER TABLE edges DROP COLUMN bare_call")
    conn.execute(
        "INSERT INTO symbols (id, file, name, kind) "
        "VALUES ('a.py::f::function::1', 'a.py', 'f', 'function')"
    )
    conn.commit()
    _run_codex_steps(conn, 4, CODEX_VERSION)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
    owed = conn.execute("SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'").fetchone()
    conn.close()
    assert "bare_call" in cols
    assert owed == ("5",), "a store with rows must re-parse to fill bare_call"


def test_cli_only_proven_never_prints_a_bare_empty_result(tmp_path, monkeypatch):
    """An empty proven radius with name-matched dependents hidden must say so."""
    from techne.cli import main
    from techne.config import Config

    db = _db(tmp_path, {"capture.py": CAPTURE})
    # Leave the edge unresolved: the state every same-file caller was in before #940.
    target = _sym(db, "should_capture")
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr("techne.cli._active_codex_db", lambda config, repo, **k: (tmp_path, db))
    result = CliRunner().invoke(
        main, ["codex", "blast-radius", target, "--direction", "in", "--only-proven"]
    )
    assert result.exit_code == 0, result.output
    assert "(nothing reachable)" not in result.output
    assert "no resolved dependents; 1 reached only by a bare-name match hidden" in result.output
