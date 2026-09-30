# Proposal: make reflect ingest exactly-once (drained marker + candidate dedup)

- **Filed:** 2026-07-04 · from a review agent's drain build
- **Status:** proposed (follow-up "B"; the drain script ships the "A" workaround today)
- **Motivation:** the out-of-band drain (a per-agent `drain.py` script) distills a
  capture-only agent's observations after the fact, and runs at BOTH fire-up and
  close, so it must be safe to double-run. Two gaps in techne make it not
  idempotent at the source; the drain works around both, but they're latent bugs
  for any caller of the reflect path.

## Gap 1 — `_insert_candidate` is a blind INSERT (no dedup)

`techne/core/reflect.py::_insert_candidate` does an unconditional
`INSERT INTO knowledge_candidates (...)`. `prompt_hash` is `NOT NULL` but has no
UNIQUE index, so re-ingesting the same thread's answer inserts a **duplicate**
pending candidate. Knowledge-level content-hash dedup only catches it later at
*promote*, and only if promoted — pending candidates accumulate.

**Fix:** `UNIQUE(prompt_hash)` (or `UNIQUE(source_thread_id, content)`) on
`knowledge_candidates` + `INSERT ... ON CONFLICT DO NOTHING`, so any caller of
`ingest_reflection_response` is idempotent. Migration + a test that re-ingests
the same batch twice and asserts one row.

## Gap 2 — no "this session/thread was reflected" marker

`reflect_on_session` increments a `threads_reflected` **counter** but persists
nothing. So "already distilled" can only be inferred as "has a candidate row" —
which fails for the **empty-yield** case: a thread the model answers `[]` writes
no candidate, so a naive worklist re-emits and **re-runs the model on it every
pass, forever**. (Confirmed real: sonnet AND opus both return `[]` on thin
orchestration threads — the common case.)

**Fix:** stamp `sessions.drained_at` (or `threads.reflected_at`) in the *same
transaction* as the candidate insert, on every successful reflect — empty yield
included. Then "undistilled" is a stored fact, not an inference, and the
empty-yield session is recorded once.

## Why together

With both, a drain is exactly-once even across a crash *between* "candidates
written" and "fingerprint advanced" (the one window the script-side `.drained.json`
can't close). The drain then drops its own `prompt_hash`-exists guard and
`.drained.json` set, and just reads `drained_at`.

## Interim (shipped)

`drain.py` owns idempotency itself: worklist = sessions with observations, no
existing candidate row, not in a `.drained.json` id-set, and drainable (ended or
stale). It records empty-yield sessions in the set and guards on `prompt_hash`
before ingest. Correct in practice; the crash-window is the only residual, and
push being content-addressed means a re-do is harmless, never a loss.
