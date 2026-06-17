# Recall Eval — Methodology & Kill-Gate (#68)

The contract. Read before changing anything that touches retrieval.

## Run
```
pytest -m eval -s
```
Excluded from the default unit suite (`addopts = -m 'not eval'`). Prints a
per-(task, arm) table + per-arm aggregate and writes JSONL to `results/`.

## Arms
- **`bare`** — no techne retrieval (empty). Control: proves each task genuinely
  needs memory. If `bare` ever scores > 0, the task is answerable without
  memory and must be tightened.
- **`techne`** — full `search()` recall (FTS, `use_vectors=False`).

## Scoring
Per task: `0.7 * evidence_recall@K + 0.3 * phrase_coverage`, zeroed if a
`must_exclude_phrases` entry appears in the top result. Aggregate = weighted
mean across tasks. Deterministic, no LLM (see `docs/eval.md` for why).

## Kill-gate (set 2026-06-17, M2)
A change to retrieval **does not merge** unless, on the frozen baseline:
- **`techne` aggregate ≥ 0.85** (the floor), **and**
- **`techne` beats `bare` by ≥ 0.30** (memory must demonstrably help).

Both are enforced as assertions in `test_recall_eval.py`, so `pytest -m eval`
fails below the gate.

### Rationale for the numbers
- Frozen Phase-0 aggregate is **1.0** (see `PHASE0.md`). The **0.85** floor =
  baseline − 0.15 tolerance: catches a real regression (≈ one of six tasks
  collapsing drops the aggregate to ~0.83, below the gate) without flapping on
  noise. **Tighten the floor toward 0.9+ as the corpus grows** and the synthetic
  "easy" bias is diluted by harder tasks.
- The **0.30** margin guards against the degenerate "memory adds nothing" case.

## Changing the baseline
The baseline (`recall_baseline.yaml` + the synthetic corpus in
`seed_eval_db.py`) is **frozen** — do not edit it to make a failing change pass.
Adding *new* tasks is allowed but requires re-freezing `PHASE0.md` and
re-justifying the gate in this file. That re-freeze is the visible,
reviewable event — the anti-goalpost-moving device.

## Known gaps (deliberate v1 cuts)
- No `techne-cold`/`techne-warm` split — needs a freshness/drift feature that
  doesn't exist yet. The result schema reserves the `arm`/variant dimension.
- No LLM-judge — v2, and it must use the operator's running model, never a
  provisioned API key (`docs/eval.md`).
- Starter queries avoid literal FTS operator chars (`#`, `.`) until #74 hardens
  `_sanitize_fts_query`; add verbatim `cabal#14` / `adapter.py` tasks then.
