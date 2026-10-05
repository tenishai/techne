"""Pluggable recall sources and the operator-defined recall order (techne#1170)."""

from __future__ import annotations

import time

import pytest

import techne.core.recall_sources as rs
from techne.config import Config, RecallOrderEntry


class FakeSource:
    def __init__(self, name, rows=None, *, escalation=False, healthy=True, delay=0.0):
        self.name = name
        self.escalation = escalation
        self._rows = rows if rows is not None else [{"content": f"{name} says hi", "id": 1}]
        self._healthy = healthy
        self._delay = delay
        self.calls = 0

    def search(self, topic, limit):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        return self._rows[:limit]

    def health(self):
        return self._healthy


class FakeEntryPoint:
    def __init__(self, name, factory):
        self.name = name
        self._factory = factory
        self.loads = 0

    def load(self):
        self.loads += 1
        return self._factory


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(rs, "_LOADED", {})


def _install(monkeypatch, *eps):
    monkeypatch.setattr(rs, "entry_points", lambda group: list(eps))


@pytest.fixture
def cfg(tmp_path):
    from techne.core.database import init_db
    from techne.core.knowledge import store

    c = Config.load()
    c.db_path = tmp_path / "local.db"
    c.data_dir = tmp_path
    c.connections = {}
    c.archivist_db_path = ""
    c.standalone = True
    init_db(c.db_path)
    for i in range(3):
        store(c.db_path, f"deploy runbook step {i} for the kafka cluster", type="fact", source="t")
    return c


def _recall(cfg, topic="deploy runbook"):
    from techne.mcp_server import _techne_recall

    return _techne_recall({"topic": topic}, cfg)


def _names(out):
    return [t["name"] for t in out["tiers"]]


def _tier(out, name):
    return next(t for t in out["tiers"] if t["name"] == name)


# --- the default path is untouched -----------------------------------------------------


def test_no_order_and_no_source_takes_the_old_path(cfg, monkeypatch):
    from techne.mcp_server import _fold_in_documents, _recall_rows

    _install(monkeypatch)
    rows = [{"id": 1, "content": "x"}]
    got, kw = _recall_rows(cfg, "t", list(rows), "archivist")
    assert kw == {}
    assert got == _fold_in_documents(cfg, "t", [dict(r) for r in rows])
    assert _names(_recall(cfg)) == ["memory", "archivist:local", "archivist:remote", "librarian"]


def test_an_unlisted_source_is_refused_and_never_imported(cfg, monkeypatch):
    ep = FakeEntryPoint("remote-lib", lambda: FakeSource("librarian:remote"))
    _install(monkeypatch, ep)

    out = _recall(cfg)

    assert ep.loads == 0
    assert _names(out) == ["memory", "archivist:local", "archivist:remote", "librarian"]


# --- plugged-in sources ----------------------------------------------------------------


def test_an_allowed_source_joins_after_the_built_ins(cfg, monkeypatch):
    src = FakeSource("librarian:remote")
    _install(monkeypatch, FakeEntryPoint("remote-lib", lambda: src))
    cfg.recall.allowed_sources = ["remote-lib"]

    out = _recall(cfg)

    assert _names(out)[-1] == "librarian:remote"
    rung = _tier(out, "librarian:remote")
    assert (rung["tier"], rung["state"], rung["results"]) == (5, "hit", 1)
    row = out["search_results"][-1]
    assert row["origin"] == "librarian:remote" and row["external"] is True
    assert row["recall_tier"] == 5


def test_the_order_places_rows_and_reports_by_source(cfg, monkeypatch):
    src = FakeSource("librarian:remote")
    _install(monkeypatch, FakeEntryPoint("remote-lib", lambda: src))
    cfg.recall.allowed_sources = ["remote-lib"]
    cfg.recall.order = [RecallOrderEntry("librarian:remote"), RecallOrderEntry("memory")]

    out = _recall(cfg)

    assert out["search_results"][0]["origin"] == "librarian:remote"
    assert all(r.get("origin") == "memory" for r in out["search_results"][1:])
    assert _names(out)[:2] == ["librarian:remote", "memory"]
    # Built-ins the order leaves out are reported, not hidden.
    assert _tier(out, "librarian")["state"] == "excluded"
    assert _tier(out, "archivist:remote")["state"] == "excluded"


def test_richards_order_with_a_fallback_remote_librarian(cfg, monkeypatch):
    """techne -> local librarian -> archivist -> remote librarian (knowledge 4171),
    the remote one a fallback: not called while the earlier sources fill `fill`."""
    src = FakeSource("librarian:remote")
    _install(monkeypatch, FakeEntryPoint("remote-lib", lambda: src))
    cfg.recall.allowed_sources = ["remote-lib"]
    cfg.recall.order = [
        RecallOrderEntry("memory"),
        RecallOrderEntry("librarian"),
        RecallOrderEntry("archivist:local"),
        RecallOrderEntry("archivist:remote"),
        RecallOrderEntry("librarian:remote", fallback=True),
    ]
    cfg.recall.fill = 3

    out = _recall(cfg)
    assert src.calls == 0
    assert _tier(out, "librarian:remote")["state"] == "skipped"
    assert _names(out) == [
        "memory",
        "librarian",
        "archivist:local",
        "archivist:remote",
        "librarian:remote",
    ]

    cfg.recall.fill = 50
    out = _recall(cfg)
    assert src.calls == 1
    assert _tier(out, "librarian:remote")["state"] == "hit"


