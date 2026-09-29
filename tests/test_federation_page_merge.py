"""techne#1089 — federated recall merges pages; it does not re-score the local one.

`merge_and_rank` used to pool the local `search()` page with the remote rows and
run `_rerank` (BM25 / recency / decay) over the pool, discarding the vec-first
order and every reserved seat (#827 `lexical_tail`, #946 `curated_head`, #1078
`identifier_head`). On the 2026-09-28 snapshot, with NO remote rows, the page's
#1 stayed #1 in 3 of 40 real prompts.

The end-to-end tests drive `techne_recall` through the MCP handler with a real
team store attached, so they hold whatever `merge_and_rank`'s signature is. The
local page is pinned the way test_search_identifier_head.py pins it: both
channels and the rare-key pool are fixed, so the page carries all three kinds of
reserved seat at known indices.
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config, FederationConnectionSpec
from techne.core import search as search_mod
from techne.core.database import init_db
from techne.core.knowledge import store

QUERY = "emit_max_chars"


def _row(table: str, rid: int, distance: float = 0.5) -> dict:
    return {"source_table": table, "id": rid, "content": f"{table}-{rid}", "distance": distance}


def _scoped(rows):
    def _fn(_db, _query, table="all", limit=20, **_kw):
        return [r for r in rows if table == "all" or table == r["source_table"]][:limit]

    return _fn


@pytest.fixture
def pinned_page(monkeypatch):
    """A local page with an identifier_head, two curated_head and a lexical_tail seat.

    12 vec observations fill the head; 4 vec knowledge rows sit past it (curated
    candidates); FTS holds two rare-identifier rows, one of which the identifier
    head seats and the other the #827 floor seats.
    """
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec += [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    # Signals a re-score would act on: the further down the vector channel, the
    # NEWER the row, and the tail row is the stronger BM25 match. Page order must
    # not follow them; on main the no-remote page came back reordered by exactly these.
    for n, r in enumerate(vec):
        r["timestamp"] = f"2026-09-{1 + n:02d} 00:00:00"
    fts = [
        dict(_row("observations", 500), rank=-1.0, timestamp="2026-01-01 00:00:00"),
        dict(_row("observations", 501), rank=-9.0, timestamp="2026-09-28 00:00:00"),
    ]
    rare = {("observations", 500)}
    monkeypatch.setattr(search_mod, "search_vec", _scoped(vec))
    monkeypatch.setattr(search_mod, "search_fts", _scoped(fts))

    def _pool(_db, _q, _table, _slots, _fraction, fts_results, **_kw):
        return [r for r in fts_results if (r["source_table"], r["id"]) in rare]

    monkeypatch.setattr(search_mod, "_identifier_pool", _pool)


def _db(path):
    init_db(path).close()
    return path


def _recall(cfg):
    from techne.mcp_server import handle_request

    resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "techne_recall", "arguments": {"topic": QUERY}},
        },
        cfg,
    )
    assert "error" not in resp, resp.get("error")
    rows = json.loads(resp["result"]["content"][0]["text"])["search_results"]
    return [r for r in rows if r.get("origin") == "memory"]


def _cfg(local_db, team_db=None):
    cfg = Config()
    cfg.db_path = local_db
    if team_db is not None:
        cfg.connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}
    return cfg


def _shape(rows):
    return [
        (r.get("_store", "local"), r["source_table"], r["id"], r.get("reserved_slot")) for r in rows
    ]


def _reserved(rows):
    return {
        i: (r["source_table"], r["id"], r["reserved_slot"])
        for i, r in enumerate(rows)
        if r.get("reserved_slot")
    }


def test_fixture_page_carries_every_kind_of_reserved_seat(tmp_path, pinned_page):
    """Sanity for the tests below: the local-only page has all three reservations."""
    rows = _recall(_cfg(_db(tmp_path / "local.db")))

    assert len(rows) == 10
    assert _reserved(rows) == {
        0: ("observations", 500, "identifier_head"),
        7: ("knowledge", 90, "curated_head"),
        8: ("knowledge", 91, "curated_head"),
        9: ("observations", 501, "lexical_tail"),
    }


def test_invariant_1_no_remote_rows_leaves_the_page_identical(tmp_path, pinned_page):
    """A team store that matches nothing must not change the page: same rows, same
    order, same reserved_slot tags as recall with no connections at all."""
    local_db = _db(tmp_path / "local.db")
    team_db = _db(tmp_path / "team.db")

    local_only = _recall(_cfg(local_db))
    federated = _recall(_cfg(local_db, team_db))

    assert all(r.get("_store") == "local" for r in federated), "fixture: no remote rows"
    assert _shape(federated) == _shape(local_only)


def test_invariant_2_reserved_seats_survive_remote_rows_in_place(tmp_path, pinned_page):
    """Remote rows that match take seats, but never a reserved one: every row
    search() seated by a reservation keeps its index and its tag."""
    local_db = _db(tmp_path / "local.db")
    team_db = _db(tmp_path / "team.db")
    for i in range(10):
        store(team_db, f"team note {i}: emit_max_chars is set in config.yaml", type="fact")

    local_only = _recall(_cfg(local_db))
    federated = _recall(_cfg(local_db, team_db))

    assert sum(r.get("_store") == "team" for r in federated) > 0, "fixture: remote rows merged"
    assert _reserved(federated) == _reserved(local_only)


def test_remote_rows_alternate_into_the_unreserved_seats(tmp_path, pinned_page):
    """The placement rule: unreserved seats take the local page's unreserved rows and
    the team store's rows alternately, local first, each in its own order. Remote
    rows keep their `_store`/`_role` provenance; the local rows pushed off the end
    are the ones lowest among the unreserved seats."""
    local_db = _db(tmp_path / "local.db")
    team_db = _db(tmp_path / "team.db")
    for i in range(3):
        store(team_db, f"team note {i}: emit_max_chars is set in config.yaml", type="fact")

    local_only = _recall(_cfg(local_db))
    federated = _recall(_cfg(local_db, team_db))

    team = [r for r in federated if r.get("_store") == "team"]
    assert len(team) == 3
    assert all(r.get("_role") == "team" for r in team)
    free_local = [r for r in local_only if not r.get("reserved_slot")]
    L = [("local", r["source_table"], r["id"], None) for r in free_local]
    T = [("team", "knowledge", r["id"], None) for r in team]
    free_seats = [s for i, s in enumerate(_shape(federated)) if i not in _reserved(federated)]
    assert free_seats == [L[0], T[0], L[1], T[1], L[2], T[2]]


# --- the rule, unit level ---------------------------------------------------------


def _merge(local, remote, limit):
    from techne.core.federation import merge_and_rank

    return [(r.get("_store", "local"), r["id"]) for r in merge_and_rank(local, remote, limit=limit)]


def _rows(store_name, ids, **tags):
    return [dict({"source_table": "knowledge", "id": i, "_store": store_name}, **tags) for i in ids]


def test_two_remote_stores_take_turns_behind_the_local_page():
    local = _rows("local", [1, 2, 3])
    remote = _rows("a", [10, 11]) + _rows("b", [20, 21])

    assert _merge(local, remote, 7) == [
        ("local", 1),
        ("a", 10),
        ("b", 20),
        ("local", 2),
        ("a", 11),
        ("b", 21),
        ("local", 3),
    ]


def test_a_remote_or_only_row_waits_for_the_local_first_tier_rows():
    """Across stores the #1079 tier rule still holds between stream heads: a remote
    row whose only evidence is a partial lexical match goes after first-tier heads."""
    local = _rows("local", [1, 2, 3], match_source="vec")
    remote = _rows("a", [10], _match_pass="or", match_source="fts")

    assert _merge(local, remote, 4) == [("local", 1), ("local", 2), ("local", 3), ("a", 10)]


def test_a_remote_first_tier_row_goes_before_a_local_or_only_head():
    local = _rows("local", [1], _match_pass="or", match_source="fts")
    remote = _rows("a", [10], _match_pass="and", match_source="fts")

    assert _merge(local, remote, 2) == [("a", 10), ("local", 1)]


def test_an_or_row_with_vector_agreement_stays_first_tier():
    local = _rows("local", [1, 2], _match_pass="or", match_source="fts+vec")
    remote = _rows("a", [10], _match_pass="and", match_source="fts")

    assert _merge(local, remote, 3) == [("local", 1), ("a", 10), ("local", 2)]


def test_a_reserved_seat_past_the_merge_keeps_its_index():
    """A page shorter than limit: the lexical_tail row keeps index 2 and remote
    rows fill the seats after it rather than pushing it down."""
    local = _rows("local", [1, 2]) + _rows("local", [3], reserved_slot="lexical_tail")
    remote = _rows("a", [10, 11, 12])

    assert _merge(local, remote, 6) == [
        ("local", 1),
        ("a", 10),
        ("local", 3),
        ("local", 2),
        ("a", 11),
        ("a", 12),
    ]
