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
    monkeypatch.setattr(rs, "_FAILED", {})
    monkeypatch.setattr(rs, "_WARNED", set())
    monkeypatch.setattr(rs, "_IN_FLIGHT", {})
    monkeypatch.setattr(rs, "_TEAM", {})
    monkeypatch.setattr(rs, "_TEAM_ERROR", {})


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
    import techne.mcp_server as srv

    _install(monkeypatch)

    def _never(*a, **k):
        raise AssertionError("the ordered path must not run without an order or a source")

    monkeypatch.setattr(srv, "_ordered_recall", _never)
    out = _recall(cfg)

    assert _names(out) == ["memory", "archivist:local", "archivist:remote", "librarian"]
    assert all("recall_source" not in r for r in out["search_results"])


def test_the_idle_recall_path_does_not_scan_entry_points(cfg, monkeypatch):
    """Nothing can be allowed, so the 9-27 ms scan is skipped on the recall path."""
    scans = []
    monkeypatch.setattr(rs, "entry_points", lambda group: scans.append(group) or [])

    _recall(cfg)
    assert scans == []
    rs.discover_sources(cfg)  # doctor's call keeps scanning
    assert scans == [rs.SOURCE_ENTRY_POINT_GROUP]


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
    assert "reserved" in found.failed["clash"]


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


# --- the federation branch of the ordered path (Prudence, PR #1174) ---------------------


def _federated(cfg, monkeypatch, remote_rows):
    """A configured remote archivist whose fan-out returns `remote_rows`, and a log
    of every fan-out call."""
    import techne.core.federation as fed
    import techne.mcp_server as srv

    calls = []

    def fan_out(db, conns, topic, limit=10, local_results=None, **kw):
        calls.append(topic)
        return local_results, [dict(r, _store="peer", _role="team") for r in remote_rows]

    monkeypatch.setattr(fed, "fan_out_search", fan_out)
    monkeypatch.setattr(srv, "_effective_connections", lambda c: {"peer": object()})
    return calls


@pytest.fixture
def busy_cfg(cfg):
    from techne.core.knowledge import store

    for i in range(12):
        store(cfg.db_path, f"deploy runbook kafka note {i}", type="fact", source="t")
    return cfg


def test_an_excluded_remote_costs_no_fan_out_and_no_memory_seats(busy_cfg, monkeypatch):
    """Probe from Prudence's review: 12 local matches came back as 5, because the
    merged page was cut to 10 before the order dropped the remote rows."""
    _install(monkeypatch)
    calls = _federated(
        busy_cfg, monkeypatch, [{"id": 900 + i, "content": "remote"} for i in range(10)]
    )
    busy_cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("librarian")]

    out = _recall(busy_cfg)

    assert calls == [], "an archivist the order leaves out is never searched"
    rungs = [r["recall_tier_name"] for r in out["search_results"]]
    assert rungs.count("memory") == 10, "memory keeps its whole page"
    assert _tier(out, "archivist:remote")["state"] == "excluded"


def test_a_remote_after_memory_keeps_its_own_rows_and_memory_keeps_its_page(busy_cfg, monkeypatch):
    _install(monkeypatch)
    _federated(busy_cfg, monkeypatch, [{"id": 900 + i, "content": "remote"} for i in range(4)])
    busy_cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("archivist:remote")]

    out = _recall(busy_cfg)

    rungs = [r["recall_tier_name"] for r in out["search_results"]]
    assert rungs.count("memory") == 10, "memory keeps its whole page"
    assert rungs[-4:] == ["archivist:remote"] * 4, "the remote follows, in order"
    assert _tier(out, "archivist:remote")["results"] == 4


def test_a_fallback_remote_that_is_not_reached_is_never_fanned_out_to(busy_cfg, monkeypatch):
    _install(monkeypatch)
    calls = _federated(busy_cfg, monkeypatch, [{"id": 901, "content": "remote"}])
    busy_cfg.recall.order = [
        RecallOrderEntry("memory"),
        RecallOrderEntry("archivist:remote", fallback=True),
    ]
    busy_cfg.recall.fill = 5

    out = _recall(busy_cfg)

    assert calls == []
    assert _tier(out, "archivist:remote")["state"] == "skipped"
    assert "not searched" in _tier(out, "archivist:remote")["detail"]


# --- a hung source cannot wedge the others (Prudence, PR #1174) --------------------------


