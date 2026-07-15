"""Base Archivist + the #224 topology lane selector (techne#346 PR 1).

standalone=true keeps the sync lane (unchanged); standalone=false routes thread
summaries to sink:archivist, which writes them into a techne-schema archivist.db.
The read-back loop (federation) is PR 2.
"""

from __future__ import annotations

import pytest

from techne.core.archivist import (
    ARCHIVIST_KNOWLEDGE_TARGET,
    ARCHIVIST_SINK,
    archivist_connection,
    archivist_db,
    ensure_registered_for,
    push_knowledge_to_archivist,
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


def test_archivist_db_rejects_personal_db_collision(config):
    """Guard: archivist_db_path must not be the personal techne db, else team intake
    pollutes personal memory (techne#346 review)."""
    config.archivist_db_path = str(config.db_path)
    with pytest.raises(ValueError, match="separate team store"):
        archivist_db(config)


def test_ensure_registered_for_gates_on_standalone(config):
    with use_registry({}):
        config.standalone = True
        ensure_registered_for(config)
        assert get_sink(ARCHIVIST_SINK_TARGET) is None  # standalone → no-op
        config.standalone = False
        ensure_registered_for(config)
        assert get_sink(ARCHIVIST_SINK_TARGET) is ARCHIVIST_SINK


def test_pending_sink_row_drains_from_fresh_process(config):
    """The recovery path (techne#346 review): a sink:archivist row deferred at
    enqueue is drainable by a later flush_sinks in a FRESH process (empty registry)
    once ensure_registered_for re-registers the sink — as the CLI/hook now do."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue, flush_sinks

    init_db(config.db_path)
    ensure_session(config.db_path, "src-sess")
    config.standalone = False

    # Session A: enqueue with delivery DEFERRED (row stays pending), then registry gone.
    with use_registry({}):
        register_base_archivist(config)
        enqueue(config.db_path, ARCHIVIST_SINK_TARGET, _payload(), deliver=False)

    # Fresh process (empty registry): ensure_registered_for re-registers, flush drains.
    with use_registry({}):
        ensure_registered_for(config)
        summary = flush_sinks(config.db_path)

    assert summary.get(ARCHIVIST_SINK_TARGET, (0, 0))[0] == 1  # 1 synced
    conn = plain_connect(archivist_db(config))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE tool_name = 'thread_summary'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ---- PR 2: knowledge egress + federation read-back loop ---------------------


def test_knowledge_push_lands_in_archivist_knowledge_table(config):
    from techne.core.database import init_db
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue

    init_db(config.db_path)
    config.standalone = False
    with use_registry({}):
        register_base_archivist(config)
        enqueue(
            config.db_path, ARCHIVIST_KNOWLEDGE_TARGET,
            {"content": "fact X", "type": "fact", "tags": "t", "confidence": "high"},
            deliver=True,
        )
    conn = plain_connect(archivist_db(config))
    try:
        row = conn.execute("SELECT content, source FROM knowledge").fetchone()
    finally:
        conn.close()
    assert row["content"] == "fact X"
    assert row["source"] == "team"  # marks team provenance


def test_push_knowledge_noop_when_standalone(config):
    from techne.core.database import init_db
    from techne.core.knowledge import store

    init_db(config.db_path)
    store(config.db_path, "local fact", type="fact")
    config.standalone = True
    with use_registry({}):
        assert push_knowledge_to_archivist(config) == 0


def test_push_knowledge_is_idempotent(config):
    """Re-pushing doesn't duplicate — the outbox dedups by content-hash (#346 PR2)."""
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.core.stores import plain_connect

    init_db(config.db_path)
    config.standalone = False
    store(config.db_path, "fact one", type="fact")
    with use_registry({}):
        register_base_archivist(config)
        push_knowledge_to_archivist(config)
        push_knowledge_to_archivist(config)  # second pass must not re-deliver
    conn = plain_connect(archivist_db(config))
    try:
        n = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_archivist_connection_and_effective_connections_gate_on_standalone(config):
    from techne.mcp_server import _effective_connections

    config.standalone = True
    assert archivist_connection(config) is None
    assert "archivist" not in _effective_connections(config)

    config.standalone = False
    spec = archivist_connection(config)
    assert spec is not None and spec.role == "team"
    assert spec.db_path.endswith("archivist.db")
    assert "archivist" in _effective_connections(config)


def test_end_to_end_team_recall_loop(config, tmp_path):
    """The whole point (#346): agent A stores a fact + pushes to a shared Archivist;
    agent B (different local db, same Archivist) recalls it back as team memory."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.mcp_server import _techne_recall

    shared = str(tmp_path / "shared-archivist.db")

    # Agent A: store locally, push to the shared Archivist.
    config.standalone = False
    config.archivist_db_path = shared
    init_db(config.db_path)
    store(config.db_path, "The deploy runbook lives in ops/deploy.md",
          type="fact", tags="ops", confidence="high")
    with use_registry({}):
        register_base_archivist(config)
        assert push_knowledge_to_archivist(config) == 1

    # Agent B: a DIFFERENT empty local db, the SAME Archivist.
    cfg_b = Config.load()
    cfg_b.db_path = tmp_path / "agentB.db"
    cfg_b.standalone = False
    cfg_b.archivist_db_path = shared
    init_db(cfg_b.db_path)

    result = _techne_recall({"topic": "deploy runbook"}, cfg_b)
    team = [r for r in result["search_results"] if r.get("_role") == "team"]
    assert len(team) == 1
    assert "deploy runbook" in team[0]["content"]
    assert team[0]["_store"] == "archivist"


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
