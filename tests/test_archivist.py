"""Base Archivist + the #224 topology lane selector (techne#346 PR 1).

standalone=true keeps the sync lane (unchanged); standalone=false routes thread
summaries to sink:archivist, which writes them into a techne-schema archivist.db.
The read-back loop (federation) is PR 2.
"""

from __future__ import annotations

import pytest

from techne.core.archivist import (
    ARCHIVIST_SINK,
    archivist_db,
    register_base_archivist,
)
from techne.lifecycle import (
    ARCHIVIST_SINK_TARGET,
    THREAD_SUMMARY_SYNC_TARGET,
    _has_live_consumer,
    _thread_summary_target,
)
from techne.sync.sinks import get_sink, use_registry


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    return Config.load()


def _payload():
    return {
        "session_id": "src-sess",
        "thread_id": "t-1",
        "thread_label": "Work on X",
        "summary": "did the thing",
        "resume_points": "next: Y",
        "agent_identity": "agentA",
    }


# ---- config -----------------------------------------------------------------


def test_standalone_defaults_true_and_round_trips(config, tmp_path):
    from techne.config import Config

    assert config.standalone is True  # behaviour-preserving default
    config.standalone = False
    config.archivist_db_path = "/tmp/custom-archivist.db"
    path = tmp_path / "rt.yaml"
    config.save(path)
    reloaded = Config.load(path)
    assert reloaded.standalone is False
    assert reloaded.archivist_db_path == "/tmp/custom-archivist.db"


# ---- lane selector (#224) ---------------------------------------------------


def test_lane_selector_by_topology(config):
    config.standalone = True
    assert _thread_summary_target(config) == THREAD_SUMMARY_SYNC_TARGET
    config.standalone = False
    assert _thread_summary_target(config) == ARCHIVIST_SINK_TARGET


def test_has_live_consumer_is_lane_aware(config):
    # standalone lane → gated on the sync adapter being non-noop.
    config.standalone = True
    config.sync.adapter = "noop"
    assert _has_live_consumer(config) is False
    config.sync.adapter = "github_issues"
    assert _has_live_consumer(config) is True

    # team lane → gated on a registered archivist sink, regardless of sync adapter.
    config.standalone = False
    config.sync.adapter = "noop"
    with use_registry({}):
        assert _has_live_consumer(config) is False
        register_base_archivist(config)
        assert _has_live_consumer(config) is True


# ---- base Archivist store + sink --------------------------------------------


def test_archivist_db_is_techne_schema(config):
    db = archivist_db(config)
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    # A real techne schema so federation can read it as a team connection (PR 2).
    assert {"knowledge", "observations"} <= tables


def test_default_archivist_path_under_data_dir(config):
    assert archivist_db(config) == config.data_dir / "archivist.db"


def test_push_lands_in_archivist_as_observation(config):
    """End-to-end: enqueue to sink:archivist → the summary is written into
    archivist.db as a raw thread_summary observation (§3)."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue

    init_db(config.db_path)
    ensure_session(config.db_path, "src-sess")

    with use_registry({}):
        register_base_archivist(config)
        enqueue(config.db_path, ARCHIVIST_SINK_TARGET, _payload(), deliver=True)

    conn = plain_connect(archivist_db(config))
    try:
        rows = conn.execute(
            "SELECT content FROM observations WHERE tool_name = 'thread_summary'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert "Work on X" in rows[0]["content"]
    assert "did the thing" in rows[0]["content"]


def test_register_base_archivist_is_idempotent(config):
    with use_registry({}):
        register_base_archivist(config)
        register_base_archivist(config)  # must not raise (same singleton)
        assert get_sink(ARCHIVIST_SINK_TARGET) is ARCHIVIST_SINK


def test_invalid_payload_rejected_at_enqueue(config):
    """The sink's payload schema validates at enqueue — a bad payload raises rather
    than silently landing malformed team memory."""
    from techne.core.database import init_db
    from techne.sync.outbox import enqueue
    from techne.sync.sinks import InvalidPayloadError

    init_db(config.db_path)
    with use_registry({}):
        register_base_archivist(config)
        with pytest.raises(InvalidPayloadError):
            enqueue(config.db_path, ARCHIVIST_SINK_TARGET, {"bogus": "shape"}, deliver=False)
