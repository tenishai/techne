# Proposal: Cold-Storage Tier — evict aged knowledge to Librarian documents, keep gist-pointers in the index

**Status**: Draft (design; pre-implementation).
**Author**: Claude (Opus 4.8) + Richard, paired session 2026-07-09.
**Tracking issue**: [techne#404](https://github.com/tenishai/techne/issues/404) (epic; sub-builds in §7).
**Grounded in**: a ground-truth source read of techne's current long-term-memory lifecycle (`core/{knowledge,librarian,library,egress,search,database}.py`, this session); and investigations of four local-first, document-backed agent-memory vaults — [open-second-brain](https://github.com/itechmeat/open-second-brain), [perseus-vault](https://github.com/Perseus-Computing-LLC/perseus-vault), memory-os, adaptative-living-library (patterns in architect.db).
**Companion**: [`memory-system-architecture.md`](../memory-system-architecture.md) — the raw→distiller→distilled pipeline; this tier is the *tail* of that pipeline (what happens to distilled knowledge when it ages out). And [`coherence-layer.md`](./coherence-layer.md) — its D2 soft-archive is the *trigger* this proposal consumes.

---

## TL;DR

**The question** (Richard, 2026-07-09): should techne have a system that pulls old entries out of the active DB, turns them into Librarian-curated documents, and keeps only pointers indexed in FTS/vector?

**The answer: yes — and every component already exists. The build is the missing *verb* that connects two things techne already shipped, plus two guards the vaults teach.**

- The **trigger** exists: `archive_scan()` flips a knowledge row to `status='archived'` when it is uncertain ∧ below the confidence floor ∧ unreaffirmed for 180 days (coherence-layer D2, `knowledge.py:734-859`). But today archiving **only sets a flag** — the fat row stays resident in `techne.db`, merely hidden from Tier-0 egress and `techne_recall`. Nothing is evicted anywhere.
- The **cold document store** exists: the **Librarian** (`librarian.db`) holds full-content `documents` with their own `documents_fts`. Today it also keeps a small **`document_card`** (gist + `document_id` pointer) back in `techne.db`'s knowledge table — FTS-indexed and **pull-only** (`PULL_ONLY_TYPES`, `knowledge.py:40`). It is wired only to **transcripts and markdown** — never to aged-out knowledge rows.

**Retrieval model — cascade, not card-in-techne (decided 2026-07-09).** For the cold tier we do **not** keep a card in `techne.db`. A card-in-techne only half-achieves the goal: the fat body leaves, but the card *and its FTS/vector entry* stay resident forever, bloating the active index over years. Instead, cold content **and its index live entirely in `librarian.db`** (give the document a `gist` column to preserve push-gist/pull-detail), and recall **cascades**: `techne.db` first → `librarian.db` second. This is memory-os's 4-tier fallback-cascade pattern, and it fully realizes "old stuff leaves the active DB." Cascade policy maps onto techne's existing two paths: focused `techne_recall`/Tier-0 egress hit `techne.db` only (cold never auto-injects — today's `PULL_ONLY` semantics); breadth `techne_search`/deep recall cascade into `librarian.db`, **score-gated** (fall through whenever the live top-hit is below a relevance floor). Trade-off: cross-store results come back **tiered** (live first, then cold), not globally merge-ranked — the right default for a pull-only cold tier, and it **deletes the anti-orphan-lint problem** (the document is the only copy; no card to dangle). *Blast radius:* this also refactors the existing transcript/markdown Librarian, which uses card-in-techne today — so it's a choice about the Librarian's retrieval model, not just cold knowledge (§2).

So the shape Richard described is already the Librarian's shape. The gap is two unbuilt verbs — and, importantly, they should be **separate**, because they have very different costs and cadences:

```
archive_scan()  ──▶  GRADUATE (relocate)         ──▶  librarian.db document [stage: aging]  +  card (gist-pointer in techne.db, FTS, pull-only) ; drop fat row
   (built)           (new · cheap · per-row · on trigger)   (Librarian store — built for transcripts today)
                                                                    │
                                                                    │  ages untouched M days · a pull re-warms → rehydrate back to a techne.db row
                                                                    ▼
                                                            COLD-STORE (curate / consolidate / compress) [stage: cold]
                                                            (new · expensive · batched · Librarian clusters aged docs into "chapters")
```

**Graduation ≠ cold-storage.** Graduation is *relocation*: the moment a row crosses the archive trigger, its fat body leaves `techne.db` for `librarian.db` and only the lightweight card stays behind — so the active DB never accumulates dead weight (the flaw in soft-archive today, where the fat archived row stays resident). Cold-storage is *curation*: the expensive Librarian pass that only touches documents which have **aged untouched** and proven they are really cold. Splitting them means the active DB stays lean immediately (cheap graduation) while curation cost is paid once, late, and in batch. It also keeps reversibility cheap in the window that matters: a freshly-graduated document is 1:1 with its old row, so re-warm-on-access is a trivial rehydrate; only truly-cold, consolidated documents return as a document reference rather than a resurrected row — which is correct, since recently-archived memory is exactly what's most likely to be pulled back.

Plus two guards the vault survey makes non-optional:
1. **Promotion-back on access** (perseus-vault's decay-refresh) — a cold card that gets pulled must be able to re-warm, or eviction is one-way and the archive silently rots into "forgotten."
2. **Reason-coded eviction** (open-second-brain's retirement taxonomy) — each evicted row carries *why* it left the active set, so the cold corpus is an auditable library, not a junk drawer.

Nothing here is new architecture. It is connection + two guards.

---

## 1. What techne has today (ground truth)

Source read, canonical checkout `tenishai/techne` @ `ffcb1a6`:

| Capability | Status | Where |
|---|---|---|
| **Soft-archive** — `archive_scan()` sets `status='archived'` on uncertain ∧ `effective_confidence()<0.1` ∧ unreaffirmed-180d; reversible via `unarchive()` | ✅ built, but **operator-invoked only** (`techne knowledge archive-scan`); no background sweep | `knowledge.py:734-859` |
| **Archive effect** — excluded from Tier-0 egress + `techne_recall`; still returned by explicit `techne_search`/FTS | ✅ built; **row stays fully resident in `techne.db`** | `egress.py:169`, `search.py:260-268` |
| **Librarian document store** — `librarian.db` `documents` table (full content + own `documents_fts`), db-per-domain | ✅ built | `core/librarian.py`, `core/library.py` |
| **Gist-pointer card** — `document_card` (gist + `document_id`) as a `knowledge` row; FTS-indexed; **pull-only**, excluded from both egress tiers | ✅ built; **fetched only via explicit `techne librarian card <id>`** | `knowledge.py:40`, `librarian.py` |
| **Doc ingest paths** — `index-transcripts` (Claude Code JSONL), `index-markdown` (any `.md` dir) | ✅ built; **not wired to knowledge/observation eviction** | `platforms/claude_code/transcript_library.py` |
| **`observations.tier`** — `hot\|warm\|cold` column | ⚠️ **dead stub**: write-once at capture, no promotion/demotion, zero consuming logic | `database.py:91` |
| **Decay formula** — `confidence_numeric × 0.5^(age/half_life)`, `reaffirm()`, `last_confirmed_at` clock | ✅ built; now wired into egress ranking (coherence D2) | `knowledge.py:544-595` |
| **Two-tier egress** — Tier-0 (SessionStart, always-on, budget-bounded) + Tier-1 (UserPromptSubmit, relevance-ranked, opt-in) | ✅ built; `PULL_ONLY_TYPES` already keeps patterns/documents/cards out of both | `egress.py`, `hooks/user_prompt_submit.py` |
| **Index** — FTS5 over **full body** for every table; optional `sqlite-vec` over full content (backfill is a separate step, not part of the write path; whether anything auto-invokes it is a deployment question — techne#704) | ✅ built; gist/pointer split exists **only** at the card level | `database.py:278-291`, `search.py:930-946` |

**The one-line reading:** techne already *demotes* stale knowledge (archive flag) and already *evicts-to-document-with-pointer* for transcripts (Librarian). It has never *connected demotion to eviction*. Archived rows just accumulate, fat, in the active DB.

---

## 1b. Reality check — when does this activate, and what does it actually save?

Measured against the live DB (`~/.techne/techne.db`, 2026-07-09), so this proposal carries its own honest priority signal:

**Activation: nothing ages out today, and won't for months.** `techne knowledge archive-scan --dry-run` (floor 0.1, window 180d) returns *"No entries qualify."* The whole knowledge corpus is **447 rows, all `status='current'`, ~4 weeks old** (earliest `2026-06-08`). Two independent gates are wide shut:
- **Age**: oldest entry ~30 days; window is 180. First age-eligibility ≈ **mid-December 2026**.
- **State**: archival requires `status='uncertain'` first, and there are **0 uncertain entries** — so the upstream contradiction→`uncertain`→archive cascade has never fired. Coherence-layer **D1 (contradiction detection) is "mostly proposed"**, so it may not be running at all. **Cold-storage is the tail of a pipeline whose head may be off** — verifying D1 actually produces `uncertain` entries is the prerequisite, and higher-leverage than this build.

**Size: knowledge is ~6% of the DB — this is a curation feature, not a size play.** `techne.db` is **42.6 MB over ~32 days** (~1.3 MB/day → ~480 MB/yr). Where the weight is:

| Object | Size | Share |
|---|---|---|
| Observations (raw): table 11.8 + vec 9.0 + FTS 3.0 + idx | **~25 MB** | **~57%** |
| `thread_boundaries`: table 9.0 + index 3.9 | **~13 MB** | **~30%** |
| **Knowledge (this proposal's target)**: table 1.0 + vec 1.0 + FTS 0.4 | **~2.4 MB** | **~6%** |

Graduating aged knowledge reclaims **~nothing**. If a lean active DB is ever the goal, the levers are **observations** (age them out via the distiller `drain.py` — §C5 is the real size item, not a "later, optional" one) and the **`thread_boundaries` footprint** (30% of the DB, ~1.3 KB/observation — possibly an efficiency bug, not a feature). And at ~480 MB/yr, SQLite has years of runway, so **disk pressure is not the driver** — build this for human-curatable long-term memory and working-set signal-to-noise, never to reclaim space.

**Net:** build ahead of need for *curation*, sequence the D1 check first, and do not justify it by size.

---

## 2. The design fork that matters

For the **cold tier only**, what is the source of truth — the document or the row?

- **(A) Row canonical, document is an export/backup.** Low risk, reversible, but yields none of the vaults' actual benefit: you still can't hand-curate long-term memory as prose, and the active DB never shrinks.
- **(B) Document canonical, DB row becomes a gist-pointer/cache.** The vaults' model, and already how the Librarian works for transcripts. You get a browsable, human-and-Librarian-curatable long-term corpus, a small fast active DB, and cheap portability (Markdown is git-diffable). Cost: recall into cold is two-hop (hit card → rehydrate document), and Librarian edits require a card+embedding reindex.

**Recommendation: (B), scoped to the cold tier.** Hot/warm knowledge stays rows-in-SQLite (fast, structured, joinable). Only knowledge that has crossed the archive threshold *and* stayed cold graduates to document-canonical. This is the minimal commitment that delivers the vaults' thesis — *a memory system a human can open and edit its old memories in* — which is techne's real gap, not capacity.

---

## 3. The builds

### C1a — Graduate (relocation; centerpiece)
`graduate_to_library(knowledge_id)` composes existing modules: (a) serialize the archived knowledge row (content + provenance + tags + `event_at`/valid-time) into a `librarian.db` document at `stage='aging'`; (b) generate a `document_card` gist-pointer (reuse the card generator transcripts use); (c) drop the fat row from `techne.db` (the card carries the pointer; keep a `superseded_by`→card tombstone only if the coherence cascade needs the edge). Reversible: `rehydrate(card_id)` reconstructs the `techne.db` row from the aging document — cheap and 1:1 during the aging window. **This is the whole ask; the active DB goes lean the instant a row graduates.**

### C1b — Cold-store (curation; the expensive Librarian pass)
`cold_store()` runs only over documents that have **aged untouched** (see C3) and moves them `stage='aging' → 'cold'`, doing the real curation: cluster related aging documents into consolidated "chapters," drop redundancy, compress. Borrow **memory-os's decoupling directly**: curation is *event/threshold-triggered* and structural, while **index freshness is a separate, scheduled, diff-only pass** — re-embed a card/document only when its content hash changed, never a full `backfill_vectors`. And adopt its **anti-orphan lint**: every `document_card` must resolve to a live document, and curated chapters carry a closed-taxonomy frontmatter — so cold-storage can't strand a pointer. After a chapter consolidates, `rehydrate` returns a *document reference*, not a resurrected row (correct: truly-cold memory rarely needs to become a live row again).

### C2 — Promotion-back on access (perseus-vault: decay-refresh)
When a `document_card` is pulled (`library card`, or a Tier-1 search hit that surfaces the card), stamp an access and let repeated access re-warm the entry — at minimum bump recency/`last_confirmed_at`; ideally auto-`rehydrate` after K hits in a window. **Without this, eviction is one-way and cold = forgotten.** This is the single most important guard.

### C3 — Run the lifecycle, with guardrails on what may graduate
`archive_scan()` is manual-only today. The lifecycle needs a cadence and two eligibility guards:
- **Cadence**: the archive trigger fires `graduate` directly (C1a) — no lingering `status='archived'` fat-row stage (that just reintroduces the accumulation we're removing; the trigger, uncertain ∧ below-floor ∧ 180-days-unreaffirmed, is already conservative). A *second* scheduled pass runs `cold_store` (C1b) over documents aged untouched a further M days. So: **graduate on trigger → age in `librarian.db` → cold-store when proven cold.** Run from a SessionEnd hook or a routine.
- **Never-graduate blocklist** (adaptative-living-library): before the age/decay gate, hard-bar categories that must never reach the document store regardless of age or recall — live credentials/secrets, raw transcripts, unverified hunches. Negative-space policy, enforced first.
- **Provenance / importance exemption** (memory-os): exempt human-authored and pinned/high-importance knowledge from auto-graduation entirely — user-asserted facts don't age out by heuristic. Optionally blend a continuous half-life score with the current hard 180-day cutoff so aging is graded, not binary.

### C4 — Reason-coded graduation + curation audit log
Two provenance mechanisms, both cheap:
- **Reason code** (open-second-brain): each graduated card/document records *why* it left the active set (`stale-no-evidence`, `superseded-by-context`, `low-confidence`, `user-rejected`, …) — one enum field, so the cold corpus is auditable and the coherence faithfulness pass can inspect what was shed.
- **Append-only curation log** (memory-os `log.md`): every `cold_store`/consolidation run appends which cards it merged into which chapter and why, framed as a debugging aid ("if a chapter looks wrong, check the log"). This closes the loop back to the originating session/thread — essential when a later conflict-detection or faithfulness check needs to trace a bad merge.

### C5 — (Later) activate the dead `observations.tier` cold slot
Observations are raw/high-volume; they should reach cold via the **distiller** (`drain.py`, already built) — aged hot observations get distilled into knowledge, and only then does the knowledge follow C1. Do **not** individually card raw observations. Knowledge-first; observations via the existing raw→distilled pipeline.

**Single-writer discipline** (open-second-brain): the Librarian is the sole writer of `librarian.db` during eviction/curation, so consolidation never races. techne's db-per-domain already isolates this; the convention just needs stating for the eviction sweep. **Valid-time retention** (perseus-vault, bi-temporal): evicted documents keep their `event_at`/valid-time so a pulled cold fact reads as "true as of X," never mistaken for current.

---

## 4. How the vault survey maps to the builds

| Vault | Contributes | To |
|---|---|---|
| **Librarian (techne, already built)** | full-content documents + gist-pointer cards, pull-only egress | the destination — C1 writes into it |
| **perseus-vault** | decay-refresh-on-access; bi-temporal valid-time | C2 (re-warm signal); valid-time retention |
| **open-second-brain** | reason-coded retirement taxonomy; single-writer consolidation; Markdown-as-source-of-truth | C4; write-safety; fork (B) |
| **memory-os** (1.2k★, the closest prior art) | decoupled curation-vs-indexing pipelines (event-triggered promote / scheduled diff-only re-embed); closed-taxonomy + mandatory-wikilink anti-orphan lint; provenance-and-importance-exempt decay; per-page curation audit log | C1b/§5 (the blueprint); C3 (provenance exemption); C4 (audit log) |
| **adaptative-living-library** (hobby) | explicit *never-promote* blocklist (raw transcripts, credentials, hunches) — negative-space policy | C3 (never-graduate guard) |

This is where the earlier "pattern lift" and the design question converge: the `deliberate-alt`-scored single-writer pointer and the `gap-fill`-scored decay-refresh and evict-to-document patterns stop being catalogue trivia — measured against *this* build, they are its sub-component designs.

---

## 5. Risks & mitigations

- **Two-hop recall latency** → acceptable: cold is pull-only by definition; a hit returns the card gist immediately and rehydrates only on demand.
- **Reindex on curation** → when the Librarian edits/consolidates a document, regenerate its card gist + embedding — but **diff-only** (re-embed solely on content-hash change, memory-os's pattern), never a full `backfill_vectors`. Needs a `reindex_document(id)` path (small) + the anti-orphan lint (every card resolves to a live document).
- **Cold = forgotten** → C2 is the explicit guard; without it, do not ship.
- **Irreversibility** → eviction must be `rehydrate`-able; keep `unarchive()` semantics end-to-end. Never hard-delete on evict.
- **Card gist quality** → if the gist is too thin, cold memory stops being findable. Reuse the transcript card generator, tuned for knowledge rows (claim + entities + why-evicted).

---

## 6. Open decisions (for the maintainer)

1. **Source of truth for cold** — (B) document-canonical (my rec) vs (A) row-canonical / doc-as-backup. §2.
2. **Granularity** — *resolved by the graduate/cold-store split (2026-07-09)*: graduate **per-row** (cheap, 1:1, reversible), then **cluster into chapters at the aging→cold transition** (C1b), not on graduation. Open sub-question: chapter boundaries — by topic/entity, by source-thread, or by time window?
3. **Aging window `M`** — how long a graduated document ages untouched before `cold_store` consolidates it. Governs the reversibility SLA (rehydrate is 1:1 while aging, a doc-reference after cold). Start conservative (e.g. 90d)?
4. **Scope v1** — knowledge-only (my rec), observations deferred to the distiller (C5)?
5. **Graded vs hard trigger** — keep the hard 180-day `archive_scan` cutoff, or blend in memory-os's continuous half-life score (C3)? Hard cutoff is simpler for v1.

---

## 7. Proposed issues

- **techne#TBD — C1a `graduate_to_library`**: archived knowledge row → `librarian.db` document (`stage='aging'`) + gist card → drop fat row; `rehydrate` inverse. (centerpiece; frees the active DB)
- **techne#TBD — C1b `cold_store`**: consolidate aged-untouched documents into chapters (`stage='cold'`); decoupled from indexing. (the expensive Librarian pass)
- **techne#TBD — C2 promotion-back on access**: access-stamp cards; re-warm / auto-rehydrate after K hits. (ship-blocker for C1a)
- **techne#TBD — C3 lifecycle + guards**: graduate-on-trigger, scheduled second-stage cold-store; never-graduate blocklist; provenance/importance exemption.
- **techne#TBD — C4 reason-coded graduation + curation audit log**: reason enum on card; append-only consolidation log.
- **techne#TBD — reindex path**: `reindex_document(id)` (diff-only re-embed) + anti-orphan lint (every card resolves to a live document).
- **techne#TBD — C5 (later)**: wire `observations.tier` cold via the distiller; retire the dead stub or make it mean something.

**One-line thesis:** techne already evicts-to-document-with-pointer — for transcripts. This proposal points that same, already-built machinery at its own aged knowledge: **graduate** the fat row out of the active DB the moment it archives, let it **age** in the Librarian, **cold-store** it only once it's proven cold — with the one guard (re-warm on access) that keeps the cold library from becoming a grave.
