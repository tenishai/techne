# Recall-Effectiveness Eval (#68)

**Status:** accepted; **M1 + M2 shipped** (frozen baseline + enforced kill-gate live). Author: Raven, 2026-06-17.

## Why
techne's unit suite proves code correctness but never measures whether the
recall layer retrieves the *right evidence*. Without that, memory changes ship
on vibes. This harness measures retrieval quality against a frozen baseline,
with a kill-gate, so changes that don't help don't merge.

## What it measures (v1)
Retrieval quality, **directly and without an LLM** — call `search()` against a
frozen synthetic corpus and score whether the gold evidence comes back. Two arms:

| arm | what | purpose |
|---|---|---|
| `bare` | no techne retrieval (empty) | control — proves the task needs memory |
| `techne` | full `search()` recall (FTS, `use_vectors=False`) | the treatment under test |

Plus per-run cost (n_results; latency/FTS-passes are easy to add) — captures the
cold/warm *spirit* (is retrieval worth its cost?) with something measurable today.

### Scoring (deterministic rubric)
Per task, `score = 0.7 * evidence_recall@K + 0.3 * phrase_coverage`, zeroed if a
`must_exclude_phrases` entry appears in the top result. Gold evidence is
referenced by a logical key resolved to a `type:id` by the seed (robust to
insertion-order changes); phrases are substring-matched on retrieved content.
Honest because "correct" is knowable against a corpus we author.

## Resolved design decisions (2026-06-17)
- **D1** — v1 is retrieval-quality, two-arm (not the full LLM-driven trio). The
  trio's `cold`/`warm` split needs a drift/freshness feature that doesn't exist
  yet, and the agent eval needs an LLM. Both deferred; the result schema keeps a
  `variant` field so they slot in non-disruptively.
- **D2** — kill-gate = absolute floor + must-beat-`bare` margin, run under a
  `pytest -m eval` marker (excluded from default unit runs via `addopts`).
- **D3** — corpus is a **synthetic seed** (`seed_eval_db.py`): deterministic,
  portable, zero secret-leak risk. Never the live `~/.techne/techne.db`.
- **D4** — this doc lives in-repo.

## v2 LLM-judge — hard constraint
When LLM-judge scoring is added it MUST use the operator's *already-running*
model, never a separately-provisioned API key. Preference order: **running
agent → local model (Ollama) → MCP sampling (when a client supports it)**. This
is why v1 scoring is deterministic — it has zero LLM dependency, so the harness
is useful now and the judge question never gates it.

## Layout
```
tests/eval/
  recall_baseline.yaml    # frozen tasks + rubrics (M1: 3; M2: 5-10)
  seed_eval_db.py         # deterministic synthetic corpus (incl. #385 gold block)
  scoring.py              # rubric scoring (recall@K + phrase coverage) +
                           # rank-aware metrics (recall_at_k/mrr/false_positive_rate/
                           # aggregate_gold, #385)
  test_recall_eval.py     # pytest -m eval runner; prints table, writes JSONL
  retrieval_gold.yaml     # #385: class-tagged gold tasks (exact/paraphrase/filter/negative);
                           # also carries #399's config_vec: block (same 24 tasks)
  test_retrieval_gate.py  # pytest -m eval_gate runner (FTS-only); prints per-task/per-class table
  conftest.py              # #399: pinned_model2vec fixture (reset+pin singleton, skip-when-absent)
  test_retrieval_gate_vec.py    # #399: pytest -m eval_vec gate runner (hybrid, use_vectors=True)
  test_ranking_ablation_vec.py  # #399: pytest -m eval_vec RRF-vs-simple ablation (report-only)
  test_retrieval_gate_scale.py  # #883/#945: pytest -m eval_scale — five arms, each
                           # varying ONE thing against the same gold set: corpus size,
                           # lexical-vs-hybrid channel, near-vs-far padding vocabulary,
                           # which table crowds, and synthetic-vs-real distractors.
                           # Informational (no floors); prints per-class deltas.
                           # #385/#399 measure retrieval on 36 rows, where paraphrase
                           # cannot fail. #945 FINDING: disjoint-vocabulary padding does
                           # not degrade paraphrase at all (1.000 at n=4,036), so the
                           # #883 curve measures semantic adjacency, not size — and no
                           # paraphrase floor should be read off it. Real borrowed rows
                           # score 0.167 where synthetic scores 0.667, so both synthetic
                           # arms understate production. The real arm is local-only and
                           # skips without a store.
  results/                # JSONL outputs (gitignored)
  EVAL.md, PHASE0.md      # (M2) methodology + frozen baseline scores + gate number
```
Run: `pytest -m eval -s`.

