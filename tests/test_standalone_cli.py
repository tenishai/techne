"""Tests for the standalone-mode init + serve commands (techne#10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Run CLI commands against a tmp_path config + db location."""
    # techne reads HOME for default ~/.techne/...
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some envs honor XDG; cover both.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return tmp_path


def test_init_standalone_prints_next_steps(isolated_config) -> None:
    """`techne init --platform standalone` must print a usable starting
    point: how to expose techne as an MCP server, how to embed it as a
    library, how to verify."""
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--platform", "standalone"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Standalone init complete" in out
    # Both paths surfaced — MCP server + library embedding
    assert "techne serve" in out
    assert "techne.run_session_start" in out
    # And how to verify the setup worked
    assert "techne status" in out


def test_init_claude_code_unchanged(isolated_config) -> None:
    """The Claude Code path should not print the standalone next-steps
    block — it has its own message."""
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--platform", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "Standalone init complete" not in result.output
    assert "techne initialized" in result.output


def test_serve_command_exists() -> None:
    """`techne serve --help` runs without error — sanity check the
    command is registered and click can build its usage doc."""
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output.lower()


def test_serve_invokes_mcp_server_main(monkeypatch, isolated_config) -> None:
    """`techne serve` calls techne.mcp_server.main verbatim."""
    called = {"yes": False}

    def fake_main():
        called["yes"] = True

    monkeypatch.setattr("techne.mcp_server.main", fake_main)

    runner = CliRunner()
    result = runner.invoke(main, ["serve"])
    assert result.exit_code == 0, result.output
    assert called["yes"] is True
