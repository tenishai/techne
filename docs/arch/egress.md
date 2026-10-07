---
card: arch-card:techne-egress
updated: 2026-10-06
anchor: techne/core/egress.py sha=6150ae0caec48e76d3efd9b60d1f2d076cd2433b
---
# egress — Tier-0 SessionStart injection rails (techne#93)

WHAT: selective-injection rail deciding what durable knowledge is PUSHED (not just pull-recallable) into a fresh SessionStart context, under a hard char budget (injection.char_budget, default 2000).
GATE: superseded_by IS NULL AND confidence='high' AND type NOT IN PULL_ONLY_TYPES (patterns/documents/document_cards = pull-only, NEVER auto-injected — core/knowledge.py:PULL_ONLY_TYPES).
LESSONS ARE NOT IN THESE BANDS (techne#1195): type NOT IN LESSON_TYPES (lesson/grant/charter). They have their own band, core/lessons.select_principles, with its OWN budget (injection.principle_char_budget, default 4000 — not a share of char_budget) and its own render. hooks/session_start.py puts it ahead of Key Knowledge on a cold start and is the only block re-injected after `compact` or `clear` (PRINCIPLE_REINJECT_SOURCES); each send is logged to context_injections as source='lesson_principles' (best-effort, like every injection log: a failed write is swallowed), which is what lessons.loaded_when_failed reads. A session with no such row reads as NULL (unknown), not 0, so a lost write costs evidence, not a false 'not loaded'.
BANDS (priority order; fill budget in order; each line prefixed ⟦techne#<id>⟧ for attribution):
  1 Identity (memory-type:user, always first)
  2 Corrections (memory-type:correction; reserved slice via correction_reserve_frac right after identity — so a belief-overturning fact can't be evicted by merely-newer routine facts; the "confident-wrong" blind-spot fix)
  3 Recent results (freshest decisions/evals/facts, cap recent_reserve_frac — recency-starvation fix)
  4 Feedback   5 Project state (latest memory-type:project / platform-state)   6 Leftover
CONSUMERS: hooks/session_start.py (injection call site); core/federation.py (fans recall over local+remote before this same priority/budget logic).
INVARIANTS:
- A correction only injects if stored at confidence='high' — else it is SILENTLY invisible to this rail.
- egress calls from hooks are wrapped: any failure → minimal banner, never a crashed/blocked session (techne#95/#119).
