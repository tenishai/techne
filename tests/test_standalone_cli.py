"""Tests for the standalone-mode init + serve commands (techne#10)."""

from __future__ import annotations

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


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    """A Config pointed at a fresh tmp db, injected via Config.load()."""
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


class TestObserveCommand:
    """`techne observe` — CLI sibling of the techne_observe MCP tool (#57)."""

    def test_records_via_content_and_stamps_platform(self, cli_config) -> None:
        from techne.core.database import connect

        result = CliRunner().invoke(
            main,
            [
                "observe",
                "--session-id",
                "oc-1",
                "--content",
                "edited bar.ts",
                "--platform",
                "opencode",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "observed session=oc-1" in result.output

        conn = connect(cli_config.db_path)
        try:
            sess = conn.execute("SELECT platform FROM sessions WHERE id = ?", ("oc-1",)).fetchone()
            obs = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", ("oc-1",)
            ).fetchall()
        finally:
            conn.close()
        assert sess["platform"] == "opencode"  # client platform stamped (#57)
        assert len(obs) == 1 and obs[0]["content"] == "edited bar.ts"

    def test_reads_content_from_stdin(self, cli_config) -> None:
        from techne.core.database import connect

        result = CliRunner().invoke(
            main,
            ["observe", "--session-id", "oc-2", "--platform", "opencode"],
            input="multi\nline\nobservation\n",
        )
        assert result.exit_code == 0, result.output
        conn = connect(cli_config.db_path)
        try:
            content = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", ("oc-2",)
            ).fetchone()["content"]
        finally:
            conn.close()
        assert content == "multi\nline\nobservation"

    def test_empty_content_errors(self, cli_config) -> None:
        result = CliRunner().invoke(main, ["observe", "--session-id", "oc-3"], input="   \n")
        assert result.exit_code != 0
        assert "content" in result.output.lower()


class TestSessionEndCommand:
    """`techne session-end` — Stop-hook analogue for plugin-driven clients (#57)."""

    def test_detects_threads_and_is_idempotent(self, cli_config) -> None:
        from techne.core.database import connect

        runner = CliRunner()
        # Seed a session with enough observations to form a thread.
        for i in range(8):
            runner.invoke(
                main,
                [
                    "observe",
                    "--session-id",
                    "oc-se",
                    "--platform",
                    "opencode",
                    "--tool-name",
                    "bash",
                    "--content",
                    f"ran command number {i}",
                ],
            )

        r1 = runner.invoke(main, ["session-end", "oc-se"])
        assert r1.exit_code == 0, r1.output
        assert "session-end oc-se" in r1.output

        conn = connect(cli_config.db_path)
        try:
            n1 = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE session_id = ?", ("oc-se",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n1 >= 1  # threads detected for the opencode session

        # Replay: content-addressed thread IDs → no duplication.
        r2 = runner.invoke(main, ["session-end", "oc-se"])
        assert r2.exit_code == 0, r2.output
        conn = connect(cli_config.db_path)
        try:
            n2 = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE session_id = ?", ("oc-se",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n2 == n1  # idempotent — safe to fire per turn


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
