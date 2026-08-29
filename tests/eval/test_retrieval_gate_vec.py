"""Vector/hybrid retrieval regression gate — techne#399.

Extends the #385 retrieval-correctness gate (test_retrieval_gate.py) with the
SAME class-tagged 24-task gold set, scored the same rank-aware way
(recall_at_k/mrr/false_positive_rate/aggregate_gold — scoring.py), but through
the HYBRID path: `search(..., use_vectors=True)` (BM25 + vector, vec-first
union since techne#401) instead of FTS-only. Reads `retrieval_gold.yaml`'s
`config_vec:` block (NOT `config:` — that stays #385's untouched FTS gate).

The payoff over #385: `paraphrase` — zero literal-token-overlap queries that
the FTS-only path structurally cannot serve — gets a real floor here, because
vec-first hybrid recall is exactly the mechanism meant to serve them.

Run explicitly (excluded from the default unit suite via addopts):
    pytest -m eval_vec -s

Determinism (techne#399, see conftest.py's `pinned_model2vec` fixture):
    - the embedding singleton is reset and rebuilt PINNED to
      model2vec/potion-base-8M before seed()/search()/backfill_vectors() run.
      model2vec is a numpy-only static backend — byte-identical run-to-run,
      unlike AUTO (which can select fastembed on <3.14).
    - half_life_days pinned explicitly, same convention as #385.
    - SKIPS (does not fail) when sqlite_vec/an embedding backend isn't
      installed, or the pinned model isn't in the local HF cache — CI-safe,
      mirrors test_recall_eval_llm.py's resolver-returns-None skip.

Backfill is MANDATORY: seed() only populates the FTS index; the `*_vec`
tables don't exist until backfill_vectors() runs. Skipping it silently
degrades search(use_vectors=True) to FTS-only (search_vec returns [] with no
vec tables to query) and paraphrase scores collapse back to ~0 — the exact
failure mode this gate exists to catch.
"""

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from techne.core.search import backfill_vectors, search

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from scoring import aggregate_gold, false_positive_rate, mrr, recall_at_k  # noqa: E402
from seed_eval_db import seed  # noqa: E402

pytestmark = pytest.mark.eval_vec

_HALF_LIFE_DAYS = 30.0  # pinned — see module docstring, matches #385's convention


def _load_gold_yaml() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "retrieval_gold.yaml").read_text())
    return doc["tasks"], doc["config_vec"]


def _resolve_ids(keys: list[str], gold: dict[str, str]) -> set[str]:
    return {gold[k] for k in keys}


#: techne#832 — where to write machine-readable metrics. Unset means don't.
_METRICS_ENV = "TECHNE_EVAL_METRICS_OUT"


def _emit_metrics(rows: list[dict], per_class: dict) -> None:
    """Write per-task + per-class metrics to $TECHNE_EVAL_METRICS_OUT, if set.

    PER-TASK IS THE HALF THAT MATTERS, and it is why this is not just a nicer
    report. The kill-gates score CLASS MEANS over 6 tasks, so a single gold
    collapsing 1.0 -> 0.0 moves the mean by 0.167 and hides inside a floor sized
    for drift. That is precisely what 275035a did: `paraphrase-dbpool` went to
    zero and the class still read 0.833, over a 0.8 floor. A diff over per-task
    rows sees the collapse at full amplitude.

    Deliberately NOT a committed baseline file. A baseline pinned in the repo
    would carry this machine's numbers to every other machine and re-introduce
    the drift problem the diff exists to avoid — the comparison has to be two
    runs on ONE runner.
    """
    dest = os.environ.get(_METRICS_ENV)
    if not dest:
        return
    payload = {
        "schema": 1,
        "gate": "retrieval_vec",
        "per_task": {r["id"]: r for r in rows},
        "per_class": per_class,
    }
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True))