def test_a_fallback_librarian_is_not_queried_once_memory_fills(cfg, monkeypatch):
    import techne.core.librarian as lib

    calls = []
    monkeypatch.setattr(lib, "recall_documents", lambda c, t, **k: calls.append(t) or [])
    _install(monkeypatch)
    cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("librarian", fallback=True)]
    cfg.recall.fill = 1

    out = _recall(cfg)

    assert calls == []
    assert _tier(out, "librarian")["state"] == "skipped"


# --- a source can never stall or break recall ------------------------------------------


def test_a_slow_source_times_out_and_recall_still_returns(cfg, monkeypatch):
    src = FakeSource("slow", delay=2.0)
    _install(monkeypatch, FakeEntryPoint("slow", lambda: src))
    cfg.recall.allowed_sources = ["slow"]
    cfg.recall.source_timeout_s = 0.2

    t0 = time.monotonic()
    out = _recall(cfg)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5
    rung = _tier(out, "slow")
    assert rung["state"] == "unavailable" and "timed out" in rung["detail"]
    assert any(r.get("origin") == "memory" for r in out["search_results"])


@pytest.mark.parametrize(
    "src, why",
    [
        (FakeSource("down", healthy=False), "health() returned False"),
        (FakeSource("boom", rows=None), None),
    ],
    ids=["unhealthy", "healthy"],
)
def test_an_unhealthy_source_reads_unavailable(cfg, monkeypatch, src, why):
    _install(monkeypatch, FakeEntryPoint(src.name, lambda: src))
    cfg.recall.allowed_sources = [src.name]

    rung = _tier(_recall(cfg), src.name)

    if why:
        assert rung["state"] == "unavailable" and why in rung["detail"]
    else:
        assert rung["state"] == "hit"


def test_a_source_that_fails_to_load_is_reported_and_recall_continues(cfg, monkeypatch):
    def boom():
        raise RuntimeError("no endpoint configured")

    _install(
        monkeypatch,
        FakeEntryPoint("broken", boom),
        FakeEntryPoint("clash", lambda: FakeSource("memory")),
    )
    cfg.recall.allowed_sources = ["broken", "clash"]
    cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("broken")]

    out = _recall(cfg)

    rung = _tier(out, "broken")
    assert rung["state"] == "unavailable" and "no endpoint configured" in rung["detail"]
    found = rs.discover_sources(cfg)
    assert "built-in rung" in found.failed["clash"]


def test_returned_content_is_redacted_and_marked_external(cfg, monkeypatch):
    leak = FakeSource("leaky", rows=[{"content": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}])
    _install(monkeypatch, FakeEntryPoint("leaky", lambda: leak))
    cfg.recall.allowed_sources = ["leaky"]

    row = next(r for r in _recall(cfg)["search_results"] if r.get("origin") == "leaky")

    assert "ghp_" not in row["content"] and "<redacted:github>" in row["content"]
    assert row["external"] is True


def test_an_escalation_source_counts_as_documentation(cfg, monkeypatch):
    ref = FakeSource("docs:remote", escalation=True)
    _install(monkeypatch, FakeEntryPoint("docs", lambda: ref))
    cfg.recall.allowed_sources = ["docs"]
    cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("docs:remote")]

    out = _recall(cfg, topic="zzqx plinthwaggle")

    assert _tier(out, "docs:remote")["escalation"] is True


# --- doctor -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "allowed, order, verdict",
    [
        ([], [], "dormant"),
        ([], ["remote-lib"], "broken"),
        (["remote-lib"], ["librarian:remote"], "live"),
    ],
    ids=["refused_unreferenced", "ordered_but_refused", "loaded"],
)
def test_doctor_flags_an_ordered_source_recall_cannot_use(
    cfg, monkeypatch, allowed, order, verdict
):
    from techne.core.checks import check_recall_sources

    _install(monkeypatch, FakeEntryPoint("remote-lib", lambda: FakeSource("librarian:remote")))
    cfg.recall.allowed_sources = allowed
    cfg.recall.order = [RecallOrderEntry(n) for n in order]

    result = check_recall_sources(cfg)

    assert result.verdict.value == verdict


def test_a_hand_written_recall_block_survives_a_save(tmp_path):
    """`techne init` is a load->save round-trip (techne#620)."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "recall:\n"
        "  order:\n"
        "    - memory\n"
        "    - {name: librarian:remote, fallback: true}\n"
        "  allowed_sources: [remote-lib]\n"
        "  fill: 4\n"
    )
    first = Config.load(path)
    first.save(path)
    again = Config.load(path)

    assert again.recall == first.recall
    assert [(e.name, e.fallback) for e in again.recall.order] == [
        ("memory", False),
        ("librarian:remote", True),
    ]
    assert again.recall.allowed_sources == ["remote-lib"] and again.recall.fill == 4
