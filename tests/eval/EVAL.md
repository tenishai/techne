# Memory Eval — Methodology & Kill-Gate (#68 → #118)

The contract. Read before changing anything that touches retrieval.

## Run
```
pytest -m eval -s
```
Excluded from the default unit suite (`addopts = -m 'not eval'`). Prints a
per-(task, arm) table, per-category recall, the cost axis, and writes JSONL to
`results/`. (The `score_task` rubric itself is unit-tested in the default suite via
`test_scoring.py`.)

## Taxonomy (MeME, techne#118)
Each task has a `category`: **exact-recall**, **aggregation**, **tracking**
(Stage 1, deterministic) and **absence**, **deletion**, **cascade** (Stage 2 —
`stage: 2`, skipped until the LLM answerer + judge land in PR3+). The deterministic
half manifests at the *retrieval* layer; the other half's failure ("the model
*asserts* an absent/deleted/should-be-uncertain fact") `search()` cannot exhibit, so
it needs an answerer.

## Arms (3-variant control)
- **`bare`** — no-memory control. Proves each task needs memory; if it ever scores
  > 0 on a positive category, the task is answerable without memory and must tighten.
- **`techne`** — curated `search()` recall (FTS, `use_vectors=False`).
- **`raw-source-dump`** — the whole corpus, uncurated (incl. superseded rows). Judged
  on the **cost axis only**, never the recall gate.

## Scoring
Per task: `0.7 · evidence_recall@K + 0.3 · phrase_coverage`, zeroed if a
`must_exclude_phrases` entry appears **anywhere in the results** (widened from
top-1 in #118, so a stale revision leaking at any rank fails a `tracking` task).
`detail` also carries `precision` (gold hits / results) and `token_cost` (char
proxy) — the cost axis. Deterministic, no LLM (see `docs/eval.md`).

## Kill-gates (per-category, techne#118)
A retrieval change **does not merge** unless, for every positive category, on the
frozen baseline (`config.gates[category]`):
- **`bare` aggregate = 0** (the category needs memory), **and**
- **`techne` ≥ floor** (= category baseline − 0.15 tolerance), **and**
- **`techne` − `bare` ≥ margin**.

Legacy `exact-recall` keeps the #68 M2 values (0.85 / 0.30) byte-for-byte.

**Plus the cost-axis thesis** (the reason curation exists): curated precision ≥
`cost_precision_ratio` (3.0) × raw-dump precision, and curated token_cost <
raw-dump. All enforced as assertions in `test_recall_eval.py`.

## Changing the baseline
The baseline (`recall_baseline.yaml` + the synthetic corpus in `seed_eval_db.py`)
is **frozen** — do not edit it to make a failing change pass. Adding *new* tasks is
allowed but requires re-freezing `PHASE0.md` and re-justifying the gates here. That
re-freeze is the visible, reviewable, anti-goalpost-moving event.

## Known gaps (deliberate cuts)
- **Stage 2 (absence/deletion/cascade)** needs an LLM answerer + defensive judge —
  it MUST use the operator's running model (operator → local Ollama → MCP sampling),
  never a provisioned API key (`docs/eval.md`); skips cleanly when none is reachable.
  `cascade` additionally waits on #117 (cascade-to-uncertain).
- No `cold`/`warm` (memory-state) split — needs a freshness/drift setup-cost feature;
  the `variant` field is reserved for it.
- No vector-fusion arm for *this* (#68/#118) harness — `use_vectors=False` still
  pins the FTS path here. The retrieval-correctness gate has one instead:
  `pytest -m eval_vec -s` (techne#399, `test_retrieval_gate_vec.py` +
  `test_ranking_ablation_vec.py`, reusing #385's `retrieval_gold.yaml` gold
  set) — see `docs/eval.md`'s "Vector/hybrid retrieval gate" section.
