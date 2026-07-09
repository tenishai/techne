"""Tests for the PreToolUse grep-assist hook (techne#206).

The hook augments the agent's own Grep calls with Codex symbol matches,
injected via hookSpecificOutput.additionalContext. It is strictly best-effort:
it must NEVER block the tool, ALWAYS exit 0, and emit NOTHING on any error or
empty path. These tests drive main() directly with a monkeypatched stdin and
capture stdout via capsys.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.core.codegraph import add_symbol
from techne.core.codex import CODEX_SCHEMA
from techne.core.stores import SqliteStore
from techne.hooks import pre_tool_use


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh TECHNE_HOME so Config.load() resolves db/data_dir under tmp_path."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    # Disable the SIGALRM budget so slow CI never trips the hard timeout mid-test.
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _write_config(home, **hook_flags):
    """Write a config.yaml under TECHNE_HOME with the given hooks.* flags."""
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": hook_flags}))


def _make_codex_db(home):
    """Create <home>/codex.db (where codex_db(config) resolves) with the v1 schema."""
    p = home / "codex.db"
    SqliteStore("codex", p).migrate(CODEX_SCHEMA)
    return p


def _run(monkeypatch, capsys, payload: dict) -> str:
    """Run the hook main() with `payload` on stdin; return captured stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0, f"hook must always exit 0, got {exc.value.code}"
    return capsys.readouterr().out


def test_flag_off_default_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF (default) → no stdout, even with a populated codex.db."""
    db = _make_codex_db(home)
    add_symbol(
        db,
        id="s1",
        file="auth.py",
        name="authenticate",
        kind="function",
        signature="def authenticate(user): ...",
    )
    # No config.yaml at all → codex_grep_assist defaults to False.
    out = _run(
        monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": "authenticate"}}
    )
    assert out == ""


def test_non_grep_glob_tool_emits_nothing(home, monkeypatch, capsys):
    """A tool that isn't Grep → no stdout (short-circuits before config load)."""
    _write_config(home, codex_grep_assist=True)
    db = _make_codex_db(home)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys, {"tool_name": "Read", "tool_input": {"pattern": "authenticate"}}
    )
    assert out == ""


def test_missing_codex_db_emits_nothing(home, monkeypatch, capsys):
    """Flag ON but no codex.db present → best-effort no-op, no crash, no stdout."""
    _write_config(home, codex_grep_assist=True)
    # Intentionally do NOT create codex.db.
    out = _run(
        monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": "authenticate"}}
    )
    assert out == ""


def test_empty_pattern_emits_nothing(home, monkeypatch, capsys):
    """Empty search pattern → no stdout."""
    _write_config(home, codex_grep_assist=True)
    _make_codex_db(home)
    out = _run(monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": ""}})
    assert out == ""


def test_no_matching_symbol_emits_nothing(home, monkeypatch, capsys):
    """Flag ON, db present, but pattern matches nothing → no stdout."""
    _write_config(home, codex_grep_assist=True)
    db = _make_codex_db(home)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": "nonexistentxyz"}}
    )
    assert out == ""


def test_matching_symbol_injects_context(home, monkeypatch, capsys):
    """Flag ON + matching symbol → protocol JSON with the symbol in additionalContext."""
    _write_config(home, codex_grep_assist=True)
    db = _make_codex_db(home)
    add_symbol(
        db,
        id="s1",
        file="auth.py",
        name="authenticate",
        kind="function",
        signature="def authenticate(user, token): ...",
    )
    out = _run(
        monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": "authenticate"}}
    )
    assert out.strip(), "expected protocol JSON on stdout"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    ctx = hso["additionalContext"]
    assert "authenticate" in ctx
    assert "s1" in ctx
    assert "auth.py" in ctx


def test_glob_tool_not_augmented(home, monkeypatch, capsys):
    """Glob is NOT augmented — its pattern is a file glob, not a symbol name (techne#206)."""
    _write_config(home, codex_grep_assist=True)
    db = _make_codex_db(home)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys, {"tool_name": "Glob", "tool_input": {"pattern": "authenticate"}}
    )
    assert out == ""


def test_stale_symbol_shows_freshness_marker(home, monkeypatch, capsys):
    """A symbol whose file is missing on disk gets a visible freshness marker.

    An absolute path that doesn't exist classifies as 'missing' — not fresh/unknown
    — so the honesty marker must appear in the injected context.
    """
    _write_config(home, codex_grep_assist=True)
    db = _make_codex_db(home)
    ghost = home / "gone" / "auth.py"  # absolute path that does not exist
    add_symbol(
        db,
        id="s1",
        file=str(ghost),
        name="authenticate",
        kind="function",
        signature="def authenticate(user): ...",
        file_hash="deadbeef",
    )
    out = _run(
        monkeypatch, capsys, {"tool_name": "Grep", "tool_input": {"pattern": "authenticate"}}
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[missing]" in ctx


def test_malformed_stdin_exits_zero(home, monkeypatch, capsys):
    """Malformed JSON on stdin → exit 0, no stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json {"))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""
