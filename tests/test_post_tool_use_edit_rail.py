"""Tests for the PostToolUse codex edit-rail (techne#335).

After an Edit/MultiEdit, the hook maps the edit to its enclosing symbol and
injects that symbol's blast-radius (transitive dependents) into context via
hookSpecificOutput.additionalContext — but only when the symbol has at least
`codex_edit_rail_min_deps` dependents (threshold-gated). Strictly best-effort:
never blocks the tool, emits nothing on any error / unindexed repo / low-risk
edit. These tests drive main() directly with a monkeypatched stdin.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.core.codegraph import add_edge, add_symbol
from techne.hooks import post_tool_use


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")  # disable SIGALRM budget in tests
    return tmp_path


def _write_config(home, **hook_flags):
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": hook_flags}))


def _make_indexed_repo(home):
    """Create a git repo with a per-repo codex db + seed the session row that
    session_start would have created (so capture_tool_use doesn't FK-fail and
    short-circuit the hook before the rail). Returns (repo, db)."""
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo
    from techne.core.database import init_db
    from techne.core.state import ensure_session

    config = Config.load()
    init_db(config.db_path)
    ensure_session(config.db_path, "sess-1")

    repo = home / "workrepo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    db = codex_db_for_repo(config, repo)
    return repo, db


def _seed_handle_with_deps(repo, db, n_deps: int):
    """Write core.py defining `handle`, index it as a symbol, and add n_deps
    callers so reachable(in) returns n_deps dependents."""
    (repo / "core.py").write_text("def handle():\n    x = 1\n    return x\n")
    add_symbol(
        db, id="core.py::handle::function::1", file="core.py", name="handle",
        kind="function", span="1:3",
    )
    for i in range(n_deps):
        add_symbol(db, id=f"c{i}.py::caller{i}::function::1", file=f"c{i}.py",
                   name=f"caller{i}", kind="function", span="1:2")
        add_edge(db, f"c{i}.py::caller{i}::function::1", "handle", "call")


def _edit_payload(repo, *, new_string="    x = 1"):
    return {
        "session_id": "sess-1",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(repo / "core.py"),
            "old_string": "    x = 0",
            "new_string": new_string,
        },
        "cwd": str(repo),
    }


def _run(monkeypatch, capsys, payload: dict) -> str:
    """Run post_tool_use.main() with `payload` on stdin; return captured stdout.

    main() returns normally (no SystemExit) except on malformed JSON.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    post_tool_use.main()
    return capsys.readouterr().out


def _edit_rail_context(out: str) -> str | None:
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_flag_off_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF → the rail stays silent even for a heavily-used symbol. Set
    explicitly: the default is ON since #1020."""
    _write_config(home, codex_edit_rail=False)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 5)
    out = _run(monkeypatch, capsys, _edit_payload(repo))
    assert _edit_rail_context(out) is None


def test_edits_symbol_over_threshold_injects_blast_radius(home, monkeypatch, capsys):
    """Flag ON + edited symbol with >= min_deps dependents → blast-radius injected."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 4)
    ctx = _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo)))
    assert ctx is not None
    assert "handle" in ctx
    assert "4 dependent" in ctx  # the affected-count header
    assert "caller0" in ctx  # a dependent is listed


def test_below_threshold_emits_nothing(home, monkeypatch, capsys):
    """A symbol with fewer than min_deps dependents → low-risk → stay silent."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 2)  # only 2 deps < threshold 3
    out = _run(monkeypatch, capsys, _edit_payload(repo))
    assert _edit_rail_context(out) is None


