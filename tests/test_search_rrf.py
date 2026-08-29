"""Tests for the `_rrf_fuse` primitive and the hybrid search() path.

`_rrf_fuse` is retained as a library primitive (the #399 ablation's RRF arm
and #401 candidate fallbacks reference it) so its unit tests below still hold.
The search() path itself moved to a VEC-FIRST UNION in techne#401 — the
integration tests assert that contract (vec channel first, FTS-only appended,
`fusion="vec_first"` provenance, NO `rrf_score`).

The unit RRF tests use plain tuples — no DB needed. The integration
tests patch search_vec to return canned channel results so we can
exercise the fusion path without the vector extras installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.core.search import RRF_K, _fts_floor, _rrf_fuse, search

# ---- _rrf_fuse unit tests --------------------------------------------------


def test_rrf_fuse_empty_channels() -> None:
    assert _rrf_fuse([]) == []


def test_rrf_fuse_all_empty_channels() -> None:
    assert _rrf_fuse([[], [], []]) == []


def test_rrf_fuse_single_channel_preserves_order() -> None:
    channel = [(("k", 1), 1), (("k", 2), 2), (("k", 3), 3)]
    fused = _rrf_fuse([channel])
    assert [key for key, _ in fused] == [("k", 1), ("k", 2), ("k", 3)]


def test_rrf_fuse_overlap_boosts_above_singleton() -> None:
    """An item that appears in BOTH channels at modest ranks must score
    higher than an item appearing in just one channel at rank 1 — this
    is the defining property of RRF.

    With k=60:
      shared (rank 5 in both) = 1/65 + 1/65 ≈ 0.0308
      singleton (rank 1)      = 1/61               ≈ 0.0164
    """
    fts = [(("k", 1), 1), (("k", "shared"), 5)]
    vec = [(("k", 2), 1), (("k", "shared"), 5)]
    fused = _rrf_fuse([fts, vec])
    keys_in_order = [key for key, _ in fused]
    assert keys_in_order[0] == ("k", "shared")


def test_rrf_fuse_top_score_is_rank_one_when_no_overlap() -> None:
    """With no overlap, the highest-ranked item (rank 1) in each channel
    ties for top by score. Sorting is stable in Python, so insertion
    order of the channels decides which comes first."""
    fts = [(("k", "a"), 1), (("k", "b"), 2)]
    vec = [(("k", "c"), 1), (("k", "d"), 2)]
    fused = _rrf_fuse([fts, vec])
    # Top two have the same score (1/61 each); next two share 1/62 each.
    top_keys = {key for key, _ in fused[:2]}
    assert top_keys == {("k", "a"), ("k", "c")}


def test_rrf_fuse_score_formula() -> None:
    """Exact score check against the documented formula."""
    fts = [(("k", "a"), 1), (("k", "shared"), 3)]
    vec = [(("k", "shared"), 2), (("k", "b"), 4)]
    fused_dict = dict(_rrf_fuse([fts, vec]))

    expected_shared = 1.0 / (RRF_K + 3) + 1.0 / (RRF_K + 2)
    expected_a = 1.0 / (RRF_K + 1)
    expected_b = 1.0 / (RRF_K + 4)

    assert fused_dict[("k", "shared")] == pytest.approx(expected_shared)
    assert fused_dict[("k", "a")] == pytest.approx(expected_a)
    assert fused_dict[("k", "b")] == pytest.approx(expected_b)


def test_rrf_fuse_k_parameter_changes_score() -> None:
    """Smaller k makes rank gaps matter more."""
    channel = [(("k", 1), 1), (("k", 2), 2)]
    aggressive = _rrf_fuse([channel], k=1)
    gentle = _rrf_fuse([channel], k=1000)
    # Both preserve ranking but aggressive has bigger score gap.
    aggr_gap = aggressive[0][1] - aggressive[1][1]
    gentle_gap = gentle[0][1] - gentle[1][1]
    assert aggr_gap > gentle_gap


# ---- search() hybrid path --------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "search_rrf_test.db"
    init_db(p).close()
    return p


def test_search_use_vectors_false_returns_no_rrf_score(db_path, monkeypatch) -> None:
    """use_vectors=False is the legacy path — no RRF, no rrf_score field."""
    fake_fts = [
        {"source_table": "knowledge", "id": 1, "content": "hello", "rank": -1.0},
    ]
    monkeypatch.setattr(
        "techne.core.search.search_fts", lambda *a, **kw: fake_fts
    )
    results = search(db_path, "hello", use_vectors=False)
    assert results == fake_fts
    assert "rrf_score" not in results[0]


def test_search_use_vectors_true_but_no_vec_results_falls_back(db_path, monkeypatch) -> None:
    """When vec returns nothing (extras not installed, etc.), falls back
    to FTS-only — preserves legacy behavior."""
    fake_fts = [
        {"source_table": "knowledge", "id": 1, "content": "hello", "rank": -1.0},
    ]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: [])
    results = search(db_path, "hello", use_vectors=True)
    assert results == fake_fts
    assert "rrf_score" not in results[0]


def test_search_hybrid_vec_first_union(db_path, monkeypatch) -> None:
    """Both channels populated → vec-first union (techne#401).

    Order is the vector channel first (by ascending distance), then FTS-only
    hits appended — NOT RRF rank-sum. Every fused row carries
    ``fusion="vec_first"`` and NO ``rrf_score``.
    """
    fake_fts = [
        {"source_table": "knowledge", "id": 1, "content": "a", "rank": -1.0},
        {"source_table": "knowledge", "id": 2, "content": "b", "rank": -0.5},
    ]
    fake_vec = [
        {"source_table": "knowledge", "id": 3, "content": "c", "distance": 0.1},
        {"source_table": "knowledge", "id": 1, "content": "a", "distance": 0.2},
    ]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", use_vectors=True)

    # Vec-first: vec channel leads by distance (id 3 @0.1, then id 1 @0.2),
    # then the FTS-only hit (id 2) is appended.
    assert [r["id"] for r in results] == [3, 1, 2]
    # id=1 is in BOTH channels → provenance only, not a rank boost.
    by_id = {r["id"]: r for r in results}
    assert by_id[1]["match_source"] == "fts+vec"
    assert by_id[3]["match_source"] == "vec"
    assert by_id[2]["match_source"] == "fts"
    assert all(r["fusion"] == "vec_first" for r in results)
    assert all("rrf_score" not in r for r in results)
    # Layer 1 (techne#402 review): the fts+vec overlap row carries the FTS
    # `rank` forward so downstream FTS-aware consumers (federation's
    # merge_and_rank → _rerank) still see it; a pure vec row has only distance.
    assert by_id[1]["rank"] == -1.0
    assert "rank" not in by_id[3]


def test_search_hybrid_marks_match_source_per_channel(db_path, monkeypatch) -> None:
    """match_source reflects which channel(s) contributed."""
    fake_fts = [
        {"source_table": "knowledge", "id": 1, "content": "a", "rank": -1.0},
    ]
    fake_vec = [
        {"source_table": "knowledge", "id": 2, "content": "b", "distance": 0.1},
    ]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", use_vectors=True)
    by_id = {r["id"]: r for r in results}
    assert by_id[1]["match_source"] == "fts"
    assert by_id[2]["match_source"] == "vec"
    assert all(r["fusion"] == "vec_first" for r in results)


def test_search_hybrid_does_not_mutate_caller_dicts(db_path, monkeypatch) -> None:
    """Fusion must copy rows before stamping match_source / fusion so the
    underlying search_fts / search_vec results stay clean for any other
    caller."""
    fake_fts_row = {"source_table": "knowledge", "id": 1, "content": "a", "rank": -1.0}
    fake_vec_row = {"source_table": "knowledge", "id": 2, "content": "b", "distance": 0.1}
    fake_fts = [fake_fts_row]
    fake_vec = [fake_vec_row]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    search(db_path, "anything", use_vectors=True)
    assert "match_source" not in fake_fts_row
    assert "fusion" not in fake_fts_row
    assert "match_source" not in fake_vec_row
    assert "fusion" not in fake_vec_row


def _canned_channels(n: int = 10):
    """Two disjoint full channels: FTS ids 1..n, vec ids 101..1xx."""
    fts = [
        {"source_table": "knowledge", "id": i, "content": str(i), "rank": -float(i)}
        for i in range(1, n + 1)
    ]
    vec = [
        {"source_table": "knowledge", "id": i + 100, "content": str(i + 100), "distance": 0.1 * i}
        for i in range(1, n + 1)
    ]
    return fts, vec


def test_search_hybrid_respects_limit(db_path, monkeypatch) -> None:
    """Limit applies to the fused output when both channels return results.

    UPDATED BY techne#827. This test previously asserted
    ``[r["id"] for r in results] == [101, 102, 103]`` — i.e. that a full vector
    channel takes EVERY slot and the FTS-only hits are truncated away. That was
    the defect written down as the contract, not a property worth keeping: it is
    exactly what made lexical-only recall unreachable in production. The page is
    still vec-first (101, 102 lead); the last slot is now the reserved floor.
    """
    fake_fts, fake_vec = _canned_channels()
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", limit=3, use_vectors=True)
    assert len(results) == 3
    # limit=3 -> floor of 1: two vec rows lead, one FTS-only hit takes the tail.
    assert [r["id"] for r in results] == [101, 102, 1]
    assert [r["match_source"] for r in results] == ["vec", "vec", "fts"]
    # Fused output carries the fusion tag on every row, never rrf_score.
    assert all(r["fusion"] == "vec_first" for r in results)
    assert all("rrf_score" not in r for r in results)


# ---- techne#827: the reserved lexical floor ---------------------------------


@pytest.mark.parametrize(
    "limit,n_fts_only,expected",
    [
        (20, 50, 1),   # DOES NOT SCALE with the page — see the ablation below
        (10, 50, 1),   # techne_recall / techne_search
        (5, 50, 1),    # per-turn recall injection
        (3, 50, 1),
        (2, 50, 1),
        (1, 50, 0),    # no room to reserve: the one slot stays with vec (#401)
        (10, 1, 1),    # never reserve more than there is to place — UNFALSIFIABLE
                       # at floor 1 (any positive count gives 1); kept for the
                       # day the floor widens. See _fts_floor (#829 r2).
        (10, 0, 0),    # nothing lexical-only -> page identical to pre-#827
        (0, 5, 0),
    ],
)
def test_fts_floor_sizing(limit, n_fts_only, expected) -> None:
    """One slot at any usable page size, zero when there is no room to reserve.

    THE SIZE IS PINNED BECAUSE IT WAS MEASURED, not because it is tidy
    (Prudence, #829 r1). The first cut reserved ``limit // 3``; running
    ``pytest -m eval_vec`` — the leg ``pyproject.toml`` deselects from the
    default suite — showed floor 0 and floor 1 both hold the #399/#401
    paraphrase arm at recall@10 1.0 / MRR 0.546, while floor 2 and 3 both drop
    it to 0.833 / 0.528. Widening this without re-running that ablation
    re-introduces a regression the gate's drift margin was hiding.
    """
    assert _fts_floor(limit, n_fts_only) == expected
    # The invariant behind the sizing: the lexical channel never takes MORE than
    # half the page. At limit=2 it takes exactly half — the earlier "never half
    # or more" prose was falsified by this table's own row (#829 r1 nit 4).
    assert _fts_floor(limit, n_fts_only) <= limit // 2


def test_lexical_only_hit_survives_a_full_vec_page(db_path, monkeypatch) -> None:
    """THE techne#827 REGRESSION.

    A vector channel that fills the page used to evict every FTS-only hit at the
    `out[:limit]` truncation. Measured consequence on the live store: 108 of 108
    lexical-only hits dropped across 15 identifier-shaped queries, so a query
    like "PR 815" could not reach the six knowledge entries whose text contains
    it while cosine distance happily returned PR #402 and #439 instead.

    Both channels are FULL and DISJOINT here, which is the shape that used to
    lose everything lexical.
    """
    fake_fts, fake_vec = _canned_channels()
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", limit=10, use_vectors=True)

    assert len(results) == 10
    ids = [r["id"] for r in results]
    fts_only_returned = [i for i in ids if i <= 10]
    assert fts_only_returned, "no FTS-only hit survived the fusion — #827 is back"
    assert len(fts_only_returned) == 1, "limit=10 reserves a floor of exactly 1"
    # Vec keeps NINE of ten: the measured cost of the floor on the paraphrase
    # arm is zero at this size and non-zero at every larger one (#829 r1).
    assert ids[:9] == [101, 102, 103, 104, 105, 106, 107, 108, 109]
    # The floor is filled in search_fts's own order, best BM25/recency first.
    assert fts_only_returned == [1]


def test_floor_does_not_shorten_a_page_when_vec_is_thin(db_path, monkeypatch) -> None:
    """A reserved slot is a floor, not a quota carved out of thin air.

    With only two vector rows the tail spends the slack rather than returning a
    short page — the caller still gets `limit` rows when the union can fill it.
    """
    fake_fts, fake_vec = _canned_channels()
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec[:2])

    results = search(db_path, "anything", limit=10, use_vectors=True)

    assert len(results) == 10
    assert [r["id"] for r in results[:2]] == [101, 102]
    assert [r["id"] for r in results[2:]] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_duplicate_fts_keys_do_not_shorten_the_page(db_path, monkeypatch) -> None:
    """The floor is sized off DISTINCT keys, not rows (#829 r1 nit 3).

    THIS TEST DOES NOT CURRENTLY BITE, AND SAYING SO IS THE POINT (#829 r2
    Important 1). Mutating the caller back to `len(fts_only)` leaves this file
    green, because `_fts_floor` returns `min(n_fts_only, 1)` after establishing
    `n_fts_only > 0` — so a count inflated from 1 to 4 by duplicates gives the
    same 1, and the two sizings are indistinguishable for every possible input
    while the floor is 1. No test written against this head can separate them.

    It is dormant, not dead: with the sizing reverted AND the floor widened to
    3, this fails as designed with an 8-row page. It is kept as a pin for the
    day someone widens the floor, which is the day the distinction matters.
    Read a green run here as "the floor is still 1", not as "the dedup works" —
    #565 is this repo's standing example of a guard that passed nothing and
    blocked nothing while looking healthy.
    """
    dupe = {"source_table": "knowledge", "id": 1, "content": "a", "rank": -1.0}
    fake_fts = [dict(dupe) for _ in range(4)]
    _, fake_vec = _canned_channels()
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", limit=10, use_vectors=True)

    assert len(results) == 10, "duplicate FTS keys shortened the page"
    assert [r["id"] for r in results] == [101, 102, 103, 104, 105, 106, 107, 108, 109, 1]


def test_the_per_turn_rail_cuts_off_the_reserved_slot(db_path, monkeypatch) -> None:
    """THE FLOOR DOES NOT REACH THE SESSION THROUGH THE PER-TURN RAIL (#829 r3).

    Pinned because three docs claimed it did. The rail asks `search()` for
    `per_turn_recall_limit * _RESEND_OVERFETCH` rows — 5 * 3 = 15, over-fetching
    so session-dedup cannot thin the delivery below k (techne#576) — and then
    cuts to k itself at user_prompt_submit.py:656. This floor reserves the
    TAIL, so the lexical row is index 14 of 15 and the rail's own `[:k]` drops
    it before the session sees anything.

    "Usually", not "always": the suppression steps run between the two cuts, so
    a turn dropping 10+ of the 14 head rows would deliver it. The pin is the
    full-vec case — the one #827 is about — where nothing is dropped.

    This is a caller-contract problem, not a fusion one: fixing it means placing
    the reserved row inside the first k, or having the rail stop discarding it.
    Tracked as techne#831. If this test starts failing because the row now lands
    inside the cut, that is the fix arriving, and the assertion should be
    inverted rather than deleted.
    """
    from techne.hooks.user_prompt_submit import _RESEND_OVERFETCH

    k = 5
    page = k * _RESEND_OVERFETCH
    fake_fts, fake_vec = _canned_channels(page)
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    rows = search(db_path, "anything", limit=page, use_vectors=True)

    assert len(rows) == page
    reserved = [i for i, r in enumerate(rows) if r["match_source"] == "fts"]
    assert reserved == [page - 1], "the reserved slot is not at the tail any more"
    assert not [r for r in rows[:k] if r["match_source"] == "fts"], (
        "a lexical row now survives the rail's [:k] cut — if that is deliberate, "
        "techne#831 has been fixed and this pin should be inverted"
    )


def test_no_fts_only_hits_leaves_the_vec_page_untouched(db_path, monkeypatch) -> None:
    """When the channels agree completely the floor is 0 and the page is exactly
    what it was before #827 — the fix is inert on the queries #401 optimised."""
    fake_vec = [
        {"source_table": "knowledge", "id": i + 100, "content": str(i + 100), "distance": 0.1 * i}
        for i in range(1, 11)
    ]
    # Every FTS row is also in the vec channel -> no FTS-only hits to place.
    fake_fts = [
        {"source_table": "knowledge", "id": r["id"], "content": r["content"], "rank": -1.0}
        for r in fake_vec
    ]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", limit=10, use_vectors=True)

    assert [r["id"] for r in results] == [r["id"] for r in fake_vec]
    assert all(r["match_source"] == "fts+vec" for r in results)


def test_small_page_still_reserves_a_slot(db_path, monkeypatch) -> None:
    """A 5-row page still gets its slot: 4 vec + 1 lexical, not 5 + 0.

    NOT "the per-turn rail's page", which is what this docstring claimed until
    #829 r3. `per_turn_recall_limit` IS 5, but the rail multiplies it by
    `_RESEND_OVERFETCH` before calling `search()` — see the test below. This is
    the smallest page the floor must survive rounding on, which is a real
    function-level property and worth its own pin; it is only the justification
    that was wrong.
    """
    fake_fts, fake_vec = _canned_channels()
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", limit=5, use_vectors=True)

    assert [r["match_source"] for r in results] == ["vec", "vec", "vec", "vec", "fts"]
    assert results[-1]["id"] == 1


def test_vec_channel_preserves_knowledge_type_through_fusion(db_path, monkeypatch):
    """THE VEC HALF OF techne#566's REGRESSION PROTECTION.

    `_build_context` renders a row's kind from `knowledge_type`. The tests for
    that fix all fed FTS-shaped rows — while production is VEC-FIRST, so the
    channel that actually leads was unpinned (#566 r1). If fusion ever dropped
    the key on the vec path, every injected line would silently revert to
    `[knowledge]` and the FTS tests would stay green.

    This repo's canned vec rows omit `knowledge_type` entirely, which is a fixture
    lying about the shape `search_vec` really returns (`search.py:682` sets it).
    So this one models the real row and asserts the key survives fusion AND the
    rendering that consumes it.
    """
    from techne.hooks.user_prompt_submit import _build_context

    fake_fts = [
        {"source_table": "knowledge", "id": 1, "knowledge_type": "fact", "content": "a", "rank": -1.0}
    ]
    fake_vec = [
        {
            "source_table": "knowledge",
            "id": 2,
            "knowledge_type": "decision",
            "content": "b",
            "distance": 0.1,
        }
    ]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    results = search(db_path, "anything", use_vectors=True)

    vec_row = next(r for r in results if r["id"] == 2)
    assert vec_row.get("knowledge_type") == "decision", "fusion dropped the vec row's kind"

    rendered = _build_context(results)
    assert "[decision]" in rendered, "the vec-first row rendered as something other than its kind"
    assert "[fact]" in rendered
    assert "[knowledge]" not in rendered
