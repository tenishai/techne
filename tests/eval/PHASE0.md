# Phase-0 Frozen Baseline (#68)

**Frozen:** 2026-06-17, on `feat/68-eval-m2` (techne 0.6.1, post #69/#70/#71/#72).
**Corpus:** `seed_eval_db.py` (synthetic, deterministic). **Tasks:** 6, in `recall_baseline.yaml`.

This is the reference measurement the kill-gate is set against. It does not
change without a documented re-freeze (see EVAL.md → "Changing the baseline").

## Measurement (`pytest -m eval -s`)

| task | category | bare | techne |
|---|---|---|---|
| cabal14-design | decision-recall | 0.0 | 1.0 |
| adapter-recency | activity-recall | 0.0 | 1.0 |
| reflect-candidates | multiword-recall | 0.0 | 1.0 |
| reflect-thread | thread-recall | 0.0 | 1.0 |
| canonical-remote | decision-recall | 0.0 | 1.0 |
| platform-state-delta | fact-recall | 0.0 | 1.0 |
| **aggregate** | | **0.0** | **1.0** |

margin (techne − bare) = **1.0**

## Gate derived from this baseline
- floor: **techne ≥ 0.85** (= 1.0 − 0.15 tolerance)
- margin: **techne − bare ≥ 0.30**

## Honest caveats
- The synthetic corpus is deliberately *findable* — every gold entry is
  retrievable, so a perfect 1.0 is expected here and is **not** evidence the
  real recall layer is perfect. The baseline's job is regression detection, not
  an absolute quality claim.
- All six queries currently route through the #70 OR-fallback (each contains a
  term absent from its gold). That's intentional coverage of the real recall
  path, but it means the baseline is partly a test of that fallback. As harder
  tasks are added, expect the aggregate to drop below 1.0 and the floor to rise.
