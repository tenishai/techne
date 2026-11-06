"""Unit tests for run.py's compare-arm helpers (issue #1342). Not eval-marked, and no
vector backend: search() and search_vec() are stubbed, so the default suite checks the
bookkeeping that the compare tables and the displacement report rest on."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("lme_run", Path(__file__).parent / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


def obs(i: int, source: str | None = None) -> dict:
    r = {"source_table": "observation", "id": i}
    if source is not None:
        r["match_source"] = source
    return r


def test_rrf_puts_shared_rows_first_and_tags_provenance() -> None:
    fused = run._rrf([obs(1), obs(2)], [obs(2), obs(3)], limit=10)
    # 2 is in both channels; 1 (fts rank 1) beats 3 (vec rank 2): 1/61 > 1/62.
    assert [r["id"] for r in fused] == [2, 1, 3]
    assert [r["match_source"] for r in fused] == ["fts+vec", "fts", "vec"]


def test_rrf_honours_limit_and_leaves_inputs_untagged() -> None:
    fts, vec = [obs(1), obs(2)], [obs(3)]
    assert len(run._rrf(fts, vec, limit=2)) == 2
    assert all("match_source" not in r for r in [*fts, *vec])


def test_arm_stats_counts_vector_hits_per_arm() -> None:
    rows = {1: ("s1", "s1_0"), 2: ("s1", "s1_1"), 3: ("s2", "s2_0")}
    res = [obs(1, "fts"), obs(2, "fts+vec"), obs(3, "vec"), {"source_table": "knowledge", "id": 9}]
    assert run._arm_stats("hybrid", res, rows, 0.12345) == {
        "n_results": 3,
        "vec_results": 2,
        "search_s": 0.123,
    }
    # search_vec rows carry no match_source, and every one is a vector hit.
    assert run._arm_stats("vec", [obs(1), obs(3)], rows, 0.0)["vec_results"] == 2
    assert run._arm_stats("fts", [obs(1)], rows, 0.0)["vec_results"] == 0


def test_top10_sources_skips_unmapped_rows_and_caps_at_ten() -> None:
    rows = {i: (f"s{i % 2}", f"t{i}") for i in range(20)}
    res = [{"source_table": "knowledge", "id": 0}, obs(99, "vec"), obs(0)]
    res += [obs(i, "vec") for i in range(1, 20)]
    top = run._top10_sources(res, rows, {"s0"})
    assert len(top) == 10
    # A row with no match_source came from FTS; id 0 is in session s0.
    assert top[0] == ["fts", True]
    assert top[1] == ["vec", False]


def _scored(fts_hit: bool, hyb_hit: bool, top10: list[list]) -> dict:
    def m(hit: bool) -> dict:
        return {"turn": {"recall_all@10": 1.0 if hit else 0.0}}

    return {"arms": {"fts": m(fts_hit), "hybrid": m(hyb_hit)}, "hybrid_top10": top10}


def test_displacement_tallies_only_questions_hybrid_lost() -> None:
    out = [
        _scored(True, False, [["vec", True], ["vec", False], ["fts+vec", True]]),
        _scored(True, False, [["vec", False]]),
        _scored(False, True, [["fts", True]]),  # gained: its slots are not tallied
        _scored(True, True, [["fts", False]]),
    ]
    text = run.displacement(out)
    assert "hybrid does not: 2 questions. The reverse: 1." in text
    assert "| vec | 3 | 1 |" in text
    assert "| fts+vec | 1 | 1 |" in text
    assert "| fts |" not in text


def test_compare_gives_each_arm_its_own_stats(tmp_path: Path, monkeypatch) -> None:
    """The round-1 bug: every compare arm reported hybrid's counts and timing."""
    import techne.core.search as s

    item = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "what colour",
        "answer_session_ids": ["a"],
        "haystack_session_ids": ["a", "b"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 03:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "blue", "has_answer": True}],
            [{"role": "user", "content": "red"}, {"role": "assistant", "content": "ok"}],
        ],
    }
    ids: list[int] = []

    def fake_backfill(db_path, allow_download=False):
        import sqlite3

        with sqlite3.connect(db_path) as c:
            ids.extend(i for (i,) in c.execute("SELECT id FROM observations ORDER BY id"))
        return {"observations": len(ids)}

    def fake_search(db_path, q, table, limit, use_vectors):
        if use_vectors:
            return [obs(ids[0], "fts+vec"), obs(ids[1], "vec"), obs(ids[2], "vec")]
        return [obs(ids[1], "fts")]

    monkeypatch.setattr(s, "backfill_vectors", fake_backfill)
    monkeypatch.setattr(s, "search", fake_search)
    monkeypatch.setattr(
        s, "search_vec", lambda db_path, q, table, limit: [obs(ids[2]), obs(ids[0])]
    )

    r = run.run_question(tmp_path, item, "compare", limit=10)
    st = r["arm_stats"]
    assert {a: st[a]["n_results"] for a in st} == {"fts": 1, "vec": 2, "hybrid": 3, "rrf": 3}
    assert {a: st[a]["vec_results"] for a in st} == {"fts": 0, "vec": 2, "hybrid": 3, "rrf": 2}
    fts_vec_s = st["fts"]["search_s"] + st["vec"]["search_s"]
    assert st["rrf"]["search_s"] == pytest.approx(fts_vec_s, abs=0.002)
    # Top-level fields stay hybrid's, which the hybrid guard reads.
    assert r["n_results"] == 3 and r["metrics"] == r["arms"]["hybrid"]
    assert r["arms"]["fts"]["session"]["recall_all@10"] == 0.0
    assert r["arms"]["hybrid"]["session"]["recall_all@10"] == 1.0
