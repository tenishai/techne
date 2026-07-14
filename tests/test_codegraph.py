"""Tests for the Codex code-graph store over codex.db (techne#89).

v1 edges are name-based: add_edge takes to_name (the callee name string), NOT
to_symbol. Graph traversal resolves callee symbols by name match when to_symbol
is NULL (the v1 default — resolution is a future pass).
"""

from __future__ import annotations

import pytest

from techne.core.codegraph import (
    SymbolResolutionError,
    add_edge,
    add_symbol,
    callees,
    callers,
    reachable,
    resolve_symbol_id,
    search_symbols,
    stats,
    symbol_at,
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


def test_reachable_out_is_dependencies(db):
    # techne#325: 'out' = dependencies (what this reaches), NOT the blast radius.
    _graph(db)
    out = reachable(db, "a", direction="out")
    assert {s["id"] for s in out} == {"b", "c", "d"}  # everything a transitively reaches
    depths = {s["id"]: s["depth"] for s in out}
    assert depths["b"] == 1 and depths["d"] == 1 and depths["c"] == 2  # min depth to c


def test_reachable_in_is_dependents_and_default(db):
    # techne#325: 'in' = dependents (what breaks if you change this) — the blast
    # radius, and the default direction.
    _graph(db)
    incoming = reachable(db, "c", direction="in")
    assert {s["id"] for s in incoming} == {"a", "b", "d"}  # everything that reaches c
    assert {s["id"] for s in reachable(db, "c")} == {"a", "b", "d"}  # default is 'in'


def test_reachable_respects_depth_and_excludes_anchor(db):
    _graph(db)
    shallow = reachable(db, "a", direction="out", max_depth=1)
    assert {s["id"] for s in shallow} == {"b", "d"}  # depth-1 only; c (depth 2) excluded
    assert "a" not in {s["id"] for s in reachable(db, "a")}  # anchor never in results


def test_fixtures_excluded_by_default(db):
    """techne#325: tests/fixtures/** symbols are name-collision noise and are
    dropped from callers/callees/reachable by default, opt back in explicitly."""
    # Real chain: caller(main) → target(handle). A fixture also defines "handle",
    # so the name-based edge match pulls it in — that's the phantom node to filter.
    _sym(db, "caller", "main")
    _sym(db, "target", "handle")
    add_symbol(db, id="fix", file="tests/fixtures/codex/callee.py", name="handle", kind="function")
    add_edge(db, "caller", "handle", "call")  # unresolved name edge → matches BOTH

    # callees of caller: real "target" only, fixture "fix" filtered by default.
    assert {s["id"] for s in callees(db, "caller")} == {"target"}
    assert {s["id"] for s in callees(db, "caller", exclude_fixtures=False)} == {"target", "fix"}

    # callers of the shared name: caller reaches both symbols named "handle";
    # the fixture one is excluded from a dependents query by default too.
    assert "fix" not in {s["id"] for s in reachable(db, "caller", direction="out")}
    assert "fix" in {
        s["id"] for s in reachable(db, "caller", direction="out", exclude_fixtures=False)
    }


def test_reachable_kind_filter(db):
    _graph(db)
    add_symbol(db, id="m", file="m.py", name="mod", kind="module")
    # Import edge: "a" imports name "mod"
    add_edge(db, "a", "mod", "import")
    calls_only = reachable(db, "a", direction="out", kinds=["call"])
    assert "m" not in {s["id"] for s in calls_only}  # import edge not followed
    with_imports = reachable(db, "a", direction="out", kinds=["call", "import"])
    assert "m" in {s["id"] for s in with_imports}


def test_symbol_at_returns_innermost(db):
    """techne#335: symbol_at maps an edited line to the innermost enclosing
    symbol (smallest span containing the line) — the edit-rail's anchor."""
    add_symbol(db, id="m", file="mod.py", name="mod", kind="module", span="1:100")
    add_symbol(db, id="c", file="mod.py", name="Klass", kind="class", span="10:50")
    add_symbol(db, id="f", file="mod.py", name="method", kind="method", span="20:30")

    assert symbol_at(db, "mod.py", 25)["id"] == "f"  # inside method → innermost
    assert symbol_at(db, "mod.py", 40)["id"] == "c"  # in class, outside method → class
    assert symbol_at(db, "mod.py", 5)["id"] == "m"  # module-level only → module
    assert symbol_at(db, "mod.py", 200) is None  # past every span → None
    assert symbol_at(db, "other.py", 25) is None  # unknown file → None


def test_symbol_at_skips_malformed_span(db):
    """A missing/garbage span is skipped, not raised (defensive)."""
    add_symbol(db, id="bad", file="x.py", name="bad", kind="function", span=None)
    add_symbol(db, id="ok", file="x.py", name="ok", kind="function", span="1:9")
    assert symbol_at(db, "x.py", 5)["id"] == "ok"
    assert symbol_at(db, "x.py", 100) is None


def test_stats(db):
    _graph(db)
    s = stats(db)
    assert s["symbols"] == 4 and s["edges"] == 4 and s["files"] == 4


# ---- resolve_symbol_id: no silent-empty on unresolvable input (techne#300) --


def test_resolve_symbol_id_bare_name_unique(db):
    """A bare name resolves to the single symbol with that name — so callers
    no longer have to copy a full id out of `search` for the common case."""
    _sym(db, "a", "main")
    assert resolve_symbol_id(db, "main") == "a"


def test_resolve_symbol_id_full_id_passes_through_or_errors(db):
    add_symbol(db, id="pkg/x.py::foo::function::1", file="pkg/x.py", name="foo", kind="function")
    # An existing full id is returned as-is.
    assert resolve_symbol_id(db, "pkg/x.py::foo::function::1") == "pkg/x.py::foo::function::1"
    # A non-existent full id ERRORS rather than returning empty downstream.
    with pytest.raises(SymbolResolutionError, match="no symbol with id"):
        resolve_symbol_id(db, "no/such.py::bar::function::9")


def test_resolve_symbol_id_no_match_errors(db):
    """The core of #300: a bare name that matches nothing raises, instead of
    the old silent empty result that looked like 'no callers'."""
    _sym(db, "a", "main")
    with pytest.raises(SymbolResolutionError, match="no symbol named"):
        resolve_symbol_id(db, "does_not_exist")


def test_resolve_symbol_id_ambiguous_lists_candidates(db):
    add_symbol(db, id="a.py::verify::function::1", file="a.py", name="verify", kind="function")
    add_symbol(db, id="b.py::verify::method::5", file="b.py", name="verify", kind="method")
    with pytest.raises(SymbolResolutionError, match="ambiguous") as ei:
        resolve_symbol_id(db, "verify")
    assert {c["id"] for c in ei.value.candidates} == {
        "a.py::verify::function::1",
        "b.py::verify::method::5",
    }