def test_one_hung_source_never_starves_a_healthy_one(cfg, monkeypatch):
    import threading

    release = threading.Event()
    hung = FakeSource("hung")
    hung.search = lambda topic, limit: release.wait(30) and []
    healthy = FakeSource("healthy")
    _install(
        monkeypatch,
        FakeEntryPoint("hung", lambda: hung),
        FakeEntryPoint("healthy", lambda: healthy),
    )
    cfg.recall.allowed_sources = ["hung", "healthy"]
    cfg.recall.source_timeout_s = 0.2
    try:
        for _ in range(rs._POOL._max_workers + 2):
            out = _recall(cfg)
            assert _tier(out, "healthy")["state"] == "hit", _tier(out, "healthy")
            assert _tier(out, "hung")["state"] == "unavailable"
        assert "still running" in _tier(out, "hung")["detail"]
    finally:
        release.set()


def test_a_failed_factory_is_not_rerun_on_every_recall(cfg, monkeypatch):
    def boom():
        raise RuntimeError("no endpoint")

    ep = FakeEntryPoint("broken", boom)
    _install(monkeypatch, ep)
    cfg.recall.allowed_sources = ["broken"]

    for _ in range(3):
        found = rs.discover_sources(cfg)
    assert ep.loads == 1 and "no endpoint" in found.failed["broken"]


@pytest.mark.parametrize("value, opens", [(True, True), ("true", False), ("false", False)])
def test_only_a_real_true_opens_the_unlisted_sources_gate(tmp_path, value, opens):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"recall": {"allow_unlisted_sources": value}}))

    assert Config.load(path).recall.allow_unlisted_sources is opens


def test_an_excluded_memory_is_never_searched_and_drops_no_team_rows(busy_cfg, monkeypatch):
    """Prudence, PR #1174 r2: reaching an archivist searched memory anyway and
    deduplicated against it, so a team row the operator also held locally vanished
    for a local copy recall never returned."""
    import techne.mcp_server as srv

    _install(monkeypatch)
    _federated(
        busy_cfg,
        monkeypatch,
        [
            {"id": 901, "content": "deploy runbook kafka note 3"},
            {"id": 902, "content": "team only"},
        ],
    )
    searches = []
    real = srv._local_search
    monkeypatch.setattr(srv, "_local_search", lambda *a, **k: searches.append(1) or real(*a, **k))
    busy_cfg.recall.order = [RecallOrderEntry("archivist:remote")]

    out = _recall(busy_cfg)

    assert searches == [], "memory is not in the order, so it is not searched"
    contents = [r["content"] for r in out["search_results"]]
    assert contents == ["deploy runbook kafka note 3", "team only"]
    assert _tier(out, "memory")["state"] == "excluded"


def test_with_memory_in_the_order_a_duplicate_team_row_is_still_dropped(busy_cfg, monkeypatch):
    from techne.core.knowledge import store

    # The duplicate must be a row memory's page certainly holds. The twelve busy
    # notes tie on BM25 and fill 10 seats, so which of them make the page depends
    # on CURRENT_TIMESTAMP's one-second resolution: a second boundary falling just
    # after "note 3" or "note 4" was stored pushed note 3 off it (issue 1184). This
    # row is the shortest match, so the strongest BM25 hit, and the newest.
    store(busy_cfg.db_path, "deploy runbook kafka", type="fact", source="t")
    _install(monkeypatch)
    _federated(
        busy_cfg,
        monkeypatch,
        [
            {"id": 901, "content": "deploy runbook kafka"},
            {"id": 902, "content": "team only"},
        ],
    )
    busy_cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("archivist:remote")]

    out = _recall(busy_cfg)

    remote = [
        r["content"] for r in out["search_results"] if r["recall_tier_name"] == "archivist:remote"
    ]
    assert remote == ["team only"], "the local copy is authoritative, as in the default path"


def test_a_fallback_counts_rows_after_cross_store_dedup(cfg, monkeypatch):
    """techne#1177 (Prudence's repro, PR #1174 r3): a remote returning copies of
    local rows was counted toward `fill`, so the fallback was skipped while recall
    returned fewer than `fill` rows."""
    from techne.core.database import connect

    conn = connect(cfg.db_path)
    try:
        local = [r[0] for r in conn.execute("SELECT content FROM knowledge ORDER BY id")]
    finally:
        conn.close()
    _federated(cfg, monkeypatch, [{"id": 900 + i, "content": c} for i, c in enumerate(local[:2])])
    src = FakeSource("librarian:remote")
    _install(monkeypatch, FakeEntryPoint("remote-lib", lambda: src))
    cfg.recall.allowed_sources = ["remote-lib"]
    cfg.recall.order = [
        RecallOrderEntry("memory"),
        RecallOrderEntry("archivist:remote"),
        RecallOrderEntry("librarian:remote", fallback=True),
    ]
    cfg.recall.fill = 4

    out = _recall(cfg)

    assert src.calls == 1, "after dedup only 3 rows came back, below fill, so it runs"
    assert _tier(out, "librarian:remote")["state"] == "hit"
    assert len(out["search_results"]) == 4


