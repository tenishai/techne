"""techne#1078 — a query naming a RARE identifier seats its exact rows at the head.

Vec-first (#401) ordered exact-identifier rows by cosine distance, so an exact hit
landed anywhere from rank 2 to off the page. Measured on the 2026-09-28 snapshot:
`emit_max_chars 100000 config.yaml techne` missed at limit 10 and 15, and
`reflect-drain`/`commentblockgate` held only the last (lexical_tail) slot.

Detection and rarity run against a real FTS index. The reservation tests pin both
channels and the rare-key lookup, the way test_search_curated_head.py does: the
claim is about ORDER, and a fixture that fixes the order states it directly.
"""

from __future__ import annotations

import json

import pytest

from techne.core import search as search_mod
from techne.core.database import connect, init_db
from techne.core.search import (
    IDENTIFIER_HEAD_SLOTS_DEFAULT,
    IDENTIFIER_MIN_DF,
    IDENTIFIER_RARE_FRACTION_DEFAULT,
    _identifier_head_reservation,
    _identifier_pool,
    _identifier_terms,
    _is_identifier_shaped,
    _rare_identifier_terms,
    search,
)
from techne.core.state import ensure_session


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p).close()
    return p


# --- detection -------------------------------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "emit_max_chars",
        "emit_max_chars:",  # a config line's trailing colon
        "b2dcdd2",  # hex sha
        "ae219b5",
        "reflect-drain",
        "--go",
        "check_staged_symlinks.sh",
        "techne.db",  # shaped, and rarity (below) is what stops it
        "potion-base-8M",
        "cleanupPeriodDays",  # camelCase
        "maxRetries",  # camelCase too short for the long-token rule
        "commentblockgate",  # a squashed compound, typed in lower case
        "scripts/check_arch_cards.py",
    ],
)
def test_identifier_shapes_are_detected(term):
    assert _is_identifier_shaped(term)


@pytest.mark.parametrize(
    "term",
    ["database", "connection", "the", "100000", "90", "troubleshoot", "--", "-", "Monday."],
)
def test_plain_words_and_bare_numbers_are_not_identifiers(term):
    assert not _is_identifier_shaped(term)


def test_terms_come_from_the_fts_split():
    """Same split `search_fts` quotes into MATCH, so a counted term is a real phrase."""
    assert _identifier_terms("emit_max_chars 100000 config.yaml techne") == [
        "emit_max_chars",
        "config.yaml",
    ]
    assert _identifier_terms("how do we rotate certificates") == []


# --- rarity ----------------------------------------------------------------------


