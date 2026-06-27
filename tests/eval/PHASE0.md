# Phase-0 Frozen Baseline (#68, re-frozen #118)

**Re-frozen:** 2026-06-27 for techne#118 (MeME taxonomy + 3-variant cost control).
Original freeze 2026-06-17 (#68 M2). **Corpus:** `seed_eval_db.py` (synthetic,
deterministic). **Stage-1 tasks:** 8, in `recall_baseline.yaml` (exact-recall ×6,
tracking ×1, aggregation ×1). Stage-2 (`absence`/`deletion`/`cascade`) are authored
but skipped — they need the LLM answerer (#118 PR3+).

This is the reference the per-category kill-gates are set against. It does not
change without a documented re-freeze (see EVAL.md → "Changing the baseline").

## Per-category recall (`pytest -m eval -s`)

| category | tasks | bare | techne | floor | margin |
|---|---|---|---|---|---|
| exact-recall | 6 | 0.0 | 1.0 | 0.85 | 0.30 |
| tracking | 1 | 0.0 | 1.0 | 0.85 | 0.30 |
| aggregation | 1 | 0.0 | 1.0 | 0.85 | 0.30 |

Every positive category: `bare = 0` (the task needs memory) and `techne = 1.0`.
Floors = category aggregate − 0.15 tolerance; legacy `exact-recall` keeps the #68
M2 gate byte-for-byte. The `tracking` task (`deploy-target-current`) is the
acceptance test for **#90 supersession** — `techne` returns only the live revision
and excludes the stale ones (checked over the whole result list).

## Cost axis — the "curated beats raw" thesis (gated)

| arm | mean_precision | mean_token_cost |
|---|---|---|
| techne (curated) | 0.426 | 693 |
| raw-source-dump | 0.06 | 2082 |

Curated recall is **~7× more precise** and **~3× cheaper** than dumping the raw
corpus — and on the `tracking` task the raw dump scores **0.0** (it leaks the stale
revisions a curated, supersession-filtered search excludes). Note the precision
ratio **scales with corpus size** (raw-dump precision ≈ gold/total), so on this
deliberately tiny ~21-row corpus the 7× is a *floor* — the gap only widens at real
scale. Gate: curated
precision ≥ `cost_precision_ratio` (3.0) × raw-dump precision, and curated
token_cost < raw-dump. raw-source-dump is deliberately kept OUT of the recall gate
(it recalls everything, so folding it in would mask regressions).

## Honest caveats
- The synthetic corpus is deliberately *findable* — a perfect 1.0 on the positive
  categories is expected and is **not** evidence the real recall layer is perfect.
  The baseline's job is regression detection, not an absolute quality claim. The
  cost axis and the tracking/supersession test are the discriminating signals.
- Most exact-recall queries route through the #70 OR-fallback (each contains a term
  absent from its gold) — intentional coverage of the real recall path.
- `adapter-recency` / `platform-state-delta` are currently `exact-recall`; they read
  semantically like tracking/aggregation and are candidates to reclassify if the
  taxonomy is refined (noted in PR1 review).