def test_retrieval_gate_vec(pinned_model2vec, tmp_path, capsys):
    db_path = tmp_path / "eval_vec.db"
    gold = seed(db_path)
    # MANDATORY (see module docstring) — without this, the *_vec tables don't
    # exist and search(use_vectors=True) silently degrades to FTS-only.
    backfill_vectors(db_path)

    tasks, cfg = _load_gold_yaml()
    k_values = cfg["k_values"]
    fpr_k = max(k_values)

    rows: list[dict] = []
    for task in tasks:
        expected = _resolve_ids(task.get("expected_keys", []), gold)
        forbidden = _resolve_ids(task.get("forbidden_keys", []), gold)

        retrieved = search(
            db_path,
            task["query"],
            table=task["table"],
            limit=10,
            use_vectors=True,
            half_life_days=_HALF_LIFE_DAYS,
        )
        ranked = [f"{r['source_table']}:{r['id']}" for r in retrieved]

        row = {
            "id": task["id"],
            "class": task["class"],
            "recall_at_5": round(recall_at_k(expected, ranked, 5), 3),
            "recall_at_10": round(recall_at_k(expected, ranked, 10), 3),
            "mrr": round(mrr(expected, ranked), 3),
            "fpr": round(false_positive_rate(forbidden, ranked, fpr_k), 3),
            "n_results": len(ranked),
        }
        rows.append(row)

    per_class = aggregate_gold(rows)

    # --- print per-task + per-class report (mirrors #385's style) ---
    with capsys.disabled():
        print(f"\n=== VECTOR retrieval gate (#399) — k_values={k_values}, fpr_k={fpr_k} ===")
        print(
            f"{'task':<30} {'class':<11} {'gated':>5} {'r@5':>5} "
            f"{'r@10':>5} {'mrr':>5} {'fpr':>5} {'n':>3}"
        )
        for r in rows:
            gated = "Y" if r["class"] in cfg else "-"
            print(
                f"{r['id']:<30} {r['class']:<11} {gated:>5} "
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

    # --- techne#832: emit the numbers so they can be DIFFED, not just floored ---
    # BEFORE THE KILL-GATES, DELIBERATELY (Prudence, #833 r1 I1). An assert below
    # ends the test, so emitting last means a run that FAILS a floor writes no
    # file — and that is precisely the run whose numbers you want, because the
    # base-vs-head diff is how you learn whether the PR made an already-unhealthy
    # base worse. Emitting first also makes the CI step's "diff anyway" path real
    # instead of unreachable.
    #
    # WHY A DIFF AT ALL: the kill-gates below are absolute, and an absolute floor
    # cannot tell a real regression from cross-machine drift at the same
    # magnitude. Commit 275035a took paraphrase r@10 from 1.0 to 0.833 and this
    # gate passed it green, because retrieval_gold.yaml's 0.8 reserves exactly
    # that much for BLAS wobble. A base-vs-head diff on ONE runner has no such
    # blind spot: the leg is byte-deterministic run-to-run (model2vec/potion is
    # numpy-only static, `pinned_model2vec` rebuilds the singleton), so any
    # movement between two runs on one machine is the CODE. Zero tolerance.
    #
    # Off by default: no env var, no file, behaviour identical to pre-#832.
    _emit_metrics(rows, per_class)

    # --- frozen per-class kill-gates (techne#399, ratcheted #401) ---
    # Unlike #385, paraphrase IS gated here — vec-first hybrid recall is
    # exactly the mechanism meant to serve zero-token-overlap queries.
    for cls in ("exact", "filter", "paraphrase"):
        g = cfg[cls]
        m = per_class[cls]
        assert m["recall_at_5"] >= g["recall_at_5_floor"], (
            f"KILL-GATE [{cls}]: recall@5 {m['recall_at_5']} < floor {g['recall_at_5_floor']}"
        )
        assert m["recall_at_10"] >= g["recall_at_10_floor"], (
            f"KILL-GATE [{cls}]: recall@10 {m['recall_at_10']} < floor {g['recall_at_10_floor']}"
        )
        # MRR floored where meaningful: exact/filter (0.85) and — since #401 moved
        # observed paraphrase MRR from 0.144 to 0.546 — paraphrase too (0.4). The
        # floor stays optional so a class without a meaningful MRR axis can omit it.
        if "mrr_floor" in g:
            assert m["mrr"] >= g["mrr_floor"], (
                f"KILL-GATE [{cls}]: MRR {m['mrr']} < floor {g['mrr_floor']}"
            )

    # `negative` stays gated on FPR ONLY, same as #385 — leak-prevention is
    # this class's remit regardless of which channel(s) retrieval used.
    neg_ceiling = cfg["negative"]["fpr_ceiling"]
    assert per_class["negative"]["fpr"] <= neg_ceiling, (
        f"KILL-GATE [negative]: FPR {per_class['negative']['fpr']} > ceiling {neg_ceiling}"
    )

    # --- defense-in-depth: forbidden_keys must never leak for ANY task, not
    #     just the negative class (same whole-list net as #385). ---
    leaks = [r["id"] for r in rows if r["fpr"] > 0.0]
    assert not leaks, f"forbidden id(s) leaked into top-{fpr_k} for task(s): {leaks}"