## Retrieval-correctness regression gate (#385)
Extends the harness above with a second, narrower gate that isolates FOUR
distinct retrieval behaviors instead of one blended recall number, scored by
rank-aware metrics (`recall_at_k`, `mrr`, `false_positive_rate` in
`scoring.py` — order-sensitive, unlike `score_task`'s rank-blind rubric):

| class | what it tests |
|---|---|
| `exact` | query shares literal tokens with the gold doc |
| `paraphrase` | query shares **zero** literal (post-porter-stem) tokens with the gold doc — a natural-language rephrase |
| `filter` | gold doc reachable only when `table=` narrows correctly; a same-text distractor lives in a different table |
| `negative` | a lexically-tempting distractor (hard-deleted, or a stale superseded revision) that must never surface |

Gold fixtures live in `seed_eval_db.py`'s clearly-delimited `#385 gold block`
(append-only, new logical keys only — never touches the #68/#118 rows above
it, so those frozen baselines don't shift). Tasks live in
`retrieval_gold.yaml`, resolved via the same logical-key gold map.

Like the recall eval, this gate is FTS-only and deterministic:
`search(..., use_vectors=False)` with `half_life_days` pinned explicitly (no
vector fusion — the vector backend is python-version-dependent — and no
wall-clock recency drift).

**Per-class floors, asserted separately.** The gate does NOT collapse the
classes into one blended recall number — a single mean lets a real regression
in one servable class hide behind another class's headroom (exact recall
dropping 1.0→0.5 would still clear a 0.5 blended floor once filter=1.0 and
paraphrase=0.0 average back up). Instead each servable class carries its own
frozen floor in `retrieval_gold.yaml`'s `config:` block (`exact` and `filter`:
recall@5 / recall@10 / mrr each ≥ observed − 0.15 ≈ 0.85; `negative`: FPR
ceiling 0.0, no slack). Baseline (2026-07-25, FTS-only): exact and filter both
1.0/1.0/1.0, negative FPR 0.0.

**`paraphrase` is informational-only — NOT gated.** Its 6 tasks share zero
literal (post-porter-stem) tokens with their gold doc by construction, and FTS
is lexical not semantic, so the FTS-only path structurally cannot serve them
(observed 0.0/0.0/0.0). Gating the deterministic path on a capability it does
not have would be dishonest, so those tasks carry `deterministic: false` and
the runner reports their scores but excludes them from pass/fail. They stay in
the gold set as pre-authored targets for the future backend-pinned **VECTOR
arm** (M3 / v2), which is where paraphrase retrieval will actually be gated.

Run: `pytest -m eval_gate -s`. Excluded from the default suite via the
`eval_gate` marker (same `addopts` exclusion pattern as `eval`/`eval_llm`).

## Vector/hybrid retrieval gate + RRF-vs-simple ablation (#399; fusion changed in #401)

