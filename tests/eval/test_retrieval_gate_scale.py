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

READ `filter`'s +0.000 WITH CARE: `pad_corpus` writes knowledge rows only, and
only 2 of the 6 filter tasks query `table=knowledge`. The other 4 query
observations and threads, which this arm never pads, so they are averaged in
having never been exposed. The two that ARE exposed hold at 1.000 throughout, so
the number is not wrong — but a `filter` floor calibrated off this arm would
rest on two tasks, not six. `exact` (6/6) and `negative` (`table=all`) are fully
exposed (Prudence, PR #884 r3).
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

FIVE ARMS NOW, AND THE FOLLOW-UP (techne#945) SETTLED THE CONFOUND. Each arm
changes exactly one variable against the same gold set and the same code:

    test_retrieval_gate_at_scale      corpus SIZE        (the #883 curve)
    test_channels_at_scale            lexical vs hybrid  (use_vectors)
    test_vocabulary_confound          near vs far words  (the confound)
    test_observations_channel_at_scale  which TABLE crowds
    test_real_content_crowds_harder   synthetic vs REAL  (local only)

MEASURED 2026-09-17, model2vec/potion-base-8M, paraphrase recall@10:

    arm                                    n=2,036   n=4,036
    near vocabulary (the #883 padding)        0.667     0.500
    far vocabulary (disjoint domain)          1.000     1.000
    real rows borrowed from the live store    0.167         —
    FTS-only (any padding)                    0.000         —

READ THE SECOND ROW BEFORE QUOTING THE FIRST. Disjoint-vocabulary padding does
not degrade retrieval AT ALL at either size — 1.000 with 4,000 embedded
distractors in the index (asserted, not assumed: the arm checks both corpora
embed the same number of rows, because padding that is missing from the vector
index would also score 1.000 and look identical here). So at the sizes this
harness can reach, **the #883 curve is measuring semantic adjacency between the
padding and the gold, not corpus size.** Scale alone costs nothing yet.

THAT IS THE ANSWER TO "CALIBRATE THE PARAPHRASE FLOOR", AND THE ANSWER IS NO.
A floor read off the near arm would encode this project's vocabulary as if it
were a property of the retriever; a floor read off the far arm would be 1.000,
i.e. a promise that retrieval never degrades, which the real arm disproves on
the first machine that has a store. The only arm that resembles production
(real rows: 0.167, against 0.200 measured on the live store in #883) is scored
against a corpus that differs per machine and cannot be committed. There is
still no defensible floor, and now there is a measurement saying why rather
than an absence of one.

WHAT THE REAL ARM IMPLIES, since it is the number closest to production: real
rows crowd roughly four times harder than the synthetic near padding at the
same size. Both synthetic arms UNDERSTATE production, so neither should be used
to argue that retrieval is healthy.

INFORMATIONAL, NOT GATED. These arms report; they assert only their own
instruments (padding present, distinct, embedded, and the two channels really
differing). No score is a pass/fail condition anywhere in this file.

Run explicitly:
    pytest -m eval_scale -s
    TECHNE_EVAL_PAD_N=4000 pytest -m eval_scale -s     # the curve
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
from seed_eval_db import (  # noqa: E402
    PAD_OBS_TAG,
    REAL_STORE_ENV,
    pad_corpus,
    pad_from_store,
    pad_observations,
    real_store_path,
    seed,
)

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
    db_path: Path,
    tasks: list[dict],
    gold: dict[str, str],
    fpr_k: int,
    *,
    use_vectors: bool = True,
    table_override: str | None = None,
) -> tuple[list[dict], dict]:
    """Score the gold set.

    `use_vectors=False` runs the SAME tasks through the lexical channel alone
    (techne#945), which is what makes the two channels separable — see
    `test_channels_at_scale`.

    `table_override` forces every task to search one table instead of its declared
    one. It exists for the cross-channel arm: each paraphrase task declares
    `table=knowledge`, so a crowd in `observations` is not merely unhelpful to it,
    it is UNREACHABLE — measured, +0.000 on every metric. Scoring at `table="all"`
    is what makes the two channels compete on one page, and it is also the
    configuration the external 35k-row evaluation used.

    Do not use it on `filter` tasks and read the result as a filter score: that
    class exists to check table scoping, so overriding the table is overriding the
    thing under test. The cross-channel arm prints paraphrase only.
    """
    rows: list[dict] = []
    for task in tasks:
        expected = _resolve_ids(task.get("expected_keys", []), gold)
        forbidden = _resolve_ids(task.get("forbidden_keys", []), gold)
        retrieved = search(
            db_path,
            task["query"],
            table=table_override or task["table"],
            limit=10,
            use_vectors=use_vectors,
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


def _print_delta(title: str, left_label: str, right_label: str, left: dict, right: dict) -> None:
    """Per-class table of two scored arms and their delta."""
    print(f"\n=== {title} ===")
    print(f"{'class':<12} {'metric':<11} {left_label:>9} {right_label:>9} {'delta':>8}")
    print("-" * 54)
    for cls in sorted(left):
        for metric in ("recall_at_5", "recall_at_10", "mrr", "fpr"):
            a = left[cls].get(metric)
            b = right[cls].get(metric)
            if a is None or b is None:
                continue
            print(f"{cls:<12} {metric:<11} {a:>9.3f} {b:>9.3f} {b - a:>+8.3f}")


def _knowledge_pad_counts(db_path: Path) -> tuple[int, int, int]:
    """(pad rows, distinct pad rows, seeded rows) read FROM THE STORE.

    Never from a pad function's return value — the #884 lesson: the old assertion
    compared `pad_corpus`'s own argument to itself and never touched the database.
    """
    conn = connect(db_path)
    try:
        pad_rows, pad_distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT content) FROM knowledge WHERE source='eval-pad'"
        ).fetchone()
        seeded_row = conn.execute("SELECT COUNT(*) FROM knowledge WHERE source!='eval-pad'")
        seeded = seeded_row.fetchone()[0]
    finally:
        conn.close()
    return pad_rows, pad_distinct, seeded


def _observation_pad_count(db_path: Path) -> tuple[int, int]:
    """(pad observation rows, distinct) read from the store, same rule as above."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT content) FROM observations WHERE tags=?",
            (PAD_OBS_TAG,),
        ).fetchone()
    finally:
        conn.close()


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
    embedded = backfill_vectors(large)
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
    finally:
        conn.close()

    with capsys.disabled():
        # n counts KNOWLEDGE rows only. Of the seeded 36, six are superseded and so are
        # never embedded and never retrievable, and the searched corpus also holds
        # observations and threads that the `filter` tasks query — so this is a
        # defensible knowledge-row count, not the size of the corpus the retriever ranks.
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
    #   (a) the padding is REALLY there  (b) it is DISTINCT  (c) it is EMBEDDED
    #
    # A fourth check — probing that a pad row comes back from search() — was
    # written and then REMOVED, because it could not fail on its own. Measured
    # three ways: it passes with nothing embedded, it passes on the lexical
    # channel alone, and it passes with the padding deleted from knowledge_fts.
    # (a) covers the FTS half (store() fires those triggers inline) and (c)
    # covers the vector half, so there is no state where the probe fails and the
    # other two pass. Reachability is a real property; it just is not a separate
    # check (Prudence, PR #884 r3, who proved it by construction).
    assert pad_rows == _PAD_N, f"padding not in the store: {pad_rows} rows, expected {_PAD_N}"
    assert pad_distinct == _PAD_N, (
        f"padding repeats itself: {pad_distinct} distinct of {pad_rows} — the corpus is "
        "smaller than it claims and crowds with copies rather than novel neighbours"
    )
    # (d) EMBEDDED. The reachability probe above passes on the LEXICAL channel alone:
    # `store()` fires the FTS triggers inline, so a pad row is findable the instant it is
    # written, embedded or not. This arm's whole thesis is about the vector channel, so
    # without this assertion all three checks above go green while backfill sits in the
    # wrong place and the arm silently reverts to #399 — the exact failure the comment at
    # the backfill call describes, and one that has already been made once on this branch
    # (Prudence, PR #884 r2, who demonstrated it passing).
    #
    # `>=` not `==`: the count includes the seeded gold rows too. It catches the REORDER,
    # not the double-backfill that broke the round-1 experiment; a knowledge_vec rowid
    # check would catch both, but `connect()` does not load sqlite_vec, so the return
    # value is the practical seam.
    if _PAD_N:
        assert embedded.get("knowledge", 0) >= _PAD_N, (
            f"backfill embedded {embedded.get('knowledge')} knowledge rows, expected >= {_PAD_N} "
            "— the padding is in the store but not in the vector index, so this arm is "
            "measuring the FTS channel on a corpus the retriever ranks as if it were small"
        )
    for cls in ("exact", "paraphrase", "filter", "negative"):
        assert cls in large_cls, f"{cls} missing from scored classes"


def test_channels_at_scale(pinned_model2vec, tmp_path, capsys):
    """Score the padded corpus through BOTH channels — techne#945.

    #883 closed asking for this and called it cheap: the hybrid arm above cannot
    say WHICH channel the degradation lives in, because it only ever runs with
    `use_vectors=True`. Running the same tasks over the same store with
    `use_vectors=False` isolates the lexical half, and the difference between the
    two columns is what the vector channel is contributing at this corpus size.

    READ `paraphrase` UNDER FTS-ONLY AS A FLOOR, NOT A FAILURE. Paraphrase queries
    share zero post-porter-stem tokens with their gold by construction
    (`retrieval_gold.yaml` says so and gates the FTS arm accordingly), so the
    lexical channel structurally cannot serve them. A 0.000 there is the design,
    and it is the number the hybrid column has to beat to justify the vector
    channel's existence at all.

    INFORMATIONAL. No floors — see the module docstring.
    """
    tasks, cfg = _load_gold_yaml()
    fpr_k = max(cfg["k_values"])

    db = tmp_path / "eval_scale_channels.db"
    gold = seed(db)
    pad_corpus(db, _PAD_N)
    backfill_vectors(db)

    _, hybrid_cls = _score(db, tasks, gold, fpr_k, use_vectors=True)
    _, lexical_cls = _score(db, tasks, gold, fpr_k, use_vectors=False)

    pad_rows, _, seeded = _knowledge_pad_counts(db)
    with capsys.disabled():
        _print_delta(
            f"channels at n={seeded + pad_rows} (#945) — what the vector channel adds",
            "fts-only",
            "hybrid",
            lexical_cls,
            hybrid_cls,
        )

    # INSTRUMENT, NOT RESULT. The one thing that must hold is that the two columns
    # were actually produced by different code paths; a `use_vectors` flag that
    # silently no-ops would print two identical columns and read as "the vector
    # channel adds nothing", which is a conclusion this arm exists to test, not
    # to assume. `exact` is served by both channels, so it is not the discriminator
    # — `paraphrase` is, and FTS cannot serve it by construction.
    assert lexical_cls["paraphrase"]["recall_at_10"] == 0.0, (
        "FTS-only scored paraphrase above zero — either the gold set stopped being "
        "token-disjoint or use_vectors=False is not reaching the lexical path"
    )
    assert hybrid_cls["paraphrase"]["recall_at_10"] > lexical_cls["paraphrase"]["recall_at_10"], (
        "the hybrid channel did not beat FTS-only on paraphrase at this scale — if this "
        "fires, the vector channel has stopped contributing and that is the finding"
    )


def test_vocabulary_confound(pinned_model2vec, tmp_path, capsys):
    """Separate 'more neighbours' from 'neighbours that look like the gold' — techne#945.

    #883's floor calibration was blocked on exactly this. Its padding is drawn
    from this project's vocabulary, which crowds realistically AND sits next to
    the gold docs semantically, so its curve is a vocabulary-and-scale figure.
    Prudence measured a disjoint-vocabulary generator once, by hand, and found it
    recovered much of the loss — but that measurement lived in a review comment,
    not in the harness, so nothing re-checks it.

    Both arms here are the same size, same shape, same index arithmetic, same
    channel; only the words differ (`_PAD_VOCABULARIES`). So:

        far   ≈ the cost of SCALE alone
        near  = scale + semantic adjacency
        gap   = what the vocabulary choice contributes

    A paraphrase floor calibrated off `near` alone would be calibrated against
    the template choice by however much that gap is worth, which is why this
    arm has to exist before any floor does.

    INFORMATIONAL. No floors.
    """
    tasks, cfg = _load_gold_yaml()
    fpr_k = max(cfg["k_values"])

    near_db = tmp_path / "eval_scale_near.db"
    near_gold = seed(near_db)
    pad_corpus(near_db, _PAD_N, vocabulary="near")
    near_embedded = backfill_vectors(near_db)
    _, near_cls = _score(near_db, tasks, near_gold, fpr_k)

    far_db = tmp_path / "eval_scale_far.db"
    far_gold = seed(far_db)
    pad_corpus(far_db, _PAD_N, vocabulary="far")
    far_embedded = backfill_vectors(far_db)
    _, far_cls = _score(far_db, tasks, far_gold, fpr_k)

    near_rows, near_distinct, seeded = _knowledge_pad_counts(near_db)
    far_rows, far_distinct, _ = _knowledge_pad_counts(far_db)
    with capsys.disabled():
        _print_delta(
            f"vocabulary confound at n={seeded + near_rows} (#945) — "
            f"far = scale alone, near = scale + adjacency",
            "far",
            "near",
            far_cls,
            near_cls,
        )
        gap = far_cls["paraphrase"]["recall_at_10"] - near_cls["paraphrase"]["recall_at_10"]
        print(
            f"\nparaphrase recall@10: far {far_cls['paraphrase']['recall_at_10']:.3f} vs "
            f"near {near_cls['paraphrase']['recall_at_10']:.3f} — vocabulary accounts for "
            f"{gap:+.3f} of the drop at this size"
        )

    # Instrument checks only: both corpora are really there and really distinct,
    # so a gap can never be an artifact of one arm being smaller or repetitive.
    assert near_rows == far_rows == _PAD_N, f"arms differ in size: {near_rows} vs {far_rows}"
    assert near_distinct == near_rows, "near padding repeats itself"
    assert far_distinct == far_rows, "far padding repeats itself"
    # AND BOTH MUST BE IN THE VECTOR INDEX. Without this, "far recovers the loss" has a
    # second explanation that looks identical in the output: far padding that was never
    # embedded cannot crowd at all, so it would score 1.000 by being ABSENT rather than by
    # being distant. Verified equal at the time of writing — both arms embed 2,030
    # knowledge rows — which is what makes the near/far gap attributable to the words.
    if _PAD_N:
        assert near_embedded.get("knowledge", 0) == far_embedded.get("knowledge", 0), (
            f"arms embedded different row counts (near {near_embedded.get('knowledge')}, "
            f"far {far_embedded.get('knowledge')}) — the gap cannot be read as vocabulary"
        )
        assert far_embedded.get("knowledge", 0) >= _PAD_N, (
            f"far padding is in the store but not the vector index "
            f"({far_embedded.get('knowledge')} embedded) — it would score 1.000 by being "
            "invisible to the retriever, not by being semantically distant"
        )


def test_observations_channel_at_scale(pinned_model2vec, tmp_path, capsys):
    """Crowd the OBSERVATIONS table instead of knowledge — techne#945.

    The #883 arm pads knowledge only. That leaves two gaps this closes. The gold
    set's `filter` tasks that query observations and threads were scored while
    never having been crowded, which the headline arm's own docstring flags as a
    caveat on any `filter` floor. And an external evaluation that measured 50%
    paraphrase recall at ~35,000 rows did it on a store with ZERO knowledge rows,
    entirely on this channel — a number the synthetic arm had no same-channel
    comparison for.

    Text is identical to the knowledge arm at every index (both call `_pad_text`),
    so the table is the only variable.

    SCORED AT `table="all"`, AND THAT IS LOAD-BEARING. Every paraphrase task
    declares `table=knowledge`, so a crowd living in `observations` is unreachable
    to it and the arm measures nothing — MEASURED before this override existed:
    +0.000 on every class, every metric, at 2,000 padded observation rows. At
    `table="all"` the two channels compete for one ten-row page, which is both the
    honest test of a cross-channel crowd and the configuration the external
    evaluation ran (`search(table="all", limit=10, use_vectors=True)`).

    Only `paraphrase` is reported. `filter` exists to test table scoping and an
    override is exactly what it is checking for, so scoring it here would be
    measuring the override rather than the crowd.

    INFORMATIONAL. No floors.
    """
    tasks, cfg = _load_gold_yaml()
    fpr_k = max(cfg["k_values"])
    paraphrase = [t for t in tasks if t["class"] == "paraphrase"]

    base = tmp_path / "eval_scale_obs_base.db"
    base_gold = seed(base)
    backfill_vectors(base)
    _, base_cls = _score(base, paraphrase, base_gold, fpr_k, table_override="all")

    obs = tmp_path / "eval_scale_obs.db"
    obs_gold = seed(obs)
    pad_observations(obs, _PAD_N)
    embedded = backfill_vectors(obs)
    _, obs_cls = _score(obs, paraphrase, obs_gold, fpr_k, table_override="all")

    pad_rows, pad_distinct = _observation_pad_count(obs)
    with capsys.disabled():
        _print_delta(
            f'observations crowd vs knowledge golds at table="all": '
            f"+{pad_rows} observation rows (#945)",
            "unpadded",
            "padded",
            base_cls,
            obs_cls,
        )

    assert pad_rows == _PAD_N, f"observation padding not in the store: {pad_rows}"
    assert pad_distinct == _PAD_N, "observation padding repeats itself"
    # Same reasoning as the knowledge arm's embedded check: without this the arm
    # silently measures FTS on a corpus the vector channel ranks as if it were small.
    if _PAD_N:
        assert embedded.get("observations", 0) >= _PAD_N, (
            f"backfill embedded {embedded.get('observations')} observations, "
            f"expected >= {_PAD_N} — the padding is in the store but not in the vector index"
        )


def test_real_content_crowds_harder(pinned_model2vec, tmp_path, capsys):
    """Synthetic padding vs REAL rows at the same size — techne#945.

    THE NUMBER THIS ARM EXISTS TO PRODUCE. #883 refused to set a paraphrase floor
    partly because every synthetic point was better than the live store (0.500 at
    n=4,000 synthetic vs 0.200 measured on 2,740 real rows), so a floor derived
    from templates would be calibrated above what production sustains. That
    comparison was made by hand, once, against a store nobody else has. This runs
    it in the harness, on whatever store the machine has.

    LOCAL ONLY, AND IT MUST SKIP CLEANLY. CI has no techne store, and the store is
    the operator's private memory: real rows enter a `tmp_path` database as
    DISTRACTORS and are never committed, never queried against, and never printed
    in full. The queries and the gold stay the committed set, so what this arm
    reports is a score, not content.

    INFORMATIONAL. No floors, and this one could not be gated even in principle —
    it scores against a corpus that differs per machine.
    """
    source = real_store_path()
    if source is None:
        pytest.skip(
            f"no techne store to borrow real distractors from (set {REAL_STORE_ENV}) — "
            "techne#945's real-content arm is local-only by design"
        )

    tasks, cfg = _load_gold_yaml()
    fpr_k = max(cfg["k_values"])

    synth = tmp_path / "eval_scale_synth.db"
    synth_gold = seed(synth)
    pad_corpus(synth, _PAD_N)
    backfill_vectors(synth)
    _, synth_cls = _score(synth, tasks, synth_gold, fpr_k)

    real = tmp_path / "eval_scale_real.db"
    real_gold = seed(real)
    borrowed = pad_from_store(real, _PAD_N, source, table="knowledge")
    if borrowed < _PAD_N:
        pytest.skip(
            f"store holds only {borrowed} usable knowledge rows, need {_PAD_N} for a "
            "same-size comparison — a smaller crowd would confound size with content"
        )
    backfill_vectors(real)
    _, real_cls = _score(real, tasks, real_gold, fpr_k)

    pad_rows, _, seeded = _knowledge_pad_counts(real)
    with capsys.disabled():
        _print_delta(
            f"real vs synthetic crowd at n={seeded + pad_rows} (#945)",
            "synthetic",
            "real",
            synth_cls,
            real_cls,
        )
        print(
            f"\nparaphrase recall@10: synthetic {synth_cls['paraphrase']['recall_at_10']:.3f} "
            f"vs real {real_cls['paraphrase']['recall_at_10']:.3f} — if real is lower, the "
            "synthetic arms understate production and no floor should be read off them"
        )

    assert pad_rows == borrowed, f"real padding not in the store: {pad_rows} vs {borrowed}"


def test_pad_vocabularies_differ_in_overlap():
    """Pin the disjointness the confound arm rests on — techne#945.

    `test_vocabulary_confound` is only meaningful if `far` really is disjoint from
    the gold. That is a property of two word lists, and word lists get edited, so
    it is asserted rather than described. Content-word overlap against the gold
    QUERIES — the corpus measured below, and what a distractor competes against —
    at the time of writing: near 24, far 0. The assertions do not hardcode 24:
    they pin `far == 0` and `near > far`, so re-measuring is the way to refresh
    the number rather than editing it to match.

    Needs no database, no model and no store.
    """
    import re

    from seed_eval_db import _PAD_VOCABULARIES

    tasks, _ = _load_gold_yaml()
    gold_text = " ".join(t["query"] for t in tasks)
    # The gold DOCUMENTS live in seed(), but their distinctive vocabulary is
    # reproduced in the queries that must retrieve them, which is what a
    # distractor competes against. Queries alone keep this test store-free.
    stop = set(
        "the a an and or of to in on at for by with from is was were be been are it its "
        "this that as we our do does not no before after each per how many can make".split()
    )

    def toks(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in stop and len(w) > 2}

    target = toks(gold_text)
    overlap = {
        name: len(toks(" ".join(" ".join(part) for part in lists)) & target)
        for name, lists in _PAD_VOCABULARIES.items()
    }

    far_shared = sorted(toks(" ".join(" ".join(p) for p in _PAD_VOCABULARIES["far"])) & target)
    assert overlap["far"] == 0, (
        f"the 'far' padding vocabulary shares {overlap['far']} content words with the gold "
        f"queries ({far_shared}) — it is no longer disjoint, so test_vocabulary_confound's "
        "near/far gap can no longer be read as the vocabulary contribution"
    )
    assert overlap["near"] > overlap["far"], (
        "the 'near' vocabulary is supposed to be the ADJACENT one; if it stops overlapping "
        "the gold, the confound arm is comparing two disjoint corpora and measures nothing"
    )
