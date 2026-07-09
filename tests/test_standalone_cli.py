"""Tests for the standalone-mode init + serve commands (techne#10)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Run CLI commands against a tmp_path config + db location.

    The claude-code init path (register_hooks/register_mcp) is kept off the real
    ~/.claude by the autouse `_isolate_host_config` guard in conftest.py, which pins
    the frozen SETTINGS_PATH/MCP_CONFIG_PATH module attrs into a per-test tmp for
    every test (techne#288). This fixture only needs to relocate HOME for techne's
    own ~/.techne state.
    """
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

        def counts():
            conn = connect(cli_config.db_path)
            try:
                threads = conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE session_id = ?", ("oc-se",)
                ).fetchone()[0]
                # thread_boundaries must NOT accumulate across replays — that was
                # the unbounded-growth bug masked by checking only thread count.
                boundaries = conn.execute(
                    "SELECT COUNT(*) FROM thread_boundaries WHERE session_id = ?", ("oc-se",)
                ).fetchone()[0]
                return threads, boundaries
            finally:
                conn.close()

        r1 = runner.invoke(main, ["session-end", "oc-se"])
        assert r1.exit_code == 0, r1.output
        assert "session-end oc-se" in r1.output
        t1, b1 = counts()
        assert t1 >= 1  # threads detected for the opencode session

        # Replay twice — content-addressed thread IDs AND DELETE-then-insert
        # boundaries → fully idempotent, safe to fire per turn.
        for _ in range(2):
            assert runner.invoke(main, ["session-end", "oc-se"]).exit_code == 0
        t3, b3 = counts()
        assert t3 == t1, "threads must not duplicate on replay"
        assert b3 == b1, "thread_boundaries must not accumulate on replay (unbounded-growth guard)"


class TestDepositCommand:
    """`techne deposit` — federation brick: ephemeral producer writes to a foreign store (#221)."""

    def test_writes_to_target_and_not_local(self, cli_config, tmp_path) -> None:
        """deposit writes to --target/techne.db, not to the local (cli_config) db."""
        from techne.core.database import connect

        foreign_dir = tmp_path / "foreign-agent"
        foreign_dir.mkdir()

        result = CliRunner().invoke(
            main,
            [
                "deposit",
                "--target",
                str(foreign_dir),
                "--session-id",
                "dep-1",
                "--content",
                "the federation is operational",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "deposited session=dep-1" in result.output
        assert str(foreign_dir / "techne.db") in result.output

        # Foreign store has the observation.
        foreign_db = foreign_dir / "techne.db"
        assert foreign_db.exists(), "techne.db must be auto-created under --target"
        conn = connect(foreign_db)
        try:
            n_foreign = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE session_id = ?", ("dep-1",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n_foreign == 1

        # The local (default) store must NOT be touched.
        conn = connect(cli_config.db_path)
        try:
            n_local = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE session_id = ?", ("dep-1",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n_local == 0, "deposit must not write to the local store"

    def test_reads_content_from_stdin(self, cli_config, tmp_path) -> None:
        foreign_dir = tmp_path / "foreign-stdin"
        foreign_dir.mkdir()

        result = CliRunner().invoke(
            main,
            ["deposit", "--target", str(foreign_dir), "--session-id", "dep-stdin"],
            input="piped content goes here\n",
        )
        assert result.exit_code == 0, result.output
        assert "deposited session=dep-stdin" in result.output

    def test_empty_content_errors(self, cli_config, tmp_path) -> None:
        foreign_dir = tmp_path / "foreign-empty"
        foreign_dir.mkdir()

        result = CliRunner().invoke(
            main,
            ["deposit", "--target", str(foreign_dir), "--session-id", "dep-err"],
            input="   \n",
        )
        assert result.exit_code != 0
        assert "content" in result.output.lower()


class TestContextCommand:
    """`techne context` — memory block for prompt injection (#57 read-side)."""

    def test_empty_db_prints_nothing(self, cli_config) -> None:
        result = CliRunner().invoke(main, ["context"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == ""

    def test_emits_memory_block_with_active_threads(self, cli_config) -> None:
        runner = CliRunner()
        # Seed an opencode session and run session-end to create an active thread.
        for i in range(8):
            runner.invoke(
                main,
                [
                    "observe",
                    "--session-id",
                    "oc-ctx",
                    "--platform",
                    "opencode",
                    "--project-path",
                    "/proj",
                    "--tool-name",
                    "bash",
                    "--content",
                    f"did thing {i}",
                ],
            )
        runner.invoke(main, ["session-end", "oc-ctx"])

        result = runner.invoke(main, ["context", "--project-path", "/proj"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "<techne-memory>" in out and "</techne-memory>" in out
        assert "Active threads" in out


def test_format_resume_context_empty_returns_blank():
    from techne.core.state import format_resume_context

    assert format_resume_context({"active_threads": [], "knowledge": []}) == ""


def test_format_resume_context_caps_length():
    from techne.core.state import format_resume_context

    big = [{"label": f"t{i}", "resume_points": "x" * 500} for i in range(20)]
    out = format_resume_context({"active_threads": big, "knowledge": []}, max_chars=300)
    # Capped body + closing tag; the truncation ellipsis is present.
    assert out.endswith("</techne-memory>")
    assert "…" in out
    assert len(out) < 400


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


class TestStatusPlatforms:
    """`techne status` shows a Platforms section (#81)."""

    def test_status_includes_platforms_section(self, cli_config) -> None:
        """`techne status` renders a Platforms section when sessions exist."""
        from techne.core.state import start_session

        start_session(cli_config.db_path, platform="claude-code")
        start_session(cli_config.db_path, platform="opencode")

        result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "Platforms:" in result.output
        assert "claude-code" in result.output
        assert "opencode" in result.output
        assert "sessions" in result.output

    def test_status_no_platforms_section_when_empty(self, cli_config) -> None:
        """`techne status` skips the Platforms section when the DB has no sessions."""
        result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "Platforms:" not in result.output
