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
    # leads with a blank line so the heading clears Claude Code's systemMessage chrome
    assert banner.startswith("\n## techne ✅")
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


# --- Vector channel health (techne#358) ---------------------------------------


def _healthy_h(config):
    h = collect_health(config)
    h["build"] = "live"
    return h


def test_banner_vectors_no_backend_warns(config):
    """Stored vectors but no backend = the silent FTS-only trap → ⚠️."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": None,
        "has_vectors": True,
        "reembed_needed": False,
        "degraded": True,
    }
    banner = format_banner(h)
    vline = [ln for ln in banner.splitlines() if "vector" in ln.lower()][0]
    assert "⚠️" in vline
    assert "keyword-only" in vline
    assert "techne[vectors]" in vline


def test_banner_vectors_reembed_needed_warns(config):
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": "model2vec (dim=256)",
        "has_vectors": True,
        "reembed_needed": True,
        "degraded": True,
    }
    banner = format_banner(h)
    vline = [ln for ln in banner.splitlines() if "vector" in ln.lower()][0]
    assert "⚠️" in vline
    assert "stale" in vline
    assert "backfill" in vline


def test_banner_vectors_healthy_silent(config):
    """Healthy backend → no vector line (warnings-only banner)."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": "model2vec (dim=256)",
        "has_vectors": True,
        "reembed_needed": False,
        "degraded": False,
    }
    banner = format_banner(h)
    assert "vector" not in banner.lower()


def test_banner_vectors_never_used_silent(config):
    """No backend AND no vectors = never opted in → no false alarm."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": None,
        "has_vectors": False,
        "reembed_needed": False,
        "degraded": False,
    }
    banner = format_banner(h)
    assert "vector" not in banner.lower()


def test_collect_health_maps_degraded_vector_status(config, monkeypatch):
    """collect_health folds vector_channel_status into a degraded verdict:
    stored vectors + no backend → degraded True."""
    fake = {
        "active_backend": None,
        "stamp": None,
        "tables": {"observations_vec": {"exists": True, "dim": 256, "rows": 10}},
        "reembed_needed": False,
    }
    monkeypatch.setattr(
        "techne.core.vector_status.vector_channel_status", lambda db_path, **kw: fake
    )
    h = collect_health(config)
    assert h["vectors"]["backend"] is None
    assert h["vectors"]["has_vectors"] is True
    assert h["vectors"]["degraded"] is True


def test_collect_health_vectors_healthy_not_degraded(config, monkeypatch):
    fake = {
        "active_backend": {"name": "model2vec", "dim": 256},
        "stamp": {"name": "model2vec", "dim": 256},
        "tables": {"observations_vec": {"exists": True, "dim": 256, "rows": 10}},
        "reembed_needed": False,
    }
    monkeypatch.setattr(
        "techne.core.vector_status.vector_channel_status", lambda db_path, **kw: fake
    )
    h = collect_health(config)
    assert h["vectors"]["degraded"] is False
    assert h["vectors"]["backend"] == "model2vec (dim=256)"


def test_collect_health_skips_backend_build_when_no_vectors(config, monkeypatch):
    """#361 review: on an install with no vec tables and no stamp, the session-start
    hot path must NOT build the embedding backend (the expensive load + dim probe)."""
    from unittest.mock import MagicMock

    spy = MagicMock()
    monkeypatch.setattr("techne.core.embeddings._get_embedding_model", spy)
    h = collect_health(config)  # fresh DB → no *_vec tables, no stamp
    spy.assert_not_called()  # backend build short-circuited
    assert h["vectors"]["degraded"] is False


def test_vector_status_probes_with_allow_download_false(config, monkeypatch):
    """The download-safety guarantee: when the backend IS built, it's with
    allow_download=False so a cold-cache fetch can't block the hook."""
    from unittest.mock import MagicMock

    from techne.core.vector_status import vector_channel_status

    fake_model = MagicMock()
    fake_model.name, fake_model.dim = "model2vec", 256
    spy = MagicMock(return_value=fake_model)
    monkeypatch.setattr("techne.core.embeddings._get_embedding_model", spy)
    # Default probe_backend_when_empty=True → build attempted even on an empty channel.
    vector_channel_status(config.db_path)
    spy.assert_called_once_with(allow_download=False)


def test_collect_health_vectors_none_on_status_error(config, monkeypatch):
    """collect_health degrades vectors→None (never raises) if the status check throws."""

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("techne.core.vector_status.vector_channel_status", boom)
    h = collect_health(config)
    assert h["vectors"] is None


# ---------------------------------------------------------------------------
# Per-category memory fill-state (techne#334)
# ---------------------------------------------------------------------------


def test_banner_memory_line_breaks_down_by_type(config):
    """The banner surfaces per-category knowledge counts so the agent can
    self-regulate what it writes (techne#334)."""
    from techne.core.knowledge import store

    store(config.db_path, "postgres is the datastore", type="fact")
    store(config.db_path, "another fact about caching", type="fact")
    store(config.db_path, "always use type hints", type="procedure")
    store(config.db_path, "chose FastAPI", type="decision")

    h = collect_health(config)
    assert h["knowledge_by_type"] == {"fact": 2, "procedure": 1, "decision": 1}

    banner = format_banner(h)
    assert "> memory:" in banner
    # canonical VALID_TYPES order: fact before decision before procedure
    assert "2 fact" in banner
    assert "1 decision" in banner
    assert "1 procedure" in banner
    assert banner.index("2 fact") < banner.index("1 decision") < banner.index("1 procedure")


def test_banner_memory_line_absent_on_empty_store(config):
    """No knowledge and no identity → no memory line (nothing to self-regulate)."""
    h = collect_health(config)
    assert h["knowledge_by_type"] == {}
    banner = format_banner(h)
    assert "> memory:" not in banner


def test_banner_memory_line_includes_identity(config):
    """Identity lives in its own table but folds into the same breakdown."""
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO identity (key, value) VALUES ('user.name', 'Richard')"
        )
        conn.commit()
    finally:
        conn.close()

    h = collect_health(config)
    banner = format_banner(h)
    assert "1 identity" in banner


def test_banner_memory_line_appends_non_canonical_types_after_canonical(config):
    """A knowledge type not in VALID_TYPES (e.g. a legacy/other-domain 'pattern'
    row) still renders — appended AFTER the canonical types so the breakdown
    always accounts for the full total (techne#334, Prudence PR #366)."""
    from techne.core.database import connect
    from techne.core.knowledge import store

    store(config.db_path, "a real fact", type="fact")
    # 'pattern' is not in VALID_TYPES, so store() would reject it — insert direct.
    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge (type, content) VALUES ('pattern', 'some pattern')"
        )
        conn.commit()
    finally:
        conn.close()

    h = collect_health(config)
    assert h["knowledge_by_type"].get("pattern") == 1
    banner = format_banner(h)
    assert "1 pattern" in banner
    # canonical 'fact' renders BEFORE the non-canonical 'pattern'
    assert banner.index("1 fact") < banner.index("1 pattern")
