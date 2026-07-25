"""RRF-vs-simple hybrid ranking ablation — techne#399. Report-only, no gate.

Same gold set, same pinned model2vec singleton as test_retrieval_gate_vec.py,
but this file MEASURES rather than gates: it compares two fusion strategies
for combining the FTS and vector channels and prints a per-class
recall@5/recall@10/MRR table. Nothing here blocks a merge — the point is the
number, not a pass/fail line.

Arm A (full, RRF):
    search(db_path, query, table=table, limit=10, use_vectors=True) —
    techne's actual Reciprocal Rank Fusion of the BM25 and vector channels
    (search.py `_rrf_fuse`).

Arm B (simple, Agentlas-style):
    search_vec() (top-k by ascending distance) unioned with search_fts() (any
    lexical hit), vec-first, lex-only rows appended, deduped on (type, id),
    truncated to `limit`. Calls search_vec/search_fts DIRECTLY and skips the
    cross-channel FUSION (no _rrf_fuse, no _mmr_reorder). NOTE: search_fts still
    reranks the FTS channel internally in BOTH arms (its own recency/BM25/decay
    _rerank) — the ONLY difference between the arms is the fusion step. This
    mirrors a "just concatenate the two channels" baseline (no reciprocal-rank
    scoring) rather than techne's tuned RRF fusion.

Expected shape (not asserted — this is what the printed table should show):
paraphrase (zero literal-token overlap) should favor simple, since RRF's rank
smoothing can let a mediocre-but-present FTS rank for a same-topic distractor
outscore a strong single-channel vector hit; exact/filter (both channels
agree, or only one channel can serve at all) should favor RRF or tie, since
fusion's whole purpose is to reward cross-channel agreement.

Run explicitly (excluded from the default unit suite via addopts):
    pytest -m eval_vec -s
"""

import sys
from pathlib import Path

import pytest
import yaml

from techne.core.search import backfill_vectors, search, search_fts, search_vec

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from scoring import aggregate_gold, mrr, recall_at_k  # noqa: E402
from seed_eval_db import seed  # noqa: E402

pytestmark = pytest.mark.eval_vec

_HALF_LIFE_DAYS = 30.0  # pinned — matches #385/#399's convention
_LIMIT = 10


def _load_gold_yaml() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "retrieval_gold.yaml").read_text())
    return doc["tasks"], doc["config_vec"]


def _resolve_ids(keys: list[str], gold: dict[str, str]) -> set[str]:
    return {gold[k] for k in keys}


def _simple_hybrid(db_path: Path, query: str, table: str, limit: int = _LIMIT) -> list[dict]:
    """Arm B: union of search_vec + search_fts, vec-first, no cross-channel fusion.

    vec_results come back already sorted ascending-by-distance (search_vec
    sorts internally before truncating to `limit`), so iterating them in
    order IS "vec-first by ascending distance" — no extra sort needed here.
    Lex-only rows (present in search_fts but not already added from the vec
    pass) are appended in search_fts's own order ("any lexical hit").
    """
    vec_results = search_vec(db_path, query, table=table, limit=limit)
    fts_results = search_fts(
        db_path, query, table=table, limit=limit, half_life_days=_HALF_LIFE_DAYS
    )

    seen: set[tuple] = set()
    merged: list[dict] = []
    for r in vec_results:
        key = (r["type"], r["id"])
        if key not in seen:
            seen.add(key)
            merged.append(r)
    for r in fts_results:
        key = (r["type"], r["id"])
        if key not in seen:
            seen.add(key)
            merged.append(r)
    return merged[:limit]


def test_ranking_ablation_vec(pinned_model2vec, tmp_path, capsys):
    db_path = tmp_path / "eval_ablation_vec.db"
    gold = seed(db_path)
    # MANDATORY — see test_retrieval_gate_vec.py's module docstring: without
    # this, search_vec() has no *_vec tables to query and Arm A/B collapse to
    # FTS-only, silently erasing the comparison this test exists to make.
    backfill_vectors(db_path)

    tasks, _cfg = _load_gold_yaml()

    rrf_rows: list[dict] = []
    simple_rows: list[dict] = []

    for task in tasks:
        expected = _resolve_ids(task.get("expected_keys", []), gold)
        table = task["table"]
        query = task["query"]

        # Arm A: full hybrid, RRF-fused.
        rrf_retrieved = search(
            db_path, query, table=table, limit=_LIMIT,
            use_vectors=True, half_life_days=_HALF_LIFE_DAYS,
        )
        rrf_ranked = [f"{r['type']}:{r['id']}" for r in rrf_retrieved]

        # Arm B: simple union, vec-first, no fusion.
        simple_retrieved = _simple_hybrid(db_path, query, table, limit=_LIMIT)
        simple_ranked = [f"{r['type']}:{r['id']}" for r in simple_retrieved]

        # Soft invariant only — this test measures, it does not gate.
        assert len(rrf_retrieved) <= _LIMIT
        assert len(simple_retrieved) <= _LIMIT

        common = {
            "id": task["id"],
            "class": task["class"],
            # PLACEHOLDER, not a measured leak rate: the ablation does not score
            # FPR and never prints it; this key exists solely because
            # aggregate_gold requires it.
            "fpr": 0.0,
        }
        rrf_rows.append({
            **common,
            "recall_at_5": round(recall_at_k(expected, rrf_ranked, 5), 3),
            "recall_at_10": round(recall_at_k(expected, rrf_ranked, 10), 3),
            "mrr": round(mrr(expected, rrf_ranked), 3),
        })
        simple_rows.append({
            **common,
            "recall_at_5": round(recall_at_k(expected, simple_ranked, 5), 3),
            "recall_at_10": round(recall_at_k(expected, simple_ranked, 10), 3),
            "mrr": round(mrr(expected, simple_ranked), 3),
        })

    rrf_by_class = aggregate_gold(rrf_rows)
    simple_by_class = aggregate_gold(simple_rows)

    with capsys.disabled():
        print("\n=== RRF vs simple-union hybrid ablation (#399) ===")
        print(
            f"{'class':<11} {'r@5 RRF':>8} {'r@5 simp':>9} "
            f"{'r@10 RRF':>9} {'r@10 simp':>10} {'mrr RRF':>8} {'mrr simp':>9}"
        )
        for cls in sorted(rrf_by_class):
            a = rrf_by_class[cls]
            b = simple_by_class[cls]
            print(
                f"{cls:<11} {a['recall_at_5']:>8} {b['recall_at_5']:>9} "
                f"{a['recall_at_10']:>9} {b['recall_at_10']:>10} "
                f"{a['mrr']:>8} {b['mrr']:>9}"
            )
        print("-" * 68)
        print(
            "(RRF = search(use_vectors=True); "
            "simp = search_vec ∪ search_fts, vec-first, no fusion)"
        )