def test_both_entry_point_pins_are_active():
    """Prudence, PR #1174 r4: a merge resolution dropped the autouse decorator from the
    sink pin, leaving it a plain function nothing called. Both pins must be live."""
    import importlib.metadata

    import techne.sync.sinks as sinks_mod

    assert sinks_mod.entry_points is not importlib.metadata.entry_points
    assert rs.entry_points is not importlib.metadata.entry_points


# --- the sync adapter's pull as the `team` source (techne#1171) --------------------------


class PullAdapter:
    """A team backend that can be read back."""

    supports_pull = True

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.queries = []

    def push(self, entries):
        return None

    def pull(self, query):
        from techne.sync.adapter import KnowledgeEntry

        self.queries.append(query)
        return [
            KnowledgeEntry(
                content=f"team note on {query.query}",
                type="fact",
                tags="t",
                confidence="high",
                source="team-backend",
            )
        ]

    def health(self):
        return self.healthy


def _adapter(monkeypatch, adapter):
    """Serve `adapter` as the configured sync adapter: its class is what
    `adapter_class` returns, and constructing that class yields `adapter`."""
    import techne.sync.adapter as ad

    cls = type("Served", (), {"supports_pull": adapter.supports_pull})
    cls.__new__ = lambda klass, **kw: adapter
    monkeypatch.setattr(ad, "adapter_class", lambda name, **kw: cls)


def test_a_pull_capable_adapter_feeds_recall_as_team(cfg, monkeypatch):
    _install(monkeypatch)
    backend = PullAdapter()
    _adapter(monkeypatch, backend)
    cfg.sync.adapter = "teamstore"

    out = _recall(cfg)

    row = next(r for r in out["search_results"] if r.get("origin") == "team")
    assert row["content"] == "team note on deploy runbook" and row["external"] is True
    assert row["knowledge_type"] == "fact"
    assert _tier(out, "team")["state"] == "hit"
    assert backend.queries[0].limit == cfg.recall.source_limit


def test_a_team_row_carries_the_optional_entry_fields_the_adapter_set(cfg, monkeypatch):
    """Issue #1335: enough to cite and supersede a recalled team row; unset fields stay off."""
    from techne.sync.adapter import KnowledgeEntry

    class CitingAdapter(PullAdapter):
        def pull(self, query):
            return [
                KnowledgeEntry(
                    content=f"team note on {query.query}",
                    type="fact",
                    tags="t",
                    confidence="high",
                    source="team-backend",
                    id="k-17",
                    written_by="agent-a",
                    supersedes="k-9",
                )
            ]

    _install(monkeypatch)
    _adapter(monkeypatch, CitingAdapter())
    cfg.sync.adapter = "teamstore"

    row = next(r for r in _recall(cfg)["search_results"] if r.get("origin") == "team")

    assert (row["id"], row["written_by"], row["supersedes"]) == ("k-17", "agent-a", "k-9")
    assert not {"updated_at", "entities", "relations"} & row.keys()


def test_team_takes_its_place_in_the_order(cfg, monkeypatch):
    _install(monkeypatch)
    _adapter(monkeypatch, PullAdapter())
    cfg.sync.adapter = "teamstore"
    cfg.recall.order = [RecallOrderEntry("team"), RecallOrderEntry("memory")]

    out = _recall(cfg)

    assert out["search_results"][0]["origin"] == "team"
    assert _names(out)[:2] == ["team", "memory"]


def test_the_builtin_adapters_add_no_team_source(cfg, monkeypatch):
    """noop and filesystem do not declare supports_pull: a default install is untouched."""
    _install(monkeypatch)
    for name in ("noop", "filesystem"):
        cfg.sync.adapter = name
        cfg.sync.path = str(cfg.data_dir / "sync")
        monkeypatch.setattr(rs, "_TEAM", {})
        monkeypatch.setattr(rs, "_TEAM_ERROR", {})
        assert rs.team_source(cfg) is None
        assert _names(_recall(cfg)) == [
            "memory",
            "archivist:local",
            "archivist:remote",
            "librarian",
        ]


