"""Tests for the Codex code-graph store over codex.db (techne#89).

v1 edges are name-based: add_edge takes to_name (the callee name string), NOT
to_symbol. Graph traversal resolves callee symbols by name match when to_symbol
is NULL (the v1 default — resolution is a future pass).
"""

from __future__ import annotations

import pytest

from techne.core.codegraph import (
    add_edge,
    add_symbol,
    callees,
    callers,
    reachable,
    search_symbols,
    stats,
)
from techne.core.codex import CODEX_SCHEMA
from techne.core.stores import SqliteStore


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "codex.db"
    SqliteStore("codex", p).migrate(CODEX_SCHEMA)
    return p


def _sym(db, sid, name, kind="function", sig=None):
    add_symbol(db, id=sid, file=f"{sid}.py", name=name, kind=kind, signature=sig)


def _graph(db):
    # a(main) → b(load) → c(connect);  a(main) → d(parse) → c(connect)
    # Edges stored as to_name (v1 name-based model):
    #   a calls "load" and "parse"; b calls "connect"; d calls "connect"
    for sid, nm in [("a", "main"), ("b", "load"), ("c", "connect"), ("d", "parse")]:
        _sym(db, sid, nm, sig=f"def {nm}(): ...")
    add_edge(db, "a", "load", "call")
    add_edge(db, "b", "connect", "call")
    add_edge(db, "a", "parse", "call")
    add_edge(db, "d", "connect", "call")


def test_search_symbols(db):
    _sym(db, "x", "authenticate", sig="def authenticate(user, token): ...")
    hits = search_symbols(db, "authenticate token")
    assert [h["id"] for h in hits] == ["x"]


def test_search_symbols_blank_query_returns_empty(db):
    """#103: blank/whitespace must not crash (FTS5 MATCH '' is a syntax error)."""
    _sym(db, "x", "authenticate", sig="def authenticate(user, token): ...")
    assert search_symbols(db, "") == []
    assert search_symbols(db, "   ") == []
    # techne#143: a lone backslash sanitizes to "" and reached MATCH '' here — now guarded.
    assert search_symbols(db, "\\") == []


def test_callers_callees_direct(db):
    _graph(db)
    # callees of "a" (main) → symbols named "load" (b) and "parse" (d)
    assert {s["id"] for s in callees(db, "a")} == {"b", "d"}
    # callers of "c" (connect) → symbols that call "connect": b and d
    assert {s["id"] for s in callers(db, "c")} == {"b", "d"}
    # nothing calls "main"
    assert {s["id"] for s in callers(db, "a")} == set()


def test_reachable_out_is_blast_radius(db):
    _graph(db)
    out = reachable(db, "a", direction="out")
    assert {s["id"] for s in out} == {"b", "c", "d"}  # everything a transitively reaches
    depths = {s["id"]: s["depth"] for s in out}
    assert depths["b"] == 1 and depths["d"] == 1 and depths["c"] == 2  # min depth to c


def test_reachable_in_is_dependents(db):
    _graph(db)
    incoming = reachable(db, "c", direction="in")
    assert {s["id"] for s in incoming} == {"a", "b", "d"}  # everything that reaches c


def test_reachable_respects_depth_and_excludes_anchor(db):
    _graph(db)
    shallow = reachable(db, "a", direction="out", max_depth=1)
    assert {s["id"] for s in shallow} == {"b", "d"}  # depth-1 only; c (depth 2) excluded
    assert "a" not in {s["id"] for s in reachable(db, "a")}  # anchor never in results


def test_reachable_kind_filter(db):
    _graph(db)
    add_symbol(db, id="m", file="m.py", name="mod", kind="module")
    # Import edge: "a" imports name "mod"
    add_edge(db, "a", "mod", "import")
    calls_only = reachable(db, "a", direction="out", kinds=["call"])
    assert "m" not in {s["id"] for s in calls_only}  # import edge not followed
    with_imports = reachable(db, "a", direction="out", kinds=["call", "import"])
    assert "m" in {s["id"] for s in with_imports}


def test_stats(db):
    _graph(db)
    s = stats(db)
    assert s["symbols"] == 4 and s["edges"] == 4 and s["files"] == 4
