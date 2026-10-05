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
        # A refused package could own these rows: allow it, not a bare prune (Prudence r2).
        assert "allowed_sinks" in result.remedy


def test_the_allow_list_round_trips_through_config_yaml(tmp_path):
    cfg = Config()
    cfg.sync.allowed_sinks = ["courier"]
    cfg.sync.allow_unlisted_sinks = True
    path = tmp_path / "config.yaml"
    cfg.save(path)

    back = Config.load(path)

    assert back.sync.allowed_sinks == ["courier"]
    assert back.sync.allow_unlisted_sinks is True


def _calls(fn: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            out.add(getattr(node.func, "id", None) or getattr(node.func, "attr", None))
    return out


def test_every_drain_registers_installed_sinks_first():
    """Prudence, PR #1173: the first version of this guard only forbade calling
    `ensure_registered_for`, so deleting the session-end hook's registration left a
    bare `flush_sinks` and 350 tests green. Now: any function that drains or prunes
    must also call `ensure_sinks_registered`. sync/outbox.py, where the drains are
    defined, is the only exemption."""
    root = Path(__file__).resolve().parent.parent / "techne"
    offenders = []
    for py in root.rglob("*.py"):
        if py == root / "sync" / "outbox.py":
            continue
        for fn in ast.walk(ast.parse(py.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = _calls(fn)
            if (
                called & {"flush_sinks", "prune_orphans"}
                and "ensure_sinks_registered" not in called
            ):
                offenders.append(f"{py.relative_to(root.parent)}:{fn.name}")
    assert offenders == []


def test_no_site_registers_the_archivist_alone():
    """The old shape of the bug: calling `ensure_registered_for` directly skips the
    installed packages. archivist.py's own enqueue path is exempt (an enqueue)."""
    root = Path(__file__).resolve().parent.parent / "techne"
    exempt = {root / "sync" / "sinks.py", root / "core" / "archivist.py"}
    offenders = [
        f"{py.relative_to(root.parent)}"
        for py in root.rglob("*.py")
        if py not in exempt and "ensure_registered_for" in _calls(ast.parse(py.read_text()))
    ]
    assert offenders == []


def test_prune_orphans_refuses_while_an_allowed_entry_point_failed(config, db, monkeypatch):
    """The `failed` half: an allowed sink whose factory raises at prune time has real
    pending rows, and pruning would kill them (#484 by another door)."""
    from techne.cli import main

    def no_credentials():
        raise RuntimeError("COURIER_TOKEN not set")

    _pending_courier_row(db)
    _install(monkeypatch, FakeEntryPoint("courier", no_credentials))
    config.sync.allowed_sinks = ["courier"]
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))

    with use_registry({}):
        result = CliRunner().invoke(main, ["outbox", "prune-orphans"])

    assert result.exit_code != 0 and "courier" in result.output
    assert _status(db, "sink:courier") == "pending"


def test_prune_orphans_target_prunes_only_the_named_dead_target(config, db, monkeypatch):
    """A package the operator keeps refused must not block pruning a target they can
    name as dead; the refused package's own rows stay."""
    from techne.cli import main

    _pending_courier_row(db)
    ghost = OutboxSink(name="sink:ghost", payload_schema=Note, handler=_deliver)
    with use_registry({ghost.name: ghost}):
        enqueue(db, "sink:ghost", {"content": "nobody"}, deliver=False)
    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))

    with use_registry({}):
        result = CliRunner().invoke(main, ["outbox", "prune-orphans", "--target", "sink:ghost"])

    assert result.exit_code == 0, result.output
    assert _status(db, "sink:ghost") == "dead"
    assert _status(db, "sink:courier") == "pending"


def test_doctor_points_a_true_orphan_at_prune_orphans(config, db, monkeypatch):
    """Nothing installed can own `sink:ghost`, so the remedy is to prune it, not to
    allow a package that does not exist (Prudence, PR #1173)."""
    from techne.core.checks import check_sink_entry_points

    ghost = OutboxSink(name="sink:ghost", payload_schema=Note, handler=_deliver)
    with use_registry({ghost.name: ghost}):
        enqueue(db, "sink:ghost", {"content": "nobody"}, deliver=False)
    _install(monkeypatch)

    with use_registry({}):
        result = check_sink_entry_points(config)

    assert result.verdict.value == "broken"
    assert "prune-orphans" in result.remedy and "allowed_sinks" not in result.remedy


def test_sinks_describe_registers_first(config, monkeypatch):
    from techne.cli import main

    _install(monkeypatch, FakeEntryPoint("courier", lambda: _COURIER))
    config.sync.allowed_sinks = ["courier"]
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))

    with use_registry({}):
        result = CliRunner().invoke(main, ["sinks", "describe", "sink:courier"])

    assert result.exit_code == 0, result.output
    assert "Sink: sink:courier" in result.output


@pytest.mark.parametrize("value, opens", [(True, True), ("true", False), ("false", False)])
def test_only_a_real_true_opens_the_unlisted_gate(tmp_path, value, opens):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"sync": {"allow_unlisted_sinks": value}}))

    assert Config.load(path).sync.allow_unlisted_sinks is opens


def _status(db, target):
    conn = init_db(db)
    try:
        return conn.execute("SELECT status FROM outbox WHERE target=?", (target,)).fetchone()[0]
    finally:
        conn.close()
