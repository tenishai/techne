# Proposal: The Coherence Layer — from *accumulate* to *compound*

**Status**: Implemented in large part — the contradiction rail (MCP `techne_conflict_emit` / `_ingest`), decayed ranking in search, and the faithfulness gate (`techne_faithfulness_emit` / `_ingest`) ship; see CHANGELOG.md for what landed when. The design below is the original draft, kept as the record.
**Author**: Raven (Opus 4.8) + Richard, paired session 2026-07-05.
**Tracking issue**: TBD (issues proposed in §6).
**Grounded in** (all 2026-07-04/05): a ground-truth read of techne's own internals; a field survey of 10 production memory systems (mem0, Zep/Graphiti, Letta, MemOS, cognee, Supermemory, Honcho, A-MEM, Memary, memobase); ~20 arxiv papers (see §7); and source-reads of Karpathy's *LLM Wiki*, NirDiamant's *Agent_Memory_Techniques*, and Google's *always-on-memory-agent*.
**Companion**: [`docs/memory-system-architecture.md`](../memory-system-architecture.md) — the *distribution* half (federation, roles-as-config, the raw→distiller→distilled pipeline, the export ledger). This proposal is the *coherence* half. **They ship together**: this proposal is the concrete mechanism for that doc's load-bearing open decision — the read-merge conflict/dedup/ranking rule (its §11 #3, "*this rule is the product*"). See §4b below.

---

## TL;DR

techne already has **~80% of a coherence layer built** — `supersede()`, `merge_into()`, a `superseded_by` FK, a real `confidence_numeric` decay formula, hybrid BM25 + recency + RRF ranking, git-anchored freshness, and a reflect → consolidate → dream candidate pipeline. **The gap is not architecture; it is *detection* and *wiring*.** Two builds, in the priority the evidence dictates:

1. **Contradiction detection → supersession** (the centerpiece). techne can *retire* a stale belief but nothing *detects* that a new fact contradicts an old one. **Embedding/FTS similarity cannot do this** (it scores a contradiction and a duplicate the same — AUROC 0.59). Adopt mem0's LLM update-decision (`ADD` / `UPDATE` / `SUPERSEDE` / `NOOP`), but route the verdict into techne's **existing preserve-not-delete `supersede()`** — which is strictly better than mem0's delete and lighter than Graphiti's graph DB.
2. **Wire the decay formula into retrieval + a lifecycle bound.** `effective_confidence()` is computed but connected to *nothing* — egress ranks by the stored `confidence='high'` *text* enum, so a two-year-old decayed "high" outranks a fresh "medium". This is a cheap wiring fix, framed as **operational** (calibration + bounded storage), *not* an accuracy play — the evidence is explicit that decay does not move recall.

Plus three smaller items: activate the dormant dependency cascade (D3), a consolidation faithfulness gate (D4), and a human-readable knowledge export + living schema (D5).

**The one-line thesis** (Karpathy's, and the field's): memory should **compile and compound**, not re-derive. techne already compiles (reflect/consolidate). What it is missing is the *lint* that keeps the compiled artifact from rotting — and rot, the benchmarks agree, is the dominant real-world failure.

---

## 1. Why this is the highest-leverage work on techne

The research is unusually clear that this is where the returns are:

