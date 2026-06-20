"""Tests for the Codex code-graph store over codex.db (techne#89)."""

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
    # a → b → c ;  a → d ;  d → c   (call edges)
    for sid, nm in [("a", "main"), ("b", "load"), ("c", "connect"), ("d", "parse")]:
        _sym(db, sid, nm, sig=f"def {nm}(): ...")
    add_edge(db, "a", "b", "call")
    add_edge(db, "b", "c", "call")
    add_edge(db, "a", "d", "call")
    add_edge(db, "d", "c", "call")


def test_search_symbols(db):
    _sym(db, "x", "authenticate", sig="def authenticate(user, token): ...")
    hits = search_symbols(db, "authenticate token")
    assert [h["id"] for h in hits] == ["x"]


def test_callers_callees_direct(db):
    _graph(db)
    assert {s["id"] for s in callees(db, "a")} == {"b", "d"}
    assert {s["id"] for s in callers(db, "c")} == {"b", "d"}
    assert {s["id"] for s in callers(db, "a")} == set()  # nothing calls main


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
    add_edge(db, "a", "m", "import")  # a imports m (not a call)
    calls_only = reachable(db, "a", direction="out", kinds=["call"])
    assert "m" not in {s["id"] for s in calls_only}  # import edge not followed
    with_imports = reachable(db, "a", direction="out", kinds=["call", "import"])
    assert "m" in {s["id"] for s in with_imports}


def test_stats(db):
    _graph(db)
    s = stats(db)
    assert s["symbols"] == 4 and s["edges"] == 4 and s["files"] == 4
