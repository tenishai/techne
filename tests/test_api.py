"""techne.api: recall and publish for an embedding runtime (issue #1335)."""

from __future__ import annotations

import pytest

import techne.core.recall_sources as rs
from techne import api
from techne.core.archivist import ARCHIVIST_PUBLISH_TARGET, PublishPayload
from techne.sync.sinks import DeliveryResult, OutboxSink, use_registry


class PoolSource:
    name = "liminal_archivist"
    escalation = False

    def search(self, topic, limit):
        return [{"content": f"pool row on {topic}", "written_by": "agent-a"}]

    def health(self):
        return True


class PoolEntryPoint:
    name = "liminal_archivist"

    def load(self):
        return PoolSource


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    for attr in ("_LOADED", "_FAILED", "_IN_FLIGHT", "_TEAM", "_TEAM_ERROR"):
        monkeypatch.setattr(rs, attr, {})
    monkeypatch.setattr(rs, "_WARNED", set())
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.knowledge import store

    cfg = Config.load()
    cfg.connections = {}
    cfg.archivist_db_path = ""
    cfg.standalone = True
    init_db(cfg.db_path)
    store(cfg.db_path, "the kafka deploy runbook lives in ops/kafka", type="fact", source="t")
    return cfg


def test_recall_returns_a_pool_row_marked_with_its_source(config, monkeypatch):
    monkeypatch.setattr(rs, "entry_points", lambda group: [PoolEntryPoint()])
    config.recall.allowed_sources = ["liminal_archivist"]

    rows = api.recall("kafka deploy", config=config)

    pool = [r for r in rows if r.get("recall_source") == "liminal_archivist"]
    assert [r["content"] for r in pool] == ["pool row on kafka deploy"]
    assert pool[0]["external"] is True and pool[0]["written_by"] == "agent-a"
    local = [r for r in rows if r.get("store") == "local"]
    assert any("ops/kafka" in r["content"] for r in local)
    assert not any(r.get("recall_source") for r in local)


def test_recall_honours_the_limit(config):
    from techne.core.knowledge import store

    for i in range(5):
        store(config.db_path, f"kafka deploy note {i}", type="fact", source="t")

    assert len(api.recall("kafka deploy", limit=2, config=config)) == 2


def _sink(delivered):
    def handler(payload):
        delivered.append(payload)
        return DeliveryResult(ok=True)

    return OutboxSink(name=ARCHIVIST_PUBLISH_TARGET, payload_schema=PublishPayload, handler=handler)


def test_publish_sends_a_tagged_row_through_the_sink(config):
    from techne.core.knowledge import store

    kept = store(config.db_path, "nzbget scripts live in /srv/nzbget/scripts", tags="team")
    store(config.db_path, "PR 349 is open", tags="cabal")
    config.archivist_publish_tags = ["team"]
    delivered = []

    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink(delivered)}):
        result = api.publish(config=config)

    assert result == {"enqueued": 1, "synced": 1, "failed": 0}
    assert [p["knowledge_id"] for p in delivered] == [kept]
    # Delivered once: a second publish finds nothing new to send.
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink(delivered)}):
        assert api.publish(config=config) == {"enqueued": 0, "synced": 0, "failed": 0}
    assert len(delivered) == 1


def test_publish_reports_a_failed_delivery(config):
    from techne.core.knowledge import store

    store(config.db_path, "the gateway runs traefik", tags="team")
    config.archivist_publish_tags = ["team"]
    sink = OutboxSink(
        name=ARCHIVIST_PUBLISH_TARGET,
        payload_schema=PublishPayload,
        handler=lambda payload: DeliveryResult(ok=False, error="archivist down"),
    )

    with use_registry({ARCHIVIST_PUBLISH_TARGET: sink}):
        assert api.publish(config=config) == {"enqueued": 1, "synced": 0, "failed": 1}


def test_publish_never_raises_when_the_lane_has_no_sink(config):
    from techne.core.knowledge import store

    store(config.db_path, "the gateway runs traefik", tags="team")
    config.archivist_publish_tags = ["team"]

    with use_registry({}):
        result = api.publish(config=config)

    assert (result["enqueued"], result["synced"]) == (0, 0)
    assert result["error"].startswith("PublishSinkMissing")


def test_publish_never_raises_when_the_flush_fails(config, monkeypatch):
    import techne.sync.outbox as outbox

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(outbox, "flush_sinks", boom)
    with use_registry({}):
        result = api.publish(config=config)

    assert result == {"enqueued": 0, "synced": 0, "failed": 0, "error": "OSError: disk full"}


def test_the_package_exposes_api():
    import techne

    assert techne.api is api
    assert "api" in dir(techne)
