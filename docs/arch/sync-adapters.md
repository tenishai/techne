---
card: arch-card:techne-sync-adapters
updated: 2026-07-20
anchor: techne/sync/adapter.py sha=ea8ee57464f12ee3d58a09b1429058de7a76d617
---
# sync-adapters — egress + ingest protocols

SEAM: two runtime_checkable Protocols:
- SyncAdapter: push(entries)→PushResult, pull(query)→list[KnowledgeEntry], health() — OUTBOUND team-backend sync.
- SourceAdapter: initial_walk(), start_watch(), stop_watch(), health() — INBOUND ingest (observations; CCMemorySource drains curated_memory). Optional `provides:{'curated','raw'}` attr for the #107 reconciler.
IMPLS: built-in noop (sync/noop.py), filesystem (sync/filesystem.py). Also: cc_memory, github_issues, jsonl_records, jsonl_transcript, sinks, outbox.
OPS: load_adapter(name, **kwargs) — built-ins first, then 3rd-party via `techne.sync_adapters` entry-point group (pyproject registers noop, filesystem); unset kwargs dropped so minimal adapters keep a no-arg __init__.
CONSUMERS: outbox flush (sync/outbox.py), `techne init` (config.sync.adapter), `techne status`.
INVARIANTS:
- PushResult.synced = count of CONSECUTIVE leading successes from batch start — NEVER higher even if a later entry would've succeeded. Partial-middle failure → retry whole-batch-from-failure next flush.
- Namespacing (techne#51): outbox targets `sync:<name>` (adapter-bound) vs `sink:<name>` (sink-bound); enqueue REJECTS un-namespaced targets so the two flush paths never cross-drain.
