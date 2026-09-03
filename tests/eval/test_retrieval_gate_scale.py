"""Retrieval gate AT PRODUCTION SCALE — techne#883.

Runs the SAME 24-task gold set as the #399 vector gate, through the SAME
`search(..., use_vectors=True)` path, against a corpus padded to production
size. Nothing about the queries, the scoring, or the retrieval code differs.
The only variable is how many plausible neighbours the gold document has to
outrank.

WHY THIS EXISTS. #385/#399 seed 27 documents. The live store carries ~2,740
knowledge rows. Measured on 2026-09-03, same code and the same pinned
model2vec, five paraphrase probes against a read-only copy of the real store:

    corpus                    recall@5   recall@10   MRR
    gate (n=27)                  0.667       1.000   0.546
    live store (n=2,740)         0.200       0.200   0.040

Two of those five probes were real same-day incidents — a stored experiment
(techne#889/#891) and a standing operator directive (techne#2812) that recall
failed to surface, in both cases while the #399 gate was green. A gate that
reports 1.0 for the capability that just failed in production is measuring
something other than the capability.

This is the `docs/arch/liveness.md` defect shape: a state the check cannot
vouch for, rendered as one it can. #399 vouches for paraphrase retrieval; it
has only ever measured paraphrase retrieval in a corpus small enough that
almost any ranking succeeds.

INFORMATIONAL, NOT GATED — deliberately, for now. Floors are a claim about
what the system should sustain, and there is no defensible number yet: the
production figure comes from five probes, and the padded corpus is synthetic
rather than a sample of real content. Publishing a floor derived from either
would be inventing a threshold and then enforcing it. This test's job is to
make the scale delta VISIBLE and re-measurable on one runner; calibrating
floors is #883's follow-up, once the delta has been watched across a few runs.

Run explicitly:
    pytest -m eval_scale -s
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

from techne.core.search import backfill_vectors, search

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from scoring import aggregate_gold, false_positive_rate, mrr, recall_at_k  # noqa: E402
from seed_eval_db import pad_corpus, seed  # noqa: E402

pytestmark = pytest.mark.eval_scale

_HALF_LIFE_DAYS = 30.0  # pinned, matching #385/#399

#: Padding size. 2,000 puts the corpus within an order of magnitude of the live
#: store (~2,740 knowledge rows at the time of writing) without making the
#: backfill slow enough that nobody runs the gate. Overridable so the scale
#: CURVE can be measured — the interesting artefact is not one number but where
#: retrieval falls off, which a single fixed n cannot show.
_PAD_N = int(os.environ.get("TECHNE_EVAL_PAD_N", "2000"))


def _load_gold_yaml() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "retrieval_gold.yaml").read_text())
    return doc["tasks"], doc["config_vec"]


def _resolve_ids(keys: list[str], gold: dict[str, str]) -> set[str]:
    return {gold[k] for k in keys}


def _score(
    db_path: Path, tasks: list[dict], gold: dict[str, str], fpr_k: int
) -> tuple[list[dict], dict]:
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
        rows.append(
            {
                "id": task["id"],
                "class": task["class"],
                "recall_at_5": round(recall_at_k(expected, ranked, 5), 3),
                "recall_at_10": round(recall_at_k(expected, ranked, 10), 3),
                "mrr": round(mrr(expected, ranked), 3),
                "fpr": round(false_positive_rate(forbidden, ranked, fpr_k), 3),
                "n_results": len(ranked),
            }
        )
    return rows, aggregate_gold(rows)


def test_retrieval_gate_at_scale(pinned_model2vec, tmp_path, capsys):
    """Score the gold set twice — unpadded and padded — and report the delta.

    Both arms are built in this one test rather than compared against #399's
    committed baseline on purpose: the numbers must come from ONE runner in ONE
    process, for the reason `test_retrieval_gate_vec._emit_metrics` already
    records — a baseline pinned in the repo carries one machine's BLAS
    tie-ordering to every other machine, and near-ties at the top-k boundary are
    exactly where this class lives.
    """
    tasks, cfg = _load_gold_yaml()
    fpr_k = max(cfg["k_values"])

    small = tmp_path / "eval_scale_small.db"
    small_gold = seed(small)
    backfill_vectors(small)
    small_rows, small_cls = _score(small, tasks, small_gold, fpr_k)

    large = tmp_path / "eval_scale_large.db"
    large_gold = seed(large)
    padded = pad_corpus(large, _PAD_N)
    # Backfill AFTER padding so the vector channel indexes the same corpus the
    # lexical one sees. Note what this ordering does NOT buy, because the
    # obvious claim is wrong and was measured: running backfill BEFORE padding
    # yields byte-identical class scores, even though the pad rows are then
    # genuinely unembedded (`store()` does not embed inline — knowledge_vec
    # stays at its pre-pad count until backfill runs again).
    #
    # That null result is itself a finding, and it narrows the diagnosis in
    # #883: if unembedded distractors degrade paraphrase exactly as much as
    # embedded ones, the degradation is not solely the embedding failing to
    # discriminate — the LEXICAL arm is crowding the merged ranking too. Which
    # channel dominates is unresolved and should not be asserted from these
    # numbers alone; it wants an arm that varies the channels independently.
    backfill_vectors(large)
    large_rows, large_cls = _score(large, tasks, large_gold, fpr_k)

    with capsys.disabled():
        print(f"\n=== retrieval at scale (#883) — padding n={padded} ===")
        print(f"{'class':<12} {'metric':<11} {'n=27':>7} {'n=%d' % (27 + padded):>7} {'delta':>8}")
        print("-" * 50)
        for cls in sorted(small_cls):
            for metric in ("recall_at_5", "recall_at_10", "mrr"):
                a = small_cls[cls].get(metric)
                b = large_cls[cls].get(metric)
                if a is None or b is None:
                    continue
                print(f"{cls:<12} {metric:<11} {a:>7.3f} {b:>7.3f} {b - a:>+8.3f}")

        collapsed = [
            r["id"]
            for r, s in zip(large_rows, small_rows)
            if s["recall_at_10"] > 0 and r["recall_at_10"] == 0
        ]
        print(
            f"\ngolds that fell out of top-10 under padding: "
            f"{', '.join(collapsed) if collapsed else 'none'}"
        )

    # The corpus must actually be large, or this test silently becomes #399
    # again — the one assertion worth making while the metrics stay
    # informational.
    assert padded == _PAD_N
    for cls in ("exact", "paraphrase", "filter"):
        assert cls in large_cls, f"{cls} missing from scored classes"
