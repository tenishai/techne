#!/usr/bin/env python3
"""Diff two retrieval-gate metric dumps and fail on regression — techne#832.

WHY THIS EXISTS. The eval-vec gate's kill-gates are ABSOLUTE floors, and an
absolute floor cannot separate a real regression from cross-machine drift at the
same magnitude. `retrieval_gold.yaml` says so in as many words: the paraphrase
recall@10 floor of 0.8 is chosen so that "a single gold slipping rank 10->11
(r@10 1.0->0.833) still passes", a tolerance reserved for near-tie BLAS
reordering across machines. A draft of #827's fix (`_fts_floor` sized at
`limit // 3`) spent that tolerance deterministically -- it took paraphrase r@10
from 1.0 to 0.833 by evicting one gold -- and the gate went GREEN. It never
reached `main`: PR #829's own body records the measurement and its review rounds
ablated the draft before merge. The commit that shipped, 33473a4, sizes
`_fts_floor` at 1, which holds r@10 at 1.0.

THIS IS THAT HUMAN, AUTOMATED. It compares two dumps rather than one dump against
a fixed number, which removes the blind spot instead of narrowing it.

ZERO TOLERANCE IS CORRECT HERE, AND IT IS A MEASURED CLAIM, NOT A PREFERENCE.
The leg is byte-deterministic run-to-run on one machine: model2vec/potion-base-8M
is a numpy-only static backend and conftest's `pinned_model2vec` rebuilds the
singleton pinned to it, so two consecutive runs differ only in wall-clock. Verified
2026-08-29 by running it twice and diffing. Therefore any movement between a BASE
run and a HEAD run ON THE SAME RUNNER is the code change, and needs no epsilon.
`--epsilon` exists for the cross-machine case, which is NOT what CI does.

PER-TASK IS WHERE THE TEETH ARE. The kill-gates score class means over six tasks,
so one gold collapsing 1.0 -> 0.0 moves the mean by 0.167 and hides. Per-task rows
see it at full amplitude, which is how this catches that shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Metrics where MORE is better. `fpr` is handled separately — it is a leak rate.
_HIGHER_IS_BETTER = ("recall_at_5", "recall_at_10", "mrr")
_LOWER_IS_BETTER = ("fpr",)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if payload.get("schema") != 1:
        raise SystemExit(f"{path}: unsupported metrics schema {payload.get('schema')!r}")
    return payload


def _regressions(base: dict, head: dict, epsilon: float) -> list[dict]:
    """Every metric that moved the wrong way by more than `epsilon`.

    THIS DIFF COVERS LIBRARY REGRESSIONS ONLY, AND THE MISSING-KEY BRANCH IS
    FORWARD-LOOKING SCAFFOLDING RATHER THAN AN ACTIVE GUARD (Prudence, #833 r1
    I4). CI swaps only `techne/`, so BOTH runs read HEAD's `retrieval_gold.yaml`
    and HEAD's `scoring.py` — the harness must be constant or you measure the
    test instead of the code. The per-task key set is therefore identical by
    construction and the branch below cannot fire under that invocation.

    The blind spot is the price of the constant harness, and it is worth naming:
    the realistic way a gold leaves the set is someone EDITING the yaml, and then
    both dumps lose it together, this diff reports nothing, and the absolute
    floors — reading the same edited file — report nothing either while the class
    mean RISES. Changes under `tests/eval/` are outside this tool's reach and
    need a human. The branch is kept because it is correct if the invocation ever
    changes (a committed baseline, or a base run with its own harness).
    """
    found: list[dict] = []
    for scope in ("per_task", "per_class"):
        b, h = base.get(scope, {}), head.get(scope, {})
        for key in sorted(b):
            if key not in h:
                found.append({"scope": scope, "key": key, "metric": "*", "base": "present",
                              "head": "MISSING", "delta": "-"})
                continue
            for metric in _HIGHER_IS_BETTER + _LOWER_IS_BETTER:
                if metric not in b[key] or metric not in h[key]:
                    continue
                bv, hv = float(b[key][metric]), float(h[key][metric])
                delta = hv - bv
                worse = -delta if metric in _HIGHER_IS_BETTER else delta
                if worse > epsilon:
                    # round: every input is already round(_, 3), so a raw float
                    # delta prints -0.16700000000000004 (#833 r1 N2).
                    found.append({"scope": scope, "key": key, "metric": metric,
                                  "base": bv, "head": hv, "delta": round(delta, 4)})
    return found


def _case_count_drift(base: dict, head: dict) -> list[str]:
    """Classes whose case count differs between the legs — techne#857.

    THE GATE COMPARES TWO NUMBERS AND SAYS NOTHING ABOUT WHAT THEY WERE
    COMPUTED OVER. That was fine while the base leg aborted on any collection
    error, because a leg that measured a short case set produced no dump at
    all. #857 relaxes that deliberately (the base leg runs MAIN's library
    against HEAD's tests, so a PR adding a module makes its test file
    unimportable and killed the whole leg), which means a short base run can
    now reach here and diff cleanly against a full head run.

    A missing class and a class measured over fewer cases both count. Reported
    rather than raised, so main() can print every drifted class at once
    instead of the first.
    """
    drift = []
    for cls, b in sorted(base.get("per_class", {}).items()):
        h = head.get("per_class", {}).get(cls)
        if h is None:
            drift.append(f"{cls}: present in base (n={b.get('n')}), MISSING from head")
        elif b.get("n") != h.get("n"):
            drift.append(f"{cls}: base n={b.get('n')}, head n={h.get('n')}")
    for cls in sorted(head.get("per_class", {})):
        if cls not in base.get("per_class", {}):
            drift.append(f"{cls}: MISSING from base, present in head "
                         f"(n={head['per_class'][cls].get('n')})")
    return drift


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", type=Path, help="metrics JSON from the BASE commit")
    ap.add_argument("head", type=Path, help="metrics JSON from the HEAD commit")
    ap.add_argument(
        "--epsilon", type=float, default=0.0,
        help="tolerance for the wrong direction (default 0.0 — correct for two "
             "runs on ONE runner, where the leg is byte-deterministic)",
    )
    args = ap.parse_args()

    base, head = _load(args.base), _load(args.head)

    # REFUSE TO DIFF UNEQUAL CASE SETS (#857) — before the table, not after, so
    # no number is printed that invites comparison. A short leg is a broken run,
    # not a regression, and saying so is the whole point.
    if drift := _case_count_drift(base, head):
        print("=== CANNOT DIFF: the two legs measured different case sets ===")
        for line in drift:
            print(f"  {line}")
        print(
            "\nOne leg did not measure what the other did, so any delta below "
            "would be an artifact of the case set, not of the code.\n"
            "Most likely the base leg hit a collection error that "
            "--continue-on-collection-errors swallowed; check its pytest output "
            "for ImportError in a file that DOES carry eval_vec cases."
        )
        return 1

    regressions = _regressions(base, head, args.epsilon)

    # ALWAYS REPORT THE CLASS MEANS, pass or fail. Half of #832 is that a
    # 1.0 -> 0.833 move was invisible in review because nothing printed it
    # while the gate stayed green. Visibility is not conditional on failure.
    print("=== retrieval gate: base -> head (per-class means) ===")
    for cls in sorted(base.get("per_class", {})):
        b = base["per_class"][cls]
        h = head.get("per_class", {}).get(cls, {})
        cells = []
        for metric in _HIGHER_IS_BETTER + _LOWER_IS_BETTER:
            if metric not in b:
                continue
            bv, hv = b[metric], h.get(metric, "MISSING")
            mark = "" if bv == hv else "  <-- CHANGED"
            cells.append(f"{metric}={bv}->{hv}{mark}")
        print(f"  [{cls:<10}] " + "  ".join(cells))

    if not regressions:
        print("\nNo regression against the base commit.")
        return 0

    print(f"\n=== REGRESSION vs base: {len(regressions)} metric(s) moved the wrong way ===")
    for r in regressions:
        print(f"  {r['scope']:<9} {r['key']:<30} {r['metric']:<12} "
              f"{r['base']} -> {r['head']}  (delta {r['delta']})")
    print(
        "\nThis compares two runs on the SAME runner, where the leg is "
        "byte-deterministic, so these deltas are the code change and not drift.\n"
        "If a drop is intended, say so in the PR and record the new numbers — "
        "do NOT widen the absolute floor to hide it, which is techne#832's "
        "original defect wearing a different hat."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