The VECTOR arm the #385 section above deferred to. Reuses the SAME 24-task
gold set and `scoring.py` rank-aware metrics, but runs `search()` with
`use_vectors=True` — a **vec-first union** since #401 (the vector channel
first by ascending distance, then FTS-only hits appended; agreement is
provenance, not a rank boost) instead of FTS-only, reading a new `config_vec:`
block in `retrieval_gold.yaml` (`config:` — #385's FTS gate — is untouched).

**Determinism — pin model2vec.** The vector backend is normally AUTO-selected
(model2vec on py3.14+/fastembed-absent, else fastembed — `core/embeddings.py`),
which is python-version-dependent and would make scores non-reproducible
across environments. `tests/eval/conftest.py`'s `pinned_model2vec` fixture
resets the process-wide embedding singleton and rebuilds it pinned to
model2vec/potion-base-8M — a numpy-only static backend, byte-identical
run-to-run — BEFORE the test seeds the corpus or calls `search()`. Order
matters: `_get_embedding_model()` only reads its `config` argument on the
first build in a process, so the pin must happen before any unconfigured call
(a bare `search()`/`search_vec()`/`backfill_vectors()` from an earlier test in
the same session would otherwise have already locked the singleton to AUTO).

**Skip-when-absent (CI-safe).** The fixture skips (does not fail) unless
`sqlite_vec` imports, an embedding backend is installed, and the pinned model
is already in the local Hugging Face cache. The cache probe goes through
`_get_embedding_model(cfg, allow_download=False)`, which sets `HF_HUB_OFFLINE=1`
for the call and raises (`LocalEntryNotFoundError`, backend-dependent) instead
of blocking on a Hub download when the cache misses — same contract as
`test_recall_eval_llm.py`'s resolver-returns-None skip, just for the vector
backend instead of an LLM.

**Backfill is mandatory.** `seed()` only populates the FTS index; the
`*_vec` tables don't exist until `backfill_vectors(db_path)` runs. Skipping it
silently degrades `search(use_vectors=True)` to FTS-only and paraphrase scores
collapse back to ~0 — the exact regression this gate exists to catch.

**`paraphrase` is gated here** (unlike #385's FTS gate) — vec-first hybrid
recall is exactly the mechanism meant to serve the zero-literal-token-overlap
queries FTS structurally cannot. Floors keep #385's boundary-tolerance
convention but were **ratcheted in #401** to lock in the vec-first win (full
rationale in the `config_vec` comment):

| class | recall@5 floor | recall@10 floor | mrr floor |
|---|---|---|---|
| exact | 0.85 | 0.85 | 0.85 |
| filter | 0.85 | 0.85 | 0.85 |
| paraphrase | 0.5 | 0.8 | 0.4 |
| negative | — (FPR ceiling 0.0, no slack, same as #385) | | |

Measured baseline (2026-07-25, `use_vectors=True` = vec-first union,
model2vec/potion-base-8M pinned, `half_life_days=30.0`): exact/filter/negative
hold their FTS-only baseline exactly (1.0/1.0/1.0 recall, 0.0 FPR — vec-first
keeps an already-rank-1 hit at rank 1, so no regression); paraphrase is
recall@5=0.667, recall@10=1.0, mrr=0.546 (up from RRF's 0.5/0.667/0.144).

**RRF-vs-simple ablation** (`test_ranking_ablation_vec.py`, same marker,
report-only — asserts nothing beyond `len(results) <= limit`). Compares the
**pre-#401 RRF fusion** — rebuilt in-test via `_rrf_fuse` over the two
channels, since `search()` itself is no longer RRF — against the vec-first
"simple" union baseline that calls `search_vec()`/`search_fts()` directly
(vec-first by ascending distance, then lex-only rows appended, deduped,
truncated to the limit — no `_rerank`/`_mmr_reorder`). Measured: simple beats
RRF on `paraphrase` (recall@5 0.667 vs 0.5, recall@10 1.0 vs 0.667, mrr 0.546
vs 0.144); RRF ties simple on `exact`/`filter`/`negative` (both arms are
1.0/1.0/1.0/0.0 there — both channels already agree). This ablation is exactly
what motivated #401: RRF's rank-smoothing helps when channels agree but mutes
a lone strong signal when they don't; the vec-first union preserves that lone
signal, and is now the default in `search()`.

Run: `pytest -m eval_vec -s`. Excluded from the default suite via the
`eval_vec` marker (same `addopts` exclusion pattern as `eval`/`eval_gate`).

## Milestones
- **M1 (done)** — corpus + schema + scoring + 3 starter tasks + runner; soft
  sanity assertion. No frozen gate.
- **M2 (done)** — 6 tasks across knowledge/observation/thread; Phase-0 baseline
  frozen (`PHASE0.md`, aggregate techne=1.0/bare=0.0); kill-gate set (≥0.85
  floor + ≥0.30 margin, `EVAL.md`) and enforced as a hard `pytest -m eval`
  assertion.
- **M3 / v2** — `techne-warm` arm once a freshness feature exists; LLM-judge via
  the running model (see constraint); run under a non-Claude-Code client (#57).

## Notes / findings
- The starter queries are multi-word and each contains a term absent from the
  gold (no FTS stemming), so they exercise the #70 OR-fallback that real recall
  topics hit.
- **M1 surfaced that #74 is more severe than first rated:** realistic recall
  inputs with literal FTS operator chars — `cabal#14` (`#`), `adapter.py` (`.`)
  — *crash* `search()` (`fts5: syntax error`). File-extension and issue-ref
  queries are common, so #74 should be hardened alongside the relevance work.
  Starter queries avoid `#`/`.` until then; add verbatim variants to lock the
  fix when #74 lands.
