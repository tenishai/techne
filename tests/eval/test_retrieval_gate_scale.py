"""Retrieval gate AT PRODUCTION SCALE — techne#883.

Runs the SAME 24-task gold set as the #399 vector gate, through the SAME
`search(..., use_vectors=True)` path, against a corpus padded to production
size. Nothing about the queries, the scoring, or the retrieval code differs.
The only variable is how many plausible neighbours the gold document has to
outrank.

WHY THIS EXISTS. #385/#399 seed 36 knowledge rows. The live store carries
~2,740. Measured 2026-09-03, same code and the same pinned model2vec, five
paraphrase probes against a read-only copy of the real store:

    corpus                    recall@5   recall@10   MRR
    gate (n=36)                  0.667       1.000   0.546
    live store (n=2,740)         0.200       0.200   0.040

WHAT THIS ARM MEASURES, AND WHAT IT DOES NOT — read this before quoting a
number from it. Padding with distinct rows (deltas from the n=36 baseline):

    pad n     recall@5   recall@10     MRR
        0        0.667       1.000   0.546   (control)
      250        0.667       0.833   0.519
    1,000        0.667       0.667   0.500
    2,000        0.667       0.667   0.500
    4,000        0.333       0.500   0.357

The degradation is real, monotonic, and confined to `paraphrase` —
exact/filter/negative hold at 1.000/1.000/1.000 with fpr 0.000 at every size.
But it is SMALLER than this arm first reported, and the difference is
instructive twice over:

  - An earlier revision's generator repeated itself from row 1,452, so the
    "2,000-row" corpus was 548 duplicates and n=4,000 was 2,548 of 4,000. That
    inflated the apparent damage at n=2,000 by roughly 2x (Prudence, PR #884).
  - The distractor vocabulary is drawn from this project, which makes the
    padding a realistic crowd AND a confound. Prudence measured a same-shape
    generator over a disjoint domain at the same size and recovered much of the
    loss, so a large share of what remains is semantic adjacency between the
    templates and the gold, not corpus size alone.

So this number is a vocabulary-and-scale figure, not a pure scale figure, and
#883's floor calibration must not treat it as the latter. The live store is
still worse than any synthetic point here (0.200 vs 0.500 at n=4,000), which is
the honest headline: real content crowds harder than templates do, and the arm
UNDERSTATES production.

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

from techne.core.database import connect
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
    # Return value deliberately discarded: it is pad_corpus's own argument handed
    # back, which is what made the old assertion a tautology. The row counts below
    # come from the store.
    pad_corpus(large, _PAD_N)
    # MANDATORY, AND AFTER PADDING. `store()` does not embed inline, so backfill
    # embeds exactly what exists when it runs: order it first and the pad rows
    # stay unembedded, invisible to the vector channel, and this arm silently
    # becomes #399 again — measured, every class delta +0.000, "golds that fell
    # out of top-10: none".
    #
    # That is not a hypothetical. An earlier revision of this file DELETED this
    # comment on the strength of a broken experiment: the check ADDED a backfill
    # before the padding while leaving this one in place, so backfill ran twice,
    # the scores were identical for a trivial reason, and the null result was
    # read as evidence that the lexical arm dominates. Both the deletion and the
    # conclusion were wrong (Prudence, PR #884). The lesson is narrower than
    # "be careful": when a check is supposed to REMOVE a step, assert that the
    # step is gone, because adding its opposite leaves the original running.
    backfill_vectors(large)
    large_rows, large_cls = _score(large, tasks, large_gold, fpr_k)

    # Read the corpus from the STORE, never from pad_corpus's return value — the
    # header must state a size it observed (Critical 1, #884).
    conn = connect(large)
    try:
        pad_rows, pad_distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT content) FROM knowledge WHERE source='eval-pad'"
        ).fetchone()
        small_n = conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE source!='eval-pad'"
        ).fetchone()[0]
        pad_ids = {r[0] for r in conn.execute("SELECT id FROM knowledge WHERE source='eval-pad'")}
    finally:
        conn.close()

    with capsys.disabled():
        print(f"\n=== retrieval at scale (#883) — {pad_rows} pad rows, {pad_distinct} distinct ===")
        print(
            f"{'class':<12} {'metric':<11} {'n=%d' % small_n:>7} "
            f"{'n=%d' % (small_n + pad_rows):>7} {'delta':>8}"
        )
        print("-" * 50)
        for cls in sorted(small_cls):
            for metric in ("recall_at_5", "recall_at_10", "mrr", "fpr"):
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

    # INFORMATIONAL MEANS ASSERT THE INSTRUMENT, NOT THE RESULT (Prudence, #884).
    # No floors — a threshold derived from five probes and synthetic padding
    # would be invented and then enforced. But the instrument itself must be
    # checked, and the previous `assert padded == _PAD_N` could not fail:
    # `pad_corpus` returns its own argument, so it compared _PAD_N to _PAD_N and
    # never touched the database. With the loop stubbed to insert zero rows the
    # test still passed green, printing "padding n=2000" over a 36-row corpus —
    # the #883 defect shape reproduced inside the fix for #883.
    #
    # These three check the instrument against the store, never the score:
    #   (a) the padding is REALLY there  (b) it is DISTINCT  (c) it is REACHABLE
    #       by the same search path the gold is scored through — padding the
    #       retriever cannot see makes the corpus look large and score small,
    #       and fails identically to everything working.
    assert pad_rows == _PAD_N, f"padding not in the store: {pad_rows} rows, expected {_PAD_N}"
    assert pad_distinct == _PAD_N, (
        f"padding repeats itself: {pad_distinct} distinct of {pad_rows} — the corpus is "
        "smaller than it claims and crowds with copies rather than novel neighbours"
    )
    if _PAD_N:
        probe = search(large, "the outbox sweeper silently skipped", table="knowledge", limit=10)
        assert any(r["id"] in pad_ids for r in probe), (
            "no eval-pad row is reachable through search() — the padding is in the store but "
            "invisible to the retriever, so the corpus is large and the scoring is not"
        )
    for cls in ("exact", "paraphrase", "filter", "negative"):
        assert cls in large_cls, f"{cls} missing from scored classes"
