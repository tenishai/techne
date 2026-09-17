"""techne#946 — a table="all" page reserves head slots for curated rows.

The #827 tail floor rescues a curated row the LEXICAL channel found. A curated row
the VECTOR channel found but ranked below the head budget was reachable by neither:
`fts_only` excludes every vec key, so `_curated_first` never sees it. An external
evaluator measured three gold queries losing their curated answer exactly that way
(ranked 1, 2 and 7 in `table="knowledge"`, absent from `table="all"`).

These drive `search()` through a monkeypatched vector channel rather than a seeded
corpus: the defect is about ORDER, and a fixture that pins the order states the claim
directly instead of hoping an embedding model reproduces it.
"""

from __future__ import annotations

import pytest

from techne.core import search as search_mod
from techne.core.database import init_db
from techne.core.search import (
    CURATED_HEAD_SLOTS_DEFAULT,
    _curated_head_reservation,
    search,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p).close()
    return p


def _row(table: str, rid: int, distance: float) -> dict:
    return {
        "source_table": table,
        "id": rid,
        "content": f"{table}-{rid}",
        "distance": distance,
    }


#: `search()` asks the vector channel for `table="knowledge"` separately when the
#: reservation has a deficit, so the fakes MUST honour `table` and `limit` the way the
#: real ones do. A fake that ignores `table` returns observations to a knowledge-scoped
#: query and hides exactly the bug this file exists to pin.
_TABLE_ALIASES = {"observations": "observations", "knowledge": "knowledge", "threads": "threads"}


def _scoped(rows: list[dict]):
    def _fn(_db, _query, table="all", limit=20, **_kw):
        out = [r for r in rows if table == "all" or _TABLE_ALIASES.get(table) == r["source_table"]]
        return out[:limit]

    return _fn


def _patch_channels(monkeypatch, vec_rows: list[dict], fts_rows: list[dict] | None = None):
    """Pin both channels, scoped by table and truncated by limit like the real ones."""
    monkeypatch.setattr(search_mod, "search_fts", _scoped(list(fts_rows or [])))
    monkeypatch.setattr(search_mod, "search_vec", _scoped(list(vec_rows)))


def _tables(rows: list[dict]) -> list[str]:
    return [r["source_table"] for r in rows]


# --- the defect ------------------------------------------------------------------


def test_a_curated_row_below_the_head_budget_reaches_the_page(db, monkeypatch):
    """The reported shape: observations fill the page, the curated row is last."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 99, 0.90))
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)

    assert ("knowledge", 99) in [(r["source_table"], r["id"]) for r in page]
    assert len(page) == 10, "the reservation displaces, it does not widen the page"


def test_without_the_reservation_that_row_is_lost(db, monkeypatch):
    """The pre-#946 page, reachable with slots=0 — this is what main returns."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 99, 0.90))
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10, curated_head_slots=0)

    assert ("knowledge", 99) not in [(r["source_table"], r["id"]) for r in page]


def test_the_promoted_row_says_why_it_is_on_the_page(db, monkeypatch):
    """Decomposability is the point of choosing slots over a score prior."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 99, 0.90))
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)

    promoted = [r for r in page if r.get("reserved_slot") == "curated_head"]
    assert [r["id"] for r in promoted] == [99]
    assert all("reserved_slot" not in r for r in page if r["source_table"] == "observations")


def test_the_reservation_is_capped_at_a_third_of_the_head(db, monkeypatch):
    """The measured knee is 3 of a 10-row page; the cap is what makes that safe at
    other page sizes rather than three quarters of a narrow one."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec += [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(5)]
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)

    assert _tables(page).count("knowledge") == 3, "the k=10 page seats the measured 3"


# --- it must not starve the page -------------------------------------------------


def test_an_unfilled_reservation_costs_nothing(db, monkeypatch):
    """No curated vec rows at all: the page is the pre-#946 page, row for row."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    _patch_channels(monkeypatch, vec)

    with_reservation = search(db, "q", table="all", limit=10)
    without = search(db, "q", table="all", limit=10, curated_head_slots=0)

    assert [(r["source_table"], r["id"]) for r in with_reservation] == [
        (r["source_table"], r["id"]) for r in without
    ]


def test_a_partly_filled_reservation_spends_the_rest_on_other_rows(db, monkeypatch):
    """One curated row against a reservation of three: the other two slots stay with
    the rows that earned them."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 99, 0.90))
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)

    assert len(page) == 10
    assert _tables(page).count("knowledge") == 1