def test_unindexed_repo_emits_nothing(home, monkeypatch, capsys):
    """Flag ON but repo has no codex db → best-effort no-op."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.state import ensure_session

    _write_config(home, codex_edit_rail=True)
    init_db(Config.load().db_path)
    ensure_session(Config.load().db_path, "sess-1")  # so capture doesn't short-circuit
    repo = home / "plain"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "core.py").write_text("def handle():\n    x = 1\n")
    out = _run(monkeypatch, capsys, _edit_payload(repo))
    assert _edit_rail_context(out) is None


def test_limit_caps_injected_dependents(home, monkeypatch, capsys):
    """More dependents than the limit → only `limit` are shown, with an overflow note."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3,
                  codex_edit_rail_limit=2)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 6)
    ctx = _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo)))
    assert ctx is not None
    assert "6 dependent" in ctx  # total is honest
    assert "and 4 more" in ctx  # 6 total - 2 shown


def test_logs_injection_for_instrumentation(home, monkeypatch, capsys):
    """The rail records its injection to context_injections (source=codex_edit_rail)
    so the #329 utilization audit can see it."""
    from techne.config import Config
    from techne.core.stores import plain_connect

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 4)
    _run(monkeypatch, capsys, _edit_payload(repo))

    conn = plain_connect(Config.load().db_path)
    try:
        rows = conn.execute(
            "SELECT source, hit_count FROM context_injections WHERE source = 'codex_edit_rail'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 4


def test_repeat_edit_same_symbol_throttled(home, monkeypatch, capsys):
    """techne#342: a symbol's blast radius is injected at most once per session —
    a re-edit stays silent, and only ONE row is logged (no count inflation)."""
    from techne.config import Config
    from techne.core.stores import plain_connect

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 4)

    assert _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo))) is not None
    # second edit of the SAME symbol, same session → throttled
    assert _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo))) is None

    conn = plain_connect(Config.load().db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM context_injections WHERE source='codex_edit_rail'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # only the first firing logged; the repeat did not inflate the count


def test_logs_total_hits_precap_radius(home, monkeypatch, capsys):
    """techne#343: total_hits records the true (pre-cap) radius; hit_count the shown."""
    from techne.config import Config
    from techne.core.stores import plain_connect

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3,
                  codex_edit_rail_limit=2)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 6)  # 6 dependents, limit 2
    _run(monkeypatch, capsys, _edit_payload(repo))

    conn = plain_connect(Config.load().db_path)
    try:
        row = conn.execute(
            "SELECT hit_count, total_hits FROM context_injections WHERE source='codex_edit_rail'"
        ).fetchone()
    finally:
        conn.close()
    assert row["hit_count"] == 2  # shown (capped by limit)
    assert row["total_hits"] == 6  # true blast-radius size


def test_non_edit_tool_emits_nothing(home, monkeypatch, capsys):
    """A non-Edit tool (e.g. Read) → the rail short-circuits, no injection."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 4)
    payload = {
        "session_id": "s", "tool_name": "Read",
        "tool_input": {"file_path": str(repo / "core.py")}, "cwd": str(repo),
    }
    out = _run(monkeypatch, capsys, payload)
    assert _edit_rail_context(out) is None


# ---------------------------------------------------------------------------
# issue #406 — synthetic <module> and test nodes never reach the payload
# ---------------------------------------------------------------------------


def _seed_noise(db, *, n_modules: int, n_tests: int):
    """Dependents of `handle` that the rail must drop: synthetic per-file module
    nodes (an importer's module scope calling it) and test functions."""
    for i in range(n_modules):
        sid = f"m{i}.py::<module>::module::0"
        add_symbol(db, id=sid, file=f"m{i}.py", name="<module>", kind="module", span="0:9")
        add_edge(db, sid, "handle", "call")
    for i in range(n_tests):
        sid = f"tests/test_core{i}.py::test_handle{i}::function::1"
        add_symbol(db, id=sid, file=f"tests/test_core{i}.py", name=f"test_handle{i}",
                   kind="function", span="1:2")
        add_edge(db, sid, "handle", "call")


def test_payload_excludes_module_and_test_nodes(home, monkeypatch, capsys):
    """Given a blast radius mixing real, <module> and test nodes, the rail injects
    and logs only the real ones; total_hits keeps the raw walk size."""
    from techne.config import Config
    from techne.core.stores import plain_connect

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 3)
    _seed_noise(db, n_modules=4, n_tests=6)
    ctx = _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo)))
    assert ctx is not None
    assert "3 dependent" in ctx
    assert "<module>" not in ctx and "test_handle" not in ctx

    conn = plain_connect(Config.load().db_path)
    try:
        row = conn.execute(
            "SELECT hit_ids, hit_count, total_hits FROM context_injections"
            " WHERE source='codex_edit_rail'"
        ).fetchone()
    finally:
        conn.close()
    hit_ids = json.loads(row["hit_ids"])
    assert sorted(hit_ids) == [f"c{i}.py::caller{i}::function::1" for i in range(3)]
    assert row["hit_count"] == 3
    assert row["total_hits"] == 13  # 3 real + 4 module + 6 test, pre-filter


