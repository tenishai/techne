#!/usr/bin/env python3
"""Diff two retrieval-gate metric dumps and fail on regression — techne#832.

WHY THIS EXISTS. The eval-vec gate's kill-gates are ABSOLUTE floors, and an
absolute floor cannot separate a real regression from cross-machine drift at the
same magnitude. `retrieval_gold.yaml` says so in as many words: the paraphrase
recall@10 floor of 0.8 is chosen so that "a single gold slipping rank 10->11
(r@10 1.0->0.833) still passes", a tolerance reserved for near-tie BLAS
reordering across machines. Commit 275035a spent that tolerance deterministically
-- it took paraphrase r@10 from 1.0 to 0.833 by evicting one gold -- and the gate
went GREEN. A human re-running the leg against the base commit is what caught it.

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
see it at full amplitude, which is how this catches 275036a's shape.
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

    A task or class present in BASE and missing from HEAD is itself reported: a
    gold silently dropping out of the set would otherwise read as "no regression".
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
                    found.append({"scope": scope, "key": key, "metric": metric,
                                  "base": bv, "head": hv, "delta": delta})
    return found


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
