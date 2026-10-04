"""Outbox sinks from installed packages: discovery, the gate, and every drain site
(techne#1172)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

import techne.sync.sinks as sinks_mod
from techne.config import Config
from techne.core.database import init_db
from techne.sync.outbox import enqueue, flush_sinks
from techne.sync.sinks import (
    DeliveryResult,
    OutboxSink,
    discover_sinks,
    ensure_sinks_registered,
    get_sink,
    use_registry,
)


@dataclass
class Note:
    content: str


DELIVERED: list[dict] = []


def _deliver(data: dict) -> DeliveryResult:
    DELIVERED.append(data)
    return DeliveryResult(ok=True)


_COURIER = OutboxSink(name="sink:courier", payload_schema=Note, handler=_deliver)


class FakeEntryPoint:
    """Stands in for importlib.metadata.EntryPoint; counts imports."""

    def __init__(self, name, factory):
        self.name = name
        self._factory = factory
        self.loads = 0

    def load(self):
        self.loads += 1
        return self._factory


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


@pytest.fixture
def config(db):
    cfg = Config()
    cfg.db_path = db
    cfg.data_dir = db.parent
    return cfg


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """No sinks loaded by an earlier test, and an empty delivery log."""
    monkeypatch.setattr(sinks_mod, "_LOADED_ENTRY_POINTS", {})
    DELIVERED.clear()


def _install(monkeypatch, *eps):
    monkeypatch.setattr(sinks_mod, "entry_points", lambda group: list(eps))


def _pending_courier_row(db):
    """A row queued by a process that had the sink, as it waits for a later one."""
    with use_registry({_COURIER.name: _COURIER}):
        enqueue(db, "sink:courier", {"content": "hello"}, deliver=False)


def test_an_unlisted_entry_point_is_refused_and_never_imported(config, monkeypatch):
    ep = FakeEntryPoint("courier", lambda: _COURIER)
    _install(monkeypatch, ep)

    with use_registry({}):
        found = discover_sinks(config)
        assert get_sink("sink:courier") is None

    assert found.refused == ["courier"] and not found.loaded
    assert ep.loads == 0, "the gate must run before ep.load(): import is execution"


def test_an_allowed_entry_point_drains_its_rows_in_a_fresh_process(config, db, monkeypatch):
    _pending_courier_row(db)
    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    config.sync.allowed_sinks = ["courier"]

    with use_registry({}):  # a fresh process: nothing registered yet
        found = ensure_sinks_registered(config)
        summary = flush_sinks(db)

    assert found.loaded == {"courier": ["sink:courier"]}
    assert summary["sink:courier"] == (1, 0)
    assert DELIVERED == [{"content": "hello"}]


def test_allow_unlisted_sinks_loads_every_entry_point(config, monkeypatch):
    other = OutboxSink(name="sink:other", payload_schema=Note, handler=_deliver)
    _install(
        monkeypatch,
        FakeEntryPoint("courier", lambda: _COURIER),
        FakeEntryPoint("pair", lambda: [other]),
    )
    config.sync.allow_unlisted_sinks = True

    with use_registry({}):
        found = discover_sinks(config)
        assert get_sink("sink:courier") and get_sink("sink:other")

    assert sorted(found.loaded) == ["courier", "pair"]


def test_a_failing_entry_point_is_recorded_and_the_rest_still_load(config, monkeypatch):
    def boom():
        raise RuntimeError("no credentials")

    _install(
        monkeypatch,
        FakeEntryPoint("broken", boom),
        FakeEntryPoint("wrong", lambda: "not a sink"),
        FakeEntryPoint("courier", lambda: _COURIER),
    )
    config.sync.allow_unlisted_sinks = True

    with use_registry({}):
        found = discover_sinks(config)
        assert get_sink("sink:courier") is not None

    assert set(found.failed) == {"broken", "wrong"}
    assert "no credentials" in found.failed["broken"]
    assert "not OutboxSink" in found.failed["wrong"]


def test_a_loaded_entry_point_is_not_rerun(config, monkeypatch):
    """A second factory call builds new handlers, which register_sink rejects."""
    ep = FakeEntryPoint("courier", lambda: OutboxSink("sink:courier", Note, lambda d: None))
    _install(monkeypatch, ep)
    config.sync.allowed_sinks = ["courier"]

    with use_registry({}):
        discover_sinks(config)
        second = discover_sinks(config)

    assert ep.loads == 1
    assert second.loaded == {"courier": ["sink:courier"]} and not second.failed


def test_prune_orphans_refuses_while_an_entry_point_is_refused(config, db, monkeypatch):
    from techne.cli import main

    _pending_courier_row(db)
    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))

    with use_registry({}):
        result = CliRunner().invoke(main, ["outbox", "prune-orphans"])

    assert result.exit_code != 0
    assert "courier" in result.output and "Not pruning" in result.output
    conn = init_db(db)
    try:
        status = conn.execute("SELECT status FROM outbox WHERE target='sink:courier'").fetchone()
    finally:
        conn.close()
    assert status[0] == "pending", "the row must not be marked dead"


def test_sinks_list_runs_discovery_and_names_refused_entry_points(config, monkeypatch):
    from techne.cli import main

    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))

    with use_registry({}):
        out = CliRunner().invoke(main, ["sinks", "list"]).output

    assert "refused entry point courier" in out


@pytest.mark.parametrize(
    "allowed, queued, verdict",
    [
        ([], False, "dormant"),
        ([], True, "broken"),
        (["courier"], True, "live"),
    ],
    ids=["refused_nothing_waiting", "refused_rows_stranded", "loaded"],
)
def test_doctor_names_stranded_targets(config, db, monkeypatch, allowed, queued, verdict):
    from techne.core.checks import check_sink_entry_points

    if queued:
        _pending_courier_row(db)
    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    config.sync.allowed_sinks = allowed

    with use_registry({}):
        result = check_sink_entry_points(config)

    assert result.verdict.value == verdict
    if verdict == "broken":
        assert "sink:courier" in result.detail and "courier" in result.detail


def test_the_allow_list_round_trips_through_config_yaml(tmp_path):
    cfg = Config()
    cfg.sync.allowed_sinks = ["courier"]
    cfg.sync.allow_unlisted_sinks = True
    path = tmp_path / "config.yaml"
    cfg.save(path)

    back = Config.load(path)

    assert back.sync.allowed_sinks == ["courier"]
    assert back.sync.allow_unlisted_sinks is True


def test_no_drain_site_registers_the_archivist_alone():
    """Every site that drains or prunes must go through ensure_sinks_registered,
    or an installed package's rows strand again at the site that forgot. This guard
    found the session-end hook, which #1172's list of sites missed.

    Exempt: sinks.py (the helper itself) and archivist.py, whose own enqueue path
    registers its own lane before writing to it, which is not a drain."""
    root = Path(__file__).resolve().parent.parent / "techne"
    exempt = {root / "sync" / "sinks.py", root / "core" / "archivist.py"}
    offenders = []
    for py in root.rglob("*.py"):
        if py in exempt:
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "ensure_registered_for":
                    offenders.append(f"{py.relative_to(root.parent)}:{node.lineno}")
    assert offenders == []