def _obs(db, texts):
    """Insert observations; returns their ids (add_observation returns none)."""
    ensure_session(db, "s-1078")
    conn = connect(db)
    try:
        ids = []
        for t in texts:
            cur = conn.execute(
                "INSERT INTO observations (session_id, content, tool_name) VALUES (?, ?, ?)",
                ("s-1078", t, "Bash"),
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def _rare(db, query, table="all", fraction=IDENTIFIER_RARE_FRACTION_DEFAULT):
    conn = connect(db)
    try:
        return _rare_identifier_terms(conn, query, table, fraction)
    finally:
        conn.close()


def test_a_common_identifier_does_not_trigger_and_a_rare_one_does(db):
    """The issue's must-not case: `techne.db` sits in ~2,900 live rows."""
    _obs(db, [f"sqlite3 techne.db 'select {i}'" for i in range(120)])
    rare_ids = _obs(db, ["set emit_max_chars: 100000", "emit_max_chars was 4000"])

    # 122 rows at 5% is a threshold of 7: techne.db (120) is common, emit (2) rare.
    terms, keys = _rare(db, "emit_max_chars techne.db", fraction=0.05)

    assert terms == ["emit_max_chars"]
    assert keys == {("observation", i) for i in rare_ids}


def test_the_threshold_is_a_fraction_of_the_scope_with_a_floor(db):
    """Rare is fewer than max(IDENTIFIER_MIN_DF, ceil(rows * fraction)) matches."""
    _obs(db, [f"merged b2dcdd2 pass {i}" for i in range(IDENTIFIER_MIN_DF)])
    _obs(db, [f"filler row {i}" for i in range(95)])  # 100 rows in scope

    assert _rare(db, "b2dcdd2", fraction=0.01)[0] == [], "the floor (5) holds"
    assert _rare(db, "b2dcdd2", fraction=0.06)[0] == ["b2dcdd2"], "6 > 5"
    assert _rare(db, "b2dcdd2", fraction=0.05)[0] == [], "ceil(100 * 0.05) = 5"


def test_a_word_like_term_must_clear_a_stricter_bar(db):
    """No digit, no underscore: `well-known` has the shape of `reflect-drain`, so
    rarity has to do more. The bar is the threshold // IDENTIFIER_WORDLIKE_DIVISOR."""
    _obs(db, [f"a well-known fix {i}" for i in range(5)])
    _obs(db, [f"b2dcdd2 merged {i}" for i in range(5)])
    _obs(db, [f"filler row {i}" for i in range(90)])  # 100 rows in scope

    # threshold ceil(100 * 0.08) = 8: b2dcdd2 (5) is rare, well-known (5) is not (bar 4)
    assert _rare(db, "well-known b2dcdd2", fraction=0.08)[0] == ["b2dcdd2"]
    # threshold 12, word-like bar 6: both rare
    assert _rare(db, "well-known b2dcdd2", fraction=0.12)[0] == ["well-known", "b2dcdd2"]


def test_rarity_is_counted_in_the_searched_scope(db):
    """The per-turn hook searches knowledge alone: its threshold comes from the
    knowledge row count, not the whole store's."""
    from techne.core.knowledge import store

    _obs(db, [f"b2dcdd2 log line {i}" for i in range(10)])
    kid = store(db, "Merged as b2dcdd2.", type="fact", source="test")

    assert _rare(db, "b2dcdd2", table="knowledge") == (["b2dcdd2"], {("knowledge", kid)})
    assert _rare(db, "b2dcdd2", table="all")[0] == [], "11 matches, threshold 5"


def test_a_term_matching_nothing_is_not_rare(db):
    _obs(db, ["unrelated"])
    assert _rare(db, "zz_no_such_key") == ([], set())


def test_a_plain_english_query_never_touches_the_store(db, monkeypatch):
    def _boom(*_a, **_kw):
        raise AssertionError("the identifier pool opened the store for plain English")

    monkeypatch.setattr(search_mod, "connect", _boom)
    assert _identifier_pool(db, "how do we rotate certificates", "all", 2, 0.002, []) == []


def test_the_pool_reaches_an_exact_row_the_fts_page_dropped(db):
    """The FTS page is `limit` rows after a recency rerank; a scoped query finds an
    exact row that did not survive it (the #946 pattern)."""
    (rid,) = _obs(db, ["checked emit_max_chars in config"])

    pool = _identifier_pool(db, "emit_max_chars config", "all", 2, 0.002, fts_results=[])

    assert [(r["source_table"], r["id"]) for r in pool] == [("observation", rid)]


def test_the_pool_prefers_the_fts_pages_order(db):
    """Among exact rows, the one matching more of the query leads. BM25 on the rare
    term alone seated `reflect-drain` shell echoes ahead of `reflect-drain --go`."""
    a, b = _obs(
        db,
        [
            "reflect-drain reflect-drain reflect-drain",
            "reflect-drain --go ran after the nightly queue had filled past its usual depth",
        ],
    )
    _obs(db, [f"filler row {i}" for i in range(38)])  # 40 rows: threshold 8, word bar 4
    fts_page = [{"source_table": "observation", "id": b, "content": "x", "rank": -1.0}]

    pool = _identifier_pool(db, "reflect-drain --go", "all", 2, 0.2, fts_results=fts_page)

    assert [r["id"] for r in pool][0] == b
    assert pool[1]["id"] == a, "precondition: BM25 alone puts the echo first"


# --- the reservation -------------------------------------------------------------


def _row(table: str, rid: int, distance: float = 0.5) -> dict:
    return {"source_table": table, "id": rid, "content": f"{table}-{rid}", "distance": distance}


def _scoped(rows):
    def _fn(_db, _query, table="all", limit=20, **_kw):
        return [r for r in rows if table == "all" or table == r["source_table"]][:limit]

    return _fn


def _patch(monkeypatch, vec, fts, rare=()):
    """Pin both channels and the rare-key set. The pool is built the way
    `_identifier_pool` builds it: FTS-page rows holding a rare key, in page order."""
    monkeypatch.setattr(search_mod, "search_vec", _scoped(list(vec)))
    monkeypatch.setattr(search_mod, "search_fts", _scoped(list(fts)))
    rare = set(rare)

    def _pool(_db, _q, _table, _slots, _fraction, fts_results, **_kw):
        return [r for r in fts_results if (r["source_table"], r["id"]) in rare]

    monkeypatch.setattr(search_mod, "_identifier_pool", _pool)


def _keys(page):
    return [(r["source_table"], r["id"]) for r in page]


def _full_vec():
    return [_row("observations", i, 0.10 + i / 100) for i in range(12)]


def test_a_rare_identifier_row_the_vector_channel_missed_leads_the_page(db, monkeypatch):
    """The reported shape: before, this row held only the last slot."""
    fts = [_row("observations", 500), _row("observations", 501)]
    _patch(monkeypatch, _full_vec(), fts, rare={("observations", 500)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    assert len(page) == 10, "the reservation displaces, it never widens the page"
    assert _keys(page)[0] == ("observations", 500)
    assert page[0]["reserved_slot"] == "identifier_head"
    assert page[0]["match_source"] == "fts"


def test_two_exact_rows_both_fit(db, monkeypatch):
    """`emit_max_chars` had two exact rows and only one fitted the lexical tail."""
    fts = [_row("observations", 500), _row("observations", 501), _row("observations", 502)]
    rare = {("observations", 500), ("observations", 501), ("observations", 502)}
    _patch(monkeypatch, _full_vec(), fts, rare=rare)

    page = search(db, "emit_max_chars", table="all", limit=10)

    head = [r for r in page if r.get("reserved_slot") == "identifier_head"]
    assert _keys(head) == [("observations", 500), ("observations", 501)], "FTS order, capped"
    assert _keys(page)[:2] == _keys(head)
    assert len(head) == IDENTIFIER_HEAD_SLOTS_DEFAULT


def test_the_pool_never_offers_a_superseded_row(db):
    """The scoped query goes through `_fts_match_rows`, so a superseded revision
    that still holds the identifier is filtered like on every FTS read."""
    from techne.core.knowledge import store, supersede

    old = store(db, "Merged as b2dcdd2 on Monday.", type="fact", source="test")
    new = supersede(db, old, "Merged as b2dcdd2 on Tuesday.", source="test")

    pool = _identifier_pool(db, "b2dcdd2", "knowledge", 2, 0.002, fts_results=[])

    assert [r["id"] for r in pool] == [new]


def test_a_row_the_vector_channel_ranked_low_moves_up_without_duplicating(db, monkeypatch):
    vec = _full_vec()
    fts = [_row("observations", 7)]
    _patch(monkeypatch, vec, fts, rare={("observations", 7)})

    page = search(db, "b2dcdd2", table="all", limit=10)

    assert _keys(page)[0] == ("observations", 7)
    assert page[0]["match_source"] == "fts+vec"
    assert page[0]["reserved_slot"] == "identifier_head"
    assert _keys(page).count(("observations", 7)) == 1
    assert len(page) == 10


def test_a_row_already_first_is_not_marked_and_the_page_is_unchanged(db, monkeypatch):
    """`reserved_slot` answers "why is this row here"; rank 1 on distance is not a
    reservation."""
    vec = _full_vec()
    fts = [_row("observations", 0), _row("observations", 500)]
    _patch(monkeypatch, vec, fts, rare={("observations", 0)})

    page = search(db, "cleanupPeriodDays", table="all", limit=10)
    baseline = search(db, "cleanupPeriodDays", table="all", limit=10, identifier_head_slots=0)

    assert json.dumps(page, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert "reserved_slot" not in page[0]


def test_the_827_tail_floor_still_gets_its_slot(db, monkeypatch):
    """The identifier rows come out of the head, so the next lexical-only row still
    lands in the reserved tail slot."""
    fts = [_row("observations", 500), _row("observations", 501)]
    _patch(monkeypatch, _full_vec(), fts, rare={("observations", 500)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    assert _keys(page)[-1] == ("observations", 501)
    assert page[-1]["reserved_slot"] == "lexical_tail"


def test_the_946_curated_reservation_still_fills(db, monkeypatch):
    vec = _full_vec() + [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    fts = [_row("observations", 500)]
    _patch(monkeypatch, vec, fts, rare={("observations", 500)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    assert len(page) == 10
    assert _keys(page)[0] == ("observations", 500)
    curated = [r for r in page if r.get("reserved_slot") == "curated_head"]
    assert len(curated) == 2, "a third of the 8 head slots the identifier row left"
    assert len(set(_keys(page))) == 10


def test_the_reservations_do_not_stack_past_a_third_of_what_is_left(db, monkeypatch):
    """The #946 cap is a third of the head the CURATED reservation competes for.
    Sized on the full head it stacked with this one: 2 identifier + 3 curated + 1
    tail, six of ten slots reserved. Sized on the 7 vec slots left, it seats 2."""
    vec = _full_vec() + [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    fts = [_row("observations", 500), _row("observations", 501), _row("observations", 502)]
    _patch(monkeypatch, vec, fts, rare={("observations", 500), ("observations", 501)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    slots = [r.get("reserved_slot") for r in page]
    assert slots.count("identifier_head") == 2
    assert slots.count("curated_head") == 2
    assert slots.count("lexical_tail") == 1
    assert len(page) == 10


def test_a_row_already_at_its_index_does_not_shrink_the_curated_budget(db, monkeypatch):
    """Seating a row where vec-first already put it moves nothing, so the curated
    reservation must see the full head, as it did before #1078."""
    vec = [_row("observations", 0, 0.05)] + _full_vec()[1:]
    vec += [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    # A lexical-only row keeps the #827 floor at 1, so the head is 9 and a third of
    # it is 3; charging the unmoved row would size the reservation on 8 and seat 2.
    fts = [_row("observations", 0), _row("observations", 500)]
    _patch(monkeypatch, vec, fts, rare={("observations", 0)})

    page = search(db, "cleanupPeriodDays", table="all", limit=10)
    baseline = search(db, "cleanupPeriodDays", table="all", limit=10, identifier_head_slots=0)

    assert json.dumps(page, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert [r.get("reserved_slot") for r in page].count("curated_head") == 3


def test_a_rare_knowledge_row_counts_toward_the_curated_quota(db, monkeypatch):
    """A knowledge row the identifier head seats is a curated row on the page, so
    the #946 reservation promotes one fewer (Prudence, #1082: the `in_head` count)."""
    vec = _full_vec() + [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    fts = [_row("knowledge", 500), _row("observations", 501)]
    _patch(monkeypatch, vec, fts, rare={("knowledge", 500)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    assert _keys(page)[0] == ("knowledge", 500)
    slots = [r.get("reserved_slot") for r in page]
    assert slots.count("curated_head") == 1, "quota 2 on the 8-slot head, one already held"
    assert [r["source_table"] for r in page].count("knowledge") == 2
    assert len(page) == 10


def test_a_page_with_no_other_lexical_row_is_refilled_from_the_displaced_vec_row(db, monkeypatch):
    """The identifier row IS the only FTS hit, so the tail is empty. The vec row it
    pushed out of the head must refill the page rather than leave it short."""
    fts = [_row("observations", 500)]
    _patch(monkeypatch, _full_vec(), fts, rare={("observations", 500)})

    page = search(db, "emit_max_chars", table="all", limit=10)

    assert len(page) == 10
    assert _keys(page)[1:] == [("observations", i) for i in range(9)]


def test_the_vector_channel_always_keeps_a_head_slot():
    assert _identifier_head_reservation(head_budget=1, slots=2) == 0
    assert _identifier_head_reservation(head_budget=2, slots=2) == 1
    assert _identifier_head_reservation(head_budget=9, slots=2) == 2
    assert _identifier_head_reservation(head_budget=9, slots=0) == 0


def test_limit_two_seats_no_identifier_row(db, monkeypatch):
    fts = [_row("observations", 500)]
    _patch(monkeypatch, _full_vec(), fts, rare={("observations", 500)})

    page = search(db, "emit_max_chars", table="all", limit=2)

    assert not any(r.get("reserved_slot") == "identifier_head" for r in page)


def test_a_row_both_pool_sources_return_is_seated_once(db, monkeypatch):
    """The real pool lists an exact row twice when it is on the FTS page AND in the
    scoped query, which is the common case. `seen` must seat it once."""
    (rid,) = _obs(db, ["merged as b2dcdd2"])
    vec = [_row("observation", 1000 + i, 0.10 + i / 100) for i in range(12)]
    fts = [_row("observation", rid)]
    monkeypatch.setattr(search_mod, "search_vec", _scoped(vec))
    monkeypatch.setattr(search_mod, "search_fts", _scoped(fts))

    page = search(db, "b2dcdd2", table="all", limit=10)

    assert _keys(page)[0] == ("observation", rid)
    assert len(set(_keys(page))) == len(page) == 10


# --- plain English is untouched --------------------------------------------------


_PLAIN_QUERIES = [
    "how do we rotate certificates",
    "what did we decide about the outbox sweeper",
    "database connection pooling connections per service recycle",
    "When data changes how do we make sure stored copy never shows old information",
]


@pytest.mark.parametrize("query", _PLAIN_QUERIES)
def test_plain_english_pages_are_byte_identical(db, monkeypatch, query):
    """No identifier token: the page is exactly the pre-#1078 composition.

    Every query word is RARE here (two rows hold them), so only the shape test keeps
    the identifier head out. Pinned against the composition itself (nine vec rows,
    then the lexical tail row), not only against `identifier_head_slots=0`, which
    runs the new code too.
    """
    ids = _obs(db, [query, query + " again"])
    vec = [_row("observation", 1000 + i, 0.10 + i / 100) for i in range(12)]
    fts = [_row("observation", i) for i in ids]
    monkeypatch.setattr(search_mod, "search_vec", _scoped(vec))
    monkeypatch.setattr(search_mod, "search_fts", _scoped(fts))

    page = search(db, query, table="all", limit=10)
    off = search(db, query, table="all", limit=10, identifier_head_slots=0)

    assert json.dumps(page, sort_keys=True) == json.dumps(off, sort_keys=True)
    expected = [dict(r, match_source="vec", fusion="vec_first") for r in vec[:9]]
    expected.append(
        dict(fts[0], match_source="fts", fusion="vec_first", reserved_slot="lexical_tail")
    )
    assert json.dumps(page, sort_keys=True) == json.dumps(expected, sort_keys=True)