def test_a_partial_deficit_still_fills_the_reservation(db, monkeypatch):
    """in_head > 0 AND deficit > 0 — the case the scoped pool is sized for.

    The pool asks for `deficit + in_head`, not `deficit`, because it is deduped
    against the curated rows already seated in the head. Sized to the deficit alone
    it would come back short by exactly `in_head` (Temperance, #966). Two curated
    rows win on distance, one more must be seated, and the page must end with three.
    """
    vec = [_row("knowledge", 90, 0.10), _row("knowledge", 91, 0.11)]
    vec += [_row("observations", i, 0.20 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 92, 0.90))
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)

    assert len(page) == 10
    assert _tables(page).count("knowledge") == 3, (
        "a partly-filled reservation must still reach its quota"
    )
    seated = [r for r in page if r.get("reserved_slot") == "curated_head"]
    assert [r["id"] for r in seated] == [92], (
        "only the promoted row is marked reserved; the two that won on distance are not"
    )


def test_a_page_already_carrying_its_quota_is_untouched(db, monkeypatch):
    """Curated rows that won on distance are not re-promoted, and nothing is marked
    reserved — the reservation only ever acts on a deficit."""
    vec = [_row("knowledge", 90 + j, 0.10 + j / 100) for j in range(4)]
    vec += [_row("observations", i, 0.50 + i / 100) for i in range(8)]
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=10)
    baseline = search(db, "q", table="all", limit=10, curated_head_slots=0)

    assert [(r["source_table"], r["id"]) for r in page] == [
        (r["source_table"], r["id"]) for r in baseline
    ]
    assert all("reserved_slot" not in r for r in page)


# --- interaction with the #827 tail floor ----------------------------------------


def test_head_and_tail_reservations_never_exceed_the_page(db, monkeypatch):
    """Both reservations live inside `limit`: the head one displaces within the head
    budget, so the tail floor is unaffected at every page size."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec.append(_row("knowledge", 99, 0.90))
    fts = [_row("threads", 500, 0.0)]  # lexical-only, so it claims the tail floor
    _patch_channels(monkeypatch, vec, fts)

    for limit in (1, 2, 3, 5, 10, 20):
        page = search(db, "q", table="all", limit=limit)
        keys = [(r["source_table"], r["id"]) for r in page]
        assert len(page) <= limit, f"page overflowed at limit={limit}"
        assert len(keys) == len(set(keys)), f"duplicate row at limit={limit}"


def test_the_lexical_floor_still_reaches_the_page_under_a_full_reservation(db, monkeypatch):
    """#827's guarantee survives #946: the reserved lexical row is still delivered,
    and still says which reservation seated it."""
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec += [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(3)]
    fts = [_row("threads", 500, 0.0)]
    _patch_channels(monkeypatch, vec, fts)

    page = search(db, "q", table="all", limit=10)

    tail = [r for r in page if r.get("reserved_slot") == "lexical_tail"]
    assert [(r["source_table"], r["id"]) for r in tail] == [("threads", 500)]
    assert _tables(page).count("knowledge") == 3


@pytest.mark.parametrize("scoped", ["observations", "threads", "knowledge"])
def test_a_single_table_query_is_never_widened(db, monkeypatch, scoped):
    """A caller who narrowed the query gets its table and nothing else.

    The first cut of #946 applied the reservation on every page, so an
    `table="observations"` query came back carrying knowledge rows. The #385 filter
    tasks caught it as a forbidden-key leak (fpr 0.0 -> 0.667 across four tasks);
    this pins the rule that fixed it.
    """
    vec = [_row("observations", i, 0.10 + i / 100) for i in range(12)]
    vec += [_row("threads", 200 + i, 0.20 + i / 100) for i in range(3)]
    vec += [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table=scoped, limit=10, curated_head_slots=3)

    assert set(_tables(page)) <= {scoped}
    assert all("reserved_slot" not in r for r in page)


def test_a_one_row_page_belongs_to_the_vector_channel(db, monkeypatch):
    """Matching `_fts_floor`'s refusal to reserve at limit<=1: a single slot handed to
    a curated row the vector channel ranked last is a different product, not a prior."""
    vec = [_row("observations", 1, 0.10), _row("knowledge", 99, 0.90)]
    _patch_channels(monkeypatch, vec)

    page = search(db, "q", table="all", limit=1)

    assert [(r["source_table"], r["id"]) for r in page] == [("observations", 1)]


# --- the sizing function ----------------------------------------------------------


@pytest.mark.parametrize(
    "head_budget,slots,expected",
    [
        (9, 3, 3),  # the limit=10 page with floor 1 — the measured knee
        (4, 3, 1),  # limit=5: a third of the head, not three quarters of it
        (19, 3, 3),  # a wide page does not scale the count up
        (1, 3, 0),  # no room to reserve
        (0, 3, 0),
        (9, 0, 0),  # operator disabled it
        (9, -1, 0),
        (2, 3, 1),
    ],
)
def test_reservation_sizing(head_budget, slots, expected):
    assert _curated_head_reservation(head_budget, slots) == expected


def test_the_default_is_the_measured_knee():
    assert CURATED_HEAD_SLOTS_DEFAULT == 3
