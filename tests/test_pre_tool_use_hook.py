"""Tests for the PreToolUse grep-assist hook (techne#206, per-repo since #209).

The hook augments the agent's own Grep calls with Codex symbol matches,
injected via hookSpecificOutput.additionalContext. Since techne#209 it resolves
the *per-repo* codex db from the Grep's target `path` (else the session `cwd`),
so symbols are scoped to the repo the agent is actually working in. It remains
strictly best-effort: it must NEVER block the tool, ALWAYS exit 0, and emit
NOTHING on any error, empty path, or unindexed repo. These tests drive main()
directly with a monkeypatched stdin and capture stdout via capsys.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.core.codegraph import add_symbol
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


def _make_repo(home, *, indexed: bool):
    """Create a git repo under home. When `indexed`, also create its per-repo
    codex db so the hook resolves to a real graph. Returns (repo, db_or_None)."""
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    repo = home / "workrepo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)  # marker: find_repo_root just checks existence
    db = codex_db_for_repo(Config.load(), repo) if indexed else None
    return repo, db


def _grep(pattern, repo=None):
    """Build a Grep PreToolUse payload; when repo given, set cwd to it."""
    payload = {"tool_name": "Grep", "tool_input": {"pattern": pattern}}
    if repo is not None:
        payload["cwd"] = str(repo)
    return payload


def _run(monkeypatch, capsys, payload: dict) -> str:
    """Run the hook main() with `payload` on stdin; return captured stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0, f"hook must always exit 0, got {exc.value.code}"
    return capsys.readouterr().out


def test_flag_off_default_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF (default) → no stdout, even with a populated per-repo db."""
    repo, db = _make_repo(home, indexed=True)
    add_symbol(
        db, id="s1", file="auth.py", name="authenticate", kind="function",
        signature="def authenticate(user): ...",
    )
    # No config.yaml at all → codex_grep_assist defaults to False.
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out == ""


def test_non_grep_glob_tool_emits_nothing(home, monkeypatch, capsys):
    """A tool that isn't Grep → no stdout (short-circuits before config load)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys,
        {"tool_name": "Read", "tool_input": {"pattern": "authenticate"}, "cwd": str(repo)},
    )
    assert out == ""


def test_unindexed_repo_emits_nothing(home, monkeypatch, capsys):
    """Flag ON but the repo has no codex index → best-effort no-op, no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, _ = _make_repo(home, indexed=False)  # git repo, but never indexed
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out == ""


def test_not_in_repo_emits_nothing(home, monkeypatch, capsys):
    """cwd outside any git repo → no repo to resolve → no stdout."""
    _write_config(home, codex_grep_assist=True)
    plain = home / "notarepo"
    plain.mkdir()
    out = _run(monkeypatch, capsys, _grep("authenticate", plain))
    assert out == ""


def test_empty_pattern_emits_nothing(home, monkeypatch, capsys):
    """Empty search pattern → no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, _ = _make_repo(home, indexed=True)
    out = _run(monkeypatch, capsys, _grep("", repo))
    assert out == ""


def test_no_matching_symbol_emits_nothing(home, monkeypatch, capsys):
    """Flag ON, db present, but pattern matches nothing → no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(monkeypatch, capsys, _grep("nonexistentxyz", repo))
    assert out == ""


def test_matching_symbol_injects_context(home, monkeypatch, capsys):
    """Flag ON + matching symbol in the cwd's repo → protocol JSON injection."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(
        db, id="s1", file="auth.py", name="authenticate", kind="function",
        signature="def authenticate(user, token): ...",
    )
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out.strip(), "expected protocol JSON on stdout"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    ctx = hso["additionalContext"]
    assert "authenticate" in ctx
    assert "s1" in ctx
    assert "auth.py" in ctx


def test_grep_path_overrides_cwd_for_repo_scope(home, monkeypatch, capsys):
    """The Grep `path` (what's being searched) selects the repo over cwd (#209)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    # cwd is a non-repo dir; the Grep path points into the indexed repo.
    outside = home / "elsewhere"
    outside.mkdir()
    payload = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "authenticate", "path": str(repo)},
        "cwd": str(outside),
    }
    out = _run(monkeypatch, capsys, payload)
    assert "authenticate" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_cross_repo_symbol_not_injected(home, monkeypatch, capsys):
    """A symbol indexed in repo B must NOT surface when grepping in repo A (#209).

    This is the signal/noise fix #206 was blocked on: the old single global
    graph injected unrelated repos' symbols.
    """
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    _write_config(home, codex_grep_assist=True)
    repo_a = home / "alpha"
    repo_b = home / "beta"
    for r in (repo_a, repo_b):
        r.mkdir()
        (r / ".git").mkdir()
    codex_db_for_repo(Config.load(), repo_a)  # A: empty
    db_b = codex_db_for_repo(Config.load(), repo_b)
    add_symbol(db_b, id="s1", file="b.py", name="beta_secret", kind="function")

    # Grep for beta_secret while working in repo A → no injection (A has no such symbol).
    out = _run(monkeypatch, capsys, _grep("beta_secret", repo_a))
    assert out == ""


def test_glob_tool_not_augmented(home, monkeypatch, capsys):
    """Glob is NOT augmented — its pattern is a file glob, not a symbol name (techne#206)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys,
        {"tool_name": "Glob", "tool_input": {"pattern": "authenticate"}, "cwd": str(repo)},
    )
    assert out == ""


def test_stale_symbol_shows_freshness_marker(home, monkeypatch, capsys):
    """A symbol whose file is missing on disk gets a visible freshness marker.

    An absolute path that doesn't exist classifies as 'missing' — not fresh/unknown
    — so the honesty marker must appear in the injected context.
    """
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    ghost = home / "gone" / "auth.py"  # absolute path that does not exist
    add_symbol(
        db, id="s1", file=str(ghost), name="authenticate", kind="function",
        signature="def authenticate(user): ...", file_hash="deadbeef",
    )
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[missing]" in ctx


def test_malformed_stdin_exits_zero(home, monkeypatch, capsys):
    """Malformed JSON on stdin → exit 0, no stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json {"))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""
