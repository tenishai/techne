"""Retrieval-correctness regression gate — techne#385.

Extends the #68/#118 recall-effectiveness harness with a class-tagged task set
(exact / paraphrase / filter / negative) scored by RANK-AWARE metrics
(recall@k, MRR, false-positive-rate — see scoring.py), rather than score_task's
rank-blind recall+phrase-coverage rubric. Same frozen-corpus, no-LLM shape as
recall_baseline.yaml; a separate file so #68/#118 stay byte-for-byte frozen.

Run explicitly (excluded from the default unit suite via addopts):
    pytest -m eval_gate -s

Determinism (matches recall_baseline.yaml's existing convention):
    - use_vectors=False — FTS-only. The vector backend is python-version-
      dependent (model2vec vs fastembed, techne#601/#602), which would make
      scores non-reproducible across environments.
    - half_life_days pinned explicitly — avoids recency-decay drift over
      wall-clock time between runs.
"""

import sys
from pathlib import Path

import pytest
import yaml

from techne.core.search import search

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from scoring import aggregate_gold, false_positive_rate, mrr, recall_at_k  # noqa: E402
from seed_eval_db import seed  # noqa: E402

pytestmark = pytest.mark.eval_gate

_HALF_LIFE_DAYS = 30.0  # pinned — see module docstring


def _load_gold_yaml() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "retrieval_gold.yaml").read_text())
    return doc["tasks"], doc["config"]


def _resolve_ids(keys: list[str], gold: dict[str, str]) -> set[str]:
    return {gold[k] for k in keys}


def test_retrieval_gate(tmp_path, capsys):
    db_path = tmp_path / "eval.db"
    gold = seed(db_path)
    tasks, cfg = _load_gold_yaml()
    k_values = cfg["k_values"]
    fpr_k = max(k_values)  # strictest cut for the leak check (techne#385)

    rows: list[dict] = []
    for task in tasks:
        expected = _resolve_ids(task.get("expected_keys", []), gold)
        forbidden = _resolve_ids(task.get("forbidden_keys", []), gold)

        # use_vectors=False (FTS-only) + pinned half_life_days: deterministic,
        # reproducible across environments (see module docstring).
        retrieved = search(
            db_path,
            task["query"],
            table=task["table"],
            limit=10,
            use_vectors=False,
            half_life_days=_HALF_LIFE_DAYS,
        )
        ranked = [f"{r['source_table']}:{r['id']}" for r in retrieved]

        row = {
            "id": task["id"],
            "class": task["class"],
            # REPORT-ONLY flag: drives the printed `gated` column, NOT the gate.
            # Gating is decided SOLELY by class membership in `config` (below);
            # paraphrase is ungated because there is no `cfg["paraphrase"]`, not
            # because of this flag — so flipping it can never shield a gated
            # (exact/filter/negative) task. The FTS-only path structurally cannot
            # serve zero-token-overlap paraphrase; those tasks are pre-authored
            # targets for the future VECTOR arm.
            "deterministic": task.get("deterministic", True),
            "recall_at_5": round(recall_at_k(expected, ranked, 5), 3),
            "recall_at_10": round(recall_at_k(expected, ranked, 10), 3),
            "mrr": round(mrr(expected, ranked), 3),
            "fpr": round(false_positive_rate(forbidden, ranked, fpr_k), 3),
            "n_results": len(ranked),
        }
        rows.append(row)

    # Per-class means over ALL scored rows (report). Gating reads these but only
    # for the classes with a configured floor (exact/filter/negative).
    per_class = aggregate_gold(rows)

    # --- print per-task + per-class report ---
    with capsys.disabled():
        print(f"\n=== retrieval gate (#385) — k_values={k_values}, fpr_k={fpr_k} ===")
        print(
            f"{'task':<30} {'class':<11} {'gated':>5} {'r@5':>5} "
            f"{'r@10':>5} {'mrr':>5} {'fpr':>5} {'n':>3}"
        )
        for r in rows:
            print(
                f"{r['id']:<30} {r['class']:<11} {('Y' if r['deterministic'] else '-'):>5} "
                f"{r['recall_at_5']:>5} {r['recall_at_10']:>5} {r['mrr']:>5} "
                f"{r['fpr']:>5} {r['n_results']:>3}"
            )
        print("-" * 76)
        print("per-class means (r@5 / r@10 / mrr / fpr):")
        for cls in sorted(per_class):
            m = per_class[cls]
            gated = "GATED" if cls in cfg else "informational"
            print(
                f"  [{cls:<10}] r@5={m['recall_at_5']}  r@10={m['recall_at_10']}  "
                f"mrr={m['mrr']}  fpr={m['fpr']}  (n={m['n']}, {gated})"
            )

    # --- frozen per-class kill-gates (techne#385) ---
    # Asserted SEPARATELY per servable class so a regression in one cannot hide
    # behind another's headroom. paraphrase has NO config entry → never gated
    # (informational only); its scores are printed above for the VECTOR-arm.
    for cls in ("exact", "filter"):
        g = cfg[cls]
        m = per_class[cls]
        assert m["recall_at_5"] >= g["recall_at_5_floor"], (
            f"KILL-GATE [{cls}]: recall@5 {m['recall_at_5']} < floor {g['recall_at_5_floor']}"
        )
        assert m["recall_at_10"] >= g["recall_at_10_floor"], (
            f"KILL-GATE [{cls}]: recall@10 {m['recall_at_10']} < floor {g['recall_at_10_floor']}"
        )
        assert m["mrr"] >= g["mrr_floor"], (
            f"KILL-GATE [{cls}]: MRR {m['mrr']} < floor {g['mrr_floor']}"
        )

    # `negative` is gated on FPR ONLY. Its recall@k / mrr columns are computed
    # and printed but DECORATIVE — leak-prevention is this class's remit; the
    # positive recall of a surviving/new revision is the `exact` class's job.
    neg_ceiling = cfg["negative"]["fpr_ceiling"]
    assert per_class["negative"]["fpr"] <= neg_ceiling, (
        f"KILL-GATE [negative]: FPR {per_class['negative']['fpr']} > ceiling {neg_ceiling}"
    )

    # --- sanity: paraphrase is present but ungated (guards against a future
    #     edit accidentally wiring it into the gate config). ---
    assert "paraphrase" not in cfg, "paraphrase must stay informational-only (not gated)"
    assert any(not r["deterministic"] for r in rows), "paraphrase tasks missing from gold set"

    # --- defense-in-depth: forbidden_keys must never leak for ANY task, not
    #     just the negative class (e.g. a filter task's cross-table distractor
    #     regressing would not move the negative-class FPR at all). Extra
    #     safety net beyond the per-class assertions above.
    leaks = [r["id"] for r in rows if r["fpr"] > 0.0]
    assert not leaks, f"forbidden id(s) leaked into top-{fpr_k} for task(s): {leaks}"
