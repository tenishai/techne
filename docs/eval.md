# Recall-Effectiveness Eval (#68)

**Status:** accepted; **M1 shipped**. Author: Raven, 2026-06-17.

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
  seed_eval_db.py         # deterministic synthetic corpus
  scoring.py              # rubric scoring (recall@K + phrase coverage)
  test_recall_eval.py     # pytest -m eval runner; prints table, writes JSONL
  results/                # JSONL outputs (gitignored)
  EVAL.md, PHASE0.md      # (M2) methodology + frozen baseline scores + gate number
```
Run: `pytest -m eval -s`.

## Milestones
- **M1 (done)** — corpus + schema + scoring + 3 starter tasks + runner; soft
  sanity assertion (techne ≥ bare, bare = 0, techne > 0). No frozen gate yet.
- **M2** — 5–10 tasks, freeze Phase-0 baseline, set GATE/MARGIN from the
  measurement, turn the gate into a hard `pytest -m eval` assertion.
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
