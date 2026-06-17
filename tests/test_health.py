"""Tests for the health summary + login banner (#76)."""

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.core.health import collect_health, format_banner


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    return cfg


def test_collect_health_db_ok(config):
    h = collect_health(config)
    assert h["db_ok"] is True
    assert h["version"]  # non-empty
    assert h["package_path"].endswith("techne")
    assert h["sync_adapter"] == "noop"
    # db_stats fields present
    for key in ("observations", "threads", "active_threads", "knowledge", "pending_outbox"):
        assert key in h


def test_collect_health_db_unreachable(tmp_path):
    cfg = Config()
    # Point at a path that cannot be opened as a DB (a directory).
    cfg.db_path = tmp_path  # a directory, not a file
    h = collect_health(cfg)
    assert h["db_ok"] is False
    assert "error" in h
    # The shared payload still carries the build metadata even on DB failure.
    assert h["version"]


def test_banner_healthy_live(config):
    h = collect_health(config)
    h["build"] = "live"
    banner = format_banner(h)
    assert banner.startswith("## techne ✅")
    assert "live" in banner
    assert "obs ·" in banner
    assert "threads" in banner


def test_banner_installed_build_warns(config):
    h = collect_health(config)
    h["build"] = "installed"
    banner = format_banner(h)
    assert "⚠️" in banner
    assert "may lag repo HEAD" in banner


def test_banner_db_unreachable(config):
    h = collect_health(config)
    h["db_ok"] = False
    h["error"] = "OperationalError: boom"
    banner = format_banner(h)
    assert "DB UNREACHABLE" in banner
    assert "boom" in banner


def test_banner_outbox_noop_no_warning(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 1189
    h["sync_adapter"] = "noop"
    banner = format_banner(h)
    assert "outbox: 1189 pending" in banner
    # noop never drains by design → informational, not a ⚠️ on the outbox line.
    outbox_line = [ln for ln in banner.splitlines() if "outbox" in ln][0]
    assert "⚠️" not in outbox_line


def test_banner_outbox_real_adapter_warns(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 5
    h["sync_adapter"] = "http"
    banner = format_banner(h)
    outbox_line = [ln for ln in banner.splitlines() if "outbox" in ln][0]
    assert "⚠️" in outbox_line


def test_banner_no_outbox_line_when_empty(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 0
    banner = format_banner(h)
    assert "outbox" not in banner