def test_noise_does_not_count_toward_min_deps(home, monkeypatch, capsys):
    """Two real dependents plus ten noise nodes is still below min_deps=3."""
    from techne.hooks.post_tool_use import _edit_rail_outcome

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 2)
    _seed_noise(db, n_modules=5, n_tests=5)
    from techne.config import Config

    out = _edit_rail_outcome(Config.load(), "sess-1", "Edit", _edit_payload(repo)["tool_input"])
    assert out == (None, "below_min_deps")


def test_noise_no_longer_displaces_deeper_real_dependents(home, monkeypatch, capsys):
    """The measured failure in issue #406: noise at depth 1 filled the cap and cut
    the real dependents. With limit=2 and six noise nodes at depth 1, the real
    direct caller must take a slot and the count must be the three real nodes."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=2,
                  codex_edit_rail_limit=2)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 0)
    _seed_noise(db, n_modules=3, n_tests=3)
    # One real direct caller `mid`, and two real callers of `mid` at depth 2.
    add_symbol(db, id="mid.py::mid::function::1", file="mid.py", name="mid",
               kind="function", span="1:2")
    add_edge(db, "mid.py::mid::function::1", "handle", "call")
    for n in ("zeta", "omega"):
        add_symbol(db, id=f"{n}.py::{n}::function::1", file=f"{n}.py", name=n,
                   kind="function", span="1:2")
        add_edge(db, f"{n}.py::{n}::function::1", "mid", "call")
    ctx = _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo)))
    assert ctx is not None
    assert "d1  mid" in ctx
    assert "3 dependent" in ctx and "and 1 more" in ctx


def test_exclude_globs_are_config(home, monkeypatch, capsys):
    """An empty glob list keeps test nodes (module nodes are always dropped)."""
    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3,
                  codex_edit_rail_exclude_globs=[])
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 0)
    _seed_noise(db, n_modules=2, n_tests=3)
    ctx = _edit_rail_context(_run(monkeypatch, capsys, _edit_payload(repo)))
    assert ctx is not None
    assert "test_handle0" in ctx and "<module>" not in ctx


# ---------------------------------------------------------------------------
# issue #721 — the decline reason is in the return contract
# ---------------------------------------------------------------------------


def test_each_decline_path_names_its_reason(home):
    """Every reachable decline returns its own reason, each witnessed next to a
    success on the same setup so a reason cannot be satisfied by accident."""
    from techne.config import Config
    from techne.hooks.post_tool_use import EDIT_RAIL_DECLINE_REASONS, _edit_rail_outcome

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=3)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 4)
    cfg = Config.load()
    ti = _edit_payload(repo)["tool_input"]

    def reason(session="fresh", tool="Edit", tool_input=ti, config=cfg):
        return _edit_rail_outcome(config, session, tool, tool_input).declined_because

    plain = home / "plain"
    (plain / ".git").mkdir(parents=True)
    (plain / "core.py").write_text("def handle():\n    x = 1\n")
    outside = home / "loose.py"
    outside.write_text("x = 1\n")
    cfg_off = Config.load()
    cfg_off.hooks.codex_edit_rail = False
    cfg_hi = Config.load()
    cfg_hi.hooks.codex_edit_rail_min_deps = 5

    got = {
        "flag_off": reason(config=cfg_off),
        "not_an_edit": reason(tool="Read"),
        "no_file_path": reason(tool_input={}),
        "no_repo": reason(tool_input=dict(ti, file_path=str(outside))),
        "repo_not_indexed": reason(tool_input=dict(ti, file_path=str(plain / "core.py"))),
        "no_edited_line": reason(tool_input=dict(ti, new_string="absent from file")),
        "below_min_deps": reason(config=cfg_hi),
    }
    assert got == {k: k for k in got}
    # The positive control, then the throttle on the same symbol.
    first = _edit_rail_outcome(cfg, "s-once", "Edit", ti)
    assert first.text and first.declined_because is None
    assert reason(session="s-once") == "already_surfaced"
    assert set(got) | {"already_surfaced"} <= EDIT_RAIL_DECLINE_REASONS


def test_symbol_decline_reasons(home, monkeypatch):
    """no_enclosing_symbol / module_symbol / error, via a stubbed symbol lookup."""
    from techne.config import Config
    from techne.hooks.post_tool_use import _edit_rail_outcome

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=1)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 1)
    cfg = Config.load()
    ti = _edit_payload(repo)["tool_input"]
    assert _edit_rail_outcome(cfg, "a", "Edit", ti).declined_because is None

    monkeypatch.setattr("techne.core.codegraph.symbol_at", lambda *a, **k: None)
    assert _edit_rail_outcome(cfg, "b", "Edit", ti).declined_because == "no_enclosing_symbol"
    monkeypatch.setattr(
        "techne.core.codegraph.symbol_at",
        lambda *a, **k: {"id": "core.py::<module>::module::0", "name": "<module>",
                         "kind": "module"},
    )
    assert _edit_rail_outcome(cfg, "c", "Edit", ti).declined_because == "module_symbol"

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr("techne.core.codegraph.symbol_at", _boom)
    assert _edit_rail_outcome(cfg, "d", "Edit", ti).declined_because == "error"


def test_decline_reasons_set_matches_the_code():
    """The set pins the reasons: every `_decline("...")` literal in the rail is in
    it, and nothing in it is unused. A new decline path must be named here."""
    import inspect
    import re

    from techne.hooks.post_tool_use import EDIT_RAIL_DECLINE_REASONS, _edit_rail_outcome

    used = set(re.findall(r'_decline\("(\w+)"\)', inspect.getsource(_edit_rail_outcome)))
    assert used == EDIT_RAIL_DECLINE_REASONS


def test_outside_repo_reason(home, monkeypatch):
    """The edited path does not sit under the resolved root (a root resolver that
    disagrees with the path, as a symlinked checkout can make it)."""
    from techne.config import Config
    from techne.hooks.post_tool_use import _edit_rail_outcome

    _write_config(home, codex_edit_rail=True, codex_edit_rail_min_deps=1)
    repo, db = _make_indexed_repo(home)
    _seed_handle_with_deps(repo, db, 1)
    cfg = Config.load()
    ti = _edit_payload(repo)["tool_input"]
    assert _edit_rail_outcome(cfg, "a", "Edit", ti).declined_because is None

    other = home / "elsewhere"
    other.mkdir()
    monkeypatch.setattr("techne.core.repo.find_repo_root", lambda _p: other)
    monkeypatch.setattr("techne.core.repo.primary_repo_root", lambda _p: other)
    monkeypatch.setattr("techne.core.codex.resolve_repo_db_for_query", lambda *_a: db)
    assert _edit_rail_outcome(cfg, "b", "Edit", ti).declined_because == "outside_repo"
