---
card: arch-card:techne-reflect-pipeline
updated: 2026-07-20
anchor: techne/core/reflect.py sha=73326da9fa3f9640503247a1b2283de8434e511a
anchor: techne/core/sleepdream.py sha=7ce4df8e9da1353e9a6edec3e205d75f13bf409f
---
# reflect / consolidate / dream — self-maintenance

SEAM: resident-model emit/ingest (techne#19) — self-maintenance runs on the AGENT'S OWN model in its own loop, NOT a headless API call → no API key / inference bill. _emit packages a prompt (PROMPT_TEMPLATE) + injectable generate_fn (core/threads.py:_make_generate_fn); the agent answers; _ingest parses the JSON-array (core/_resident.py:extract_json_array) into knowledge_candidates rows. NEVER auto-mutates live knowledge/identity (opt-in via config.reflect.enabled, off by default).
PIECES:
- reflect.py: per fresh thread → proposes durable facts/decisions/procedures/identity claims (candidates, human-reviewed).
- sleepdream.py (techne#108):
  - consolidate (sleep): find dup/overlapping live knowledge — Tier0 exact content_fingerprint buckets | Tier1 FTS self-sim top-neighbor | Tier2 vector dup-band (deferred) → action='merge'.
  - dream (recombine): non-obvious A+B→C across a shared-tag group → action='add', origin='dream', confidence='low' (abstention-first; most groups yield nothing).
API: MCP pairs techne_reflect_emit/ingest, techne_consolidate_emit/ingest, techne_faithfulness_emit/ingest, techne_dream_emit/ingest, techne_memory_claims_emit/ingest; CLI techne reflect/consolidate/dream. The /techne-promote skill drives this end-to-end each session.
INVARIANTS:
- Every consolidate cluster has a stable cluster_hash (prompt_hash col) → a cluster already proposed (pending, promoted, OR kept-distinct) is never re-judged (_already_proposed) — stops the anti-bloat feature from bloating its own review queue.
- _FAITHFULNESS_GATED_ACTIONS={'merge','supersede'}: promoting a merge/supersede candidate needs a prior faithfulness check (raises FaithfulnessGateError unless force=True); keep this gate in sync with faithfulness_emit's SQL filter.