def test_an_adapter_that_does_not_opt_in_is_not_a_source(cfg, monkeypatch):
    class PushOnly(PullAdapter):
        supports_pull = False

    _install(monkeypatch)
    _adapter(monkeypatch, PushOnly())
    cfg.sync.adapter = "pushonly"
    cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("team")]

    rung = _tier(_recall(cfg), "team")

    assert rung["state"] == "unavailable" and "does not declare supports_pull" in rung["detail"]


def test_an_unreachable_backend_degrades_and_logs_once(cfg, monkeypatch, caplog):
    _install(monkeypatch)
    _adapter(monkeypatch, PullAdapter(healthy=False))
    cfg.sync.adapter = "teamstore"

    with caplog.at_level("WARNING", logger="techne.core.recall_sources"):
        first = _recall(cfg)
        second = _recall(cfg)

    for out in (first, second):
        assert _tier(out, "team")["state"] == "unavailable"
        assert any(r.get("origin") == "memory" for r in out["search_results"])
    warnings = [r for r in caplog.records if "'team' unavailable" in r.getMessage()]
    assert len(warnings) == 1


def test_a_plugged_in_source_cannot_claim_the_team_name(cfg, monkeypatch):
    _install(monkeypatch, FakeEntryPoint("imposter", lambda: FakeSource("team")))
    cfg.recall.allowed_sources = ["imposter"]

    found = rs.discover_sources(cfg)

    assert "reserved" in found.failed["imposter"]


# --- load failures and construction are reported truthfully and bounded (PR #1175) -------


def test_a_refused_adapter_is_reported_as_a_load_failure_and_warned_once(cfg, monkeypatch, caplog):
    import techne.sync.adapter as ad

    def refuse(name, **kw):
        raise ValueError("unlisted adapters are refused by default (techne#389)")

    _install(monkeypatch)
    monkeypatch.setattr(ad, "adapter_class", refuse)
    cfg.sync.adapter = "teamstore"
    cfg.recall.order = [RecallOrderEntry("memory"), RecallOrderEntry("team")]

    with caplog.at_level("WARNING", logger="techne.core.recall_sources"):
        first = _recall(cfg)
        _recall(cfg)

    rung = _tier(first, "team")
    assert rung["state"] == "unavailable"
    assert "could not be loaded" in rung["detail"] and "techne#389" in rung["detail"]
    assert "supports_pull" not in rung["detail"]
    assert len([r for r in caplog.records if "'team' unavailable" in r.getMessage()]) == 1


def test_a_constructor_that_fails_once_is_retried(cfg, monkeypatch):
    import techne.sync.adapter as ad

    built = []
    backend = PullAdapter()

    class Flaky:
        supports_pull = True

        def __new__(cls, **kw):
            built.append(1)
            if len(built) == 1:
                raise ConnectionError("backend not up yet")
            return backend

    _install(monkeypatch)
    monkeypatch.setattr(ad, "adapter_class", lambda name, **kw: Flaky)
    cfg.sync.adapter = "teamstore"

    first, second = _recall(cfg), _recall(cfg)

    assert _tier(first, "team")["state"] == "unavailable"
    assert "backend not up yet" in _tier(first, "team")["detail"]
    assert _tier(second, "team")["state"] == "hit"


def test_a_slow_constructor_cannot_block_recall(cfg, monkeypatch):
    import techne.sync.adapter as ad

    class Slow:
        supports_pull = True

        def __new__(cls, **kw):
            time.sleep(2.0)
            return PullAdapter()

    _install(monkeypatch)
    monkeypatch.setattr(ad, "adapter_class", lambda name, **kw: Slow)
    cfg.sync.adapter = "teamstore"
    cfg.recall.source_timeout_s = 0.2

    t0 = time.monotonic()
    out = _recall(cfg)

    assert time.monotonic() - t0 < 1.5
    assert _tier(out, "team")["state"] == "unavailable"


def test_doctor_points_team_at_the_sync_adapter_not_the_allow_list(cfg, monkeypatch):
    from techne.core.checks import check_recall_sources

    class PushOnly(PullAdapter):
        supports_pull = False

    _install(monkeypatch)
    _adapter(monkeypatch, PushOnly())
    cfg.sync.adapter = "pushonly"
    cfg.recall.order = [RecallOrderEntry("team")]

    result = check_recall_sources(cfg)

    assert result.verdict.value == "broken"
    assert "supports_pull" in result.remedy and "allowed_sources" not in result.remedy
