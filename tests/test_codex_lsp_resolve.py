"""Tests for codex LSP edge resolution (techne#321).

Hermetic tests use a fake LSP client + a hand-built codex db (no tree-sitter grammars,
no real server) to prove the core value: two same-named callees get *disambiguated*.
An opt-in test drives real pyright end-to-end.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from techne.codex.lsp_resolve import _map_symbol, resolve_edges_lsp
from techne.core.codegraph import callers
from techne.core.codex import CODEX_SCHEMA
from techne.core.lsp_client import CallSite
from techne.core.stores import plain_connect


def _sid(file, name, kind, line):
    return f"{file}::{name}::{kind}::{line}"


def _build_repo(root: Path):
    # Two modules define `foo`; caller imports mod_a's and calls it — the same
    # ambiguity as the epic's `supersede`, shrunk to a fixture.
    (root / "mod_a.py").write_text("def foo():\n    return 1\n")
    (root / "mod_b.py").write_text("def foo():\n    return 2\n")
    (root / "caller.py").write_text("from mod_a import foo\n\n\ndef bar():\n    foo()\n")


def _build_codex_db(db: Path):
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)

    def ins(file, name, kind, line):
        sid = _sid(file, name, kind, line)
        conn.execute(
            "INSERT INTO symbols (id, file, name, kind, span, language) VALUES (?,?,?,?,?,?)",
            (sid, file, name, kind, f"{line}:{line + 1}", "python"),
        )
        return sid

    a = ins("mod_a.py", "foo", "function", 1)
    b = ins("mod_b.py", "foo", "function", 1)
    bar = ins("caller.py", "bar", "function", 4)
    conn.execute(
        "INSERT INTO edges (from_symbol, to_name, to_symbol, kind, resolution_status) VALUES (?,?,?,?,?)",
        (bar, "foo", None, "call", "unresolved"),
    )
    conn.commit()
    conn.close()
    return a, b, bar


class FakeLsp:
    """A stand-in LSP client: routes outgoing_calls by the querying file's basename."""

    def __init__(self, routes):
        self.routes = routes

    def supports(self, cap):
        return cap == "callHierarchyProvider"

    def open_file(self, path, language_id="python"):
        pass

    def outgoing_calls(self, path, line, char, settle=(1, 0.0)):
        return self.routes.get(Path(path).name, [])


def test_resolver_disambiguates_same_named_callee(tmp_path):
    _build_repo(tmp_path)
    db = tmp_path / "codex.db"
    a, b, bar = _build_codex_db(db)

    # BEFORE — name-matching makes BOTH `foo`s claim `bar` as a caller (the bug).
    assert any(r["id"] == bar for r in callers(db, a))
    assert any(r["id"] == bar for r in callers(db, b))  # wrong: mod_b.foo is never called

    # The LSP resolves bar's call to mod_a.foo (def at 0-based line 0 == 1-based line 1).
    fake = FakeLsp({"caller.py": [CallSite("foo", 12, str(tmp_path / "mod_a.py"), 0, 4)]})
    report = resolve_edges_lsp(db, tmp_path, client=fake)
    assert report["resolved"] == 1

    conn = plain_connect(db)
    try:
        row = conn.execute(
            "SELECT to_symbol, resolution_status FROM edges WHERE from_symbol = ?", (bar,)
        ).fetchone()
    finally:
        conn.close()
    assert row["to_symbol"] == a
    assert row["resolution_status"] == "lsp"

    # AFTER — only mod_a.foo claims bar; mod_b.foo is correctly empty. The fix.
    assert [r["id"] for r in callers(db, a)] == [bar]
    assert callers(db, b) == []


def test_resolver_noop_without_server(tmp_path):
    _build_repo(tmp_path)
    db = tmp_path / "codex.db"
    _build_codex_db(db)
    report = resolve_edges_lsp(db, tmp_path, language="cobol")  # no server for cobol
    assert report.get("skipped") == "no language server"
    # Edge untouched — graceful, no regression.
    conn = plain_connect(db)
    try:
        assert conn.execute("SELECT to_symbol FROM edges").fetchone()["to_symbol"] is None
    finally:
        conn.close()


def test_map_symbol_prefers_exact_line(tmp_path):
    db = tmp_path / "codex.db"
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.execute("INSERT INTO symbols (id,file,name,kind,span) VALUES ('f.py::f::function::10','f.py','f','function','10:12')")
    conn.execute("INSERT INTO symbols (id,file,name,kind,span) VALUES ('f.py::f::function::40','f.py','f','function','40:42')")
    conn.commit()
    try:
        assert _map_symbol(conn, "f.py", "f", 39) == "f.py::f::function::40"  # LSP 0-based 39 → 1-based 40
        assert _map_symbol(conn, "f.py", "f", 9) == "f.py::f::function::10"
        assert _map_symbol(conn, "f.py", "missing", 0) is None
    finally:
        conn.close()


@pytest.mark.skipif(
    not (shutil.which("pyright-langserver") and os.environ.get("RUN_LSP_INTEGRATION")),
    reason="set RUN_LSP_INTEGRATION=1 (and install pyright-langserver) to run",
)
def test_resolver_with_real_pyright(tmp_path):
    _build_repo(tmp_path)
    db = tmp_path / "codex.db"
    a, b, bar = _build_codex_db(db)
    report = resolve_edges_lsp(db, tmp_path, settle=(20, 2.0))
    assert report["resolved"] >= 1
    assert [r["id"] for r in callers(db, a)] == [bar]
    assert callers(db, b) == []
