---
card: arch-card:techne-reflect-pipeline
updated: 2026-08-07
anchor: techne/core/reflect.py sha=840a00803602808f6b94f1e7683c33218ec38356
anchor: techne/core/sleepdream.py sha=7ce4df8e9da1353e9a6edec3e205d75f13bf409f
---
# reflect / consolidate / dream — self-maintenance

SEAM: resident-model emit/ingest (techne#19) — self-maintenance runs on the AGENT'S OWN model in its own loop, NOT a headless API call → no API key / inference bill. _emit packages a prompt (PROMPT_TEMPLATE) + injectable generate_fn (core/threads.py:_make_generate_fn); the agent answers; _ingest parses the JSON-array (core/_resident.py:extract_json_array) into knowledge_candidates rows. NEVER auto-mutates live knowledge/identity (opt-in via config.reflect.enabled, off by default).
TRIGGER (techne#514): reflection debt is DURABLY QUEUED, not re-derived from recency. session_end enqueues one reflection_queue row per thread (THREAD IDS, re-rendered at drain time — a stored prompt would embed a snapshot that drifts from the ledger it renders from). Before #514 session_end COUNTED the debt and enqueued nothing, so reflection survived only the polite exit path; every crash/timeout dropped it silently. The queue DECOUPLES producer from reflector: any session can drain another's threads, because answering a rendered prompt needs the thread's observations, not the original session's context.
PIECES:
- reflect.py: per fresh thread → proposes durable facts/decisions/procedures/identity claims (candidates, human-reviewed).
- reflect.py queue trio (#514): enqueue_reflection_debt (session_end, INSERT OR IGNORE on the UNIQUE thread_id — idempotent by schema, so per-turn firing and racing peers both no-op) / pending_reflection_queue (SessionStart nudge reads it) / drain_reflection_queue (STAMPS drained_at, never deletes — the queue is the record that the debt existed and was paid).
- sleepdream.py (techne#108):
  - consolidate (sleep): find dup/overlapping live knowledge — Tier0 exact content_fingerprint buckets | Tier1 FTS self-sim top-neighbor | Tier2 vector dup-band (deferred) → action='merge'.
  - dream (recombine): non-obvious A+B→C across a shared-tag group → action='add', origin='dream', confidence='low' (abstention-first; most groups yield nothing).
API: MCP pairs techne_reflect_emit/ingest, techne_consolidate_emit/ingest, techne_faithfulness_emit/ingest, techne_dream_emit/ingest, techne_memory_claims_emit/ingest; CLI techne reflect/consolidate/dream. The /techne-promote skill drives this end-to-end each session.
INVARIANTS:
- reflection_queue.drained_at is NULLABLE, not a status enum: pending is the ABSENCE of a drain, so there is no second source of truth to disagree with the timestamp. Drain is guarded on `drained_at IS NULL` so a re-drain never rewrites the first payment.
- reflection_queue.thread_id is `REFERENCES threads(id) ON DELETE CASCADE` and the CASCADE is REQUIRED: re-detection deletes and re-inserts a session's whole thread set in one transaction (lifecycle.py), so a plain reference makes the queue BLOCK re-detection with a FOREIGN KEY failure.
- Every consolidate cluster has a stable cluster_hash (prompt_hash col) → a cluster already proposed (pending, promoted, OR kept-distinct) is never re-judged (_already_proposed) — stops the anti-bloat feature from bloating its own review queue.
- _FAITHFULNESS_GATED_ACTIONS={'merge','supersede'}: promoting a merge/supersede candidate needs a prior faithfulness check (raises FaithfulnessGateError unless force=True); keep this gate in sync with faithfulness_emit's SQL filter.
