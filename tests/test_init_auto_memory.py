"""techne#1023: init offers to turn off Claude Code auto-memory, and never silently.

Every case runs against conftest's isolated SETTINGS_PATH; nothing here can touch
the operator's real ~/.claude/settings.json.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.platforms.claude_code import hooks


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.setattr("techne.cli.find_curated_memory_files", lambda: [], raising=False)
    monkeypatch.setattr("techne.core.memory_doctor.find_curated_memory_files", lambda: [])
    return tmp_path


def _settings() -> dict:
    return json.loads(hooks.SETTINGS_PATH.read_text()) if hooks.SETTINGS_PATH.exists() else {}


def _init(monkeypatch, *, interactive: bool, answer: str = "", cc_memory: bool = False):
    monkeypatch.setattr("techne.cli._interactive", lambda: interactive)
    files = ["MEMORY.md"] if cc_memory else []
    monkeypatch.setattr("techne.core.memory_doctor.find_curated_memory_files", lambda: files)
    result = CliRunner().invoke(main, ["init", "--no-skills"], input=answer)
    assert result.exit_code == 0, result.output
    return result.output


def test_yes_sets_only_that_key(isolated, monkeypatch):
    hooks.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    hooks.SETTINGS_PATH.write_text(json.dumps({"theme": "dark", "env": {"X": "1"}}))
    out = _init(monkeypatch, interactive=True, answer="y\n")
    s = _settings()
    assert s["autoMemoryEnabled"] is False
    assert s["theme"] == "dark" and s["env"] == {"X": "1"}, "other keys must survive"
    assert "auto-memory: off" in out


def test_no_leaves_it_on(isolated, monkeypatch):
    out = _init(monkeypatch, interactive=True, answer="n\n")
    assert "autoMemoryEnabled" not in _settings()
    assert "Left on" in out


def test_non_interactive_only_recommends(isolated, monkeypatch):
    out = _init(monkeypatch, interactive=False)
    assert "autoMemoryEnabled" not in _settings()
    assert "Recommended: set `autoMemoryEnabled: false`" in out


def test_existing_cc_memory_is_not_offered(isolated, monkeypatch):
    """spool-primary posture works WITH Claude Code memory; turning it off would
    silently orphan that arrangement."""
    out = _init(monkeypatch, interactive=True, answer="y\n", cc_memory=True)
    assert "autoMemoryEnabled" not in _settings()
    assert "techne memory sync" in out


def test_already_off_says_nothing(isolated, monkeypatch):
    hooks.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    hooks.SETTINGS_PATH.write_text(json.dumps({"autoMemoryEnabled": False}))
    out = _init(monkeypatch, interactive=True, answer="y\n")
    assert "auto-memory is on" not in out


def test_env_var_counts_as_off(isolated, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    assert hooks.auto_memory_enabled() is False
