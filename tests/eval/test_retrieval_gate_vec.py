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


def _is_gated(cfg: dict, cls: str) -> bool:
    """Is this class actually ASSERTED, as opposed to merely present in the yaml?

    The report used `cls in cfg`, which is true for any class carrying a config
    block whether or not anything reads it -- so a class with unread floors
    printed as GATED. Enforcement is what this must key on.
    """
    g = cfg.get(cls)
    if not isinstance(g, dict):
        return False
    return ("recall_at_5_floor" in g and "recall_at_10_floor" in g) or "fpr_ceiling" in g


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
        # techne#935: which CHANNEL served each hit, keyed the same way as `ranked`.
        match_sources = {f"{r['source_table']}:{r['id']}": r.get("match_source") for r in retrieved}

        row = {
            "id": task["id"],
            "class": task["class"],
            "recall_at_5": round(recall_at_k(expected, ranked, 5), 3),
            "recall_at_10": round(recall_at_k(expected, ranked, 10), 3),
            "mrr": round(mrr(expected, ranked), 3),
            "fpr": round(false_positive_rate(forbidden, ranked, fpr_k), 3),
            "n_results": len(ranked),
            "gold_match_sources": [match_sources.get(e) for e in expected],
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
            gated = "Y" if _is_gated(cfg, r["class"]) else "-"
            print(
                f"{r['id']:<30} {r['class']:<11} {gated:>5} "
                f"{r['recall_at_5']:>5} {r['recall_at_10']:>5} {r['mrr']:>5} "
                f"{r['fpr']:>5} {r['n_results']:>3}"
            )
        print("-" * 76)
        print("per-class means (r@5 / r@10 / mrr / fpr):")
        for cls in sorted(per_class):
            m = per_class[cls]
            gated = "GATED" if _is_gated(cfg, cls) else "informational"
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
    # DERIVED FROM THE CONFIG, NOT HARDCODED (techne#935 pre-pass). This was a
    # literal tuple of class names, so adding a class WITH floors to the yaml left
    # those floors unread -- dead config that the report nonetheless printed as
    # GATED, because the report's label keys on `cls in cfg` while enforcement
    # keyed on the tuple. Two predicates, one of them decorative.
    #
    # A class is gated iff it declares recall floors. `negative` declares an
    # fpr_ceiling instead and is asserted separately below; `k_values` is not a
    # class.
    gated_classes = sorted(
        c
        for c, g in cfg.items()
        if isinstance(g, dict) and "recall_at_5_floor" in g and "recall_at_10_floor" in g
    )
    assert gated_classes, "no class declares recall floors; the gate would assert nothing"

    # AND PIN THE SET, BECAUSE THE DERIVATION IS SYMMETRIC (Prudence, techne#935).
    # Deriving the gated set from config makes ADDING a gate free -- the win -- and
    # makes REMOVING one SILENT, which is the same failure this change exists to
    # eliminate, pointed the other way. Executed: deleting the `paraphrase:` block
    # from config_vec is a four-line YAML edit; at base it raised KeyError and the
    # gate failed, at head the gate passed GREEN and the report printed
    # "[paraphrase] ... (n=6, informational)". Paraphrase is the class the vec arm
    # exists for. `assert gated_classes` only catches losing EVERY class at once.
    #
    # The repo already carries this shape at test_retrieval_gate.py's
    # `assert "paraphrase" not in cfg` -- a pin on the config's shape guarding the
    # opposite direction.
    _EXPECTED_GATED = frozenset({"exact", "filter", "paraphrase", "lexical"})
    assert set(gated_classes) == _EXPECTED_GATED, (
        f"gated set changed: {sorted(gated_classes)} != {sorted(_EXPECTED_GATED)}. "
        "Adding a class is fine -- update this pin deliberately. Losing one is not."
    )
    for cls in gated_classes:
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

    # --- techne#935: the lexical task must stay LEXICAL -------------------------
    #
    # WITHOUT THIS THE TASK CAN SILENTLY STOP TESTING ANYTHING. Its whole purpose
    # is to be the one positive gold the VECTOR channel does not return, so that
    # it reaches the page only through the reserved lexical tail slot and the
    # fusion tail becomes observable at all. If a model change, a fusion change or
    # a re-worded row makes the vector channel start returning it, recall stays
    # 1.0, every floor above still passes, and the gate quietly goes back to
    # covering nothing -- which is precisely the gap techne#935 documents, with a
    # green tick on top. The channel is therefore asserted directly, not inferred
    # from the score.
    # THE LOOP IS VACUOUS WITHOUT A LEXICAL ROW, so the task's own presence is
    # pinned first (Prudence, techne#935). Deleting the task AND its two config
    # blocks returned the gate to its pre-#935 state, fully green: the coverage
    # this change adds was not itself pinned.
    lexical_rows = [r for r in rows if r["class"] == "lexical"]
    assert lexical_rows, (
        "no lexical task ran. The lexical gold is the ONLY positive task whose "
        "gold the vector channel does not return, so without it this gate cannot "
        "observe the fusion tail at all -- which is the gap techne#935 exists to "
        "close. Removing it must fail here, not pass silently."
    )
    for r in lexical_rows:
        sources = r["gold_match_sources"]
        assert sources and all(src == "fts" for src in sources), (
            f"KILL-GATE [lexical]: {r['id']} gold arrived via {sources!r}, expected "
            "['fts']. This task exists to exercise the LEXICAL TAIL; served by the "
            "vector channel it tests nothing, and the recall floors above cannot "
            "see the difference. Re-derive the dilution band in seed_eval_db.py "
            "(techne#935) rather than relaxing this assertion."
        )

    # `negative` gates FPR — leak-prevention is this class's remit regardless of
    # which channel(s) retrieval used.
    neg_ceiling = cfg["negative"]["fpr_ceiling"]
    assert per_class["negative"]["fpr"] <= neg_ceiling, (
        f"KILL-GATE [negative]: FPR {per_class['negative']['fpr']} > ceiling {neg_ceiling}"
    )

    # ...AND NOW ITS RECALL TOO, DECIDED RATHER THAN OMITTED (techne#935).
    #
    # Three of the six negative tasks carry `expected_keys`: negative-stale-oncall,
    # -redis and -ci each assert that the CURRENT revision surfaces while its
    # superseded predecessor does not. That is a real servable behaviour, and until
    # now a regression in it printed in the report and failed nothing -- half of
    # what those tasks assert was decorative.
    #
    # THE FLOOR IS CALIBRATED TO THE VACUOUS THREE, WHICH IS WHY IT IS NOT 0.85 BY
    # CONVENTION ALONE. The other three tasks have `expected_keys: []`, and
    # `recall_at_k([], ...)` returns 1.0 vacuously -- verified, not assumed -- so
    # they permanently prop the class mean up. One REAL task regressing moves the
    # mean 1.0 -> 0.833, so the floor has to sit above that to bite. 0.85 does, and
    # it is also the house observed-minus-0.15 number, so the two agree here by
    # arithmetic rather than by coincidence being assumed.
    #
    # These golds sit at rank 1, not in the drift-fragile rank 4-10 band the
    # paraphrase floors are loosened for, so a tight floor carries no BLAS-wobble
    # risk.
    neg_cfg = cfg["negative"]
    if "recall_at_10_floor" in neg_cfg:
        assert per_class["negative"]["recall_at_10"] >= neg_cfg["recall_at_10_floor"], (
            f"KILL-GATE [negative]: recall@10 {per_class['negative']['recall_at_10']} < "
            f"floor {neg_cfg['recall_at_10_floor']} — a superseded-revision task stopped "
            "returning the CURRENT revision. FPR alone cannot see this."
        )

    # --- defense-in-depth: forbidden_keys must never leak for ANY task, not
    #     just the negative class (same whole-list net as #385). ---
    leaks = [r["id"] for r in rows if r["fpr"] > 0.0]
    assert not leaks, f"forbidden id(s) leaked into top-{fpr_k} for task(s): {leaks}"
