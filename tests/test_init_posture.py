"""CLI `techne init --posture` tests (techne#107 PR4).

Covers the three resolution paths (auto/CC-present, auto/no-CC, explicit
override) and the `techne status` posture line. Mirrors the CLAUDE_CONFIG_DIR
redirect + Config.load monkeypatch pattern from test_memory_doctor.py /
test_standalone_cli.py.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so techne uses a tmp ~/.techne — avoids touching real state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return tmp_path


@pytest.fixture
def cc_memory_dir(tmp_path, monkeypatch):
    """Create a fake CC project memory dir so find_curated_memory_files() is non-empty.

    Uses CLAUDE_CONFIG_DIR redirection (same as test_memory_doctor.py).
    The project cwd for slug resolution defaults to Path.cwd() inside
    find_curated_memory_files — so we monkeypatch Path.cwd to a tmp project.
    """
    from techne.platforms.claude_code.paths import project_slug

    cwd = tmp_path / "proj"
    cwd.mkdir()
    config_dir = tmp_path / "ccconfig"
    slug = project_slug(cwd)
    mem_dir = config_dir / "projects" / slug / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("# memory\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(cwd)
    return mem_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, catch_exceptions=False, env=env)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitPosture:
    def test_auto_with_cc_memory_sets_spool_primary(
        self, isolated_home, cc_memory_dir, monkeypatch
    ):
        """auto + CC memory present → spool-primary."""
        result = _invoke(["init", "--platform", "standalone"])
        assert result.exit_code == 0, result.output
        assert "spool-primary" in result.output

        # Round-trip: the saved config must persist spool-primary
        cfg = Config.load()
        assert cfg.posture.mode == "spool-primary"
        assert cfg.posture.cc_memory_detected is True

    def test_auto_without_cc_memory_sets_observe(self, isolated_home, monkeypatch):
        """auto + no CC memory → observe."""
        # No CC memory dir seeded; CLAUDE_CONFIG_DIR points at an empty tmp dir
        empty_cc = isolated_home / "ccconfig"
        empty_cc.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty_cc))

        result = _invoke(["init", "--platform", "standalone"])
        assert result.exit_code == 0, result.output
        assert "observe" in result.output
        assert "spool-primary" not in result.output

        cfg = Config.load()
        assert cfg.posture.mode == "observe"
        assert cfg.posture.cc_memory_detected is False

    def test_explicit_observe_overrides_cc_detection(
        self, isolated_home, cc_memory_dir, monkeypatch
    ):
        """--posture observe forces observe even when CC memory is present."""
        result = _invoke(["init", "--platform", "standalone", "--posture", "observe"])
        assert result.exit_code == 0, result.output
        assert "observe" in result.output

        cfg = Config.load()
        assert cfg.posture.mode == "observe"
        # cc_memory_detected still reflects detection result
        assert cfg.posture.cc_memory_detected is True

    def test_explicit_spool_primary_without_cc(self, isolated_home, monkeypatch):
        """--posture spool-primary sets spool-primary even when no CC memory detected."""
        empty_cc = isolated_home / "ccconfig"
        empty_cc.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty_cc))

        result = _invoke(
            ["init", "--platform", "standalone", "--posture", "spool-primary"]
        )
        assert result.exit_code == 0, result.output
        assert "spool-primary" in result.output

        cfg = Config.load()
        assert cfg.posture.mode == "spool-primary"
        assert cfg.posture.cc_memory_detected is False


class TestStatusPosture:
    def test_status_shows_posture_line(self, tmp_path, monkeypatch):
        """techne status must display the Posture: line near the Platform: line."""
        db = tmp_path / "techne.db"
        init_db(db).close()
        cfg = Config()
        cfg.db_path = db
        cfg.data_dir = tmp_path
        cfg.posture.mode = "spool-primary"

        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

        result = _invoke(["status"])
        assert result.exit_code == 0, result.output
        assert "Posture: spool-primary" in result.output

    def test_status_default_posture_observe(self, tmp_path, monkeypatch):
        """Default (observe) posture also shows up in status."""
        db = tmp_path / "techne.db"
        init_db(db).close()
        cfg = Config()
        cfg.db_path = db
        cfg.data_dir = tmp_path

        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

        result = _invoke(["status"])
        assert result.exit_code == 0, result.output
        assert "Posture: observe" in result.output
