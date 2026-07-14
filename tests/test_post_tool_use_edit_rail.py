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


def test_flag_off_default_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF (default) → the rail stays silent even for a heavily-used symbol."""
    _write_config(home)  # codex_edit_rail defaults False
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
