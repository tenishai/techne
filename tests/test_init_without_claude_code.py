"""#1123: `techne init` on a machine without Claude Code.

The default platform still wires the hooks (a user may install Claude Code
afterwards), but init says Claude Code was not found and never talks about, or
offers to change, Claude Code's auto-memory.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.platforms.claude_code import hooks, paths

NOT_FOUND = "Claude Code not found; techne's hooks are registered"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
    cc_dir = tmp_path / "cc" / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc_dir))
    # Registration writes where detection looks, as on a real machine, so a
    # detection moved after registration would see the dir it just created.
    from techne.platforms import claude_code

    for target in (hooks, claude_code):
        monkeypatch.setattr(target, "SETTINGS_PATH", cc_dir / "settings.json", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.setattr("techne.core.memory_doctor.find_curated_memory_files", lambda: [])
    monkeypatch.setattr("techne.cli._interactive", lambda: True)
    return tmp_path


def _init(answer: str = "y\n"):
    result = CliRunner().invoke(main, ["init", "--no-skills", "--no-vectors"], input=answer)
    assert result.exit_code == 0, result.output
    return result.output


def test_absent_says_so_and_never_offers_auto_memory(isolated, monkeypatch):
    monkeypatch.setattr(paths, "_claude_on_path", lambda: False)
    out = _init()
    assert NOT_FOUND in out, out
    assert "techne init --platform standalone" in out
    assert "auto-memory" not in out, out
    # Still wired, and nothing about auto-memory was written for a product that
    # is not there.
    s = json.loads(hooks.SETTINGS_PATH.read_text())
    assert "hooks" in s
    assert "autoMemoryEnabled" not in s
    # ~/.claude.json IS written: it is where the MCP server is registered.
    assert "techne" in json.loads(hooks.MCP_CONFIG_PATH.read_text())["mcpServers"]


def test_present_on_path_is_unchanged(isolated, monkeypatch):
    monkeypatch.setattr(paths, "_claude_on_path", lambda: True)
    out = _init()
    assert NOT_FOUND not in out
    assert "auto-memory is on" in out
    assert json.loads(hooks.SETTINGS_PATH.read_text())["autoMemoryEnabled"] is False


def test_an_existing_projects_dir_counts_as_present(isolated, monkeypatch):
    """`claude` can be off PATH (desktop app, unexported npm prefix); its projects
    dir, where every session writes a transcript, says it has run here."""
    monkeypatch.setattr(paths, "_claude_on_path", lambda: False)
    paths.claude_projects_dir().mkdir(parents=True)
    out = _init(answer="n\n")
    assert NOT_FOUND not in out
    assert "auto-memory is on" in out


def test_a_bare_config_dir_does_not_count(isolated, monkeypatch):
    """A config dir with no projects/ is what a previous `techne init` leaves
    behind, so on its own it cannot mean Claude Code is installed (#1137)."""
    monkeypatch.setattr(paths, "_claude_on_path", lambda: False)
    paths.claude_config_dir().mkdir(parents=True)
    (paths.claude_config_dir() / "settings.json").write_text("{}")
    out = _init()
    assert NOT_FOUND in out
    assert "auto-memory" not in out, out


def test_second_init_without_claude_code_still_says_not_found(isolated, monkeypatch):
    """#1137: the first init creates the config dir; the rerun the upgrade steps
    ask for must not then claim auto-memory is on, or prompt about it."""
    monkeypatch.setattr(paths, "_claude_on_path", lambda: False)
    first = _init()
    assert paths.claude_config_dir().is_dir()
    assert not paths.claude_projects_dir().exists()
    # _interactive is pinned True, so an offer would show its prompt here.
    second = _init(answer="")
    for out in (first, second):
        assert NOT_FOUND in out, out
        assert "auto-memory" not in out, out
        assert "[y/N]" not in out and "[Y/n]" not in out, out
    assert "autoMemoryEnabled" not in json.loads(hooks.SETTINGS_PATH.read_text())


def test_second_init_with_claude_code_offers_again(isolated, monkeypatch):
    """The rerun on a machine that has Claude Code is unchanged: it still offers."""
    monkeypatch.setattr(paths, "_claude_on_path", lambda: True)
    _init(answer="n\n")
    out = _init(answer="n\n")
    assert NOT_FOUND not in out
    assert "auto-memory is on" in out


def test_presence_is_measured_before_registration_creates_the_dir(isolated, monkeypatch):
    """Registration creates the config dir, so a check made after it always says
    "present". The dir must not exist before, and must exist after."""
    monkeypatch.setattr(paths, "_claude_on_path", lambda: False)
    assert not paths.claude_config_dir().exists()
    out = _init()
    assert paths.claude_config_dir().is_dir()
    assert NOT_FOUND in out
