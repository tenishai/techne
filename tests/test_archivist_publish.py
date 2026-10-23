"""The archivist publish lane (#1298): tagged knowledge for a network archivist."""

from __future__ import annotations

import json
import time

import pytest

from techne.core.archivist import (
    ARCHIVIST_PUBLISH_TARGET,
    LEDGER_TARGETS,
    PublishPayload,
    PublishSinkMissing,
    publish_tagged_knowledge,
)
from techne.sync.sinks import DeliveryResult, OutboxSink, use_registry


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    init_db(cfg.db_path)
    return cfg


def _sink(handler=None):
    return OutboxSink(
        name=ARCHIVIST_PUBLISH_TARGET,
        payload_schema=PublishPayload,
        handler=handler or (lambda payload: DeliveryResult(ok=True)),
    )


def _rows(config, target=ARCHIVIST_PUBLISH_TARGET):
    from techne.core.stores import plain_connect

    conn = plain_connect(config.db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT data_json, status, error_message FROM outbox WHERE target = ? ORDER BY id",
                (target,),
            )
        ]
    finally:
        conn.close()


def test_lane_is_off_without_tags(config):
    from techne.core.knowledge import store

    store(config.db_path, "mash1 runs traefik", tags="team")
    # No tags configured and no sink registered: nothing is enqueued and nothing raises.
    with use_registry({}):
        assert publish_tagged_knowledge(config) == 0
    assert _rows(config) == []


def test_only_tagged_rows_are_enqueued(config):
    from techne.core.knowledge import store

    kept = store(
        config.db_path, "nzbget scripts live in /var/servers/nzbget/scripts", tags="mash1,Team"
    )
    store(config.db_path, "PR 349 is open", tags="cabal")
    config.archivist_publish_tags = ["team"]
    config.instance_name = "dev04"
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink()}):
        assert publish_tagged_knowledge(config) == 1

    (row,) = _rows(config)
    payload = json.loads(row["data_json"])
    assert payload["knowledge_id"] == kept
    assert payload["chain_id"] == kept
    assert payload["content"].startswith("nzbget scripts")
    assert payload["written_by"] == "dev04"


def test_a_revision_keeps_the_chain_id_of_the_first_row(config):
    from techne.core.knowledge import store

    first = store(config.db_path, "archivist is at archivist.mash1.lan", tags="team")
    second = store(
        config.db_path, "archivist is at archivist.mash1.lan/mcp", tags="team", supersedes=[first]
    )
    third = store(
        config.db_path,
        "archivist is at archivist.mash1.lan/mcp behind basicAuth",
        tags="team",
        supersedes=[second],
    )
    config.archivist_publish_tags = ["team"]
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink()}):
        assert publish_tagged_knowledge(config) == 1

    (row,) = _rows(config)
    payload = json.loads(row["data_json"])
    assert payload["knowledge_id"] == third
    assert payload["chain_id"] == first


def test_a_second_call_enqueues_nothing_new(config):
    from techne.core.knowledge import store

    store(config.db_path, "CWA metadata.db runs in WAL mode", tags="team")
    config.archivist_publish_tags = ["team"]
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink()}):
        assert publish_tagged_knowledge(config) == 1
        assert publish_tagged_knowledge(config) == 0
    assert len(_rows(config)) == 1
    assert ARCHIVIST_PUBLISH_TARGET in LEDGER_TARGETS


def test_publish_never_delivers_inline(config):
    from techne.core.knowledge import store

    store(config.db_path, "a fact", tags="team")
    config.archivist_publish_tags = ["team"]
    calls = []
    with use_registry(
        {ARCHIVIST_PUBLISH_TARGET: _sink(lambda p: calls.append(p) or DeliveryResult(ok=True))}
    ):
        publish_tagged_knowledge(config)
    assert calls == []
    assert [r["status"] for r in _rows(config)] == ["pending"]


def test_lane_on_without_a_sink_raises(config):
    from techne.core.knowledge import store

    store(config.db_path, "a fact", tags="team")
    config.archivist_publish_tags = ["team"]
    with use_registry({}), pytest.raises(PublishSinkMissing):
        publish_tagged_knowledge(config)
    assert _rows(config) == []


def test_credentials_are_redacted_before_enqueue(config):
    from techne.core.knowledge import store

    store(
        config.db_path,
        "the key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 for now",
        tags="team",
    )
    config.archivist_publish_tags = ["team"]
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink()}):
        publish_tagged_knowledge(config)
    (row,) = _rows(config)
    assert "sk-ant-api03" not in row["data_json"]


def test_flush_budget_leaves_the_rest_pending_without_an_error(config):
    from techne.core.knowledge import store
    from techne.sync.outbox import flush_sinks

    for n in range(3):
        store(config.db_path, f"fact number {n}", tags="team")
    config.archivist_publish_tags = ["team"]

    def slow(payload):
        time.sleep(0.05)
        return DeliveryResult(ok=True)

    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink(slow)}):
        publish_tagged_knowledge(config)
        summary = flush_sinks(config.db_path, budget_s=0.01)

    assert summary.get(ARCHIVIST_PUBLISH_TARGET) == (1, 0), summary
    statuses = [(r["status"], r["error_message"]) for r in _rows(config)]
    assert statuses.count(("synced", None)) == 1
    assert statuses.count(("pending", None)) == 2


def test_flush_without_a_budget_delivers_everything(config):
    from techne.core.knowledge import store
    from techne.sync.outbox import flush_sinks

    for n in range(3):
        store(config.db_path, f"fact number {n}", tags="team")
    config.archivist_publish_tags = ["team"]
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink()}):
        publish_tagged_knowledge(config)
        flush_sinks(config.db_path)
    assert {r["status"] for r in _rows(config)} == {"synced"}


@pytest.mark.parametrize(
    "raw, expected",
    [(["team", " infra "], ["team", "infra"]), ("team, infra", ["team", "infra"]), ([], [])],
)
def test_publish_tags_load_and_round_trip(tmp_path, monkeypatch, raw, expected):
    import yaml

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"archivist_publish_tags": raw}))
    cfg = Config.load()
    assert cfg.archivist_publish_tags == expected
    assert cfg.to_yaml_dict()["archivist_publish_tags"] == expected


def test_flush_budget_loads_and_rejects_negative(tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"sync": {"flush_budget_s": 2}}))
    cfg = Config.load()
    assert cfg.sync.flush_budget_s == 2.0
    assert cfg.to_yaml_dict()["sync"]["flush_budget_s"] == 2.0

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"sync": {"flush_budget_s": -1}}))
    with pytest.raises(ValueError, match="flush_budget_s"):
        Config.load()


def test_publish_tags_are_governed_as_egress():
    from techne.security.config_trust import EGRESS_KEYS

    assert "archivist_publish_tags" in EGRESS_KEYS