- **"The gap between *has-memory* and *no-memory* is often larger than the gap between different LLM backbones."** (Du 2026 survey, arxiv:2603.07670.) Memory-architecture investment can rival model scaling.
- **The real failure mode is *under*-forgetting, not over-forgetting.** The forgetting-aware benchmark Memora/FAMA (Uddin et al., ACL 2026, arxiv:2604.20006) finds agents "offer marginal improvements" precisely because of "frequent reuse of invalid memories and failures to reconcile evolving memories." Stale facts persist and get *acted on*.
- **More storage does not fix it.** The Supersede study (arxiv:2606.27472) shows a 15-point gap (92% full-context vs 77% self-maintained memory) and — decisively — scaling storage 24× as conversation grew 24× produced **no recovery (28% → 28%)**. The bottleneck is *maintenance logic* — tracking which version of a fact is current — not capacity.
- **The model cannot self-detect stale knowledge.** Temporal drift is encoded orthogonally to correctness and uncertainty in LLM representations (Geometry of Forgetting, arxiv:2605.09195; probes hit AUROC 0.83–0.95, the model's own entropy signals ~0.5, chance). **Supersession must live in the memory infrastructure, not the model.** techne *is* that infrastructure.

techne's mission — a solo dev's cross-session, cross-project agent memory that compounds over years — is exactly the setting where the update/supersession gap bites hardest.

---

## 2. What techne already has (ground truth)

From a source read of the current code (`techne/core/{knowledge,database,search,reflect,sleepdream,anchor,egress}.py`):

| Capability | Status | Where |
|---|---|---|
| **Supersession** — `superseded_by` FK, `supersede()`, `merge_into()`, `reconfirm()` | ✅ built | `knowledge.py:136-165` |
| **Dependency cascade** — `knowledge_deps` + `_cascade_uncertain` (flip dependents to `uncertain`) | ⚠️ (at proposal time) built but wired to **nothing** — since resolved by D3/#215: edges are written via `_add_dep_in_conn` on the dream-promote path; the dead public `add_dependency()` was removed in #251 | `database.py` (`knowledge_deps` table) |
| **Decay formula** — `confidence_numeric × 0.5^(age/half_life)` + `reaffirm()` + `last_confirmed_at` clock | ⚠️ computed by `effective_confidence()` but **never called by any retrieval/egress path** | `knowledge.py` (`effective_confidence()`) |
| **Hybrid ranking** — BM25 (FTS5, per-table normalized) + recency `exp(-age·ln2/30)` at 0.7/0.3; optional RRF with a vector channel; optional MMR | ✅ built | `search.py:135-194, 539-605` |
| **Git-anchored freshness** — 5-state (unchanged/modified/moved/deleted/unknown) | ✅ built, but **annotates only — never ranks** | `anchor.py:258-315` |
| **Candidate pipeline** — reflect (extract) → consolidate (dedup clusters, merge/keep) → dream (recombine), all staged in `knowledge_candidates` with provenance before touching live | ✅ built | `reflect.py`, `sleepdream.py` |
| **event_at** (real-world subject-event time) | ✅ column exists | `database.py` |

**This is a strong base.** techne independently arrived at the raw → curated → schema shape (observations → knowledge → identity) that Karpathy's *LLM Wiki* describes, and it already does *retire-not-delete* — the exact preservation semantics the research (Graphiti/TOKI/Engram) says is correct.

---

## 3. The precise gaps

1. **No contradiction detection anywhere.** Consolidate clusters only *FTS-similar* rows and asks "same or distinct?" — never "do these conflict?" A contradiction in different vocabulary (`"use postgres"` → `"we switched to sqlite"`) never clusters, so the (fully-built) `supersede()` is never triggered. **The retirement machinery exists; nothing detects what to feed it.**
2. **Decay is inert.** `effective_confidence()` exists but no path calls it. Egress selects by the stored `confidence='high'` text enum → a deeply-decayed old "high" outranks a fresh "medium". The formula has *zero* effect on what gets recalled.
3. **The dependency cascade has no inputs.** `_cascade_uncertain` is live infrastructure; at proposal time nothing fed it. *(Resolved by D3/#215: `_add_dep_in_conn` now writes edges on the dream-promote path; the dead public `add_dependency()` was removed in #251.)*
4. **Freshness annotates but never ranks;** there is no config for the decay half-life (a dead pointer in `knowledge.py:47`); there is no lifecycle (archive/retire) — knowledge grows forever.

---

## 4. Design

### D1 — Contradiction detection → supersession *(the centerpiece)*

**Mechanism (mem0's, verified in `mem0/configs/prompts.py`).** For a new/candidate knowledge item, gather *comparison candidates* from existing knowledge, present `[new + candidates]` to an LLM, and get a per-candidate verdict:

- `NOOP` — already present, do nothing.
- `UPDATE` — the new item *augments* an existing one without contradiction → edit/`reaffirm()`.
- `SUPERSEDE` — the new item *contradicts/replaces* an existing one → route to techne's **existing `supersede()`** (mark `superseded_by`, **keep the old row as audit trail**, cascade uncertain).
- `DISTINCT` — genuinely new → `add`.

**The synthesis — why this beats the field:** mem0 does the detection well but **DELETEs** the loser (no trace); Graphiti preserves via a bi-temporal graph but **needs Neo4j**. techne already has the *preserve* half (`superseded_by`). Combining mem0's *detection* with techne's *preservation* + *dependency cascade* is strictly better than either, on plain SQLite.

**The crux — candidate selection.** Because similarity *cannot* find contradictions (MemStrata: cosine distinguishes contradicted-vs-duplicated at **AUROC 0.59**, near chance; arxiv:2606.26511), we must **not** rely on FTS/vector neighbors to pick what to compare. Select comparison candidates by **shared entities / tags / subject** (and same `type`), then let the LLM adjudicate the semantic conflict. *Detection is an LLM judgment over entity-overlapping candidates, not a similarity threshold.* This is the single most important design point.

**Where it runs.** Start by extending the **consolidate** emit: alongside the "merge or keep?" question, add a "does this contradict any entity-overlapping entry?" pass that can emit a `supersede` candidate (today consolidate can only emit `add`/`merge`). Optionally, later, a dedicated periodic **"lint"** pass (Karpathy's term) using his 6-item detection taxonomy (contradictions / stale-superseded / orphans / missing-coverage / missing-cross-refs / gaps). Keep it staged in `knowledge_candidates` → human/agent review before it touches live (see D4).

**Temporal, lightly.** Use the `event_at` column: when two entries conflict, the one with the later `event_at` (or `created_at` fallback) is current; the earlier is superseded. This is techne's cheap stand-in for Graphiti's valid-time axis — **enough** without the 4-timestamp bi-temporal graph.

*Evidence:* mem0 `prompts.py` (source, Apache-2.0); MemStrata (similarity fails → structural detection required; bi-temporal 0.95–1.00 vs RAG 0.20–0.47; arxiv:2606.26511); Engram (accurate supersession on a *lean* 9.6k-token slice **beats** full 79k-token history by **+10.4pp**; arxiv:2606.09900); Geometry of Forgetting (infra, not model; arxiv:2605.09195).

### D2 — Wire decay into retrieval + a lifecycle bound *(operational, deliberately modest)*

- **Ranking:** call `effective_confidence()` as a **tiebreak/penalty** in egress (and optionally search) so a deeply-decayed "high" cannot outrank a fresh "medium". Not a gate — a re-weight.
- **Lifecycle:** a **soft-archive** scan — entries whose `effective_confidence()` falls below a floor *and* are `status='uncertain'` *and* have not been reaffirmed → move out of *default* recall (still searchable, never hard-deleted). This bounds growth.
- Add the missing `knowledge_confidence_half_life_days` config (fix the dead pointer).

*Evidence — and the caveat that sets the scope:* decay's benefit is **operational, not accuracy**. ZenBrain's NoDecay ablation: ΔP@5 = **0.002** (statistically real, practically nil; arxiv:2604.23878). SuperLocalMemory shows aggressive decay **costs 4.4pp** (arxiv:2604.04514). So: keep the half-life *generous* (≥30d), use decay as a tiebreak, and **never hard-delete**. The value is calibration + bounded storage + honest confidence — not recall.

### D3 — Activate the dependency cascade

`knowledge_deps` + `_cascade_uncertain` are built and dormant. Populate edges from the D1 path: when a supersession or update references a prior entry, write a `(dependent, trigger, kind)` edge. Then superseding a trigger correctly flips its live dependents to `uncertain` in production. **This is techne's differentiator** — dependency-aware supersession, which mem0/Graphiti/mem0 do not do.

### D4 — Consolidation faithfulness gate *(resolved: soft gate, #252)*

TRUSTMEM (arxiv:2606.25161) shows naive LLM writes to memory produce **40%+ omission, 79% corruption, 50% hallucination**. techne's candidate-staging (review before promote) is already the right instinct. A **faithfulness check** runs on `merge`/`supersede` candidates before promotion — the agent answers a prompt asking whether the replacement content preserves all load-bearing claims of its sources (coverage/preservation), and the verdict (`faithfulness_ok` 1/0/NULL, `faithfulness_warning`) is stored on the candidate row.

**Resolved behavior (techne#252, 2026-07-06) — SOFT GATE, enforced by default, operator-overridable:**

- `promote_candidate` checks `faithfulness_ok` for `merge`/`supersede` candidates *before* touching the candidate's status.
- If `faithfulness_ok == 0` (explicitly checked and flagged unfaithful) and `force=False` (the default): promotion is **refused** — a `FaithfulnessGateError` is raised and the candidate stays `pending`. The error message cites the stored `faithfulness_warning` so the operator can see exactly what was flagged.
- The operator can override with `--force` (CLI: `techne reflect promote <id> --force`) after reviewing the warning. The same `force=True` keyword is available on the Python API.
- Unchecked candidates (`faithfulness_ok IS NULL`) and faithful candidates (`== 1`) always promote normally — the gate only blocks explicitly-flagged-unfaithful `merge`/`supersede`.
- Non-gated actions (`add`, `retire`) are never blocked regardless of `faithfulness_ok`.

This closes the doc-vs-code gap: the check was previously computed and stored but never enforced at promote time.

### D5 — Readability + schema-as-living-document *(Karpathy lifts)*

- **Knowledge export view:** `techne knowledge export` → a flat, git-diffable markdown render (an `index` + entries by tag/type), giving Karpathy's *readability/trust/debuggability* on top of techne's queryable DB. The right answer to the "wiki-you-read vs DB-you-query" fork is **both**: SQLite for structure/decay/query, a markdown projection for the human.
- **Schema-as-living-contract:** make the reflect/consolidate prompts + identity layer an explicitly **operator-co-evolvable** document ("how techne maintains my knowledge"), per Karpathy's `CLAUDE.md` framing — refined over time, not fixed.

---

## 4b. At the federation boundary (why this ships *with* the distribution architecture)

The companion doc ([`memory-system-architecture.md`](../memory-system-architecture.md)) names its own hardest question and calls it "the product": **§11 open-decision #3 — the read-merge conflict/dedup/ranking rule** when `recall(topic)` fans out across `local + team + static` stores and must return "one ranked, deduped answer." **That rule *is* this coherence layer, applied at the merge boundary instead of the store boundary.** Designing them together is not optional — the federation layer doesn't merely *use* coherence, it *raises the stakes* on it:

1. **Cross-store dedup ≠ cross-store contradiction.** The architecture doc reaches for **content-hash dedup**. Hashing catches *duplicates*; it cannot catch *contradictions* (my local `"we switched to sqlite"` vs. the team's stale `"use postgres"` hash differently and both survive). This is the same wall as §4/D1: similarity/hashing scores a contradiction and a duplicate identically (MemStrata, AUROC 0.59). **So D1's contradiction detection must run at fan-out merge time, not only at store time** — the design of the `check` (entity/tag candidate selection + LLM adjudication) is written to be callable in both places.
2. **"mine-beats-theirs? recency? confidence?" is already answered.** The architecture doc leaves the tiebreak open; the evidence closes it: for a genuine *supersession* the later `event_at` wins (recency/valid-time); for a non-conflicting tie, `effective_confidence()` (D2) breaks it; *provenance* (local vs. team) is a weak signal, not the primary rule. Merge ranking = D2's ranking, over the unioned candidate set.
3. **The distiller needs the faithfulness gate (D4) *most* here.** For the architecture doc's §2 *deposited* raw — an ephemeral sub-agent drops raw to the Archivist and leaves — nobody is present to review the distillation, yet naive distillation corrupts ~79% (TRUSTMEM). D4's coverage/preservation check is the safeguard exactly where the producer is absent.
4. **Sovereignty holds across the seam.** D1 *detects and stages* (candidate lane), it does not auto-apply — consistent with the architecture doc's principle #1 (techne "produces and remembers; the agent decides"). A team-store contradiction surfaces as a reviewable supersede candidate, not a silent overwrite of the agent's own belief.

**Consequence for build order:** the coherence primitives (A-track: D1 detection, D2 ranking) are a *prerequisite* for the federation read-merge (B-track). The federation `recall` fan-out (companion §4) should be built to call the same `run_conflict_check` / decayed-ranking functions this proposal introduces — one implementation, two call sites.

---

## 5. What we deliberately do NOT build

- **A graph DB / full 4-timestamp bi-temporal model** (Graphiti). Over-engineered for a solo-dev SQLite store; `superseded_by` + `event_at` + `status` captures ~80% of the value. (Products survey's own verdict.)
- **Aggressive Ebbinghaus/FSRS decay curves + access-count tracking.** The evidence says decay is operational-only and *aggressive* decay hurts recall. Not worth the complexity.
- **Cross-encoder reranking.** Unneeded at personal-memory scale (<~thousands of facts); BM25 + vector + RRF (already built) suffices.
- **In-weights knowledge editing (ROME/MEMIT).** Doesn't scale to lifelong updates (WikiBigEdit, ICML 2025, arxiv:2503.05683); an external versioned store — which techne is — is the recommended path.
- **AGPL-licensed dependencies.**

---

## 6. Build order (proposed issues)

**Phase 1 — the win (supersession):**
- **I1 · Contradiction detection at consolidate.** Entity/tag/subject candidate selection + LLM adjudication → `SUPERSEDE`/`UPDATE`/`NOOP` verdicts → route to `supersede()`/`reaffirm()`; staged as candidates. *Done when:* storing `"we switched to sqlite"` after a live `"use postgres"` entry is detected on the next consolidate pass and emits a `supersede` candidate; the older `event_at` loses; test.
- **I2 · Populate `knowledge_deps` in production.** Write dependency edges on the supersession/reference path so `_cascade_uncertain` fires live. *Done when:* superseding a trigger flips a real dependent to `uncertain`; test.

**Phase 2 — hygiene (decay wiring):**
- **I3 · Wire `effective_confidence()` into egress ranking** (tiebreak/penalty) + add `knowledge_confidence_half_life_days` config. *Done when:* a decayed "high" ranks below a fresh "medium" in egress; test.
- **I4 · Soft-archive lifecycle** for decayed + uncertain + unreaffirmed entries (out of default recall, still searchable). *Done when:* a below-floor entry is archived, excluded from default recall, still returned by explicit search; test.

**Phase 3 — quality + readability:**
- **I5 · Consolidation faithfulness check** on merge/supersede candidates (coverage/preservation).
- **I6 · `techne knowledge export`** markdown projection (index + entries).
- **I7 · Schema-as-living-document** — editable, documented reflect/consolidate/identity contract.

Recommended: I1 → I2 first (they *are* the coherence layer), then I3/I4 (cheap hygiene), then Phase 3.

---

## 7. Evidence appendix (key citations)

**Supersession (the converged answer — bi-temporal, retire-not-delete):** MemStrata / Temporal Validity (arxiv:2606.26511) · TOKI bitemporal operator algebra (arxiv:2606.06240) · Engram bi-temporal engine (arxiv:2606.09900) · Supersede / memory-update gap (arxiv:2606.27472) · Geometry of Forgetting (arxiv:2605.09195) · WikiBigEdit / limits of weight editing (arxiv:2503.05683) · Graphiti bi-temporal edges (arxiv:2501.13956) · mem0 update-decision (arxiv:2504.19413 + `mem0/configs/prompts.py`).

**Decay (operational, not accuracy — do not over-invest):** ZenBrain NoDecay ablation ΔP@5=0.002 (arxiv:2604.23878) · SuperLocalMemory −4.4pp (arxiv:2604.04514) · FSFM forgetting taxonomy (arxiv:2604.20300).

**Consolidation (verify writes):** Generative Agents reflection (arxiv:2304.03442) · Reflexion (arxiv:2303.11366) · A-MEM (arxiv:2502.12110) · TRUSTMEM (arxiv:2606.25161).

**Benchmarks (what empirically matters):** LongMemEval (arxiv:2410.10813) + V2 (arxiv:2605.12493) · LoCoMo (arxiv:2402.17753) · Memora/FAMA (arxiv:2604.20006) · survey meta-findings (arxiv:2603.07670) · event-centric encoding (arxiv:2511.17208).

**Framing:** Karpathy *LLM Wiki* gist (compile-not-retrieve; the "lint" detection taxonomy; schema-as-contract) · NirDiamant *Agent_Memory_Techniques* (portable ConflictResolver / decay / RRF reference implementations).
