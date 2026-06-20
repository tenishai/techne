# Architectural patterns

Generated export from techne architect.db (`techne architect export`). Re-import with `techne architect ingest`. architect.db is canonical — edit there (via the architect persona), not here.

---

## Capability probing with fallback dispatch

- **Source**: https://github.com/RohiRIK/OpenLtm
- **Surveyed**: 2026-06-09

At startup, probe for a system `libsqlite3` (env override → Homebrew → Linux paths), try to load `sqlite-vec` and similar extensions, verify each via a live query. Cache the result as `{customSqlite, vec, honker}`. Callers branch on what's actually available — same codebase spans a bare dev laptop and a well-provisioned server without config changes.

**Why interesting**: Not feature flags — runtime capability *negotiation*. Different from "if env var set, use X" because the probe confirms each capability actually works end-to-end before code paths commit.

**Where to apply in techne**: Gate vec0 / WAL behaviors (we currently eat the `fastembed`-has-no-3.14-wheel pain in `pyproject.toml` extras); probe for optional MCP servers at startup; factor `techne/sync/adapter.py` `load_adapter` to probe per-adapter before dispatch.

---

---

## Decay-at-query-time with importance-tiered half-lives

- **Source**: https://github.com/RohiRIK/OpenLtm
- **Surveyed**: 2026-06-09

Relevance formula: `score = importance × confidence × 0.5^(days_since / half_life)` where `half_life` varies by `importance` tier and `importance=5` never decays. Decay is **never stored** — recomputed on every read. `last_used_at`, `last_confirmed_at`, and `created_at` are all candidates for the reference timestamp, so *access itself refreshes relevance*.

**Why interesting**: Two non-obvious choices. (a) Storing decay is a footgun — goes stale, has to be batch-recomputed; recompute-on-read makes the model invariant immune to staleness. (b) Access-as-confirmation gives you LRU-like ranking without an explicit LRU structure, and lets agents naturally surface what they actually use.

**Where to apply in techne**: We currently age threads with status transitions (`active → completed → stale`) on hard time thresholds (`complete_after_hours`, `stale_after_days`). A continuous decay score read at query time would be richer signal. The access-as-confirmation idea slots cleanly into our observation model — observations are already timestamped; threads could inherit a `last_accessed` field updated by reads.

---

---

## Decay-gated injection via `last_read` session counter

- **Source**: https://github.com/zvoque/passive-adr
- **Surveyed**: 2026-06-09

A `SessionStart` hook injects settled architectural decisions as terse one-liners; a `PostToolUse` hook updates a `last_read` session-ID counter in each ADR's frontmatter whenever the file is read. At inject time, ADRs whose `last_read` falls within a recency window are emitted verbose; cold ones collapse to one-line stubs. Records aren't evicted — just abbreviated.

**Why interesting**: A token-budget mechanism that *self-calibrates from usage* rather than a fixed TTL. Frequently-touched decisions stay verbose automatically; ignored ones quietly shrink. No curation step. Different lever from OpenLtm's score-decay — that one re-ranks; this one re-summarizes.

**Where to apply in techne**: Thread *recall* could adopt a similar hot/cold gate: at the moment we inject thread context into a session (via the `SessionStart` injection that already happens), collapse low-recency threads to their `summary` line rather than the full `resume_points` block. Pair with our existing `last_used_at`-style timestamp (or add one) and the OpenLtm-style score formula — score governs ranking, the verbosity gate governs token spend.

---

---

## Recursion guard via environment flag on self-spawned agent

- **Source**: https://github.com/zvoque/passive-adr
- **Surveyed**: 2026-06-09

Before spawning a headless `claude` subprocess to summarize the transcript, the sweep hook sets `PASSIVE_ADR_SWEEPING=1`. The hook itself checks this flag at entry and exits 0 immediately if set. The env var is inherited by the child process, acting as an in-process mutex across the fork boundary.

**Why interesting**: Prevents hook re-entrancy with zero IPC and no lock files. When the child is the same binary that fires the hook (e.g., a hook that calls `claude` which then runs `SessionStart` hooks of its own), env-var inheritance is more reliable than file locks (no stale lock cleanup, no race on lock acquisition) and simpler than PID tracking.

**Where to apply in techne**: If techne's `PreCompact` or `PostCompact` hook ever spawns a headless agent (e.g., for parent-model summarization per the deferred Cabal-style architecture), the same `TECHNE_<event>_RUNNING=1` env-var guard prevents recursive hook firing without any lock/PID infrastructure. Cheaper than the current Workflow-token recursion-protection patterns.

---

---

## Two-stage classification: heuristic → LLM tiebreaker → graceful fallback

- **Source**: https://github.com/RohiRIK/OpenLtm
- **Surveyed**: 2026-06-09

Heuristic keyword scoring runs first. If confidence < 0.6, call an LLM (their case: Haiku) with a 500-char window and 8s timeout. On *any* LLM failure, return the heuristic result unchanged. Return struct carries `{category, confidence, source}` so callers know which path fired.

**Why interesting**: Cheap local signal first, LLM as tiebreaker, *always* return something — the LLM is optional uplift, not a required dependency. The `source` field in the return makes the staging visible to consumers (debug, audit, telemetry).

**Where to apply in techne**: Overlay an LLM tiebreaker for boundary scores in the gray zone (e.g., total in 0.5–0.7), with the same hard fallback to heuristic on any LLM failure. The `source` field maps cleanly to our typed-column provenance model (`summary_llm` vs `summary_heuristic`).

---

---

## Batch entity deduplication with similarity-gated merge

- **Source**: https://github.com/mem0ai/mem0
- **Surveyed**: 2026-06-10

Named entities extracted from a batch of new memories are first deduplicated **in-process** (string-level), then embedded once, then vector-searched against the entity store; matches above 0.95 cosine similarity are merged rather than inserted.

**Why interesting**: The 0.95 threshold avoids both false-merge (near-synonyms collapsed wrongly) AND entity explosion (the same person mentioned slightly differently). No separate dedup LLM call needed — the threshold IS the dedup logic.

**Where to apply in techne**: Our `heuristic_threads.py` entity extraction is currently free-text. If we add an entity store (project names, repos, files-of-interest mentioned across threads), a similarity-gated merge would let cross-thread entity references converge without LLM cost.

---

---

## Bi-temporal supersession with retained history

- **Source**: https://gist.github.com/Jakedismo/94d9d37f6de7787bdac2a96500a28ba3
- **Surveyed**: 2026-06-10

Contradicting a claim appends a new revision with `status: superseded` and `superseded_by` pointing at the new record. The old record is never deleted, and the supersession records the *evidence episode* (which observation triggered the change). World-valid-time and system-belief-time are separately tracked.

**Why interesting**: Append-only JSONL plus a `latest revision wins` read rule gives you a full belief-history queryable at any point — counterfactual replay ("what did the agent believe at T?") becomes a filter expression, not a database design problem. No DB required; the schema is the data structure.

**Where to apply in techne**: The `knowledge` table already has `superseded_by` (we noticed this in the schema dump on session start). What's missing is (a) recording the evidence observation that triggered the supersession, and (b) bi-temporal split — currently `created_at`/`updated_at` are system time only. Adding `valid_from`/`valid_to` columns gives techne true bi-temporal queries: "what did the agent know about X *as of* session Y" rather than just "what's currently the latest." Useful for debugging hallucinations or replaying a session's reasoning.

---

---

## Candidate-expansion before rerank

- **Source**: https://github.com/getzep/graphiti
- **Surveyed**: 2026-06-10

All retrieval paths fetch `2 * limit` candidates, then apply a second-pass reranker (RRF, MMR, cross-encoder, or node-distance) to reduce to `limit`. The expansion ratio is baked into the retrieval contract.

**Why interesting**: Moves the recall-vs-precision tradeoff out of every call site and into a single retrieval contract. Reranker always has enough signal; callers don't have to reason about it.

**Where to apply in techne**: `techne search` returns a flat FTS5 BM25 list. Wrapping it in a retrieve-`2 * limit`-then-rerank layer — even just by recency or thread-proximity score — would improve result quality with no schema change.

---

---

## Context overflow auto-summarization (in-step, not background)

- **Source**: https://github.com/letta-ai/letta
- **Surveyed**: 2026-06-10

When the context window fills, the agent calls the LLM to summarize the oldest messages and replaces them in-place. The summarization happens **inline during the step**, not as a background compaction job. The caller never sees an overflow error — memory just silently compresses.

**Why interesting**: Inline summarization removes a whole class of failure modes (overflow stalls, background-job races) at the cost of a synchronous LLM call. Right tradeoff for a memory layer where reliability beats latency.

**Where to apply in techne**: Threads that defeat the structural heuristic (long-running, ambiguous boundaries) could hit an inline LLM-driven summarization fallback at session end rather than producing an unwieldy multi-thread breakdown. Pairs with the `summary_llm` typed column we already have.

---

---

## DRIFT: query-seeded iterative follow-up expansion

- **Source**: https://github.com/microsoft/graphrag
- **Surveyed**: 2026-06-10

DRIFT runs a primer query (gets an initial answer + LLM-generated follow-up questions), then recursively runs local search on those follow-ups, producing a ranked Q&A tree rather than a single context window.

**Why interesting**: Turns one query into a self-guided exploration tree before final synthesis — a *retrieval-time* strategy that needs no special index structure. Works on top of whatever store you already have.

**Where to apply in techne**: The MCP `recall` tool could spawn 2-3 auto-generated follow-up queries against its own observation store before synthesizing a final answer. Vague or broad agent queries ("what was I doing about auth?") would benefit. Adds latency, gains recall — opt-in flag, not default.

---

---

## Dual-write vector index with pluggable backend

- **Source**: https://github.com/letta-ai/letta
- **Surveyed**: 2026-06-10

Archival passages are written to SQL first, then conditionally mirrored to a vector store (Turbopuffer) based on a **per-agent** archive configuration. Embeddings are padded to a fixed `MAX_EMBEDDING_DIM` for cross-provider compatibility.

**Why interesting**: Pluggable backend selection happens at write time per agent, not at query time globally — different agents can use different vector stores within the same deployment without forking retrieval logic. Embedding-dim padding sidesteps the "what if we switch providers" lock-in.

**Where to apply in techne**: If/when techne adds semantic recall, this per-thread (or per-session) backend dispatch lets us ship SQLite-only today and swap in a vector store per thread later without changing the query interface. Already aligns with the sync-adapter pattern philosophy.

---

---

## Hierarchical community summaries (multi-resolution memory)

- **Source**: https://github.com/microsoft/graphrag
- **Surveyed**: 2026-06-10

Entities are clustered via Leiden into a hierarchy; summaries are generated bottom-up so each level abstracts the level below. Retrieval can target any level — coarse (cheap, broad) to fine (rich, slow).

**Why interesting**: Hierarchy level becomes a tunable retrieval knob. Distinct from both flat similarity search and supersession chains — it gives the caller a "zoom" parameter rather than forcing a single-resolution answer.

**Where to apply in techne**: Threads + observations could be clustered by topic at index time, with a rolling summary per cluster. Broad queries hit the summary; narrow queries fall through to raw entries. Composes with passive-adr's decay-gated injection (verbosity per resolution level) and Letta's overflow summarization.

---

---

## Pointer claims as a first-class memory kind

- **Source**: https://gist.github.com/Jakedismo/94d9d37f6de7787bdac2a96500a28ba3
- **Surveyed**: 2026-06-10

Claims are typed `value` or `pointer`. A pointer stores a re-derivation recipe (e.g. `"read package.json -> packageManager field"`) instead of the value itself. The compiler prefers the pointer; any cached value claim is treated as *just a cache*. Re-derivation cost is an explicit field on the record that drives retrieval policy.

**Why interesting**: Collapses the staleness problem for cheap-to-verify facts without deleting history. The agent no longer has to choose between "store the value" (which goes stale) and "don't store it" (which costs re-derivation every time). Both — but the system knows which is canonical and which is the cache.

**Where to apply in techne**: When techne records facts that can be re-verified in one tool call (current branch, file existence, package manager, last-N commit count), emit a pointer-style observation with the re-derivation recipe instead of a value observation. The query layer can choose: cheap, possibly-stale value vs. fresh, slightly-slower derivation. Especially relevant for the `knowledge` table where supersession is already a concept — adding `pointer` as a type alongside `decision`, `fact`, etc. fits the existing schema with no migration cost beyond a column type widening.

---

---

## Tag junction table for O(1) filtered recall

- **Source**: https://github.com/letta-ai/letta
- **Surveyed**: 2026-06-10

Tags are stored both as a JSON column on the passage row AND in a separate `PassageTag` junction table. The JSON is for display; the junction table is for filtered lookups.

**Why interesting**: Avoids the JSON-column-scan antipattern without forcing a schema migration when tag-based filtering gets added later — the dual store lets you defer "do we need fast tag queries?" indefinitely.

**Where to apply in techne**: Our `knowledge` table already has a `tags` TEXT column (comma-delimited). If tag-filtered queries ever become hot (e.g. "all decisions tagged `architecture`"), drop in a thin `KnowledgeTag` junction table without touching existing read paths or migrating data.

---

---

## Token-budget-aware multi-type context assembly

- **Source**: https://github.com/microsoft/graphrag
- **Surveyed**: 2026-06-10

Local search gathers FIVE heterogeneous context types (text units, community reports, entities, relationships, covariates) then ranks and trims **all of them jointly** to fit a single pre-defined context window. Not "per-type budgets" — one global ranking spans types.

**Why interesting**: A single priority ranking spans types so the most relevant signals win regardless of category. Per-bucket limits force suboptimal allocation when one type matters more than another for a specific query.

**Where to apply in techne**: When assembling recall context from `threads` + `observations` + `knowledge` simultaneously, rank all candidates by relevance score before truncating rather than allocating fixed token budgets per table. The `effective_summary()` priority model we already have is the seed of this idea, but applied at the wrong scope.

---

---

## Two-clock temporal model on facts

- **Source**: https://github.com/getzep/graphiti
- **Surveyed**: 2026-06-10

Every fact-bearing edge carries both `created_at` (wall clock — when the record was written) and `valid_at`/`invalid_at` (episode/document time — when the fact was true in the world). The same dual-clock applies to summarization watermarks on `SagaNode`.

**Why interesting**: Decouples ingestion order from temporal validity. Backdated or out-of-order observations don't corrupt temporal ordering — the graph stays queryable "as of" any point in episode-time regardless of when they were ingested. Concrete production implementation of the bi-temporal idea the Jakedismo gist sketches.

**Where to apply in techne**: Observations and `knowledge` rows currently use a single timestamp. Adding `event_at` (document time) alongside `created_at` (wall time) would let techne correctly handle backfilled transcripts (which we just shipped) and any future log-replay scenario. The migration is two nullable columns — cheap.

---

---

## Two-stage extract-then-reconcile LLM pipeline

- **Source**: https://github.com/mem0ai/mem0
- **Surveyed**: 2026-06-10

Extraction (*what facts exist in this message?*) and reconciliation (*how do they change stored memory?*) are separate LLM calls with separate prompts. The extractor is the only thing that reads raw messages; the reconciler operates on the extracted facts plus retrieved context.

**Why interesting**: Hard separation of concerns prevents the reconciliation prompt from hallucinating facts not present in the source — by design, the reconciler can't see the raw text. Cuts a real failure mode in compound prompts.

**Where to apply in techne**: If techne ever LLM-extracts knowledge from observations (not currently doing this), the same split — `extract(observation) → facts`, then `reconcile(facts, existing_knowledge) → updates` — keeps the reconciliation prompt small and auditable.

---

---

## `linked_memory_ids` at extraction, not retrieval

- **Source**: https://github.com/mem0ai/mem0
- **Surveyed**: 2026-06-10

The extraction LLM is instructed to emit `linked_memory_ids` — explicit pointers to related existing memories — as part of each new fact it produces. Association becomes a **write-time artifact**, not a query-time inference.

**Why interesting**: Links captured at write time are cheap to traverse at read time AND survive re-embedding or store migration. Reconstructing associations via ANN search at query time gives up both properties.

**Where to apply in techne**: Observations and knowledge entries could carry explicit `related_to` / `supersedes` IDs written by the ingestion LLM (or by the heuristic, where it can infer them — e.g., same file_path within a short gap). Gives recall a graph traversal path without a second vector query. Composes with the Graphiti two-clock model and the Jakedismo pointer-claims pattern.

---

---

## CEL-based RBAC policies for fine-grained authorization

- **Source**: https://github.com/agentgateway/agentgateway
- **Surveyed**: 2026-06-12

Both projects express authorization decisions as Common Expression Language (CEL) expressions over JWT claims + request attributes — e.g. `claims.persona == "rook" && tool.startsWith("memory.") && !tool.endsWith(".delete")`. Operators write rules in config; the gateway evaluates them at dispatch time.

**Why interesting**: Limenal's current model is `allowed_handlers` (flat list) plus `cabal_tool_extras` (server-side allowlist clamp). CEL adds a third tier — composable predicates over the (persona, tool, payload) tuple without writing Python. Standard, sandboxed, has bindings in many languages so the rules survive a future polyglot rewrite.

**Where to apply in limenal**: Add an optional `[personas.X.policy] expression = "..."` in identity.toml, evaluated at the dispatch layer in `routes.py` right after `allowed_handlers`. Use `celpy` (pure-Python CEL evaluator). Composes with existing clamping — policy is a stricter inner gate, not a replacement.

---

---

## Checkpoint-based durable execution + resume

- **Source**: https://github.com/langchain-ai/langgraph
- **Surveyed**: 2026-06-12

Every node in the agent graph writes a checkpoint of `(state, next_node, position)` to a configurable backend (in-mem, SQLite, Postgres) after it runs. If the process dies mid-graph, the next invocation passes the thread_id and resumes from the last saved checkpoint — no replay of completed nodes. The checkpoint backend is a Protocol so operators can swap SQLite for Postgres without code changes.

**Why interesting**: Cabal has Conversation + Event rows but no "resume Task from step N" path — a daemon restart mid-LLM-loop loses the in-flight tool-use chain. LangGraph's checkpoint Protocol is the canonical shape: save after each step boundary, key by stable thread_id, expose a `resume(thread_id)` entry point. The Protocol-over-impl approach matches your existing CredentialResolver + TraceClient patterns.

**Where to apply in cabal**: Add `CheckpointStore` Protocol with SQLite default impl. `LLMPersona.handle_task` writes a checkpoint after each tool-use step. Conversation row gains `checkpoint_id`. New `/admin/tasks/{id}/resume` endpoint replays from last checkpoint. Pairs with the bulletin-board generation work — resuming on a *different generation* of the same persona is a clean failure-recovery story.

---

---

## Document-librarian persona (built-in RAG)

- **Source**: https://github.com/run-llama/llama_index
- **Surveyed**: 2026-06-12

Indexing a folder/repo into a queryable corpus is a built-in agent role rather than something each handler bring its own. The librarian persona owns the index lifecycle (ingest, refresh, evict) and exposes a single `query(question, top_k)` interface to other agents. Other agents call it the same way they'd call any tool; the librarian handles chunking, embedding, retrieval, and rerank internally.

**Why interesting**: Cabal's Archivist persona uses techne for memory of past *sessions*. There's no equivalent for memory of a *document corpus* (a codebase, a docs site, a folder of PDFs). LlamaIndex's pattern — make it a peer persona, not a tool every handler reimplements — slots cleanly into cabal's persona model. Pairs with the CodeGraph-Rust watch ingest above for keeping the index fresh.

**Where to apply in cabal**: New `Librarian` persona in `cabal.personas.librarian`. Identity.toml gains `[personas.librarian.corpus]` block declaring source directories. Backed by techne for storage (FTS5 + vec0). Exposes via the same `tool_proxy` path that limenal-backed personas use. The dispatch router treats it like any other persona; clients call it via `cbl dispatch librarian "..."`.

---

---

## File-watch with debounce for continuous code-corpus ingest

- **Source**: https://github.com/Jakedismo/codegraph-rust
- **Surveyed**: 2026-06-12

A filesystem-watch process emits per-file change events; a debouncer collapses bursts (saves during a search-replace, file format on save) into single ingest jobs. Embeddings are recomputed and the graph is patched in-place — never a full re-index. A background resync loop catches drift between watch events that got dropped (sleep on a low-priority thread, weekly full rebuild).

**Why interesting**: Code agents need a fresh corpus to be useful but full re-indexing on every edit is the wrong shape. Watch + debounce + background resync is the standard "warm cache, periodic rebuild" pattern applied to embeddings. The 2-tier (event-driven incremental + background full) is the resilience layer — events get lost, the full sweep catches up.

**Where to apply in techne**: Currently techne ingests via hooks (Claude Code's `PostToolUse`, etc.). A `techne watch <dir>` mode would let standalone runtimes (per [techne#10]) keep a code corpus current without a hook-emitting host. Most useful for the planned "document librarian" persona in cabal — point at a repo, get a continuously-fresh memory of it.

---

---

## Git-aware diff streaming in chat UI

- **Source**: https://github.com/Aider-AI/aider
- **Surveyed**: 2026-06-12

For code-editing agents, the stream the operator sees is *diffs* (file:linenumber + colored hunks), not raw assistant text. Aider's coder generates `SEARCH/REPLACE` blocks, applies them, and renders the resulting diff in the terminal as it streams. A failed edit (search block didn't match) is shown inline with the actual file content for context, not buried in an error message.

**Why interesting**: For coding personas this is a massively better operator experience than "wall of text containing a code block." Diff is the natural unit. Cabal's `cbl chat` is text-only today — fine for archivist, weak for any persona that edits code. Worth noting: aider's `EditBlockCoder` is one of several edit formats it supports; the format is per-model.

**Where to apply in cabal**: Add an `EditFormat` declaration to persona config (`identity.toml`: `edit_format = "search-replace" | "udiff" | "whole-file" | "none"`). When set, `cbl chat` interprets the assistant's response through the corresponding parser and renders as a diff. The Architect persona (code-focused) gets `udiff`; archivist stays `none`. Doesn't require any LLM changes — it's a render-layer addition.

---

---

## Hybrid rank fusion: BM25 + vector + graph traversal via RRF

- **Source**: https://github.com/rohitg00/agentmemory
- **Surveyed**: 2026-06-12

Three independent recall channels run in parallel against the same observation set: lexical (BM25 over FTS), semantic (vector cosine over embeddings), and structural (graph traversal over an entity-relation overlay). Results are fused via Reciprocal Rank Fusion — `score = sum(1 / (k + rank_i))` across channels — so each channel contributes proportionally to where in its ranking the candidate landed. Mnemosyne does a fixed-weight variant (50% vector + 30% BM25 + 20% importance); RRF is the channel-agnostic version that doesn't need hand-tuned weights.

**Why interesting**: Each channel has a different failure mode — BM25 misses synonyms, vector misses exact-token requirements, graph misses orphan nodes. Fusion smooths over the failure cases without committing to one channel as primary. RRF specifically doesn't require the channel scores to be on the same scale, which matters because BM25 scores are negative-to-zero and vector cosines are 0..1.

**Where to apply in techne**: `core/search.py` currently does FTS-or-vector branched on `use_vectors=True`. Replace with parallel-fetch-then-fuse: each branch returns top-K candidates with their ranks, RRF merges, return the fused top-N. The graph channel is a future add (you'd need an entity overlay first, which the bi-temporal work in [techne#8] would expose). Two-channel RRF on FTS + vector is the cheap immediate upgrade and corroborates [[architectural-patterns#Composite observations]] re: read-time ranking.

---

---

## Late-binding task definitions (event-driven flows)

- **Source**: https://github.com/crewAIInc/crewAI
- **Surveyed**: 2026-06-12

Tasks are defined as standalone units with declared inputs / outputs / required tools, NOT bound to a specific agent at definition time. At dispatch, the broker matches tasks against available agents by tool overlap, cost budget, and current load — the same task can run on different agents on different days. Flows are composed by emitting events; downstream tasks subscribe to event types rather than being called by name.

**Why interesting**: Cabal's dispatch is target-bound today (`cbl dispatch archivist "..."` — the operator picks the persona). Late-binding decouples *what needs to be done* from *who does it*, which is the natural fit for cost-aware routing ("this task could run on Haiku-via-archivist or Sonnet-via-librarian, pick the cheaper"). Lines up with BudgetGuard which already has the cost data.

**Where to apply in cabal**: Add a `Capability` declaration to persona config (`identity.toml`: list of capability tags). Tasks gain an optional `required_capabilities` field. Dispatch router gains a `select_target` step that filters personas by capability match, ranks by BudgetGuard remaining + CircuitBreaker open/closed, picks. Keep target-explicit dispatch as the override path. Doesn't break the A2A protocol — `target` becomes optional when capabilities are specified.

---

---

## OAuth 2.0 passthrough federation at the handler boundary

- **Source**: https://github.com/IBM/mcp-context-forge
- **Surveyed**: 2026-06-12

Handlers declare their auth model in the capability card; the gateway federates OAuth token exchange to the upstream (GitHub, Slack, Notion) instead of storing client secrets centrally. The gateway holds the OAuth client config; the *user's* token comes from a per-session exchange.

**Why interesting**: For delegated-auth upstreams (anything that wants a user token, not a service token), a central credential broker like Cabal can't actually mint the right thing — it can store a PAT but not a user-scoped OAuth token. Federation lets per-user identity flow end-to-end without the broker holding a user-impersonating secret.

**Where to apply in limenal**: Add an `auth: oauth` variant to `CapabilityCard.credential_keys`. The resolver returns OAuth client metadata (client_id + authorize URL) instead of a static secret; the handler does the user-token exchange on first use and caches per-session. Cabal stays the source for service tokens; OAuth handlers escape the broker for user tokens.

---

---

## Persistent credential-access audit trail

- **Source**: https://github.com/samanhappy/mcphub
- **Surveyed**: 2026-06-12

MCPHub backs its credential cache with Postgres and logs every access: `(timestamp, requester, scope, cache_hit, value_hash)`. Compliance answer to "who pulled the `archivist/slack` credentials in the last 30 days?" is a SQL query, not "grep production logs."

**Why interesting**: Limenal's `CabalCoreCredentialResolver` is in-memory TTL only — no audit trail. Cabal already has an `Event` table for the broker; piping resolver hits into it costs one row per fetch and gives compliance + leak-detection answers for free.

**Where to apply in limenal/cabal**: In the resolver, emit a `credential.fetched` event after each successful fetch carrying `(persona, handler, requester_jti, cache_hit)`. No schema change — `Event.payload_json` is already JSON. Cheap; high signal for incident response.

---

---

## Relation confidence with half-life decay

- **Source**: https://github.com/gannonh/memento-mcp
- **Surveyed**: 2026-06-12

Relations between entities carry a `confidence` (0..1) and a `last_confirmed_at` timestamp. A query-time decay function applies a 30-day half-life: `effective_confidence = confidence * 0.5^(days_since_confirmed / 30)`. Edges that haven't been re-confirmed by new observations fade out gracefully instead of being deleted; a re-observation bumps `last_confirmed_at` and restores full weight.

**Why interesting**: Distinct from OpenLtm's record-level decay because it operates on *relationships*, not individual facts. Entity A → relation R → entity B can stay in the graph indefinitely; what fades is the system's confidence that R is still true. Combines naturally with bi-temporal modeling — `valid_at` says when the relation became true, `last_confirmed_at` says when we last saw evidence for it.

**Where to apply in techne**: Techne's knowledge entries already carry a `confidence: low/medium/high` field that doesn't decay. Replace with a numeric `confidence: float` + `last_confirmed_at` timestamp; expose `effective_confidence(now)` as a read-time helper. Pairs with the [techne#8] bi-temporal work. Pure addition; existing data migrates by stamping `last_confirmed_at = created_at` and mapping enum → numeric.

---

---

## Semantic tool routing via vector search

- **Source**: https://github.com/samanhappy/mcphub
- **Surveyed**: 2026-06-12

MCPHub federates many backend MCP servers behind one endpoint and routes tool calls by *semantic match* rather than prefix. An agent asks "send a message to the team" and MCPHub embeds the request, looks it up against tool descriptions across all registered backends, picks the winner.

**Why interesting**: Collapses N-handler discovery into one semantic query — the agent doesn't have to know the namespace map (`slack.send_message` vs `mail.send` vs `notify.webhook`). Composes cleanly on top of static prefix dispatch as an optional layer.

**Where to apply in limenal**: Add an opt-in `tools/discover` JSON-RPC method that embeds the request and ranks tools across all registered handlers. Keeps the deterministic `handler.tool` contract intact — discover is a router, not a replacement. Re-uses techne's existing embedding stack (`sqlite-vec` + `fastembed`) since the corpus is tiny.

---

---

## AbortGraph: transitive parent→child cancellation across orchestration trees

- **Source**: https://agentafk.com/
- **Surveyed**: 2026-06-13

A dedicated `AbortGraph` data structure tracks parent–child session relationships across nested subagent / skill / tool invocations. Cancelling a parent propagates transitively to every descendant — the depth doesn't matter, no manual signal wiring needed at each layer. Pairs with `AbortCoordinator` which routes the cancellation signal through the graph.

**Why interesting**: Most "abort" implementations either cancel only the immediate caller or rely on every layer remembering to chain `AbortSignal` through. The graph model makes cancellation a property of the orchestration tree rather than a discipline each layer has to maintain. When a user cancels at the top, every nested LLM call, tool execution, and subagent run hears about it without the implementer of layer N having thought about it. Different from a `CancellationToken` chain because the graph is observable — you can see the live tree of in-flight work.

**Where to apply in techne / cabal**: Techne's hook lifecycle currently operates per-session; nothing today cancels in-flight memory writes if the parent session dies. Bigger fit: cabal's persona dispatch can spawn nested tool-use loops via limenal handlers — an `AbortGraph` is the right shape for "operator hits Ctrl-C, all the way down stops cleanly." Pairs with [[architectural-patterns#Checkpoint-based durable execution + resume]]: abort writes a checkpoint, resume picks up from it.

---

---

## Debounced async persistence for high-frequency writes

- **Source**: https://github.com/open-webui/open-webui
- **Surveyed**: 2026-06-13

Yjs collaborative-doc updates enqueue a write task with a 0.5s debounce. Only the *last* update in a burst hits the database; intermediate states exist only in-memory. No message queue, no Redis dependency, just a task reference + timer per document. Crash window: up to 500ms of unsaved state, intentional tradeoff.

**Why interesting**: Decouples write frequency from storage I/O without pulling in a full message-queue dependency. Usable in single-process deployments where Redis Streams or RabbitMQ would be overkill. The "only the last update" semantics is the key — for state that's *idempotent on the latest value* (a doc snapshot, a thread summary, a cursor position), batching is free: intermediate writes are pure waste. Different from "batch every N seconds" because it batches on *quiescence*, not on a fixed window.

**Where to apply in techne**: Techne's structural heuristic fires on every observation event and rewrites thread state. For long sessions with many rapid hook firings, the per-observation commit is overhead. A 500ms debounce on the "thread state changed" write (with the observation INSERT still synchronous) batches the cumulative state recalc into one SQLite commit at quiescence. Pairs with [[architectural-patterns#Context overflow auto-summarization (in-step, not background)]] — debounced write is the cousin to inline-on-overflow: both compress the work to the moment it's actually needed.

---

---

## Declarative schema reconciliation with auto-column diffing

- **Source**: https://github.com/nousresearch/hermes-agent
- **Surveyed**: 2026-06-13

On startup, the agent diffs live SQLite columns against the canonical `SCHEMA_SQL` constant and issues `ALTER TABLE ADD COLUMN` for any gaps. No versioned migration files, no Alembic, no migration sequencer — for additive changes the schema definition *is* the migration. Drops/renames/type changes still need explicit work, but those are rare; the 90% case (adding a nullable column) becomes free.

**Why interesting**: Eliminates an entire class of migration debt for append-only schema evolution. Most SQLite-backed agents either ship versioned migration files (which then need to be ordered, tested, and replayed) or just `CREATE TABLE IF NOT EXISTS` and pray. Auto-reconciliation is the middle path: the schema constant is the source of truth at every startup, and additions are detected and applied automatically. Different from "just recreate the table" because existing data is preserved; different from Alembic because there's no version table to maintain.

**Where to apply in techne**: Techne's SQLite schema in `techne/core/database.py` could adopt this for additive changes — adding a new column to `threads`, `observations`, or `knowledge` would require only updating the `SCHEMA_SQL` constant, not authoring and sequencing a migration file. Composes naturally with the existing `CREATE TABLE IF NOT EXISTS` style. Keep explicit migrations for destructive changes; auto-reconcile handles the rest. Particularly useful as the bi-temporal / pointer-claims patterns ([[architectural-patterns#Two-clock temporal model on facts]], [[architectural-patterns#Pointer claims as a first-class memory kind]]) get layered in — each adds nullable columns to existing tables.

---

---

## Deterministic bucket-hash IDs for idempotent compaction

- **Source**: https://github.com/melandlabs/openloomi
- **Surveyed**: 2026-06-13

Records are bucketed for summarization by hashing `(userId | summaryTier | groupId | endTimestamp)` using a stable djb2-style hash. Summary IDs are deterministic, so a compaction cycle that fails halfway produces the same IDs on re-run and the upsert naturally deduplicates. No UUID generation, no separate dedup table, no retry log.

**Why interesting**: Idempotency comes from the ID derivation, not from a tracking table. A failed cycle doesn't need to remember what it did — the next run reconstructs the same IDs from the same input and the upsert is the dedup. Distinct from a content hash (which deduplicates *identical content*); this is a *bucket* hash that deduplicates *same logical work*. Cheap to add, eliminates a whole class of retry/race bugs.

**Where to apply in techne**: Techne's heuristic thread detection and any future compaction could use the same bucket-hash approach to make runs idempotent without a separate dedup table. Specifically: when `heuristic_threads.py` boundaries a thread, derive the thread ID from `(session_id, first_observation_id, boundary_signal)` instead of a random UUID — re-running the heuristic on the same observation stream produces the same thread, so backfill replay (which we just shipped) becomes safe by construction. Pairs with [[architectural-patterns#Bi-temporal supersession with retained history]] for replay correctness.

---

---

## Frontmatter-declared pip deps in user plugin code

- **Source**: https://github.com/open-webui/open-webui
- **Surveyed**: 2026-06-13

User-supplied tool/function code embeds YAML-like frontmatter inside the module docstring listing pip requirements. The loader parses the frontmatter, dedupes the dep list, pip-installs anything missing, then `exec`s the code in a fresh `ModuleType` namespace with import-path rewriting (`from utils...` → `from open_webui.utils...`). A content-hash cache means re-installation is skipped on unchanged code.

**Why interesting**: Collapses plugin manifest + dependency spec + code into one artifact with zero separate config files. The plugin contract survives copy-paste distribution — you can share a single `.py` file and the host bootstraps it correctly. Different from a `requirements.txt`-per-plugin because there's no second file to keep in sync with the code. The import-path rewriting is the lever that lets plugin code reference the host's utilities without the plugin needing to know the host's install layout.

**Where to apply in techne / cabal**: Techne sync adapters could embed their pip deps in module frontmatter so the MCP server auto-installs adapter requirements on first use rather than maintaining a global `requirements.txt`. Bigger fit: cabal personas declared via `identity.toml` could point at a `.py` skill file whose frontmatter declares its own deps — the daemon installs on first dispatch. Composes with [[architectural-patterns#Capability probing with fallback dispatch]] (probe + frontmatter together let an adapter declare what it *wants* and the host verifies what it *got*).

---

---

## Intent-gated safety interruption (lexical pre-check before confirm)

- **Source**: https://github.com/abhinand5/pi-setup
- **Surveyed**: 2026-06-13

Before blocking a destructive action with a "are you sure?" dialog, the extension checks whether the user's **most recent message** contained explicit destructive-intent verbs matching the flagged operation (e.g., user said "delete the foo.py" → tool wants to delete `foo.py` → skip the confirmation). Purely lexical match, no second LLM call. The signal lives in the user's stated intent.

**Why interesting**: Cuts confirmation-prompt fatigue without giving up the safety net entirely. Distinct from blanket "skip confirmations" flags because the gate is per-call and grounded in the actual user utterance. Fast and predictable — no model variance, no latency. The bypass only fires when the user's intent already covers the operation; ambiguous cases still prompt. Different from [[architectural-patterns#CEL-based RBAC policies for fine-grained authorization]] which decides on persona+tool; this decides on *current-turn intent*.

**Where to apply in techne**: Techne's hook layer could expose `last_user_message` to the destructive-tool gate so a "delete observation 42" tool call that follows a user message saying "delete observation 42" doesn't double-prompt. Bigger fit in cabal's dispatch: when an operator types `cbl dispatch X "rm the stale conversation"` the resulting tool call's destructive-confirm could check the dispatch prompt for the matching intent.

---

---

## Per-file promise-chain mutation queue (zero-dep file write serialization)

- **Source**: https://github.com/earendil-works/pi/tree/main/packages/coding-agent
- **Surveyed**: 2026-06-13

A `Map<string, Promise<void>>` keyed by `realpath` serializes concurrent writes to the same file while letting writes to different files run in parallel. New writes attach to the tail of the existing chain via `.then()`; completion auto-removes the entry from the map to prevent leaks. No `Mutex` class, no `async-mutex` dep, no `fs.open` exclusive flag, no lock file — just promise chaining as the synchronization primitive.

**Why interesting**: The cleverness is using `realpath` resolution to dedupe symlinks (two paths pointing at the same inode share a queue entry) without needing any OS-level locking. The auto-cleanup on tail completion is the tricky part — most "queue per key" implementations leak map entries forever. Different from a global write queue because parallel writes across files aren't artificially serialized. Different from a per-call lock because the *chain order* matches arrival order without needing a separate scheduler.

**Where to apply in techne**: Techne's SQLite writes are already serialized by SQLite itself, but the planned `techne watch <dir>` mode (see [[architectural-patterns#File-watch with debounce for continuous code-corpus ingest]]) writes corpus files. If that ever races with other writers, this pattern serializes per-file without a lock file. Direct fit in any techne tool that writes a config snapshot or export artifact and could be invoked concurrently from multiple hook firings.

---

---

## Pydantic `$defs` flattening for cross-provider tool schemas

- **Source**: https://github.com/khoj-ai/khoj
- **Surveyed**: 2026-06-13

Converts a Pydantic model's JSON schema into a fully self-contained tool definition by resolving all `$ref`/`$defs` enum references inline, eliminating external schema dependencies before sending to any LLM provider.

**Why interesting**: Provider APIs (Anthropic and Gemini especially) reject schemas with `$defs` references; this normalization layer makes Pydantic models usable as tool schemas across all providers without per-provider shims.

**Where to apply in techne**: If techne exposes MCP tools defined as Pydantic models, this flattening step ensures schemas remain valid across any client that consumes them.

---

---

## Reference-based session handoff (pointers, not content)

- **Source**: https://github.com/abhinand5/pi-setup
- **Surveyed**: 2026-06-13

The `handoff` skill produces an inter-session continuity document that points to existing artifacts (PRDs, diffs, issue URLs, commit SHAs, MCP resource paths) **by reference** rather than inlining their content. The handoff is written to the OS temp dir to avoid polluting the workspace. On next session start, the agent dereferences the pointers to rehydrate context as needed.

**Why interesting**: Most memory systems copy or summarize prior context, which means (a) the handoff bloats with each compaction and (b) the stored copy goes stale relative to the canonical source. Storing pointers fixes both — the handoff stays token-light and always reflects the *current* state of the referenced artifact. The cost is one extra read at rehydrate time. Composes tightly with [[architectural-patterns#Pointer claims as a first-class memory kind]] — same principle at the session level rather than the claim level.

**Where to apply in techne**: Techne's thread compaction or session-start injection could emit a pointer manifest (file paths + git SHAs + MCP URIs + observation IDs) instead of full summaries. Stored memory shrinks, freshness is automatic. Particularly relevant for the resume-after-restart path where the in-flight thread's referenced files may have moved on. Pairs with [[architectural-patterns#Streaming context scrubber: stateful tag excision across chunk boundaries]] if the rehydrate step ever streams.

---

---

## Schema-as-source-of-truth for tool risk classification

- **Source**: https://agentafk.com/
- **Surveyed**: 2026-06-13

Tool definitions carry `category`, `concurrencySafe`, and `riskClass` fields directly on the definition. The runtime derives `SAFE_TOOLS`, `WRITE_TOOLS`, and read/write predicates *from the schema* at startup rather than maintaining parallel allowlists. Adding a new tool forces the developer to declare its risk class at the definition site — the registry is the source of truth and permission enforcement is a view over it.

**Why interesting**: Eliminates a whole drift class — the bug where the tool registry has 12 tools but the "writes allowed" list has 9 because someone added a new tool and forgot the second update. The declaration is colocated with the tool, so reviewers see it during PR review. Different from a separate decorator list because the data lives on the canonical tool definition and is queryable as a single registry rather than scattered across modules. Composes with [[architectural-patterns#CEL-based RBAC policies for fine-grained authorization]] — CEL rules can reference `tool.riskClass` directly without restating the classification.

**Where to apply in techne / limenal**: Techne's MCP tool registrations could carry a `riskClass` (read/write/destructive) directly on the tool spec. Hooks and the sync adapter then gate on `tool.riskClass != "read"` rather than maintaining a hardcoded list. Bigger payoff in limenal: handler tool definitions already declare a name + schema; adding `risk_class` and `concurrency_safe` means the dispatch layer in `routes.py` can derive the rate-limit / circuit-breaker scope from the definition rather than a parallel config file.

---

---

## Score-gated tiered memory with pluggable storage adapter

- **Source**: https://github.com/melandlabs/openloomi
- **Surveyed**: 2026-06-13

Records flow `short → mid → long` based on a weighted retention score (recency 35%, access-count 30%, importance 25%, media 10%, pinned boost). When a cohort ages past a threshold, an LLM summarizer compresses the group into a `MemorySummary` and the raw details are archived. The forgetting engine is decoupled from persistence via a `MemoryStorageAdapter` interface so the same logic runs over IndexedDB in the browser and (presumably) Postgres/SQLite on the server. The cycle is lock-guarded and dry-run capable.

**Why interesting**: The adapter boundary is enforced purely through TypeScript interfaces with no framework coupling — the engine has no `import` from any storage backend. Different from a thin repository wrapper because the engine actually owns multi-step transactions (score → group → summarize → archive) that the adapter executes piecewise. Dry-run mode makes the policy tunable without touching real data. Composes with [[architectural-patterns#Hybrid rank fusion: BM25 + vector + graph traversal via RRF]] — the score formula could feed RRF as a fourth channel.

**Where to apply in techne**: Techne's SQLite-backed thread store could adopt the same `MemoryStorageAdapter` contract to let the forgetting/compaction engine run identically against SQLite, a test in-memory store, or a future remote backend without touching engine logic. Closest fit is `techne/core/threads.py` where status transitions (active/completed/stale) already do half the job — formalizing the engine + adapter split would let us swap the SQLite backend for a per-thread vector store (per [[architectural-patterns#Dual-write vector index with pluggable backend]]) without rewriting the lifecycle.

---

---

## Session visibility tiering via SQL CASE on existing markers

- **Source**: https://github.com/nousresearch/hermes-agent
- **Surveyed**: 2026-06-13

A single SQL query with a CASE expression classifies sessions as listable, hidden-ephemeral (subagent runs, compression continuations), or branch children — deriving the visibility tier from existing markers (`_branched_from` JSON field, parent `end_reason`, `parent_session_id`) rather than adding a `session_type` enum column. The schema stays minimal; the UX filter is emergent.

**Why interesting**: Encoding visibility as a derived property of existing relational signals avoids the classic "we need a status enum" sprawl. Distinct from a simple computed column because the classification depends on parent-row state (compression chains and subagent runs only look ephemeral *relative to* their parent's `end_reason`). The CASE query is also self-documenting — anyone reading it can see exactly which markers drive the visibility decision.

**Where to apply in techne**: Techne's thread detection currently uses status transitions (`active → completed → stale`). When listing threads for `techne search` or session-start injection, a similar derived-visibility CASE could distinguish "real threads" from "boundary-detection artifacts" or "single-observation noise threads" without adding a `thread_type` column. Pairs with [[architectural-patterns#Deterministic bucket-hash IDs for idempotent compaction]] — both rely on the principle that the right primary signals are already in the schema.

---

---

## Sink-boundary credential redaction in structured telemetry

- **Source**: https://agentafk.com/
- **Surveyed**: 2026-06-13

Pattern-based redaction (`sk-*` API keys, `KEY=value` env-style pairs, Telegram bot token shapes) is applied at the telemetry sink — the single write path to disk — rather than at each call site. Tool calls, error messages, and structured event payloads all pass through the same redactor before serialization. New tools get credential safety for free.

**Why interesting**: Most projects sprinkle `redact()` calls through individual loggers, which guarantees that the next new logger forgets one. Centralizing at the sink means: (a) one place to update when a new credential shape appears, (b) impossible to bypass without writing to a different sink, (c) the redaction rules are auditable in one file. The shape generalizes — any structured output stream (JSONL traces, SQLite blobs, exported reports) wants the same sink-side filter.

**Where to apply in techne**: Techne's observations write through a single SQLite store; adding a sink-boundary redactor before the `INSERT` into `observations.content` would prevent API keys / OAuth tokens from persisting if an agent ever logs them by accident. Single-function add in `techne/core/database.py` write path. Composes with limenal's [[architectural-patterns#Persistent credential-access audit trail]] — sink-side redaction on the audit log itself means the audit trail can't accidentally leak the thing it's tracking.

---

---

## Streaming context scrubber: stateful tag excision across chunk boundaries

- **Source**: https://github.com/nousresearch/hermes-agent
- **Surveyed**: 2026-06-13

A small state machine strips `<memory-context>` tags from streaming LLM output, correctly handling the case where the tag is split across SSE chunk boundaries. The scrubber buffers only the open-tag-prefix worth of bytes (not the full response), tracks whether it's currently inside a tag, and emits cleaned bytes downstream as soon as it can prove they're safe.

**Why interesting**: Most streaming pipelines hit this and pick wrong: either buffer the entire response (kills streaming latency, defeats the point) or naive regex per chunk (misses tags split at the boundary, leaking raw XML to the user). The boundary-aware state machine is the minimal correct solution — buffers only as much as the longest possible partial match. The same shape applies to *any* fenced metadata in a streamed response (thinking blocks, tool-call markup, internal prompts).

**Where to apply in techne**: If techne ever streams memory-context blocks back through the MCP server or into a stdout display, this pattern avoids leaking raw XML/tag content without forcing full-response buffering. Immediate fit: any future MCP `recall` that streams large summaries with internal markup. Also reusable inside the techne `<<<memory>>>` fence stripping if that ever needs to happen on a streaming source rather than a fully-buffered string.

---

---

## Tool-call grouping for parallel execution under role-alternation constraint

- **Source**: https://github.com/khoj-ai/khoj
- **Surveyed**: 2026-06-13

Clusters simultaneous tool calls under a single assistant message followed by a single aggregated tool-results user message, maintaining the required alternating-role message structure even when multiple tools fire in one turn.

**Why interesting**: Most codebases either serialize tool calls (losing parallelism) or produce malformed message lists; this pattern preserves parallel execution semantics while satisfying strict role-alternation constraints that LLM providers enforce.

**Where to apply in techne**: Techne's thread detection and history reconstruction could adopt the same grouping contract so that parallel hook executions within one lifecycle event are stored as a single atomic turn rather than fragmenting the thread boundary.

---

---

## Typed extension sidecar entries in the session log

- **Source**: https://github.com/earendil-works/pi/tree/main/packages/coding-agent
- **Surveyed**: 2026-06-13

Sessions are append-only NDJSON. Each line is a typed `SessionEntry`: `message`, `compaction`, `branch_summary`, `model_change`, etc. Extensions persist their own opaque typed payloads as `CustomEntry` rows (with a `customType` discriminator), **completely separate from LLM context**. On reload, each extension scans the log for its own entries and reconstructs internal state. The `fromHook` flag on first-class entries follows the same shape — hook-generated rows are tagged for filtering.

**Why interesting**: Gives extension authors durable per-extension state without spinning up a separate DB, defining a schema, or coupling to the core data model. The discriminator keeps payloads opaque to the core — extensions can change their internal schema without touching the parent. The "scan log for own entries" pattern is cheap because NDJSON reads are linear and the discriminator is the first key. Different from a key-value sidecar table because the *temporal ordering* of state changes is preserved, which matters for replay and audit.

**Where to apply in techne**: Techne's hooks currently have no durable per-hook state — if a hook needs to remember "I last fired at observation N," that lives in process memory. Adding a `hook_state` row type with `(hook_name, payload_json)` lets each hook persist its own state with no schema change. Composes with [[architectural-patterns#Declarative schema reconciliation with auto-column diffing]] — the discriminator field is enough; no per-hook column needed. Also a clean home for sync-adapter cursors (`(adapter_name, last_synced_observation_id)`).

---

---

## Bandit-in-the-gateway: variant weights driven by feedback

- **Source**: https://github.com/tensorzero/tensorzero
- **Surveyed**: 2026-06-14

Variant weights in `tensorzero.toml` are updated by an offline bandit optimizer that reads from a feedback table (each row: `(inference_id, metric_name, value)`) and rewrites the config file. New weights take effect on the next request. The gateway itself becomes the bandit's arm-selection mechanism — the application calls `function("sherlock")` and the gateway picks the variant that's currently winning. Multi-armed bandit policy lives in the optimizer; arm execution lives in the gateway.

**Why interesting**: Pushes the experimentation loop *down* into the LLM infrastructure layer rather than living in the application. App code never changes; quality improvements ship as variant additions + weight updates. Distinct from "track which prompt won and manually update" because the loop closes automatically — feedback in, weights out, gateway routes accordingly. The feedback endpoint is the missing piece most LLM stacks lack.

**Where to apply in cabal**: Probably **defer** as a V1+ enhancement, but the *infrastructure shape* should exist from day one so the loop can close later. Specifically: (a) thread `episode_id` everywhere (see above); (b) the investigation-engine exposes a `feedback_for_task(task_id, metric, value)` endpoint — operator marks "this investigation was useful" or "this missed the bug"; (c) feedback rows go into cabal's existing observability store. The bandit optimizer can wait until there's actually variance worth optimizing. Pairs with [[architectural-patterns#Confidence-gated auto-resolution with human escalation queue]] — both are "loop in human feedback to improve the system over time" with different escalation models.

---

---

## Behavioral drift measurement over existing transcript artifacts

- **Source**: https://github.com/Poorna-Repos/opus-fable-mode
- **Surveyed**: 2026-06-14

Rather than instrumenting new telemetry, the script mines the JSONL session logs Claude Code already writes (`~/.claude/projects/**/*.jsonl`), classifies messages into time-bucketed model cohorts (`opus_pre`, `opus_post`, `fable`), and computes four behavioral metrics: verbosity (word count percentiles per prose message), tool-to-prose ratio, unsolicited-caveat frequency, and self-actor opener rate. Results suppressed as "insufficient" until the post-cohort bucket holds at least 30 prose messages — explicit floor before drawing conclusions.

**Why interesting**: Demonstrates the session logs already carry enough structure (content block types, text vs. tool_use classification, opener phrase patterns) to detect subtle behavioral shifts without adding instrumentation. The cohort-bucket-then-compare approach is the classic A/B shape applied to *agent behavior over time* — model swaps, prompt changes, hook additions all become measurable as cohort transitions. The explicit "insufficient data" floor is the discipline most behavioral comparison scripts skip; it stops you from declaring a trend on five samples.

**Where to apply in techne**: Techne's observation/thread records already capture content-block-level structure. A small `techne metrics` subcommand that buckets observations by (session-start-date, model fingerprint, hook config hash) and reports verbosity / tool-use rate / opener distribution would surface agent behavioral drift as a health signal — useful for detecting "did this prompt change actually help?" without dedicated benchmarks. Composes with [[architectural-patterns#Bi-temporal supersession with retained history]] — bi-temporal queries let you ask "what did the agent look like *as of* T" by replaying the buckets at that point.

---

---

## Claude Code slash-command surface as the platform UX

- **Source**: https://github.com/paca-ai/paca
- **Surveyed**: 2026-06-14

Paca ships Claude Code skills (slash-commands) that map natural-language editor input to platform mutations: `/paca-epic` creates an epic, `/paca-sprint` plans a sprint, `/paca` is the catch-all entry. The platform inverts the usual integration direction — instead of the platform calling out to Claude as a tool, the platform *embeds Claude Code as its primary UX surface*. No standalone chat UI; the editor IS the chat UI.

**Why interesting**: Skips the "build a chat frontend" cost entirely. The operator already uses Claude Code; the platform just ships skills that make it the entrance. Distinct from a CLI tool because slash-commands carry natural-language context, not just args. Different from an MCP server because slash-commands are operator-facing UX, not agent-facing tools — they're the human entrypoint. Composes well with MCP: slash-commands invoke the platform's MCP tools internally; operator sees a polished UX, agent sees a clean tool surface.

**Where this does NOT fit our platform**: Initially recommended for cabal; **rejected** 2026-06-14 per [[feedback-cabal-standalone-terminal]]. Cabal's primary UX is its own agentic terminal app (`cbl`, with Rook as the default persona), not a slash-command surface inside Claude Code. The pattern assumes the host editor IS the platform UX — which collapses when that editor isn't running, when the operator wants to reach cabal from a server / iPhone / Home Assistant, or when other MCP clients (openwebui, future remote-MCP) want a first-class entrypoint. Cabal exposes Claude-Code-callable functionality via limenal's MCP gateway instead; that path serves Claude Code AS one client among many rather than embedding cabal inside it. Pattern kept here because it's still architecturally interesting for projects whose primary UX legitimately *is* a single editor (paca-ai's case); future surveys should still flag it.

---

---

## Confidence-gated auto-resolution with human escalation queue

- **Source**: https://github.com/incidentfox/self-learning-ai-agent
- **Surveyed**: 2026-06-14

Before inserting new knowledge, the system embeds the candidate, finds semantically similar existing items, then asks an LLM to classify the relationship as one of {duplicate, supersede, contradict, complement, unrelated} with a confidence score. **Confidence > 0.8 → auto-apply.** Confidence ≤ 0.8 → enqueue for human review with the full context (new fact, conflicting facts, LLM's reasoning). The store never gets silent contradictions, and the operator never has to triage easy cases.

**Why interesting**: A concrete shape for the harder design question — "when should an agent's judgment be the final word, and when does a human need to see it?" The threshold makes the bar explicit and tunable. Distinct from "always log + human-review" (high friction) and "always auto-apply" (silent corruption). Composes with [[architectural-patterns#Bi-temporal supersession with retained history]] — auto-applied supersessions still record the evidence episode, so a later human review of the queue can still inspect what happened.

**Where to apply in techne**: Techne's `knowledge` table already supports supersession. Adding an LLM-classifier step before upsert (with a configurable confidence threshold) lets techne reconcile new observations against existing knowledge without overwriting. The escalation queue maps to a new `knowledge_pending_review` table (or a flag on `knowledge`). Composes with techne#17 (sink-side redaction) — the LLM only sees redacted text. Pairs naturally with the architectural-patterns survey work itself: surveying a new URL produces "new pattern" candidates that may conflict with existing ones; a confidence-gated dedup could replace the current manual `[[wikilink]]` composition step.

---

---

## Config-driven function/variant schema (prompts as config artifacts)

- **Source**: https://github.com/tensorzero/tensorzero
- **Surveyed**: 2026-06-14

Each "function" in `tensorzero.toml` declares a typed input schema and (optionally) a structured output schema. Each function has named **variants** — each variant maps to a specific model + minijinja prompt templates. Variants carry weights for sampling, or can be pinned by name via `variant_name`. Prompt versioning becomes a config artifact (diff-able in VCS, swappable without code change). No code touches a prompt template — they're files referenced from config.

**Why interesting**: Inverts the usual "model + prompt are constants in code" pattern. With named variants, you can A/B-test prompt changes (`v1`, `v2_more_directive`, `v3_persona_voice`), pin a variant per environment, or run them at weighted ratios without touching application code. Distinct from feature flags because the variants are FIRST-CLASS data model concepts — the gateway logs which variant served each request, so quality metrics attribute back to prompt versions cleanly. Composes with [[architectural-patterns#Per-flavor prompt templates registered alongside the flavor]] — k8sgpt's pattern is "flavors own prompt templates"; TensorZero's pattern is "prompt templates have versions, weights, and metrics."

**Where to apply in cabal**: Each cabal persona becomes a `[functions.<persona>]` block; the existing identity.toml prompt+model binding becomes a `[functions.<persona>.variants.<name>]` block. The investigation-engine's prompt assembler reads variant config rather than a single model+prompt. Direct enablement of "try the new Sherlock voice on 10% of investigations" without forking code. LLMClient's `acompletion` signature already takes a `model` param — replace with `(function, variant?)` and let the engine resolve to model+prompt.

---

---

## Evidence-per-claim citation discipline for memory-system docs

- **Source**: https://github.com/carsteneu/ai-memory-comparison
- **Surveyed**: 2026-06-14

Every supported feature must link to a specific public source — a file, line, or doc URL. Absence of a link means the feature is **marked unsupported regardless of the project's reputation**. The `evidence/` directory is the audit trail. `CRITERIA.md` rules: a README claim isn't evidence (could be aspirational); a doc page is partial evidence; a file path with line numbers in the source is canonical evidence.

**Why interesting**: A falsifiability constraint applied to feature claims. It forces the distinction between "we designed for this" and "this is actually in the code." Most memory-system READMEs read like marketing — "comprehensive lifecycle hooks!" with no pointer to where they live. This discipline makes the README structurally honest. Composes with [[feedback-survey-before-propose]] — the same rule applied to design proposals would have stopped me from proposing things that already exist; applied to user-facing docs, it stops me from claiming things that don't.

**Where to apply in techne**: techne's README and MCP tool descriptions could adopt the same discipline. Each advertised capability (hybrid RRF, structural thread detection, lifecycle hooks, sqlite-vec backfill, half-life decay) links to the concrete file/line where it is implemented. Builds operator trust + serves as a self-audit — if a section can't be cited, either build it or remove the claim. Particularly useful for the AgentCard descriptions cabal personas advertise — capability claims that can't be cited shouldn't be advertised.

---

---

## Feature taxonomy as a structured 79-field schema (gap-analysis instrument)

- **Source**: https://github.com/carsteneu/ai-memory-comparison
- **Surveyed**: 2026-06-14

A static comparison site for 73 AI memory systems built around a precise **79-field schema across 7 orthogonal axes**: Vital Signs, Architecture, Data Model (16 fields), Search & Retrieval (10), Knowledge Lifecycle (7), Extraction Pipeline (8), Platform Support (11 boolean integrations), Benchmarks (5 — incl. LoCoMo, LongMemEval, PersonaMem). Each system's row is ~79 structured fields. `CRITERIA.md` defines what counts as proof per field.

**Why interesting**: The taxonomy itself is a **gap-analysis instrument**, not just a comparison table. Fields like `anticipatedQueries`, `originTrust`, `quarantine`, `recurrence`, `layeredMemory`, `timeTravel`, `contradiction`, `trustModel` name discrete capabilities most memory implementations treat as emergent rather than designed. Naming them forces explicit decisions — "we have decay" vs "we have decay-with-half-life-per-importance-tier" become different rows. Composes with [[architectural-patterns#Bi-temporal supersession with retained history]] and [[architectural-patterns#Decay-at-query-time with importance-tiered half-lives]] — those patterns lit up fields that the schema names.

**Where to apply in techne**: Audit techne against the 79-field schema as a roadmap. Specifically, fields techne may partially implement but doesn't expose: `decay` (techne#13 numeric confidence + half-life decay — built but not flagged in README), `supersede` (knowledge.superseded_by exists), `contradiction` (no explicit handling), `quarantine` (nope), `trustModel` (confidence field but no provenance trust separately), `layeredMemory` (threads / observations / knowledge IS layered), `recurrence` (heuristic threads catch some of this). The gap list becomes a list of capabilities to either expose (add to README + MCP descriptions) or build (file techne issues).

---

---

## Intent-adaptive retrieval strategy fan-out

- **Source**: https://github.com/incidentfox/self-learning-ai-agent
- **Surveyed**: 2026-06-14

A lightweight intent classifier maps each query into a class (factual, procedural, troubleshooting, comparative, etc.). Each intent class declares *which subset* of seven retrieval strategies to run in parallel — factual queries skip HyDE and decomposition; troubleshooting queries skip pure BM25 and lean on multi-hop. The strategy roster is the same; the fan-out is per-intent.

**Why interesting**: Different from fixed fan-out (always run all 7, expensive) or fixed pipeline (always run the same 2, cheap but wrong for some queries). Strategy selection becomes a routing decision. The intent classifier is the cheap-front-end pattern again — same shape as [[architectural-patterns#Two-stage classification: heuristic → LLM tiebreaker → graceful fallback]]: a cheap signal narrows the expensive work. Composes with [[architectural-patterns#Hybrid rank fusion: BM25 + vector + graph traversal via RRF]] — RRF gives you rank-fusion across whatever strategies actually ran.

**Where to apply in techne**: Techne's recall currently runs FTS (and vector when enabled) regardless of query shape. An intent classifier — even just heuristic-keyword based — could pick between {structural-heuristic only, FTS+vector, FTS+vector+graph-traversal} per query. Skips expensive backends when the query shape doesn't need them. Particularly useful when the planned graph/bi-temporal work lands (techne#8) — fact-as-of-T queries route to the bi-temporal walker; current-state queries route to flat FTS. Don't fan out where you don't need to.

---

---

## JSON filter mixin: `jq` + `max_depth` as LLM-callable tool parameters

- **Source**: https://github.com/HolmesGPT/holmesgpt
- **Surveyed**: 2026-06-14

A reusable mixin that adds two parameters to any JSON-returning tool: `max_depth` (recursive truncation of nested objects beyond N levels) and `jq` (server-side filter via the `jq` Python package). The LLM sees these as optional tool args. Instead of dragging a full payload into context and then asking the LLM to ignore most of it, the LLM filters at the source: `{"jq": ".items[] | select(.status.phase != \"Running\")"}`.

**Why interesting**: Inverts the standard "tool returns data, agent filters" pattern. The LLM declares its filter as part of the call. Two specific knobs (`jq` and `max_depth`) are sized to cover ~95% of "this JSON is too big" cases without inventing a query language. Generic mixin = one implementation, every JSON-returning tool inherits the capability. Different from a global response-truncation post-process because the LLM owns the filter — it can refine across turns ("now show me just `.metadata.name` and `.status.conditions`") without re-querying the upstream.

**Where to apply in techne / cabal**: Mixin pattern fits cabal's Sherlock tools cleanly — any tool returning JSON (substrate corpora, K8s queries, REST API responses) inherits `jq` + `max_depth`. For techne, MCP handlers that return structured JSON (knowledge entries, thread summaries) could accept the same parameters from the calling agent. Composes with [[architectural-patterns#Spill-to-disk for oversized tool results + bash read-back]] — `jq` as a server-side filter is the cheap version; spill-to-disk is the fallback when even the filtered result is huge.

---

---

## Per-flavor prompt templates registered alongside the flavor

- **Source**: https://github.com/k8sgpt-ai/k8sgpt
- **Surveyed**: 2026-06-14

Each analyzer (their term for what would be a "flavor" in cabal's investigation engine) can supply its own prompt template. The engine looks up `templates[result.Kind]`; if nothing is registered for that kind, a default fires. The selection happens at the LLM-call site, keyed on the result's `Kind` string. Adding a new analyzer means dropping its prompt template alongside its analyzer code — no central prompt file to edit.

**Why interesting**: "Flavor knowledge" — what to ask the model about a Pod vs. a NetworkPolicy, or in cabal's terms, what to ask about an SRE issue vs. an AWS IAM problem vs. a network DNS failure — lives where the flavor lives. The engine stays generic; the flavor self-contains its diagnostic framing. Different from a global prompt registry (engine has to know about every flavor) and from a monolithic system prompt (every flavor pollutes every investigation). Composes with [[architectural-patterns#Per-toolset `llm_instructions` composed into the system prompt]] — HolmesGPT does per-toolset; k8sgpt does per-analyzer. The cabal engine wants both axes: toolset-level instructions for "how to call this tool" + flavor-level instructions for "how to frame this kind of investigation."

**Where to apply in cabal/sherlock**: Each cabal investigator flavor (Sherlock/SRE, Pinkerton/network, etc.) owns its prompt templates and registers them alongside its persona config. The engine's system-prompt assembler iterates `(flavor.templates, [adapter.llm_instructions for adapter in enabled_adapters])` at request time. No global prompt registry; no engine touching flavor-specific content. The persona block in identity.toml gains a `prompt_dir` field pointing at the flavor's template directory.

---

---

## Per-toolset `llm_instructions` composed into the system prompt

- **Source**: https://github.com/HolmesGPT/holmesgpt
- **Surveyed**: 2026-06-14

Each toolset declares an `llm_instructions` field (loaded from a `.jinja2` file via `_load_llm_instructions_from_file()`, `tools.py:1108`). The system prompt template iterates all *enabled* toolsets at request time and injects each toolset's instructions under a `## {toolset.name}` heading. The system prompt is composed from the toolset list; it doesn't know about specific integrations ahead of time.

**Why interesting**: The clean solution to "how does the LLM know when to reach for the Kubernetes substrate vs. the Grafana skill vs. the Datadog skill" without a monolithic system prompt that grows with every integration. Behavioral guidance lives next to the tools it guides — when you add a new toolset, you ship its prompt fragment with it. The composition happens at request time, so disabled toolsets don't pollute the prompt. Different from a registry-of-strings pattern because the instructions are *templated* (Jinja2) per toolset and can reference toolset-specific config.

**Where to apply in techne / cabal**: Direct lift for cabal's Sherlock persona (see `cabal/docs/proposals/sherlock.md`). Each `ToolAdapter` (MCP-substrate or Skill) carries an `llm_instructions: str | None`. The agent loop's system prompt assembler iterates enabled adapters and folds their instructions in. For techne, the MCP handlers it exposes could each carry their own `llm_instructions` advertised in the AgentCard `metadata.cabal_instructions` — clients can render or inject as appropriate.

---

---

## Reversible anonymize-before-LLM / de-anonymize-after boundary

- **Source**: https://github.com/k8sgpt-ai/k8sgpt
- **Surveyed**: 2026-06-14

Before any prompt leaves the cluster boundary, sensitive values (secrets, IPs, names, tokens) are replaced with masked tokens. Each `Failure` carries a `Sensitive{Unmasked, Masked}` pair list — a reversible token map. The LLM sees only the masked text. When the response comes back, the same map is replayed in reverse to restore the real values before the result is stored or shown. The LLM never sees, and the response cache never holds, plaintext sensitive data.

**Why interesting**: A structured redact/restore *cycle* enforced at a single boundary — distinct from one-way scrubbing (which loses information) and from sink-side redaction (which only protects storage). The reversible token map lets the agent reason about specific values ("pod foo-789 is failing because secret bar is missing") in the response without leaking either name upstream. Composes with [[architectural-patterns#Sink-boundary credential redaction in structured telemetry]] — that one stops credentials at the disk; this one stops them at the network. Both are needed.

**Where to apply in cabal/sherlock**: The investigation engine's spill-to-disk step ([[architectural-patterns#Spill-to-disk for oversized tool results + bash read-back]]) and any context-compaction LLM call should run through this redact/restore boundary. Tool output that contains real credentials or PII gets masked → fed to the model → response restored. The mask map lives on the in-flight Task state (cabal `Task.metadata.cabal_redaction_map`), so resume / resubscribe work with no extra plumbing. Particularly important for cabal because A2A is the wire protocol and the LLM is on the other side of an HTTP boundary — pure sink redaction isn't enough.

---

---

## Spill-to-disk for oversized tool results + bash read-back

- **Source**: https://github.com/HolmesGPT/holmesgpt
- **Surveyed**: 2026-06-14

The actual "petabyte-scale" mechanism (the README's headline "tool output transformer pipeline" is largely deprecated — only `LLMSummarizeTransformer` exists and its docstring says LEGACY/NOT recommended). The real story: when a tool result exceeds `min(15% of context window, 25k tokens)`, the full result is written to `$TMPDIR/.holmes/<uuid>/tool_results/<tool>_<callid>.{json,txt}`. The LLM is sent a **pointer message** containing: the file path, a small preview (sliced from the start, character budget ~half the safety margin), and explicit `cat` / `jq` / `grep` invocation hints. The LLM uses bash in subsequent turns to filter / paginate / search the spilled data. Images get a separate `read_image_file` tool.

**Why interesting**: Resolves the "tool returned 50MB of logs" problem without inventing a streaming protocol or building an LLM-summarization pipeline (which has its own latency and accuracy costs). The trick is that the LLM itself becomes the filter: `cat foo.json | jq '.pods[] | select(.status.phase != "Running")'` is exactly what the LLM should be doing anyway. Critical dependency: a `bash` skill must be in the toolbox (allowlisted) for read-back — without it, spilled data is silently unreachable. Composes with [[architectural-patterns#JSON filter mixin: `jq` + `max_depth` as LLM-callable tool parameters]] — `jq` becomes the primary read-back mechanism.

**Where to apply in techne / cabal**: Direct lift for Sherlock's tool dispatch in PR-C — wrap result emission so `tokens(result) > threshold` triggers spill + pointer. For techne specifically, large `recall` results (an MCP tool that returns 200 observations across a long thread) could spill rather than truncating; the agent then uses `cat | jq` to surface the relevant entries. Pairs naturally with [[architectural-patterns#Reference-based session handoff (pointers, not content)]] — same "pointer not copy" philosophy at a different scope (turn vs. session).

---

---

## Stateless thermostat hook (re-assert each turn vs. one-shot init)

- **Source**: https://github.com/Poorna-Repos/opus-fable-mode
- **Surveyed**: 2026-06-14

A `UserPromptSubmit` hook re-emits the full governor directive on **every user turn**, not once at session start. No state tracking, no "did I inject this already?" check — the hook is a pure function of "is this a user turn? if yes, prepend the directive." Conceptually a thermostat, not a thermostat-setpoint: the directive is constantly re-asserted because the system keeps drifting from it as context grows.

**Why interesting**: Reframes hook design around the observation that LLM behavior degrades as context accumulates — a *decay model*, not a *setup model*. The fix is repetition, not richer state. Different from [[architectural-patterns#Decay-gated injection via `last_read` session counter]] (which *abbreviates* old content); this one *re-asserts* high-priority constraints unchanged. The two patterns compose: rare directives collapse to one-liners (passive-adr), critical directives re-emit verbatim every turn (this).

**Where to apply in techne**: Techne's `SessionStart` hook injects memory once at startup, then loses salience as the conversation grows. For high-priority constraints (memory hygiene rules, the "verify before recommending from memory" rule, the user's working-style preferences), a `UserPromptSubmit` companion hook that re-asserts those *constraints* (not the full memory) every turn would keep them in working context. Stateless, idempotent, opt-in per memory entry via a `re_assert: true` field in the memory frontmatter.

---

---

## Structured BDD/spec docs as shared human-agent memory

- **Source**: https://github.com/paca-ai/paca
- **Surveyed**: 2026-06-14

Gherkin scenarios and System Design Documents are first-class data entities, stored alongside tasks in Postgres and exposed to both the human UI and the AI agents through the same query path. Both humans and agents read and write the same Gherkin. The structured natural-language layer becomes the shared context store — not chat history, not vector embeddings, not freeform notes.

**Why interesting**: An alternative answer to "what is memory for AI agents?" Most platforms reach for episodic memory (chat history), semantic memory (vector embeddings), or graph memory (entity relations). Paca picks **structured-spec memory** — Gherkin's `Given/When/Then` is constrained enough to be parseable, expressive enough to capture intent, and *human-edited* so the agent isn't guessing from synthetic embeddings. The audit trail is just git on the spec files. Distinct from prompt engineering because the specs live independently of any single agent call.

**Where to apply in techne / cabal**: Techne could grow a structured-spec memory tier alongside its observation/knowledge/thread stores — `kind:spec` knowledge entries containing Gherkin-like or design-doc-shaped content. Personas reading the substrate would see structured intent, not just past observations. The Architect persona's existing patterns store is the closest existing analog; extending it to ingest BDD-shaped artifacts from the operator's repos would give cabal personas a verifiable "what was supposed to happen" view they currently lack. Speculative — only worth doing if the operator finds himself writing spec-shaped notes anyway.

---

---

## Typed SSE event factories (named per event type)

- **Source**: https://github.com/incidentfox/incidentfox
- **Surveyed**: 2026-06-14

All agent-to-client streaming uses a single `StreamEvent` dataclass with a `.to_sse()` serializer. Instead of one mega-constructor with optional fields for every event type, there's a **named factory function per event type** — `thought_event(...)`, `tool_start_event(...)`, `tool_end_event(...)`, etc. Each factory enforces its own field contract at the call site. Partial-field logic (e.g., `parent_tool_use_id` only present on nested tool calls) lives in the factory, not in consumers.

**Why interesting**: Distinct from a class hierarchy because there's no runtime overhead (no method dispatch, no MRO), and distinct from a flat dataclass because the call site has to use the right factory — typos become unbound-name errors, not silently-missing fields. The wire protocol becomes self-documenting: `grep '_event(' events.py` lists every event type in the system. Consumers can pattern-match on event name without reconstructing the type from optional-field presence.

**Where to apply in techne**: Techne's hook lifecycle currently emits events through whatever shape the hook returns. Standardizing on a `HookEvent` dataclass + factory-per-event-type (`thread_started_event`, `tool_called_event`, `compaction_began_event`, etc.) gives downstream MCP clients a typed, versionable stream they can match on. Composes with [[architectural-patterns#Typed sidecar entries in the session log]] — the event type discriminator is the same discriminator used for storage; one schema decision serves both wire and disk.

---

---

## Warm-sandbox claim/reclaim with TTL reset on follow-up

- **Source**: https://github.com/incidentfox/incidentfox
- **Surveyed**: 2026-06-14

Sandboxes (K8s pods inside gVisor) are pre-warmed and stamped `claim-{thread_id}`. On a follow-up message, the manager *patches* `spec.lifecycle.shutdownTime` on the existing CRD to push the TTL out — rather than spawning a new pod. The K8s controller handles reaping idle sandboxes when the TTL expires; the orchestrator never tracks explicit cleanup. The `/claim` endpoint also writes a per-tenant JWT to `/tmp/sandbox-jwt` for the Envoy sidecar to pick up on every outbound call (credential-injection at the sidecar boundary, not in agent code).

**Why interesting**: Two ideas stacked. (a) **Liveness-via-TTL-reset** sidesteps both cold-start cost (sandbox is already warm) and persistent-process management (no \"is this thing still alive?\" tracking). The controller is the GC. (b) **Sidecar credential injection** keeps secrets out of the agent's context entirely — the agent never sees the token; Envoy reads it from the shared volume and attaches it to outbound calls. Composes with [[architectural-patterns#Sink-boundary credential redaction in structured telemetry]] — sidecar injection + sink redaction means credentials live at the network boundary, never in logs or context.

**Where to apply in techne / cabal**: Techne's thread lifecycle is currently status-transition driven (active → stale on time). A TTL-reset-on-activity field (`thread.expires_at`) with a background sweeper that drops expired rows is a cleaner shape — every observation arrival just bumps `expires_at = now() + ttl`, and the sweeper runs on a fixed cadence. Bigger fit in cabal: persona dispatch with a warm subprocess pool would benefit directly from the K8s-style claim pattern — `cbl dispatch X` patches the TTL on an existing process rather than forking a new one. Pairs with the AbortGraph work (cabal#11).

---

---

## WebAssembly plugin sandbox for untrusted extensions

- **Source**: https://github.com/paca-ai/paca
- **Surveyed**: 2026-06-14

Backend plugins compile to WebAssembly and execute in a sandbox. A matching Go SDK gives plugin authors the host-call surface. The host process never runs untrusted native code — plugins are memory-safe by construction, language-agnostic at the source level, and isolated by the WASM runtime. Distribution happens through an in-app marketplace; reference plugins ship as separate repos (`paca-plugin-github`, `paca-plugin-bdd`, `paca-plugin-checklist`).

**Why interesting**: Most plugin systems pick between two bad options — fork subprocesses (heavy, slow start, OS-level boundary only) or eval dynamically (insecure, language-locked, no real isolation). WASM threads the needle: memory-safe boundary at the byte-code level, fast cold start, language-agnostic source. Distinct from network-isolated approaches because there's no IPC overhead. The Go SDK pattern is the right shape — plugin authors get typed host calls, not raw imports/exports.

**Where to apply in limenal/cabal**: Limenal currently has no formal sandbox for third-party MCP tool implementations — subprocess model is unbounded. A WASM boundary for user-contributed handlers would be the right shape: an Architect-third-party plugin can run logic but can't reach files / network / credentials except through host-provided imports. For cabal, third-party SKILL.md bundles in Sherlock/Pinkerton/etc. could similarly compile to WASM if the skill author wants stronger isolation than `ulimit -v` + Bash provides. Composes with [[architectural-patterns#Per-tool memory limit via `ulimit -v` shell prefix]] — WASM for untrusted, ulimit+bash for trusted-but-bounded.

---

---

## `episode_id` threading: every LLM call belongs to a logical session

- **Source**: https://github.com/tensorzero/tensorzero
- **Surveyed**: 2026-06-14

Every inference accepts an optional `episode_id` that groups N inference calls into one logical session. Returned in every response. Stored in ClickHouse alongside `inference_id`. The episode is a first-class data model concept — not a metadata tag or trace field. Result: multi-turn agent sessions are queryable as a unit (replay all inferences in an episode; aggregate cost per episode; attribute feedback to the episode rather than a single inference).

**Why interesting**: Distinct from tracing (which is hierarchical and operations-focused) and from session IDs (which are usually app-level conventions, not gateway-level concepts). By baking episode into the data model, the gateway can serve queries like "show me the full LLM-call sequence for this investigation," "what did this episode cost end-to-end," and "this feedback applies to the whole episode, not the last turn." Composes with bi-temporal modeling and pointer claims patterns — episodes are the natural unit for "what did the agent know during this run."

**Where to apply in cabal**: The investigation-engine has natural episodes — one investigation run = one episode. cabal's A2A `Task.id` IS the episode_id. Threading it through `LLMClient.acompletion(... episode_id=task.id)` and storing it in the inference log gives free per-investigation cost accounting, full-trace replay, and feedback attribution. Add as a passthrough param on `LLMClient` Protocol without any TensorZero adoption — LiteLLM supports custom metadata; the discipline lives at the call sites. Episodes also map cleanly to the cursor-based resubscribe model from the investigation-engine proposal: replay all events for episode X.

---

---

## Dual-trigger command parsing at the prompt boundary

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

Mode transitions activate via *either* explicit slash syntax (`/ponytail full`) *or* freeform natural language (`"stop ponytail"`, `"go ultra"`), both parsed from the raw prompt string in a single `UserPromptSubmit` hook. Lightweight regex over the existing prompt stream; no separate NL intent classifier, no second LLM call.

**Why interesting**: Most plugins pick one path — slash commands (precise, but require memorization) or NL parsing (forgiving, but usually means an extra LLM intent call). Doing both in one regex pass at the hook layer captures the cheap signal — explicit slash commands AND high-confidence phrasings — without latency or model dependency. The ambiguous cases fall through to the regular agent loop, which is the right place for them. Distinct from a chatbot intent classifier because there's no probabilistic ranking — the regex either matches or doesn't, and the user-facing contract stays predictable.

**Where to apply in techne**: Techne's thread-detection heuristic operates on structural signals (file paths, time gaps, entity overlap). A `UserPromptSubmit` hook could carry an explicit user override channel — both `/techne thread new` (slash) and `"start a new thread"` / `"forget that"` (NL regex) parsed in one pass — letting the operator force a thread boundary the heuristic would have missed. Composes with [[architectural-patterns#Intent-gated safety interruption (lexical pre-check before confirm)]] — both lean on cheap lexical matching at the prompt boundary; one for safety bypass, this one for thread control.

---

---

## Inline tech-debt annotation convention with named upgrade condition

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

A greppable `// ponytail:` prefix marks a deliberate simplification *and names its upgrade condition* in the same line — e.g. `// ponytail:single-tenant — promote to per-tenant config when a second customer ships`. A companion harvest command scans the tree, groups by category, and produces a debt inventory the operator can act on. Zero-cost convention adopted at write time; harvester is purely additive tooling.

**Why interesting**: Distinct from `TODO` / `FIXME` because those document a *gap* ("something missing here") with no signal about when to act. This convention documents a *ceiling* ("we chose this on purpose; here's the trigger to revisit"). The named upgrade condition turns ambient debt into a list of conditional commitments. Composes with [[architectural-patterns#Schema-as-source-of-truth for tool risk classification]] — both colocate machine-readable metadata with the thing it describes, so the inventory is always in sync with the code. Zero-cost-now-harvest-later split is the right shape — the convention is useful even before the harvester ships.

**Where to apply in techne / cabal**: Adopt `# techne:` and `# cabal:` prefixes immediately for the same purpose. Both repos have plenty of "this is fine for single-user; revisit at multi-user" / "synchronous now; async later" / "in-memory cache; move to SQLite if hot" decisions buried in module docstrings — the inline convention would surface them as a queryable inventory. Direct fit for the deferred-from-MVP slots [[feedback-mvp-cut-pattern]] surfaces; turning those into inline `# cabal:mvp — promote when ...` notes makes them findable. Build the harvester later as a thin `cbl tech-debt` command.

---

---

## Polymorphic hook output routing by platform identity

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

A single `writeHookOutput(event, state)` function detects the active host platform via env vars (`COPILOT_PLUGIN_DATA`, `PLUGIN_DATA`, default → Claude Code) and serializes the same internal state into whichever JSON schema that platform requires — `additionalContext` for Copilot, `systemMessage` for Codex, raw output for Claude. One runtime owns all serialization paths; platforms are named variants in a switch.

**Why interesting**: Inverts the usual "each integration owns its own hook script" pattern. Adding support for a new host editor becomes a pure data change (one more case in the routing function), not a new hook file with its own activation, lifecycle, and divergent logic. The internal event/state is the source of truth; per-platform shapes are projections. Different from a Strategy pattern because the dispatch key is *environmental* (which host am I running inside?) rather than *operational* (which behavior do I want?) — the caller doesn't choose, the runtime detects.

**Where to apply in techne**: Techne's MCP server emits responses in one shape today. If techne ever grows additional consumers — a Claude Code direct-injection path, a webhook adapter, a future remote-MCP transport, a Cabal-specific A2A surface — a single `emit_response(event, payload)` with per-consumer serialization branches keeps the event model canonical. Composes with [[architectural-patterns#Typed SSE event factories (named per event type)]] — factories define what events exist; polymorphic routing decides how each consumer sees them.

---

---

## Promptfoo skill evaluation harness (task × model × skill-arm matrix)

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

A promptfoo eval config defines a `task × model × skill-arm` matrix — for each task scenario, run the same prompt against each model with each variant of the skill (none / lite / full / ultra), then measure token cost, code quality (rubric or test pass), and rule adherence per cell. Replaces "it felt better when I was using it" with a quantified comparison before shipping a skill change. ~2 hr one-time harness setup; reusable for every subsequent skill iteration.

**Why interesting**: Most agent skills ship on vibes — the author runs through their own task list once, decides "this is better," merges it. Promptfoo's matrix forces three discriminations: skill-armed-vs-bare (does this skill help at all?), variant-vs-variant (which arm wins?), model-sensitivity (does the skill help on Haiku but hurt on Sonnet?). A skill that only helps in one cell is a worse buy than the author thinks. Composes with [[architectural-patterns#Confidence-gated auto-resolution with human escalation queue]] — both turn agent behavior into measurable outcomes; promptfoo evals are the offline analog of the online confidence threshold.

**Where to apply in techne / cabal**: Highest-leverage of the four ponytail patterns. We ship skills (`/code-review`, `/investigate-url`, `/survey-field`, the architectural-patterns survey itself) without quantified effectiveness measurement. A promptfoo harness covering 5–10 representative tasks × 2 models × {bare, skill-on} would catch regressions on skill edits and surface which models the skill actually helps. Direct enablement of "should this skill ship?" as a numeric question. Pairs naturally with [[architectural-patterns#Behavioral drift measurement over existing transcript artifacts]] — that one watches production drift; this one is the lab bench.

---

---

## SessionStart always-on context injection (no slash command required)

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

The activation hook fires on every `SessionStart` and injects the active skill context automatically — no `/ponytail` invocation required. The plugin assumes the operator wanted it on (the install act is the consent); the hook makes "on" the default and gives slash commands for *changing the state*, not for *turning the plugin on at all*. Persisted state in `.ponytail-active` survives session boundaries so intensity is sticky.

**Why interesting**: Inverts the usual slash-command-as-entrypoint model. For skills the operator has already opted into, requiring them to re-invoke every session is friction without value. The hook becomes the entrypoint; slash commands become the dial. Distinct from [[architectural-patterns#Stateless thermostat hook (re-assert each turn vs. one-shot init)]] — that one re-injects every user turn (because directives decay within a session); this one auto-injects at session start (because slash-command invocation friction kills adoption). The two compose: install-time consent → SessionStart auto-injection → per-turn re-assertion for high-priority constraints.

**Where to apply**: Immediate fit — `/startup` should auto-run on `SessionStart` rather than requiring operator invocation. The state we want injected (open threads, recent decisions, current intensity level) is exactly the kind of thing the operator already opted into when they configured the platform. For techne: skill files that the operator has registered should inject themselves; explicit slash commands stay for state changes, not for activation.

---

---

## Tiered-intensity skill levels (lite / full / ultra / off in one skill file)

- **Source**: https://github.com/DietrichGebert/ponytail
- **Surveyed**: 2026-06-15

A single skill file carries four sections — `lite`, `full`, `ultra`, `off` — each with progressively stronger directives for the same underlying philosophy ("laziest senior dev"). The activation hook reads the current `.ponytail-active` state and injects *only the active level's section* into the system prompt. Operators dial intensity per-task (`/ponytail lite` for exploration, `/ponytail ultra` for cleanup sweeps) without switching skills, copying files, or maintaining N variants.

**Why interesting**: Distinct from feature flags (which gate features) and from prompt versioning (which picks ONE variant per request). Intensity is treated as a continuous knob along a known axis, with the skill file itself holding all positions. The same conceptual rule expressed at four levels of insistence means edits stay coherent — change the philosophy once, all four tiers update together. Composes with [[architectural-patterns#Decay-gated injection via `last_read` session counter]] (verbosity by usage) — ponytail does verbosity by operator command, passive-adr does it by access pattern; both calibrate "how much of this should be in context."

**Where to apply in techne / cabal**: Persona system prompts could ship lite/full variants gated by `identity.toml` — `mode = "lite"` for low-stakes exploratory dispatch, `mode = "full"` for the default. Direct fit for any cabal skill that has "always-on but adjustable" energy — code review, security review, the architectural-patterns survey itself. Worth pairing with the techne reflection memory ([[reference_cc_memory_architecture]]) so techne can capture which intensity actually moved the needle.

---

---

## "Opinions" as a versioned data artifact with justification-required overrides

- **Source**: https://github.com/shaz/gojira
- **Surveyed**: 2026-06-16

All platform defaults — resource sizes, HPA targets, probe shapes, sizing classes — live in a single `sizing.yaml` shipped inside the Python package and parsed through Pydantic models at load. The design doc names the stance explicitly: "the opinions are the product: visible, documented, overridable." When users do override, the config schema *requires a `justification:` field* alongside the override value (social enforcement first, programmatic admission control later).

**Why interesting**: Two non-obvious moves combined. (a) Treating defaults as a *product artifact* rather than buried constants — the YAML file is reviewable, diffable, versionable, and is itself the documentation of what the platform believes. (b) The justification field on overrides turns "I disagree with the default" from a silent fork into a recorded conversation. Over time, the justification corpus becomes a feedback signal: if the same override appears with the same justification across N repos, the default is wrong. Distinct from feature flags (gating availability) and from config files (parameterizing behavior) — this is the *philosophy of the product* expressed as a diffable file.

**Where to apply in techne / cabal**: Techne's persona sizing, retention windows, summarization thresholds — currently scattered as constants in modules — could collapse into `techne/opinions/defaults.yaml`. Cabal's routing weights and persona-default behaviors likewise. The justification-required override is the bigger lift: cabal's `identity.toml` could require a `justification:` next to any value that deviates from the persona-class default, giving you a built-in audit trail for why this persona is configured this way. Composes with [[architectural-patterns#Inline tech-debt annotation convention with named upgrade condition]] — both make a deliberate decision legible at the point of decision, but ponytail's `// ponytail:` marks *code* simplifications, this marks *config* deviations.

---

---

## Adversarial council with anonymous peer review before synthesis

- **Source**: https://github.com/Jartan-LLC/memcp
- **Surveyed**: 2026-06-16

A structured prompt workflow fans a decision out to **five named adversarial personas** — Contrarian, First Principles, Expansionist, Outsider, Executor — each producing an independent take. The non-obvious stage: before synthesis, each persona's output is shown anonymously to the others, who then surface disagreements and blind spots in writing. A chairman role synthesizes the original takes plus the peer-review pass.

**Why interesting**: Most "council" or "debate" prompts run personas in isolation and aggregate at the end — the failure mode is consensus-by-averaging, which silently smooths over the disagreements that carry the most signal. Inserting an anonymous peer-review stage forces personas to *read each other's reasoning* before the synthesizer sees anything, and anonymization removes the "agree with the senior persona" failure mode that hits when reviewers know who wrote what. The structural move: cross-critique is its own stage with its own output, not a sidecar to synthesis. Composes two ideas — multi-perspective generation + anonymous peer review — into one orchestration template.

**Where to apply in cabal**: Direct fit. Cabal already has the infrastructure to instantiate named sub-agents and pass artifacts between them; the council workflow is a reusable orchestration template for any high-stakes call that needs multiple lenses before commitment — schema changes, breaking API decisions, big architectural pivots. The peer-review stage maps naturally onto a cabal "review round" between named personas with anonymized inputs. Worth shipping as a stock `cabal council` skill rather than re-implementing per use case. Composes with the [[architectural-patterns#Standing programs as model-free graph reflexes (with self-penalizing watchers)]] pattern — that one's about *deterministic* watchers; this is about *generative* deliberation. Both reduce reliance on a single chain of thought.

---

---

## Augmentation-only write guard enforced at the tool boundary

- **Source**: https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf
- **Surveyed**: 2026-06-16

The OKF reference enrichment agent runs two passes (BQ schema enrichment, then web-citation augmentation) that write to the same bundle directory. The web pass's `write_concept_doc` tool refuses writes that *reduce* the schema-field count or citation-entry count vs what already exists, returning a detailed error rather than silently overwriting. "Augment, not replace" is enforced at the tool boundary — the prompt instructs the agent to augment, but the tool *guarantees* the augmentation by failing closed on shrinking writes. Last-resort guard.

**Why interesting**: Trusting prompt-following for invariants is the standard mistake — models are correlation engines, not constraint solvers. When the invariant matters (don't lose data during summarization, don't replace verified facts with hallucinated ones), the right level for enforcement is the tool boundary, not the prompt. Distinct from validation-on-input (schemas, type checks) because the constraint is *relational* — "the new write must dominate the prior state along these axes." Composes two ideas: tool-as-guard for LLM outputs + monotone-only writes for evidence-bearing fields.

**Where to apply in techne**: Direct fit for techne's summarization and reflect passes ([[project_reflect_experiment_2026-06-15]]). Reflect writes to `candidates` rather than live store to avoid silent destruction, which is the soft version of this guard — humans confirm before promotion. The hard version: `techne_store` could reject a summary-write that drops observation count, fact references, or evidence pointers vs the prior thread state. Composes with [[architectural-patterns#Invariants and eval results stored as first-class memory cells in the same store]] — that one persists *that* invariants were checked; this one enforces a specific structural invariant at the write site. See also the standalone observation [[architectural-patterns#Markdown body + YAML frontmatter + directory hierarchy as the de-facto agent-substrate format]] — OKF is one of five+ independent projects converging on the same shape.

---

---

## Benchmark variant trio: no-memory / memory-cold / memory-warm

- **Source**: https://github.com/Foxfire1st/agents-remember
- **Surveyed**: 2026-06-16

Every benchmark prompt runs three variants of the same task: **baseline** (no memory plugged in), **memory-cold** (onboarding present but freshness check not pre-run, so initialization happens within the measured window), and **memory-warm** (drift verification already passed before measurement starts). Results are reported per-variant in JSONL. The warm variant isolates whether memory provides value *beyond* its setup cost.

**Why interesting**: Most agent-memory evals conflate two costs into one number — "memory turned on" vs "memory turned off" — which can mask the failure mode where retrieval quality wins on long sessions but initialization overhead loses on short ones. The cold-vs-warm split lets you see when memory is net-negative (short tasks where setup dominates) and when it earns its keep (longer tasks where retrieval payback kicks in). Sharp diagnostic, simple to construct. Composes with [[architectural-patterns#Promptfoo skill evaluation harness (task × model × skill-arm matrix)]] — that pattern is the matrix layout; this is the variant axis *inside* the memory-arm cells.

**Where to apply in techne**: Techne has no published eval harness ([[project_platform_state_2026-06-16]] notes 575 tests but they're unit-level). Adopting the cold/warm/baseline trio for a small benchmark set (e.g., 5–10 representative recall tasks × {bare, techne-cold, techne-warm}) would answer "does our memory hook add net value, and at what task length does it start earning?" — a question we currently can't answer numerically. Cheap to construct (the runner just needs to know whether to pre-execute the drift/freshness pass), high diagnostic value. Composes also with [[reference_cc_memory_architecture]] — that note says CC and techne are complementary; the trio would let us measure where each is the load-bearing layer.

---

---

## Budget-capped short-lived token: one primitive for key protection AND spend ceiling

- **Source**: https://github.com/sricola/drydock
- **Surveyed**: 2026-06-16

A host-side gateway holds the real upstream API key; the sandboxed agent receives only a short-lived bearer token whose budget ceiling is baked in at mint time. Key exfiltration and runaway spend collapse into the same mitigation — one primitive, two threats. The gateway brokers every outbound call so revocation, audit, and budget enforcement all live in one place.

**Why interesting**: Token-issuing gateways are standard; what makes this distinct is the *composition* — the budget cap is not a separate quota layer added on top, it's part of the token's identity. Once the cap is hit the token is dead, the same way an expired token is dead. Treating "money the agent can spend" and "trust the agent gets" as one revocable artifact is a cleaner model than tracking them in parallel systems.

**Where to apply in cabal / limenal**: Direct fit for cabal's multi-persona dispatch — each persona's outbound LLM call could go through a gateway that mints a per-task token with a budget ceiling, so a misbehaving persona caps its blast radius automatically. Limenal already mediates per-persona credential policy; adding a budget-cap-at-mint dimension turns it from "who can call X" into "who can call X for how much." Particularly load-bearing given the [[reference_anthropic_auth_tier]] reality — when LLM access flows through an OAuth tier with no per-call quota, the architecture has to enforce the ceiling itself.

---

---

## Canary tests for private library internals as explicit-risk discipline

- **Source**: https://github.com/Jartan-LLC/memcp
- **Surveyed**: 2026-06-16

When production code depends on an undocumented or private library behavior (private attribute access, internal class layout, an import path that isn't part of the public API), a dedicated **canary test** is written to assert exactly that shape. The test's job isn't to verify the production code — it's to verify the dependency hasn't moved under it. If the upstream library refactors the internal, the canary fails loudly on the next test run, surfacing the coupling as a tracked risk rather than as a silent runtime error in production.

**Why interesting**: Standard testing wisdom says "test against public APIs." This is the deliberate inversion: when you've already chosen to couple to a private internal (sometimes you have to — an SDK doesn't expose the hook you need), treating that coupling as if it doesn't exist is the actual mistake. The canary makes the implicit explicit. The discipline is "promote silent assumptions to loud assertions." Distinct from regression testing because the test asserts on the *dependency's* shape, not on production behavior — a passing canary means the brittle bridge is still standing.

**Where to apply in techne / limenal / cabal**: All three projects wrap MCP SDK / FastMCP / mem0 / similar libraries whose internals shift between minor versions. `CLAUDE.md` already calls out the FastMCP import path as a known deviation, and [[reference_fastmcp_dns_rebinding]] notes a specific FastMCP internal that bit us. Adding canary tests for those specific brittle couplings would catch silent breakage at SDK upgrade time, before it reaches persona behavior in production. Cheap to write, near-zero ongoing cost, and the failure mode they catch (LLM SDK refactors its internals in a minor version bump) is one of the most common production-quality surprises in the AI-tooling space.

---

---

## Capability-gated tool registration (no dead tools surfaced to the LLM)

- **Source**: https://github.com/Jartan-LLC/memcp
- **Surveyed**: 2026-06-16

Backend implementations declare a set of capability flags via `capabilities()` returned from the Protocol they implement. At server startup, the tool registration loop iterates the static tool catalog and *only wires* the MCP tools whose declared backend capabilities are satisfied. The result: a backend that lacks (e.g.) semantic search literally does not expose `memory_search_semantic` as a callable tool; the LLM sees a shrunken surface, not a tool that errors with "not implemented."

**Why interesting**: Distinct from [[architectural-patterns#Capability probing with fallback dispatch]] — that pattern probes capabilities at startup and branches the *behavior* of a single code path based on what's there. This one branches the *menu* itself: the tool either exists or it doesn't, and the LLM never sees a never-callable name. Two consequences: (a) zero "not implemented" failures surfaced to the agent, which means zero hallucination opportunities from a mismatch between the tool list and reality; (b) capability declarations are reviewable at the backend's Protocol surface, which makes adding a new backend a structured exercise rather than discovering missing pieces at first invocation.

**Where to apply in techne / limenal**: Limenal's per-persona credential/policy gate is the natural fit — only expose MCP tools whose required credential exists for the requesting persona. A persona without GitHub credentials shouldn't see `github_create_issue` in its tool list at all. Same shape for techne hook registration: hooks declare prerequisites, the registrar filters at install time. Composes with the standalone [[architectural-patterns#Markdown body + YAML frontmatter + directory hierarchy as the de-facto agent-substrate format]] — `SKILL.md` frontmatter already supports `allowed-tools` which is the static-side analog; capability-gating is the runtime side that prunes from the allowed set what's actually supported by the backend.

---

---

## Commit-mapped ledger as hard consistency contract between parallel repos

- **Source**: https://github.com/Foxfire1st/agents-remember
- **Surveyed**: 2026-06-16

Two repositories run in parallel — the code repo and the memory repo — and a `memory.md` ledger commits a mapping from every code-commit SHA to the memory-repo state at that commit. `worktree_sync` and `worktree_start` refuse to proceed if the ledger lacks an entry for the current code SHA. Cross-repo consistency is enforced at operation boundaries, not assumed at quiet times.

**Why interesting**: "Code and its docs/memory live in different repos" is a common shape; the typical failure mode is silent drift — the docs refer to commit-state that no longer matches reality, and nothing catches it until a human notices. A ledger turns the relationship into a hard invariant: there's a defined memory state corresponding to every code state, and operations fail loudly if the mapping is missing. The "fail-on-missing-mapping" stance is what's lifted — most cross-repo consistency tools soft-warn (out-of-sync, will sync soon) rather than hard-block. Composes with [[architectural-patterns#`episode_id` threading: every LLM call belongs to a logical session]] — both encode "this artifact belongs to that state" as a first-class identity field, not metadata.

**Where to apply in techne**: Techne memory frequently references commit SHAs, PR numbers, and file paths from active repos ([[reference_locations]] is partly a ledger of where things live, without the SHA mapping). A lightweight `memory_at_commit` index would let techne validate "this observation about cabal still corresponds to a commit cabal has shipped" rather than discovering at recall time that the reference is stale. Composes with [[project_reflect_experiment_2026-06-15]] — reflect's "live store untouched, write_to: candidates" stance is the soft version of this; a commit-mapped ledger would let candidates be promoted only when both store and code are at compatible states.

---

---

## Committed threat model as in-scope/out-of-scope contract

- **Source**: https://github.com/sricola/homebrew-drydock
- **Surveyed**: 2026-06-16

A first-class committed document enumerates exactly which protections the system guarantees (in-scope mitigations) and which risks it explicitly does NOT cover (out-of-scope), terminating in a named residual-risk statement the operator must acknowledge. Written as a contract — diffable, reviewable, versioned in the same repo as the code that implements it.

**Why interesting**: Most security documentation reads like marketing — "we protect against X" without a counterpoint of "and we explicitly do not protect against Y." The in/out symmetry makes the boundary auditable and forces honesty: if the project adds a feature that crosses the line, the threat model has to change in the same PR. Distinct from a security README because the out-of-scope list is the load-bearing half.

**Where to apply in techne / cabal / limenal**: Limenal mediates per-persona credential and policy decisions — a committed threat model would make the trust boundary between personas explicit (what does limenal guarantee about credential isolation? what would still leak if a persona is compromised?). Same shape applies to techne's hook lifecycle (which hooks can mutate state? which can't?) and cabal's A2A protocol (what does a sealed envelope actually protect against?). Composes with [[architectural-patterns#Evidence-per-claim citation discipline for memory-system docs]] — both treat documentation as a structured artifact with discipline, not prose.

---

---

## Drift-gated trust checkpoint as a blocking session-entry contract

- **Source**: https://github.com/Foxfire1st/agents-remember
- **Surveyed**: 2026-06-16

Every session begins with a mandatory MCP call (`context_packet`) that returns memory freshness, drift count, branch staleness, and provider health *before any analysis or planning proceeds*. The check is a **hard gate**: stale state halts the session and routes the agent to repair (drift fix, onboarding update, branch sync) rather than letting it continue against degraded context. The repair path then re-enters the gate; only a clean checkpoint releases the agent into real work.

**Why interesting**: Most session-start hooks inject context and proceed regardless (see [[architectural-patterns#SessionStart always-on context injection (no slash command required)]] and [[architectural-patterns#Decay-gated injection via `last_read` session counter]] — both are about *what* to inject, neither blocks). This pattern treats freshness as a hard contract: degraded inputs can produce confidently-wrong outputs, so the right move is to fix the inputs before reasoning, not to reason about how degraded they are. Distinct from background health monitoring because the agent itself is the actor that handles the failure — the gate IS the agent's first task in the session.

**Where to apply in techne / cabal**: Techne's structural thread heuristic and the reflect-experiment already produce freshness/quality signals that are currently injected but not gated on. A `SessionStart` gate that halts with "you have N stale facts pointing at deleted files, repair first" would be cheap to add and would prevent the failure mode where memory confidently references things that no longer exist. Cabal's per-persona dispatch could gate on credential expiry / mailbox backlog at activation time. Composes with [[architectural-patterns#Markdown claims as runtime-verifiable contracts (with negation as first-class)]] — that pattern *detects* drift mechanically; this one *acts* on it at the session boundary.

---

---

## Externalized `.git` to neutralize hook-injection on agent worktrees

- **Source**: https://github.com/sricola/drydock
- **Surveyed**: 2026-06-16

When staging a repository for an agent to work in, the `.git` directory is placed *outside* the agent-visible work directory and re-linked at the boundary. The agent sees a normal worktree but cannot drop a malicious hook into `.git/hooks/` because `.git` isn't reachable from inside the sandboxed path.

**Why interesting**: Git-hook injection is a well-known agent-compromise vector (a poisoned repo's `pre-commit` runs as the operator the moment they `git commit`). Most mitigations are reactive — scan hooks, disable `core.hooksPath`, run with `--no-verify`. Externalizing `.git` removes the attack surface structurally; the agent literally cannot reach the hook directory, no scanner required. Low-effort, no runtime cost, and silent to the agent.

**Where to apply in techne / cabal**: Any future code path that clones a repo into an agent's working context should adopt this. Currently neither techne nor cabal stages repos for agent execution, but the [[architectural-patterns#WebAssembly plugin sandbox for untrusted extensions]] energy is the same — defense-by-structure rather than defense-by-policy. Worth keeping in the pattern catalog for the moment we add repo-staging (e.g., a `cabal task` that clones a target repo into a worker persona's context).

---

---

## Gap-driven memory growth (post-task hook: "if no entry covers this, write one")

- **Source**: https://github.com/theDakshJaitly/mex
- **Surveyed**: 2026-06-16

A soft mandatory hook in the agent-facing scaffold: after every task, the agent checks whether a pattern exists for the *class of task just completed*. If not, it creates one in `.mex/patterns/`. If a pattern exists, nothing is added. Memory grows exactly where gaps exist and stops growing when coverage is complete — no upfront taxonomy, no log-everything overhead.

**Why interesting**: Distinct from the two common memory-growth strategies. (a) Log-everything (Letta, MemOS) overgrows; signal-to-noise drops as the store accumulates. (b) User-curates (Obsidian-style) undergrows; depends on operator discipline that wanes. This is *coverage-driven growth*: the rate of new entries is bounded by the rate of genuinely-novel task classes, and asymptotes naturally as the operator's working domain becomes well-mapped. Compounding utility per session without compounding noise.

**Where to apply in techne**: Techne's auto-memory ([[reference_cc_memory_architecture]]) is observation-driven — save when something noteworthy happens. This pattern is *gap-driven* — save when no existing entry covers the task class. The two are complementary, not competing: observation-driven catches surprises, gap-driven catches structural omissions. Implementation hook: at task-completion (or `SessionEnd` if it existed), classify the task, query techne for coverage, prompt for entry creation if absent. Composes with [[architectural-patterns#Inline tech-debt annotation convention with named upgrade condition]] — both turn deferred work into a discoverable record at the moment it's relevant.

---

---

## Git-hook installation that's idempotent, env-escapable, AND rebase-aware

- **Source**: https://github.com/ColinVaughn/CodeGraph
- **Surveyed**: 2026-06-16

Three orthogonal safety primitives composed into one install path:
1. **Sentinel comments** wrap the injected hook block so re-running install is a no-op; uninstall greps the markers and excises cleanly.
2. **Runtime escape hatch** — `CODEGRAPH_SKIP_HOOK=1` disables the hook without uninstalling, so a developer can opt out for a single command (`git commit -m 'fix' && CODEGRAPH_SKIP_HOOK=1 git rebase main`).
3. **Rebase-awareness** — the hook body checks for `ORIG_HEAD` / `MERGE_HEAD` presence at fire time and short-circuits during rebase, merge, and cherry-pick operations.

**Why interesting**: Git hooks shipped as a default opt-in are famously a source of "auto-tooling broke my workflow" complaints. Most projects address one concern (usually idempotent install) and fail the other two: re-running install double-injects, the env-var escape doesn't exist so every dev has to manually disable, hooks fire during rebases and corrupt mid-operation state. Composing all three closes the failure-mode triangle. The rebase-awareness specifically is the hard-won bit — discovering that `ORIG_HEAD`/`MERGE_HEAD` presence signals "you are mid-rebase, get out of the way" is the kind of operational knowledge that lives in commit messages of bug fixes, not in tutorials.

**Where to apply in techne**: Techne's hook lifecycle is in-process for session boundaries, not git-hook-based — but if techne ever installs git hooks (the natural place would be for [[architectural-patterns#Commit-mapped ledger as hard consistency contract between parallel repos]], where memory state needs to track code commit SHAs), this pattern is the playbook. Cabal too: if a persona ever auto-installs git hooks for repo-scoped task management, the three-primitive composition is the production-safe shape. Composes with [[architectural-patterns#Externalized `.git` to neutralize hook-injection on agent worktrees]] — that pattern defends against *malicious* hooks at the structural level; this pattern makes *your own* hooks operationally safe.

---

---

## Golden-file snapshot tests for generated artifacts (apply to prompt rendering)

- **Source**: https://github.com/shaz/gojira
- **Surveyed**: 2026-06-16

The template renderer's output (GitHub Actions workflows, Kustomize manifests) is regression-tested by full-file comparison against committed snapshots in `tests/golden/`. The design doc states the stance explicitly: "template output is the product — snapshot-test it." Rather than asserting individual keys in the rendered YAML, the entire artifact is the contract; any generator change produces a visible diff at PR-review time.

**Why interesting**: Snapshot testing itself is standard. The transferable insight is the *philosophical commitment* paired with *what counts as "the product"*: when the artifact a system generates is the deliverable, individual-field assertions miss the forest. A full-file snapshot makes intentional changes and regressions equally legible — both surface as diffs in PR review rather than buried in test fixtures. The author treating "the rendered file is the contract" as a design principle (not a test-strategy choice) is the lift.

**Where to apply in techne / cabal**: Direct fit for prompt rendering. Techne's session-start injection, persona system prompts, MCP tool descriptions — all are *rendered artifacts*, and changes to the rendering pipeline currently have no regression check beyond "does the agent still feel right." Committing the rendered output as golden files would catch silent regressions (template variable rename, accidentally double-injected memory block, lost frontmatter) at PR review. Composes with [[architectural-patterns#Promptfoo skill evaluation harness (task × model × skill-arm matrix)]] — golden files catch *deterministic* regressions in what the prompt says; promptfoo catches *behavioral* regressions in what the prompt causes. Both are cheap to adopt; together they cover the legibility gap that LLM-driven tooling has by default.

---

---

## Invariants and eval results stored as first-class memory cells in the same store

- **Source**: https://github.com/H-XX-D/recall-memory-substrate
- **Surveyed**: 2026-06-16

The eval harness runs four case types (search, semantic, subgraph, invariant) and stores results as typed `RecallEvalResult` cells in the same SQLite store the evals test. Five model-free structural invariants audit graph integrity continuously; their pass/fail history is queryable alongside operational memory through the same APIs. The system can reason about its own past reliability without separate observability infrastructure.

**Why interesting**: Most projects treat eval results as ephemeral CI artifacts that disappear after the run — score, log, forget. This collapses the "did the system work" axis into the "what does the system know" axis, in the same data plane. Two consequences: (a) the agent can act on its own degradation signals at recall time ("don't trust this subgraph, invariant X has been failing on it for two weeks"), and (b) eval history is searchable the same way knowledge is, so you can ask "when did pattern Y start failing" without leaving the substrate. Distinct from external monitoring/dashboards because the consumer of the signal is the same actor producing it.

**Where to apply in techne**: Techne already produces quality signals at runtime — structural thread heuristic scores, hook lifecycle outcomes, summarization confidence — and they currently vanish after the producing call. Persisting these as typed observation rows (`kind="quality_signal"` with `source`, `subject`, `score`) would let recall surface "this thread has had three failed reflect-passes" alongside its content. Composes with [[architectural-patterns#Decay-at-query-time with importance-tiered half-lives]] — that pattern computes one score, this one accumulates a *history* of scores. Composes also with the reflect-experiment ([[project_reflect_experiment_2026-06-15]]) — reflect outcomes are exactly the kind of self-quality signal worth retaining.

---

---

## Loop policy as a dispatch-time decision, not a runtime behavior

- **Source**: https://github.com/agentlas-ai/Hephaestus
- **Surveyed**: 2026-06-16

A mode-classification step runs *before* any agent executes and emits two outputs: the selected mode (single-agent / team-builder / packager) and a `loop_policy` (`none` / `self-correct` / `verified`). The loop policy binds how much iteration depth the agent is allowed — `none` is one-shot, `self-correct` lets the agent re-attempt on its own evidence, `verified` requires an external check loop. The choice is derived from task complexity and whether the agent will perform external writes; it's a *dispatch-time* output, not a knob the agent flips at runtime.

**Why interesting**: Most agent frameworks let the executing agent decide whether to retry / verify / self-critique, which means iteration depth is an emergent property of the model's introspection at the moment of doubt — fine when the model is right about its uncertainty, expensive when it isn't. Pulling the decision earlier in the pipeline, into classification, makes trust an explicit dispatch output: the router decides how much iteration this task class warrants, the agent inherits the budget. Composes with [[architectural-patterns#Two-gate autonomy with recurrence-threshold floor and severity-override ceiling]] — that pattern gates *whether* to act on a proposal; this gates *how much effort* to spend producing it. Two complementary axes of trust-as-policy.

**Where to apply in cabal / techne**: Cabal's persona-dispatch already classifies which persona to route a task to; adding a loop-policy field to the dispatch output is the natural extension. Personas would receive their iteration budget at activation, not negotiate it mid-task. Techne's hook lifecycle could attach the same field on `task.start` so downstream tools (eval runs, summarization passes) know whether they're in a "one shot" or "verify before commit" context without each tool re-asking. The composition matters: a `verified` loop policy + a tool-boundary [[architectural-patterns#Augmentation-only write guard enforced at the tool boundary]] is a stronger correctness contract than either alone.

---

---

## Markdown claims as runtime-verifiable contracts (with negation as first-class)

- **Source**: https://github.com/theDakshJaitly/mex
- **Surveyed**: 2026-06-16

A parser pass over markdown extracts inline code spans, fenced blocks, and bold dep/version text into typed `Claim` objects (path, command, dep, version), each carrying a source location and a `negated` flag. Eleven independent checkers consume the claim stream and verify each one against the live filesystem / process / package manager. The output is a typed `DriftIssue` per failed claim — "this doc says X, reality says Y." Token-free because verification is mechanical, not LLM-driven.

**Why interesting**: Treats prose documentation as a *structured runtime contract*, not opaque text. Most doc-linting tools verify spelling or link integrity; this verifies *truth-of-content*. The negation flag is the deliberate move — "we do NOT use X" is a first-class assertion that fails when X *does* appear, catching the failure mode where docs say "this is deprecated" but the code still uses it. Composes two ideas: structured extraction from semi-structured input + mechanical truth-checking instead of LLM judgment.

**Where to apply in techne**: Memory entries are markdown with embedded claims — file paths, function names, flag names, commit refs (see [[reference_cc_memory_architecture]]'s "before recommending from memory: check it exists" rule). A drift-detection pass over the memory store could verify the claim "techne#57 is open" by hitting the GitHub API, or "`techne/sync/adapter.py` exists" by stat'ing the path. The negation handling matters for memory: feedback memories often say "do NOT do X" — drift-checking those means verifying X still doesn't appear. Pairs naturally with [[architectural-patterns#Decay-gated injection via `last_read` session counter]] — that one decides what to *show*, this one decides what's still *true*.

---

---

## Negative-scope routing fixtures: the router must NOT claim these queries

- **Source**: https://github.com/agentlas-ai/Hephaestus
- **Surveyed**: 2026-06-16

The routing benchmark JSONL includes explicit **anti-fixtures** alongside positive routing cases: queries the router must *deflect* to a hub fallback rather than confidently match to a specific destination. They're tagged (`anti`, `hub`) and scored with the same rigor as positive cases — if the router picks a specific destination on an anti-fixture, the eval fails. Out-of-scope is a first-class assertion target, not the implicit complement of "did the right thing fire."

**Why interesting**: Routing evals reliably suffer the same disease as RAG retrieval evals — heavy positive-case coverage, near-zero negative-case coverage. The result: a router that confidently matches everything to *something* passes its evals while doing exactly the wrong thing on edge queries. Anti-fixtures invert the failure mode: a router that hallucinates a destination on a deliberately-ambiguous query fails loudly. The discipline is "explicit scope deflection is a behavior, not the absence of behavior." Cheap to add; catches the most embarrassing class of routing regression.

**Where to apply in cabal**: Cabal's A2A persona routing makes exactly this kind of decision — "which persona should this task go to" — and currently has no visible negative-scope regression suite. When a new persona is added, the routing model becomes more eager to match queries to *something*, and tasks that should hub-fallback get misrouted instead. A small set of anti-fixtures ("this query is ambiguous on purpose, route to hub") would catch the scope-creep regression class. Composes with [[architectural-patterns#Benchmark variant trio: no-memory / memory-cold / memory-warm]] — that's variant-axis discipline (test the same task under different memory states); this is fixture-class discipline (positive AND negative cases get equal coverage). Both are cheap forms of eval rigor that catch silent classes of regression.

---

---

## Protocol-enforced `check()`/`apply()` idempotency with ref-on-skip teardown

- **Source**: https://github.com/shaz/gojira
- **Surveyed**: 2026-06-16

Every provisioner step implements a two-method Protocol: `check() -> str | None` returns the existing resource ref if already present, `apply() -> str` creates and returns the ref. The engine records both DONE and SKIPPED outcomes in a DB ledger keyed by `step_name@scope_key`. The `scope_key` suffix (`step@dev`, `step@qa`) lets the same step sequence run independently per project / per env without duplicating step classes. Re-runs are safe, resume-from-failure works, and — the load-bearing detail — teardown enumerates the SKIPPED rows to learn what exists *without re-querying the cloud API*, because each ref was stored at check time.

**Why interesting**: Two compositions on top of a check/apply idempotency engine that most reconciler patterns (Terraform plan/apply, k8s reconcile) don't bother with. (a) Protocol-level enforcement: a step that forgets `check` doesn't typecheck — the discipline is structural, not advisory. (b) Storing the ref on the SKIPPED row turns the ledger into a teardown manifest *for free*, no external state reconciliation needed. (c) `scope_key` namespacing decouples step logic from multiplicity — N environments means N rows, not N step subclasses.

**Where to apply in techne / cabal**: Techne's hook lifecycle and MCP capability registration could adopt this for setup steps that should be idempotent across restarts — e.g., registering MCP tools, creating DB schemas, wiring sync adapters. Storing the resource ref on skip means `techne teardown` could enumerate what to remove without re-probing the host. Cabal's per-persona orchestration maps directly onto `scope_key=persona_id` — same capability-check step keyed per persona, independent idempotency, single step impl. Composes with [[architectural-patterns#Capability probing with fallback dispatch]] — that one decides *what's available*, this one decides *what's already done*; together they give you a full provisioning loop.

---

---

## SRI for content integrity + ed25519 for provenance, gated at different lifecycle points

- **Source**: https://github.com/EliasT5/spectre-agent
- **Surveyed**: 2026-06-16

Code-tier module bundles are validated twice with different mechanisms at different lifecycle points: **SHA-384 SRI** is checked at fetch time (the loader refuses to import the bundle if the hash doesn't match), and **ed25519 author signature** is checked at registration time (the manifest is rejected if the signature doesn't verify against a trusted author key). Either check on its own is insufficient — SRI proves the byte-stream wasn't tampered in transit but not who made it; ed25519 proves authorship but not which version was loaded.

**Why interesting**: Package managers (apt, npm) combine these checks but typically as one fused step. Splitting them across lifecycle stages is the deliberate move: an operator can install signed modules from a trusted author once (provenance check), then run them repeatedly with byte-level integrity checks each load (content check). A compromised CDN can't silently swap a bundle (SRI catches it); a compromised signature key can't backdoor previously-installed modules (their cached SRI still has to match). The two layers fail independently. Distinct from "use signing" because the lifecycle separation is what makes the failure modes orthogonal.

**Where to apply in techne / cabal**: Neither techne (hook lifecycle, skill loading) nor cabal (persona plugin manifests) currently has cryptographic provenance for installed agent-side code. Adding both checks would be cheap (ed25519 keys + signature verify on `techne install` / cabal persona registration; SHA-384 SRI check on every load). The lifecycle split matters specifically because [[feedback_memory_portable_not_client_tied]] requires hooks/skills to flow across clients — when a memory entry says "load this skill," you want to verify both who said it (provenance) and that what you're loading right now matches what was reviewed (content). Composes with the standalone [[architectural-patterns#Markdown body + YAML frontmatter + directory hierarchy as the de-facto agent-substrate format]] entry — SKILL.md frontmatter is the natural place to put both the signature and the SRI hash.

---

---

## SSE upsert/finalize protocol: streaming-aware replay buffer with bounded growth

- **Source**: https://github.com/looptroop-ai/LoopTroop
- **Surveyed**: 2026-06-16

A two-event SSE protocol decouples token-by-token producers from replay buffers. Events tagged `op: 'upsert' + streaming: true` **replace** the prior event at the same slot in the buffer rather than appending; a subsequent `op: 'finalize'` event **removes** the slot entirely. The result: a 500-token streamed message takes one buffer slot during streaming, zero after finalization, and late-joining clients still see a coherent replay of completed messages.

**Why interesting**: The usual SSE buffer designs each fail in a specific way for token streams. (a) Append-every-event grows unbounded — a token stream of length N produces N buffer entries. (b) Latest-event-only loses history for replay. (c) Per-message buffers require the producer to know its message-id taxonomy. This three-state shape (upsert-during, finalize-after) threads the needle: the buffer's slot is the *message identity*, not the *event count*, so token frequency is decoupled from buffer pressure, and finalization is the clean signal that "this slot is done — drop it." Composes two ideas: slot-as-identity replay + lifecycle-event finalization.

**Where to apply in techne / limenal**: Limenal's MCP gateway and techne's SSE adapter both handle streaming tool responses where token-by-token deltas are common. Currently either pays for unbounded growth or loses replay fidelity. Adopting the upsert/finalize shape would let both bound their buffer footprint while preserving "what messages can a reconnecting client replay" for the common AI-tool-result case. Pairs naturally with [[architectural-patterns#`episode_id` threading: every LLM call belongs to a logical session]] — the episode_id is the natural slot key.

---

---

## Session-type as explicit taxonomy for prompt-template selection

- **Source**: https://github.com/looptroop-ai/LoopTroop
- **Surveyed**: 2026-06-16

Prompt scaffolding ships as three named rule-sets selected by the *kind of session* the agent is in: `GLOBAL` (fresh boot, no continuity), `SAME_SESSION` (continuing the same logical thread), `CONVERSATIONAL` (multi-turn dialogue). The dispatcher chooses one at activation time; the chosen rules govern artifact-first output format, field-based error embedding, and tone. Compliance is enforced structurally (output schema disagrees with the rule-set when violated), not by post-hoc validation.

**Why interesting**: Distinct from intensity-tier variation ([[architectural-patterns#Tiered-intensity skill levels (lite / full / ultra / off in one skill file)]] — same skill, dial up insistence) and from skill activation patterns ([[architectural-patterns#SessionStart always-on context injection (no slash command required)]] — same content, different injection timing). This axis is **session continuity** — same agent, same task, but the rules of engagement differ based on whether prior context exists. The taxonomy is *named* rather than *inferred*, which means a downstream tool can ask "what session-type are we" and pick consistent format, not negotiate every call.

**Where to apply in techne**: Techne's `SessionStart` hook fires on every session start, but its injection currently doesn't distinguish "fresh boot, no recent threads" from "resumed work, existing active thread" — both get the same context dump. Adopting an explicit session-type signal (fresh vs resumed vs interactive-question-from-operator) would let techne select different injection shapes: fresh sessions get the full thread overview, resumed sessions get a delta-since-last-injection, conversational sessions get the most-recent-thread terse. The taxonomy makes "what kind of session is this" a first-class field that downstream consumers can branch on, instead of inferring it from heuristics each call. Composes with [[architectural-patterns#Resume trigger phrases]] — that one detects a specific session-type at the message level; this generalizes the axis.

---

---

## Skillbook: issue + remedy + effectiveness in one addressable record

- **Source**: https://github.com/kayba-ai/kyoko
- **Surveyed**: 2026-06-16

A "skill" record co-locates four things that are usually scattered across separate stores: the failure description (`issue`), the learned fix (`insight`), the evidence corpus (occurrences, timestamps), and **fine-grained effectiveness counters** (`helpful`/`harmful`/`neutral` tallies attached to the remedy itself). Delivery rules conditionally inject skills based on profile, workflow node, or span context. A single recall query returns "did we see this, what fixed it, and did the fix actually work."

**Why interesting**: Most agent-memory architectures split this concept across observations (what happened), insights (what we learned), and feedback (whether the insight worked) — three separate retrievals to reconstruct one decision. Worse: feedback often lives in eval logs or dashboards, not memory, so the agent recalls "Y is the fix" without recalling "Y has been harmful 2 of 3 times." Kyoko collapses the loop: the remedy carries its own track record, so recall surfaces the *calibrated* fix, not just the proposed one. The harmful/neutral counts are what makes this distinct from generic issue-tracking — most bug trackers record "was this closed" but not "did the closure stick."

**Where to apply in techne**: Direct fit for a new memory shape — `feedback`-typed entries already record "do this / don't do this" guidance ([[feedback_decision_authority]], [[feedback_partnership_stance]], etc.) but carry no effectiveness signal. A skill-like record (`issue` + `insight` + `helpful/harmful/neutral` counters) would let the operator's accept/reject choices on auto-memory candidates feed back into a calibration loop: rules that get rejected repeatedly demote themselves; rules that get applied without correction stay. Composes with [[architectural-patterns#Standing programs as model-free graph reflexes (with self-penalizing watchers)]] — that pattern is the *mechanism* for self-penalization in a graph store; this is the *schema shape* that makes effectiveness data co-located with the rule it qualifies.

---

---

## Standing programs as model-free graph reflexes (with self-penalizing watchers)

- **Source**: https://github.com/H-XX-D/recall-memory-substrate
- **Surveyed**: 2026-06-16

Hyperedges in the memory graph can embed typed executable programs (`watch`, `drift`, `quorum`) that score live graph state against thresholds and file their results back through the normal admission gate as new edges/cells. The programs are model-free — pure deterministic graph logic, no LLM in the loop. The genuinely sharp move: these programs are themselves *calibrated actors* in the same evidence calculus as everything else, so a chronically-wrong watcher sees its own confidence sink as the contradictions it raises get refuted. The watcher self-penalizes.

**Why interesting**: Most monitoring systems exempt the monitor from the same scoring they apply to their targets — a noisy alerting rule keeps alerting forever unless a human silences it. Making the watcher subject to the same calibration loop as the data it watches gives you automatic noise suppression for free. Distinct from feedback loops because the loop is structural (same admission gate, same calibration math), not bolted on as a separate "alert quality" pipeline. Composes two ideas: trust monitoring offloaded to deterministic logic + the monitor's output enters the same evidence economy as the thing being monitored.

**Where to apply in cabal / techne**: Cabal's multi-persona dispatch has no inter-persona trust drift detector — when persona A's outputs to persona B start being downstream-rejected, nothing accumulates that signal. A model-free `quorum`-style watcher (over A→B handoff outcomes) that files concerns through cabal's existing event log would be a circuit breaker without a separate observability stack. Techne's reflect-experiment is calibration-adjacent and a natural fit for the self-penalizing watcher shape — a `drift` program that flags candidate-vs-live divergence trends, with its own confidence calibrated against whether Richard accepted or rejected the flagged candidates.

---

---

## Strict-identity + advisory-metadata schema split for forward-compat

- **Source**: https://github.com/theDakshJaitly/mex
- **Surveyed**: 2026-06-16

The JSONL event schema explicitly splits fields into two policy classes. **Strict-validated**: `kind` (an enum the reader rejects unknown values for). **Free-form, never enum-validated**: `source`, `status`, `trace` (advisory strings the reader passes through unchanged). The split is documented at the schema level with rationale: "future readers don't reject historical entries when new sources / statuses appear."

**Why interesting**: Schema evolution is usually treated as a versioning problem (bump the version, write a migration). This treats it as a *field-by-field policy* problem: decide up-front which fields are *identity* (semantics depend on the value being known) and which are *metadata* (semantics tolerate unknown values). Identity gets enum-validation; metadata gets pass-through. The split prevents the failure mode where adding a new source string requires a coordinated rollout across producers and consumers — producers can introduce new sources unilaterally because consumers never gate on them.

**Where to apply in techne**: Techne's observation and thread schemas (SQLite-backed) likely have enums where free-form would absorb future change better. Candidates: `source` (the tool/client that produced the observation), `status` on threads, `tags`. Worth a schema audit: for each enum-typed column, ask "does the reader's behavior actually change if it sees an unknown value here? If no, the enum is debt." Composes with [[architectural-patterns#`episode_id` threading: every LLM call belongs to a logical session]] — that pattern adds an identity field; this pattern decides what kind of validation it gets.

---

---

## Three-tier plugin delivery: native / data / code (with a deliberate zero-execution tier)

- **Source**: https://github.com/EliasT5/spectre-agent
- **Surveyed**: 2026-06-16

Each installable module declares one of three delivery modes:
- **native** — first-party route compiled into the shell; full trust, no sandbox
- **data** — declarative JSON widget config; the shell interprets it but no third-party code runs at all
- **code** — third-party ESM bundle loaded into an SRI-verified, sandboxed iframe

Permissions and capabilities are scoped per tier — a `data` module can be dynamic (config-driven layout, formatted output) without ever executing untrusted JavaScript.

**Why interesting**: Most plugin systems present a binary choice — either you accept arbitrary code execution (and need WASM/iframe/CSP sandboxing) or you accept static config (and lose dynamism). The `data` tier is the under-recognized middle: declarative dynamism. JSON describes the widget's structure, the shell does the interpretation, and the worst-case from a malicious authored module is bad rendering, not arbitrary execution. Distinct from feature flags (which gate *whether* something appears) and from manifest formats (which describe *what* something is) — this gates *the execution model itself*. Composes with [[architectural-patterns#WebAssembly plugin sandbox for untrusted extensions]] — that pattern is the code-tier solution; this one says "don't reach for the code tier when data will do."

**Where to apply in limenal / cabal**: Limenal's per-persona policy could adopt a tiered execution-model gate: persona-scoped tools could be `data` (pure config-driven, no execution — for things like "render this dashboard with these filters"), `native` (built-in adapter, fully trusted), or `code` (sandboxed MCP call to a user-authored handler). The data tier matches a lot of what currently lives as "tool descriptions" — they're declarative, the agent interprets them, no third-party execution. Promoting that to a named tier with policy attached makes the trust gradient explicit. Cabal persona skills that are pure prompt + config (no executable hooks) belong in the data tier; skills with shell scripts or Python belong in code.

---

---

## Tool allowlist declared as content inside the activation slash-command file

- **Source**: https://github.com/H-XX-D/recall-memory-substrate
- **Surveyed**: 2026-06-16

The slash command markdown file that wires up a session also declares the allowed-tools set inline, scoping what the agent may call during a recall-wired session to a specific set of `recall*` commands. The sandbox declaration is content, not configuration — it lives in the same file an operator reads to understand what the command does, and changes to the allowlist are reviewable as documentation diffs.

**Why interesting**: Distinct from [[architectural-patterns#Claude Code slash-command surface as the platform UX]] (which is about slash commands as the UX shape) and [[architectural-patterns#SessionStart always-on context injection (no slash command required)]] (which is about activation timing). The lift here is *what kind of artifact a sandbox is*: most permission systems live in JSON config files separate from the prompts they constrain, so the permission and the intent drift independently. Co-locating the allowlist with the activation contract means the human-readable description of what the agent should do and the machine-readable description of what it's allowed to do can never disagree without a single-file edit.

**Where to apply in cabal / limenal**: Cabal's persona activation could declare per-persona tool allowlists inside the persona prompt file itself rather than in a separate `permissions.toml`. Limenal's per-persona credential policy currently sits in config; bringing it inline to the persona's activation contract would make "what can this persona do" inspectable as the same artifact as "what is this persona for." Composes with [[architectural-patterns#"Opinions" as a versioned data artifact with justification-required overrides]] — both move metadata from buried config to legible co-located documentation, but ponytail/opinions are about *defaults*, this is about *capabilities*.

---

---

## Trust-level ladder for assertions: L0 generated → L1 repeated → L2 regression → L3 human-approved

- **Source**: https://github.com/kayba-ai/kyoko
- **Surveyed**: 2026-06-16

Every machine-generated check (eval, assertion, invariant) carries a trust-level field that promotes through four named tiers as evidence accumulates: `L0_generated` (freshly written by the system, no validation), `L1_repeated` (two stable consecutive results), `L2_regression` (catches a real before/after divergence in replay), `L3_human_approved` (manually reviewed; never reached by auto-promotion). Promotion criteria are explicit and one-directional — checks can demote on contradiction but auto-promote only when the named conditions fire.

**Why interesting**: Most eval/assertion systems treat a check as a boolean (passes / fails). This collapses two orthogonal questions into one: "did the check fire?" and "do I trust the check itself?" The trust ladder operationalizes the second axis as a first-class field with named promotion gates, letting downstream consumers weight L2/L3 differently from L0 (e.g., "fail the build only on L3 violations; surface L0 violations as candidates for review"). Distinct from confidence scores (which are continuous and post-hoc) because the trust level reflects *the check's track record*, not the agent's certainty on a given invocation. Composes with [[architectural-patterns#Invariants and eval results stored as first-class memory cells in the same store]] — that pattern persists eval history as data; this pattern uses that history to calibrate the eval's own authority.

**Where to apply in techne**: Memory entries could attach a trust tier with similar promotion semantics: `L0` for freshly-extracted observations (not yet referenced again), `L1` for facts that recur across sessions, `L2` for facts that survive contradiction attempts, `L3` for human-confirmed (the existing [[project_reflect_experiment_2026-06-15|reflect candidates]] are L0; promotion to live store is L2/L3). Retrieval could weight by trust tier — `L3` entries inject verbose, `L0` entries inject only on direct match. Maps to the existing [[architectural-patterns#Decay-at-query-time with importance-tiered half-lives]] pattern: tier and decay are orthogonal axes, both modulating recall ranking.

---

---

## Two-gate autonomy with recurrence-threshold floor and severity-override ceiling

- **Source**: https://github.com/kayba-ai/kyoko
- **Surveyed**: 2026-06-16

Autonomy is not a boolean. The pipeline has **two explicit gates** — surface→propose, propose→apply — each independently switchable between HITL and auto-advance. Both gates require a numeric `recurrence_threshold` (the same issue must appear N times before auto-advance fires), with a `severity_override` that can shorten the threshold by one *but never reduce it to one*. High-severity failures get a shortcut; pure one-off observations never auto-apply, regardless of severity.

**Why interesting**: Most agent-autonomy switches are binary (`autonomous: true/false`), which collapses two independent decisions — "is this evidence sufficient" and "is this consequence acceptable" — into one knob. Kyoko's split says: trust isn't a slider, it's a *graph* with two gates and a floor. The floor at n=1 is the sharp move: severity can accelerate trust but can't manufacture it from a single observation, which prevents the failure mode where a critical-looking one-off triggers unrecoverable action. Distinct from rate-limiting (which caps how often you can act) and from confidence thresholds (which gate on a single metric) — this gates on *repetition as evidence*.

**Where to apply in cabal**: Cabal's persona orchestration could adopt the two-gate model for agent-proposed context mutations (memory writes, identity-config changes, capability additions): gate 1 = "should this surface to operator," gate 2 = "should this apply." Compatible with [[feedback_decision_authority]] — Raven still acts autonomously, but on observation streams that recur N times rather than fire-on-first-signal. The recurrence requirement is what makes autonomous action *evidence-bound* rather than *reactive*. Composes with [[architectural-patterns#Drift-gated trust checkpoint as a blocking session-entry contract]] — that one halts a session on stale state; this one says "and don't auto-repair on the first sighting either."

---

---

## Union-composition merge driver for derived/computed JSON artifacts

- **Source**: https://github.com/ColinVaughn/CodeGraph
- **Surveyed**: 2026-06-16

A custom git merge driver is registered for `graph.json` (the computed code-graph artifact). When two branches modify the file, the driver union-composes them with deterministic semantics: **nodes from `other` win on ID collision** (newer-branch-wins for the conflict-resolution direction), **edges deduplicate by identity tuple**, and **hard caps** (50 MB file size, 100k nodes) cause the merge to fail loudly rather than silently truncate. The artifact is treated as a first-class versioned git asset — *not* gitignored, *not* punted to "rebuild after merge."

**Why interesting**: Derived/computed artifacts are usually gitignored ("rebuild from sources, don't track") or accepted as unresolvable merge conflicts ("you'll have to manually rebuild"). Treating one as *mergeable with semantic rules* is uncommon and deliberate. The fail-loud cap is the production-readiness signal: an unbounded merge of two arbitrary computed artifacts can produce nonsense, so the driver refuses past a threshold instead of producing a corrupted file. Composes two ideas: derived state as a versioned asset + semantic (not textual) merge logic with safety bounds. Distinct from regenerate-on-merge because the merge driver runs during the merge itself, not as a post-merge build step.

**Where to apply in techne**: Techne's live SQLite store is inherently unmergeable across branches (SQLite is a binary format with no union semantics), but the same pattern *does* apply to **memory exports / backups in JSON form**, which is the natural multi-machine sync mechanism. If techne ever ships an "export memory to portable JSON, sync via git, re-import" flow, a merge driver with union-by-identity semantics + per-thread node caps would make that flow safe under concurrent edits from two operator machines. Composes with [[feedback_memory_portable_not_client_tied]] — that principle wants memory to flow across clients; this pattern is one of the mechanisms that makes the flow operationally safe. Also worth noting alongside [[architectural-patterns#Augmentation-only write guard enforced at the tool boundary]] — both ensure that automated multi-source writes don't silently lose information.

---

---

## Fast membership-query tier as compositional pre-filter for retrieval

- **Source**: https://github.com/Mikhail-Za/tastebud-memory
- **Surveyed**: 2026-06-17

The memory system explicitly positions itself as a **pre-filter**, not a replacement for full retrieval. The MCP server exposes fast membership queries ("did project X touch this codebase last week?") that decode in milliseconds against compact vector representations, and the agent uses those answers to decide which expensive log payloads to actually load. The architecture is two-tier by design: cheap membership/enumeration first, full retrieval only when the index gates positive.

**Why interesting**: Most agent memory systems collapse two orthogonal operations into one: "find similar things" and "did this happen." Embedding-search-as-everything makes both into approximate similarity over the same payload size — fine for "find similar," wasteful for "did this happen" (you don't need the full content to answer yes/no). Tastebud's stance: membership/enumeration is a distinct first-class operation that deserves its own data structure and its own pre-fetch query. The composition is two-tier-retrieval applied to *agent memory* specifically — most agent memory architectures don't have the membership tier at all. Distinct from b-tree-then-row-fetch (classic database) because the membership tier here is *semantic* (does this concept appear) rather than *exact* (does this key exist).

**Where to apply in techne**: Techne's sync adapters and MCP server currently return thread data via tag/FTS queries. Adding a fast membership tier — "which threads touched this entity / commit / file?" — that returns a bare ID list before any payload load would let the agent prune the candidate set cheaply before deciding which full threads to inject. Composes with [[architectural-patterns#Decay-gated injection via `last_read` session counter]] — that pattern decides verbosity at injection; this pattern decides *whether to inject at all* by gating on membership first. Two cheap tiers compose into a smarter retrieval pipeline than either alone.

---

---

## Kill-gated eval with frozen Phase 0 benchmark as anti-rationalization device

- **Source**: https://github.com/Mikhail-Za/tastebud-memory
- **Surveyed**: 2026-06-17

Before any live deployment, the team writes down a frozen baseline of ~10 representative queries with **honestly-scored** expected outputs — and commits the scores to disk where they can't quietly change later. Each subsequent eval phase (adversarial blind review, vector-layer backtest) must pass an explicit numeric threshold against that frozen baseline, with a hard **kill condition** documented inline: "if tagging reliability fails this gate, stop — downstream improvements cannot compensate." No softening into "needs more work next sprint."

**Why interesting**: The frozen-pre-eval-baseline shape is standard ML practice. The genuinely sharp move is the *kill condition* — explicit, written-down, structural authorization to halt the whole project if a specific gate isn't cleared. Most teams have implicit kill criteria that quietly degrade into "ship anyway, fix later" once the team has invested effort. Writing the kill condition into the methodology *before* knowing the results is an anti-rationalization device: it makes goalpost-moving structurally visible, because cleared-gate-vs-killed-project is a binary that's hard to reframe after the fact. Distinct from [[architectural-patterns#Promptfoo skill evaluation harness (task × model × skill-arm matrix)]] (matrix layout for measurement) and [[architectural-patterns#Benchmark variant trio: no-memory / memory-cold / memory-warm]] (variant axis): those answer *what* to measure; this answers *what to do when the measurement says stop*.

**Where to apply in techne / cabal**: Techne's structural thread heuristic and cabal's persona routing both ship without kill-gates — changes go live when they "feel right." A frozen baseline of representative queries + an explicit "if score < X on the baseline, do not ship" gate would catch the regression class where a change feels like an improvement on the dev examples but degrades the actual distribution of queries. Composes with [[architectural-patterns#Trust-level ladder for assertions: L0 generated → L1 repeated → L2 regression → L3 human-approved]] — that pattern is about which assertions to trust; this is about which deployments to refuse.

---

---

## LLM writes only to inbox, deterministic sweeper owns the live store

- **Source**: https://github.com/Mikhail-Za/tastebud-memory
- **Surveyed**: 2026-06-17

The LLM's *only* write surface is a single small JSON file in an inbox directory. A separate deterministic process — the "sweeper" — runs on a schedule, reads the inbox, performs idempotency checks against the authoritative store, and is the only writer to the live data. The LLM cannot touch the live store; the sweeper cannot generate new content. The two responsibilities are structurally separated, not just role-tagged in a prompt.

**Why interesting**: The standard pattern for LLM-driven memory is "LLM generates → tool writes to store directly" — one call, one mutation. The failure mode is silent: a hallucinated tag, a prompt-drift episode, or a malformed extraction goes straight into the authoritative store and pollutes future recall. By splitting into *inbox + sweeper*, the LLM-as-classifier and code-as-mutator boundary is structural — the sweeper can be reviewed/tested in isolation, and corruption is contained to a review queue you can inspect before merge. Distinct from [[architectural-patterns#Augmentation-only write guard enforced at the tool boundary]] (OKF) — that enforces an *invariant* at the write site; this enforces a *workflow separation*. Both close the "LLM silently corrupts the store" failure mode; one with a rule, one with a topology. Composes with [[project_reflect_experiment_2026-06-15|reflect's candidates vs live split]] — that pattern is the techne version of the inbox/sweeper shape applied to memory.

**Where to apply in techne**: Techne's hook lifecycle currently lets hooks write back to memory directly. Routing those writes through an inbox + deterministic committer would contain any future LLM-driven drift to a review queue rather than the live store. The reflect-experiment is already this shape for one category (candidate memories); generalizing it as the *default* shape for any LLM-driven write — not just summarization — is the lift. Cabal's persona orchestrator similarly: persona outputs become inbox entries, a deterministic committer reconciles them with existing state.

---

---

## Memory validity tied to VCS reachability (resume auto-invalidates after rebase/force-push)

- **Source**: https://github.com/oldskultxo/aictx
- **Surveyed**: 2026-06-17

On resume, the runtime validates stored work-state against the live git repo before treating it as actionable. The check has three parts: (a) the recorded branch still exists, (b) the recorded commit is still **reachable** from HEAD's history, (c) the modified-files set still makes sense in the current tree. If reachability fails — most commonly because the original branch was rebased, squashed, or force-pushed away — the stored state is auto-invalidated rather than silently replayed against a code base that doesn't match what was observed.

**Why interesting**: Most agent-memory systems decouple memory from code state entirely — memory is just facts in a store, code is somewhere else, and reconciling them is implicit ("the agent will notice if something looks wrong"). Anchoring validity to a VCS invariant (commit reachability) is portable, zero-config, and *operates without the agent reasoning about it* — the runtime decides whether to surface the memory based on a deterministic check, not an LLM judgment about whether the memory still applies. The reachability move specifically (not just "what commit was this against" but "is that commit still in HEAD's history") is the operational lift: a commit that's been rebased away is not reachable from HEAD, even if the SHA still exists in reflog. Composes with [[architectural-patterns#Commit-mapped ledger as hard consistency contract between parallel repos]] — that pattern *records* the commit-to-memory mapping; this pattern *acts on* the mapping at resume time, detecting force-push / rebase invalidation.

**Where to apply in techne**: Techne's structural thread heuristic could incorporate a git-reachability check on threads recorded against a specific commit. When the operator resumes work and the recorded commit is no longer reachable from any current branch HEAD (the most common cause: rebased dev branch), the thread's facts that referenced that commit demote to a "potentially stale" tier rather than inject as live. Cheap (one `git merge-base --is-ancestor` per recorded commit), high-signal — catches the failure mode where memory confidently references state that no longer exists in the repo's working history. Composes with [[architectural-patterns#Drift-gated trust checkpoint as a blocking session-entry contract]] — that pattern blocks on stale state at session entry; this is one of the specific staleness checks that pattern could include.

---

---

## Named engrams as context-pressure checkpoints (reset to milestone, not just compact uniformly)

- **Source**: https://github.com/fajarhide/omni
- **Surveyed**: 2026-06-17

The session tracker maintains two parallel structures: **engrams** (named subtask milestones the agent has explicitly demarcated — "scaffolded the auth module," "all unit tests passing") and a **hot-file access-count map**. When token pressure crosses configurable thresholds (65% warn, 82% critical), the agent protocol instructs not just "summarize more aggressively" but **"reset to the last engram"** — reverting to a named checkpoint state rather than performing uniform compaction.

**Why interesting**: Standard context-pressure responses do uniform compaction — summarize older messages, drop low-relevance facts, hope nothing important got dropped. This is the right shape when you have no semantic markers but is wasteful when you do. Engrams give the agent a natural unit to reset to: "you finished the auth module two engrams ago, drop everything between then and now and re-derive from the engram summary." The composition is two ideas — pressure threshold (when to act) + named checkpoint (what to act on) — into a structured recovery path rather than a single hard cutoff. The agent gets a *graceful* degradation curve, not a cliff. Distinct from [[architectural-patterns#`episode_id` threading: every LLM call belongs to a logical session]] because engrams are *self-declared milestones inside a session*, not call-level identity.

**Where to apply in cabal**: Cabal's multi-persona orchestrator could adopt the engram/pressure pattern to coordinate graceful context hand-off — when persona A's session approaches its limit, reset to the last engram, hand off the engram summary to persona B with fresh budget. The engram becomes the persona-to-persona handoff artifact, not a fresh prompt. Composes with [[architectural-patterns#Loop policy as a dispatch-time decision, not a runtime behavior]] — that pattern decides iteration budget up front; engrams give a way to *recover* when the budget runs out mid-task without losing everything since the last checkpoint.

---

---

## Positional-contagion line scorer: severity propagates to the next N lines

- **Source**: https://github.com/fajarhide/omni
- **Surveyed**: 2026-06-17

Terminal output lines are first scored into four tiers (Critical / Important / Context / Noise) with numeric weights based on content match. After tier assignment, a positional pass elevates the **next 5 lines following any Critical line** to at least Important (0.7). The heuristic is structural, not content-based: "the lines immediately after a bad signal are usually the context the human needs to act on it," which recovers stack-trace bodies, error continuations, and stderr breadcrumbs without any per-language regex.

**Why interesting**: Most signal-extraction systems do per-line classification independently — each line is scored on its own content. This misses the dominant pattern in terminal output: errors are *spans*, not points. An exception's first line names the type; the next 4-5 lines name the stack frames; everything after is context. Without contagion, a content-based scorer keeps the "ValueError" line and discards the frames that explain it. The 5-line window is calibrated, not arbitrary — long enough for a typical stack trace, short enough that runaway escalation doesn't promote the whole rest of the log to important. Composes one trivial idea (severity tier) with one structural insight (severity has range) into a system that catches what raw severity scoring misses.

**Where to apply in techne / cabal**: Techne's hook output summarization scores tool output for what to retain in memory; a positional-contagion pass would catch the "preserved the error line but not the stack frames" failure mode. Cabal's inter-agent message scoring (when one agent emits a structured failure for another to handle) maps the same way — the 5 messages after the failure signal probably carry the context the receiver needs to recover. Composes with the existing [[architectural-patterns#Tiered loop policy derived at classification time]] — that pattern decides iteration depth; this decides what to *retain* of each iteration's output.

---

---

## Pre-compact delta guard: don't re-inject summaries that haven't changed

- **Source**: https://github.com/fajarhide/omni
- **Surveyed**: 2026-06-17

The `pre_compact` hook serializes session state (errors, hot files, loop checkpoints, engrams) into a structured token budget and emits it for injection. The deliberate move: a fingerprint of the serialized payload is compared against the last emitted fingerprint, and if they match the hook **emits a minimal no-op signal** rather than re-injecting the same summary. Long-running sessions don't accumulate redundant copies of an unchanged state.

**Why interesting**: Compaction hooks fire on a schedule (every N tokens, every N minutes, every tool call) regardless of whether session state has actually evolved. Without a guard, the same "you have these 3 hot files and 2 active errors" summary gets re-pinned into context every compaction cycle, displacing useful content with redundant restatements of unchanged facts. The fingerprint-and-skip discipline is cheap (one hash compare) and catches a common failure mode that is easy to overlook because it doesn't manifest as a bug — context just gets noisier over time. Distinct from caching because the *output is suppressed*, not memoized — the goal isn't to avoid recomputing the summary, it's to avoid re-emitting it.

**Where to apply in techne**: Techne's session lifecycle hooks (`SessionStart` injection, `pre_compact` if it exists) regularly re-emit thread overviews and recent observations. A fingerprint guard on each injection — "did anything in the candidate set change since last injection?" — would prevent the same thread block from getting re-pinned every compaction, freeing context budget for actually new content. Composes with [[architectural-patterns#Drift-gated trust checkpoint as a blocking session-entry contract]] — that one halts on stale state; this one suppresses re-emission of unchanged state. Both treat "unchanged" as a first-class signal worth acting on, not the absence of change.

---

---

## Telemetry flight recorder: ring buffer + freeze-on-error + agent context injection

- **Source**: https://github.com/kmgreiner3/argus
- **Surveyed**: 2026-06-17

A custom OpenTelemetry exporter writes trace/span data to an in-memory ring buffer *always-on*. On error or operator-defined trigger, the buffer is **frozen** — snapshotting the last few seconds of execution (tool calls, span timing, parent/child relationships). A local MCP server then feeds that frozen snapshot into the responding agent's context as durable memory. The design explicitly dogfoods: argus's own agents use the recorder when investigating their own incidents, and the snapshots persist into techne as memory entries the agent can recall later.

**Why interesting**: Most agent-error recovery either re-prompts with the error message ("we got X, fix it") or summarizes what the agent thinks happened. Both rely on the agent's *retrospective narrative*, which is exactly the thing degraded by the failure. Feeding actual telemetry — the real sequence of tool calls, span durations, parent/child structure — gives the agent *evidence* instead of memory. The three-primitive composition is the lift: ring-buffer-always-on (no overhead at steady state, like Linux's kernel oops buffer or BPF tools), freeze-on-trigger (zero collection cost until something matters), agent-context injection (the consumer of the data is the actor who needs it most). Adding persistence into techne completes the loop: incidents become memory, not log files.

**Where to apply in techne / cabal**: Direct fit. Techne could ship a `flight_recorder` hook that wraps tool-call execution with always-on span capture, freezes a ring buffer on error/exception, and stores the frozen snapshot as a typed observation (`kind="incident_trace"`). Cabal personas inheriting that capability would have "why did this break" answered from telemetry instead of guesswork. Substantially cheaper than running a full OTel stack — the ring-buffer is in-process Python, the freeze condition is whatever the hook decides, the persistence layer (techne) already exists. Composes with [[architectural-patterns#Invariants and eval results stored as first-class memory cells in the same store]] — that pattern persists eval invariants as memory; this persists execution evidence as memory; both treat techne as the durable record of "what actually happened" rather than a side store of facts.

---

---

## Cost-bounded discovery escalation with explicit halt conditions

- **Source**: https://datatracker.ietf.org/doc/draft-pro-adp-agent-discovery/
- **Surveyed**: 2026-06-18

ADP resolves an agent across three transport layers — SVCB DNS record → `/.well-known/agent.json` over HTTP → WebSocket session — but mandates the client **stop at the lowest layer that fully answers its query** ("If SVCB answers your question, do not issue an HTTP request"). It's not a try-each-in-order fallback chain; it's a forward-only escalation where each layer has a *stated halt condition*, so a caller that only needs an endpoint never opens a connection and a caller that only needs a fingerprint never leaves DNS.

**Why interesting**: Most multi-tier lookup code is written as "try cheap, fall back to expensive on failure" — escalation driven by *errors*. ADP inverts it: escalation is driven by *insufficiency of answer*, with halt codified per layer. That bounds cost and shrinks the side-channel/over-fetch surface (you don't reveal you're opening a live session just to read a capability list). The discipline — "name what each tier is allowed to answer, and stop there" — is the lift, not the DNS/HTTP/WS specifics.

**Where to apply in cabal**: The rookery agent-lookup path. Today contact tends toward "open the channel to find out who's there." Restructure as in-memory registry → A2A AgentCard (HTTP) → live mailbox/WSS session, with explicit halt conditions ("capability query halts at AgentCard; only a dispatch needs the session"). Avoids opening sessions for metadata reads. Composes with the rookery mailbox shape ([[reference_rookery_mcp]]) as the top escalation tier.

---

---

## Filesystem-ACL session discovery (no auth server)

- **Source**: https://github.com/cline/cline
- **Surveyed**: 2026-06-18

Multiple clients attach to a shared, long-lived hub daemon by reading a random per-process token from an owner-only discovery file; the OS filesystem ACL on that file IS the authorization boundary. The session persists across client attach/detach, so a new client joins a warm session without re-negotiating credentials or re-initializing.

**Why interesting**: solves "who's allowed to attach to this running session" for local multi-client scenarios with no central auth server and no network handshake — the file-permission bit is the capability. Distinct from a bearer-token service (needs an issuer) and from a bare unix socket (no per-process identity).

**Where to apply**: the laptop-first rookery runs several local processes (limenal multiplexer, MCP backends, Cabal, the cbl terminal) and multiple persona clients. A fs-ACL discovery-token pattern would let a new client attach to a pre-warmed limenal/MCP session without re-auth, the owner-only file as the trust boundary — lighter than issuing bearer tokens for purely same-host attach. Contrast the rookery mailbox's bearer-token model (right for networked/remote; heavier than needed for same-host).

---

---

## Flakiness as a first-class eval metric (pass@k / pass^k / flakiness triad)

- **Source**: https://github.com/cline/cline
- **Surveyed**: 2026-06-18

Agent-behavior smoke tests run N trials per scenario and report three distinct metrics: pass@k (≥1 of k passes), pass^k (all k pass — reliability), and a flakiness rate (mixed pass/fail across trials). e2e runs Docker-isolated real tasks nightly. Flakiness is tracked and triaged separately from correctness.

**Why interesting**: treats non-determinism as its own quality dimension. A correct-but-flaky scenario and a deterministically-broken one get different signals and different fixes — most agent eval collapses both into one pass/fail. pass^k (all-must-pass) surfaces reliability that pass@k hides.

**Where to apply in techne / cabal**: both have deterministic unit/integration tests, but agent-behavior eval is a different beast. A CI lane using the triad would distinguish a flaky persona-routing/tool-call from a genuinely broken one and let flakiness be prioritized separately — increasingly relevant as persona behaviors and MCP dispatch grow non-deterministic surface area.

---

---

## Gateway owns the schema/error contract the MCP ecosystem doesn't provide

- **Source**: https://agent-ready.dev/state-of-mcp-servers
- **Surveyed**: 2026-06-18

A data scan of 229 introspected MCP servers found the wire contract is far thinner than gateway code usually assumes: **only 22% declare output schemas**, and **only 5% of schema-bearing tools model error paths** (declare failure conditions). Tool *descriptions* (99%) and input metadata (95%) are near-universal — but what a tool *returns*, and how it *fails*, is undocumented for the overwhelming majority. So an agent today infers success/failure from response *content*, not schema.

**Why interesting**: The instinct for a proxy/gateway is "validate against the upstream's declared schema." This data flips the default: schema-absence is the *norm*, not an edge case (≥78% have no output schema; ≥95% have no typed errors). The design implication isn't "validate" — it's **synthesize**: the gateway must own output-schema inference and error normalization itself, because trusting upstreams to provide them means having a contract <1/4 of the time. Magnitude is the insight — it converts "nice-to-have validation" into "load-bearing gateway responsibility."

**Where to apply in limenal**: limenal sits in front of MCP servers as a per-persona broker — the natural place to (a) synthesize/cache an output schema per tool (from observed responses or a one-time probe) and (b) classify raw error responses into a typed failure signal before they reach cabal/techne. Surface structured outcomes to orchestration instead of propagating opaque blobs and untyped errors. Design the response path assuming the upstream declares neither.

---

---

## Home-anchored portable path persistence

- **Source**: https://github.com/LepistaBioinformatics/agents-room
- **Surveyed**: 2026-06-18

Persisted file paths are stored `~/...`-relative via `rel()`, and the absolute `homeDir` in effect at write-time is recorded as its own key. Reads resolve via `abs()` against the current home, but the captured write-time home lets cross-machine/backup restores detect and correct a changed home directory rather than guessing.

**Why interesting**: the usual two options both lose — absolute paths break on machine change; lazy-resolve breaks when home is ambiguous (containers, multi-user, restored backups). Capturing the write-time home as explicit data is a third option that makes the migration auditable — you can see which home a path was written under, not just resolve optimistically.

**Where to apply in techne**: techne's SQLite store persists file paths for memory/thread artifacts, and its sync adapters move that store across machines/clients (cross-client scoping #81). Home-anchored `rel()`/`abs()` + recorded write-time home would make techne backups and cross-machine sync lossless instead of carrying machine-specific absolute paths. Ties to "memory portable, not client-tied."

---

---

## M1–M13 MCP-server readiness rubric as a gateway registration health-gate

- **Source**: https://agent-ready.dev/state-of-mcp-servers
- **Surveyed**: 2026-06-18

Agent Ready publishes a concrete, reproducible 13-check rubric (M1–M13) scoring an MCP server 0–100 across handshake success, metadata completeness, parameter documentation, output-schema presence, tool annotations, and error-path modeling — applied empirically to 229 servers (mean 81/100). It's the first published, data-grounded readiness checklist for MCP servers rather than an informal "does it connect" test.

**Why interesting**: Most "is this integration good enough" decisions are vibes; this is a named, weighted, reproducible rubric backed by a real distribution (so you know what a 22nd-percentile vs 81st-percentile server looks like). A rubric *with a population behind it* lets you set a meaningful threshold instead of an arbitrary one.

**Where to apply in limenal**: Run M1–M13 (or a subset) at **upstream-registration time** — when a persona's policy adds a new MCP server, score it and store a readiness number in limenal's server registry. cabal orchestration can then soft-reject / flag / down-rank low-quality tool sources before they reach an agent (e.g. "this server has no output schemas and undocumented errors → admit read-only, or warn"). Turns "which MCP servers do we trust" from a manual call into a gated, scored admission step. Composes with the schema/error-synthesis stance above — the rubric *scores* the gap, the synthesis layer *compensates* for it.

---

---

## Meta-tool replacement for dense tool corpora (Starlark code-mode)

- **Source**: https://github.com/maximhq/bifrost
- **Surveyed**: 2026-06-18

When a gateway aggregates 8+ MCP servers with 150+ tools, the tool-definition payload dominates every request's input-token budget. Bifrost's code-mode replaces the full tool catalog with exactly **four meta-tools**: `listToolFiles`, `readToolFile`, `getToolDocs`, `executeToolCode`. The model discovers and calls tools by writing Starlark (Python subset) executed in a sandboxed interpreter; intermediate results stay in the sandbox, never round-tripping through the model. Their benchmark: 508 tools / 16 servers → 92.8% input token reduction ($377 → $29 at scale) while maintaining 100% task completion.

**Why interesting**: The inversion is non-obvious. Classic MCP puts the full schema in context every turn; code-mode replaces the *schema* with a *filesystem-like discovery protocol*. The model pays once (per session) to discover what's available, then writes orchestration code rather than issuing sequential tool calls. The Starlark sandbox enforces no imports/file-I/O/network — pure orchestration logic only. This is distinct from "summarize tools at gateway level" (which still injects some schema) and from "semantic search over tools" (which requires a vector store) — the model literally programs the gateway.

**Where to apply in limenal / agentgateway**: When limenal or agentgateway is federating many handlers, token bloat from handler-schema injection is the primary cost driver per request. A code-mode shim at the dispatcher level — replacing N handler schemas with 4 meta-tools — could reduce token overhead proportionally with handler count. Most relevant once the handler count exceeds ~20. The Starlark sandbox design is the hard part (safe eval, no escape); the meta-tool protocol itself is straightforward to implement as a virtual MCP server. Composes with [[architectural-patterns#Semantic tool routing via vector search]] — code-mode is better for deep corpora; vector routing is better for flat corpora where the model can still reason over the full schema.

---

---

## Pluggable per-deployment context-compaction seam

- **Source**: https://github.com/cline/cline
- **Surveyed**: 2026-06-18

Context compaction is a composable plugin slot (`registerMessageBuilder()`), not one global strategy baked into the loop. A host registers a builder that replaces the middle of conversation history with a structured summary (tool names, file mentions, role counts) before the message array reaches the provider; the core's safety builder runs *after* the plugin, so a custom strategy can't violate the token-budget invariants. Tiered reserve thresholds (64K→27K, 128K→30K, 200K→40K) drive when compaction fires.

**Why interesting**: most agents bake one compaction strategy into the agent loop. Making it a seam orthogonal to the loop — with a guaranteed post-plugin safety pass — lets different deployments apply domain-specific summarization without forking core, and bounds the blast radius of a bad custom builder.

**Where to apply in techne**: techne is the shared memory layer; exposing the same seam would let cabal personas register persona-specific compaction (a coding persona preserves diffs; a research persona preserves citations) over the shared store, rather than one global summarizer. Composes with [[architectural-patterns#Hierarchical community summaries (multi-resolution memory)]].

---

---

## Self-describing credential envelope (enc:/plain: prefix)

- **Source**: https://github.com/LepistaBioinformatics/agents-room
- **Surveyed**: 2026-06-18

Each stored credential carries its own decryption hint as an inline prefix — `enc:<base64>` when OS-keychain encryption succeeded, `plain:` when it fell back. Readers strip the prefix and branch on it; no separate schema column, metadata sidecar, or migration script tracks which values are encrypted. The envelope survives JSON round-trips and tool/version migrations because the data describes itself.

**Why interesting**: graceful-degradation encryption normally needs out-of-band state ("was the keychain available on the host that wrote this?") to read a value back. The inline prefix makes each value self-describing, so a value encrypted on one host and read where the keychain differs fails loudly per-value instead of corrupting silently — distinct from a schema flag, which a migration can desync from the data.

**Where to apply in cabal**: cabal-as-credential-broker stores per-(persona,handler) secrets and hands credential blobs to limenal/consumers. A self-describing envelope (`enc:`/`plain:`, or later `vault:`/`kms:`) lets a consumer strip-and-use without knowing the broker's storage state out-of-band, and lets the deferred vending-machine→token-exchange upgrade ([[project_agentgateway_decision]]) carry its own credential-type tag rather than a parallel schema column.

---

---

## Verification depth as an ordered trust enum (not a boolean)

- **Source**: https://datatracker.ietf.org/doc/draft-pro-adp-agent-discovery/
- **Surveyed**: 2026-06-18

ADP represents agent trust as an ordered enum with defined evidence per rung — `dns-verified` → `dane-verified` → `key-verified` → `peer-verified` (the last requiring a bidirectional Ed25519 AGP handshake) — rather than `authenticated: true/false`. A caller sees not just *whether* identity was established but *how much evidence* was collected to establish it, and can make policy decisions on that depth.

**Why interesting**: Auth is almost always modeled as a boolean (or a token's presence). Making verification *depth* a first-class ordered value lets policy say "this action requires peer-verified, that one accepts dns-verified" — graduated trust without a bespoke per-call check. It's a typed-state inversion of the usual "authenticated yes/no" gate.

**Where to apply in limenal**: The per-persona credential/policy layer. Gate capability grants on trust rung, not a boolean — e.g. only a peer-verified counterparty (completed a live mutual handshake) may invoke high-privilege tools, while a merely dns-verified one is read-only. Gives limenal a principled axis for tiering tool exposure by how strongly the caller's identity was proven, instead of all-or-nothing.

---

---

## Async pre-warm of a slow-loading server at session start

- **Source**: https://github.com/Goldziher/basemind
- **Surveyed**: 2026-06-19

The SessionStart hook spawns the MCP server binary in the background (a throwaway `--version`-style call) to load and warm it before the first real tool invocation. Cold-start cost is paid asynchronously during session init instead of synchronously on the user's first query.

**Why interesting**: it moves a fixed latency from the worst-perceived moment (the user waiting on their first tool call) to a hidden one (session startup, when nothing's blocking). Not a cache optimization — a *when-do-you-pay* reframe, effectively free because session-init has idle time.

**Where to apply in techne**: techne's MCP server has a real cold-start — fastembed's embedding model (all-MiniLM via ONNX) is a heavyweight singleton (`_get_embedding_model()`), and sqlite-vec index load adds to it; the first semantic-search query eats the whole load. A SessionStart hook firing an async warm (load the embedding model + open the vec tables) would hide that behind session init. techne already registers SessionStart hooks, so it's a natural add — most impactful once vector search is the default recall path.

---

---

## Confidence-banded rerank (provenance weight, not just relevance)

- **Source**: https://github.com/juliangeymonat-jpg/mothrag
- **Surveyed**: 2026-06-19

Post-retrieval passages are reranked by **which confidence band of query-derived facts they support** — high/mid/low-confidence OpenIE triples apply multiplicative gamma weights (1.0 / 0.5 / 0.0) before fusing with dense relevance scores. A passage supporting an *uncertain* fact counts less than one supporting a *high-certainty* fact: a citation-quality signal layered on top of relevance.

**Why interesting**: rerank usually optimizes relevance alone; here provenance/certainty is a first-class multiplicative term in the score, so the assembled context is weighted toward what's *trustworthy*, not just what's *similar*. It's a rerank (graded down-weight), not a filter (binary drop).

**Where to apply in techne**: the search reranker (RRF + MMR diversity, #11/#41) could add a confidence-band multiplier from the existing numeric-confidence/decay model (#13) before context assembly — recently-confirmed or structurally-parsed memories outweigh stale/low-provenance ones instead of being dropped. Composes tightly with graded staleness (techne#90): a `modified`/stale card down-weights rather than vanishes, and with the Librarian cards ([[project_librarian_card_architecture]]) whose `source_version` is exactly a provenance signal.

---

---

## Constitutional escalation gate (the orchestrator structurally cannot call the human)

- **Source**: https://github.com/ChronoAIProject/consensus-rnd
- **Surveyed**: 2026-06-19

Human escalation can be triggered **only** by a designated meta-reflector agent emitting an explicit marker (`META_RESOLVED:escalate-human:`); the controller is forbidden from applying that label itself. Privilege separation enforced as prose-spec ("constitutional" constraint), not a runtime permission check.

**Why interesting**: it's a governance constraint on *who may interrupt the loop*, distinct from a permission check on *what may run* — it stops the orchestrator from short-circuiting the consensus ladder to bail out to a human, so the deliberation protocol can't be bypassed by the process that runs it.

**Where to apply**: cabal multi-persona arbitration — no solver persona may invoke the human-interrupt channel directly; only a designated arbitrator persona emitting a specific marker may, so no single persona short-circuits deliberation. [[project_colony_persona_model]].

---

---

## Content-addressed blob store with view-scoped indexes

- **Source**: https://github.com/Goldziher/basemind
- **Surveyed**: 2026-06-19

Extracted content is stored as blobs keyed by source-content hash, shared across named "views" (working / staged / rev-sha). A view is a thin `path→hash` index, so identical content across branches or sources pays zero duplication, and re-parsing/re-extraction happens only when content actually changes — not when a branch pointer or reference moves.

**Why interesting**: decouples "which state/source references this" from "what was extracted from it." Most ingestion stores content per-source (or per-reference), so the same spec ingested from two places gets parsed and embedded twice. Content-addressing makes dedup and change-detection fall out of the hash: unchanged content is never re-processed, and N references to one blob cost one extraction.

**Where to apply in techne (documents/library layer)**: the new documents layer stores full content + embeddings keyed by `source_url`, and `store_document` already hashes content to detect change-on-refetch — that's a degenerate content-addressing. Generalizing to a shared blob table (hash → content + embedding, with `documents` rows referencing the blob) would dedup identical content across *different* `source_url`s (a spec linked from several issues, the same paper re-ingested) so it's embedded once, not per-reference. Most valuable before chunking lands, since re-embedding cost scales with chunk count.

---

---

## Demonstrate-before-commit onboarding (zero-footprint guided trial)

- **Source**: https://github.com/sverklo/sverklo
- **Surveyed**: 2026-06-19

A `prove --no-write --guided` command runs PageRank over the symbol graph, picks a high-value entry point, and **generates a tailored natural-language prompt for the user** — producing immediately useful output **without writing any config**. The trial demonstrates value before the user commits to integration.

**Why interesting**: it deliberately separates "demonstrate value" from "commit to setup," and goes further than a dry-run by *using the tool's own analysis* (graph centrality) to auto-select what to show. Most agent infra requires full install before any value is visible.

**Where to apply in techne**: techne onboarding requires hook installation before anything is visible. A `techne prove` dry-run that surfaces top memories / suggested hooks (or, for Codex, the PageRank-ranked "start here" symbols) without writing config would lower adoption friction — relevant to the multi-client portability goal ([[feedback_memory_portable_not_client_tied]]).

---

---

## Deterministic LLM-free routing with inline audit receipts

- **Source**: https://github.com/juliangeymonat-jpg/mothrag
- **Surveyed**: 2026-06-19

Routing decisions run on a frozen, ~11-dim linguistic feature vector computed from **pure regex rules — no embedding, no LLM call** — fed to per-arm `applicable()` predicates that gate each arm *before* any model is invoked. Each predicate's threshold is committed **inline next to its empirical audit** (a 50-question labeling pass with P/R/F1 per feature) — a "falsifiability receipt" in the docstring rather than a magic number. The whole routing layer is unit-testable with no keys, no corpus, no warm embedding model.

**Why interesting**: two disciplines the field skips — (1) routing is *deterministic by construction* (a feature dataclass, not an embedding/LLM classifier), so it's diffable, versionable, and testable in isolation; (2) every threshold carries its own provenance, so a routing choice is reviewable without replaying benchmarks. Most routing is either an LLM call or tuned magic numbers with no audit trail.

**Where to apply in techne**: the structural thread heuristic already does linguistic classification — formalize its signals into a frozen `SemanticFeatures`-style dataclass with inline audit notes per threshold, making the heuristic diffable across versions and testable without fixtures. Broader fit: our auth tier ([[reference_anthropic_auth_tier]]) makes LLM-free-by-construction routing the *default*, not a fallback — pairs with transparent typed degradation ([[architectural-patterns#Transparent typed degradation (same return type, LLM or rule engine)]]).

---

---

## Dual-format in-stream citations (fenced evidence-list footer)

- **Source**: https://github.com/sverklo/sverklo
- **Surveyed**: 2026-06-19

A search/recall response carries the answer as human-readable text **and** appends a fenced ` ```evidence-list ``` ` JSON block of the structured citations (bounded to ~16) in the *same* stream. Naive consumers read the prose and ignore the footer; structured consumers parse the footer to extract verifiable citations with no second round-trip.

**Why interesting**: it's a composition, not an idiom — one response serves both a chat-style reader and a programmatic verifier without a separate "get citations" tool call. The fence makes the machine-parseable part trivially extractable and trivially ignorable.

**Where to apply in techne**: techne MCP tool responses (`techne_recall`/`techne_search`) return largely unstructured text today; a fenced citation footer would let cabal agents re-verify specific memory chunks (pairs naturally with git-SHA graded staleness above) without an extra lookup call.

---

---

## Externalized phase-state for cold-startable orchestration (zero local state)

- **Source**: https://github.com/ChronoAIProject/consensus-rnd
- **Surveyed**: 2026-06-19

The controller derives **every** routing/phase decision from durable *external* surfaces — GitHub PR labels, log-tail terminal markers (`EXIT=0` + verb markers), and live `ps` output — and never reads a local state file. So every wake-up is a cold read; the loop survives `/clear`, daemon restart, or model reload with no checkpoint or replay machinery.

**Why interesting**: not "use a DB" — the inversion is that the orchestrator carries *zero* in-process state, so the collaboration surface *is* the state machine and restartability falls out for free. Most agent loops keep phase state in-process or in a local JSON/SQLite checkpoint and need replay logic to recover.

**Where to apply**: cabal/techne orchestrators that lean on in-process or SQLite phase state — externalizing markers onto the A2A thread record (or a thread tag) would make a multi-persona loop resumable across context compaction or crash, no replay. Composes with `techne_resume` and the structural thread heuristic; [[reference_a2a_v1_bilateral]] is the candidate surface.

---

---

## Four-state scoring hygiene (fail ≠ N/A ≠ not-yet-scored ≠ optional-bonus)

- **Source**: https://isyourmcpready.com/
- **Surveyed**: 2026-06-19

The scorer rigorously separates a *failed* check from four non-failure states, each with explicit copy: **not-applicable** ("Optional bonus, not present. Doesn't affect your score"), **not-yet-scored** ("excluded from the score, not counted as a failure"), **optional cross-standard bonus** ("a plus, but it doesn't affect your MCP readiness score"), and **not-run-at-this-tier** ("Not run on the public scan. A Tier-1 scan runs this check"). A gap never masquerades as a failure; an un-evaluated check neither inflates nor deflates the grade.

**Why interesting**: the discipline the field skips — most rubrics collapse "missing," "not-applicable," and "not-measured" into "fail," which makes a score untrustworthy (you can't tell a real deficiency from an un-run check). Naming the states keeps the grade honest.

**Where to apply**: any scoring/eval rubric we build — the sverklo-style token-economy benchmark gating ([[architectural-patterns#Retrieval-economy benchmark (token cost, not just correctness)]]), a techne memory-quality or MCP-readiness score. Same "don't collapse distinct states into a binary" discipline as graded staleness (techne#90 / git-SHA four-state).

---

---

## Git-SHA graded staleness (four-state evidence verification)

- **Source**: https://github.com/sverklo/sverklo
- **Surveyed**: 2026-06-19

Each stored memory chunk carries a content hash **plus the git SHA at creation time**. At recall, the chunk is re-classified against current HEAD into one of four states — **unchanged / moved / modified / deleted** — by comparing the stored hash and location to the live tree, rather than a binary file-exists check.

**Why interesting**: converts memory staleness from all-or-nothing into a *graded* freshness signal. An agent can trust an `unchanged` memory, surface a `modified` one with reduced confidence, and follow a `moved` one to its new location — instead of silently dropping anything whose file changed. Git SHA as the verification anchor (not wall-clock mtime) means the freshness check is deterministic and travels with the repo.

**Where to apply in techne**: techne's staleness model invalidates on file deletion only. Adding hash+SHA to evidence-anchored memories would let recall return degraded-confidence (`moved`/`modified`) memories with a freshness tag rather than dropping them — the standout lift from this repo, directly relevant to evidence-anchored facts. Composes with the bi-temporal "git commit as the time axis" idea.

---

---

## Git-diffable memory journal (append-only JSONL mirror of the store)

- **Source**: https://github.com/sverklo/sverklo
- **Surveyed**: 2026-06-19

Every memory mutation (remember / forget / promote / demote / touch) is appended as one JSON line to `.sverklo/memories.jsonl` *alongside* the SQLite write. Deletions are tombstone records, not row removals — the JSONL is a complete, append-only history that lives in the repo and shows up in a PR diff.

**Why interesting**: the lift is the *reviewability* angle, not event-sourcing in general. Because the journal is line-oriented JSON in the working tree, a memory change is reviewable in code review and debuggable with `git log`/`grep` — no SQL, no extra tooling. The SQLite store stays the queryable index; the JSONL is the human-auditable truth.

**Where to apply in techne**: techne's SQLite-only store has no human-readable audit trail — diagnosing "why did this memory change" needs SQL against a binary db (and on APFS, mtime lies — see [[reference_techne_server_vs_cli_venv]]). A JSONL mirror would make memory mutations reviewable in a diff and recoverable without the db.

---

---

## Graph queries as a SQL UDF (Cypher-on-SQLite, composes with FTS/vec in one file)

- **Source**: https://github.com/colliery-io/graphqlite
- **Surveyed**: 2026-06-19

A SQLite loadable extension exposes the whole graph layer as a scalar SQL function — `SELECT cypher('MATCH (a:Symbol)-[:CALLS]->(b) RETURN b', params)` — plus built-in algorithms (BFS/DFS, Dijkstra, PageRank, Louvain). A per-connection **CSR (compressed-sparse-row) adjacency cache** is built from the underlying SQLite tables on demand (`gql_load_graph` / `gql_reload_graph` give explicit invalidation control), so traversal is in-memory while storage stays in the one file. `wrap(existing_conn)` injects graph semantics into a connection you already hold. Python binding via `sqlite3.load_extension()`.

**Why interesting**: graph queries become *composable with the rest of SQL* — a single statement can `cypher()` for reachability and JOIN the result against FTS5 / sqlite-vec / a documents table, because there's no separate engine or connection protocol to cross. This is the "graph ergonomics without a graph DB" middle path: you keep one portable SQLite file, gain Cypher + algorithms, and existing WAL/FTS/vec keep working unchanged. Distinct from [[architectural-patterns#Graph reachability without a graph DB (in-memory adjacency, rebuilt on demand)]] (which hand-rolls BFS) — same CSR-on-demand idea, but Cypher + algorithms + a UDF surface come for free, and the explicit `reload` primitive separates rebuild from query.

**Where to apply (Codex repo storage)**: it's a credible *upgrade* over hand-rolling `symbols`+`edges`+BFS — keeps techne's single-file substrate (the reason we rejected a standalone graph DB), composes with FTS/vec in the same query, and supplies pathfinding + centrality + community detection for free. Cost: a native C extension dep (`load_extension` must be enabled) + pre-1.0 maturity → scope it to an optional `techne[codex]` dep, not core. See `evaluations/codex-repo-storage-evaluation.md`.

---

---

## Graph reachability without a graph DB (in-memory adjacency, rebuilt on demand)

- **Source**: https://github.com/manojmallick/sigmap
- **Surveyed**: 2026-06-19

Call/import edges aren't persisted in a graph DB. Forward + reverse adjacency `Map`s are built on demand (from parsed imports / a symbol table) and traversed with depth-bounded BFS to answer reachability / blast-radius queries ("what breaks if I change X?"). The persistent store holds nodes/edges flatly; the graph is a *materialized-on-demand view*, not a storage engine.

**Why interesting**: the "code is a graph → use a graph DB" reflex is mostly unnecessary. Reachability needs graph *traversal*, not graph *storage* — and traversal over a few hundred-thousand edges held in memory is sub-millisecond. It decouples the (relational, cheap, portable) storage decision from the (in-memory, on-demand) query decision. sigmap ships this graph-DB-free at 519★/benchmarked, and the choice is load-bearing (zero deps, sub-ms queries).

**Where to apply (Codex repo storage in techne)**: a Codex domain can hold `symbols` + `edges` tables in SQLite and rebuild forward/reverse adjacency in memory for call-graph / blast-radius queries — no neo4j, no break from techne's single-file substrate, no second operational store. Composes with [[architectural-patterns#File-watch with debounce for continuous code-corpus ingest]] for incremental updates. See `evaluations/codex-repo-storage-evaluation.md`.

---

---

## Graph-boosted keyword ranking (structural code retrieval without embeddings)

- **Source**: https://github.com/manojmallick/sigmap
- **Surveyed**: 2026-06-19

Code retrieval ranks files by weighted keyword signals (exact / symbol / prefix / path match + penalties), *then* boosts candidates by their 1-hop/2-hop neighbors in the import graph — a file near a strong match in call/import space gets lifted. No embeddings, no vector store. Benchmarked at 75.6% hit@5 vs 13.6% keyword-only baseline across 21 repos.

**Why interesting**: structural code relationships carry most of the retrieval signal embeddings are usually reached for — and a cheap graph-neighbor boost on top of keyword match captures it, measurably (a >5× hit@5 lift). Distinct from semantic/vector retrieval (no model, no embeddings) and from plain keyword (the graph boost is the differentiator). For *code* specifically, the import/call graph is a stronger relevance prior than embedding similarity.

**Where to apply (Codex retrieval in techne)**: layer import/call-edge boosting on top of techne's existing FTS5 over symbol signatures — Codex gets strong code retrieval without adopting a vector store for code at all (sqlite-vec stays for prose/memory; code leans on structure). Composes with the adjacency-maps pattern above — the same in-memory graph serves both reachability and the ranking boost.

---

---

## JSON body as the row, with a virtual computed-column key

- **Source**: https://github.com/dpapathanasiou/simple-graph
- **Surveyed**: 2026-06-19

A node row stores the entire entity as one JSON `body` column; the primary key is a **generated VIRTUAL column** extracting it from the blob — `id TEXT GENERATED ALWAYS AS (json_extract(body, '$.id')) VIRTUAL NOT NULL UNIQUE`. The JSON is the single source of truth; the indexed key is derived, never written separately.

**Why interesting**: eliminates the usual dual-write (caller sets both the blob and a key column, which then drift) while keeping an indexed lookup. The key can never disagree with the data because it *is* the data, projected. Not a standard idiom — most schemas carry a redundant key column the application must keep in sync.

**Where to apply (techne / Codex)**: Codex symbol nodes could store the full symbol as JSON and derive a stable `fqn` (fully-qualified name) key as a generated column — no schema drift between blob and key as the symbol shape evolves. Same trick fits the documents layer's `meta` field if a derived key is ever wanted. Pure SQLite, no extension.

---

---

## Named-bias solver ensemble (divergence is the signal, not noise)

- **Source**: https://github.com/ChronoAIProject/consensus-rnd
- **Surveyed**: 2026-06-19

Three solver agents get the *same* problem framed from deliberately opposing philosophies — **minimal-change**, **aggressive-deletion**, **structural-cleanliness** — and a meta-judge compares their divergent outputs before any implementation proceeds. The bias is load-bearing: the solvers are *not* meant to converge independently.

**Why interesting**: the inverse of accuracy-ensembling (where you average independent attempts to cancel noise). Here disagreement is the *product* — opposed priors surface trade-offs a single framing would miss, and the judge resolves them with the trade-space made explicit.

**Where to apply**: cabal deliberation on design decisions — assign personas explicit opposed priors (minimal-footprint vs clean-abstraction vs delete-first) rather than treating them as interchangeable domain-owners, giving the arbitrator richer disagreement to resolve. Pushes [[project_colony_persona_model]] from domain-ownership toward viewpoint-diversity. Also a workflow-script shape: a judge-panel of opposed lenses rather than redundant verifiers.

---

---

## Posture-adaptive grading (scrutiny scales with the subject's declared posture)

- **Source**: https://isyourmcpready.com/
- **Surveyed**: 2026-06-19

The rubric grades each server against the standard *appropriate to its own posture* rather than a fixed bar: **"Anonymous, read-only servers — auth checks are treated as optional"** vs **"Servers that require a token — full auth and safety scrutiny."** A deeper **Tier-1 scan** (caller token + actual tool invocations) unlocks checks the anonymous public scan can't run.

**Why interesting**: an inversion of one-size-fits-all scoring — a read-only anonymous server isn't penalised for lacking auth it doesn't need, while a token-gated server earns the full safety audit. Scrutiny is a function of declared capability/auth posture, and inspection *depth* escalates with the access the subject grants the scanner.

**Where to apply**: limenal policy — required controls / scrutiny level for a backend should scale with its capability and auth posture, not a flat checklist. Also the model for any techne readiness or quality score: grade against the tier the thing claims, escalate depth as access is granted.

---

---

## Pre-flight cost projection per persona/tenant (offline-testable budget guard)

- **Source**: https://github.com/jaswanthsurya007-source/enterprise-decision-intelligence
- **Surveyed**: 2026-06-19

Before *every* LLM call the loop calls `BudgetAccountant.check()` with the **projected** token cost; if it would breach the per-tenant daily cap the turn degrades immediately **without calling the API**. Accounting works offline via a `4-chars-per-token` heuristic, so the cost-guard logic is fully testable with no network and no key.

**Why interesting**: the cap is enforced **pre-flight** (project-then-decide), not reconciled after the spend, and the accounting module is deliberately decoupled from the API client so it runs and tests offline. Most budget logic is post-hoc metering that can only catch an overspend after it happened.

**Where to apply in limenal**: limenal already owns per-persona credential/policy but has no **cost** governance — this is its natural home. A pre-flight budget check per persona/tenant before any proxied LLM call, with the offline-safe heuristic as the estimator, makes cost a first-class policy dimension alongside credentials. Composes with posture-adaptive grading ([[architectural-patterns#Posture-adaptive grading (scrutiny scales with the subject's declared posture)]]) — budget tier scales with persona trust.

---

---

## Provenance sentinel (classify human-vs-AI by sentinel absence, not author identity)

- **Source**: https://github.com/ChronoAIProject/consensus-rnd
- **Surveyed**: 2026-06-19

Every AI-generated message ends with a fixed Unicode sentinel (`⟦AI:AUTO-LOOP⟧`); the monitor daemon flags human input by the **absence** of the sentinel rather than by checking author identity. Unforgeable from the AI's own output stream and evaluable with no GitHub API / auth call.

**Why interesting**: author-identity checks break under shared bot accounts, impersonation, or relays; an output-stream sentinel is O(1) and robust there. **Caveat** — detect-by-absence only holds inside a *closed loop where you control every AI writer*; any un-tagged AI output (a different tool, a relay that strips the marker) reads as "human."

**Where to apply**: techne's structural thread heuristic (human-vs-AI turn inference) *inside cabal*, where all agents are ours — a sentinel on outbound A2A messages makes the classification O(1) and exact. **Not** safe for techne ingesting arbitrary external client transcripts (we don't control those AIs' output) — there the structural heuristic stays necessary.

---

---

## Reader-capability-inverse redundancy scaling

- **Source**: https://github.com/juliangeymonat-jpg/mothrag
- **Surveyed**: 2026-06-19

The number of redundant/duplicate retrieval arms is scaled **up for weaker reader models and left at base for stronger ones**, via piecewise-linear interpolation over empirically-measured `(baseline F1, lift Δpp)` anchors per model. The redundancy signal keys on **reader headroom, not query difficulty**.

**Why interesting**: an inversion of the naive "throw more arms at harder queries." The dial is the *model's* capability, not the *question's* hardness — a weak model gets compensatory redundancy, a strong one runs lean — and the anchors come from production runs, not hand-tuned heuristics.

**Where to apply**: the local-model fallback path ([[reference_anthropic_auth_tier]]) — when techne/cabal drop to a weaker local model, scale up cross-persona verification / redundant retrieval to compensate; on a strong model, run lean. A capability prior on "how much redundancy to spend" composes with the pre-flight budget guard ([[architectural-patterns#Pre-flight cost projection per persona/tenant (offline-testable budget guard)]]) and named-bias verification. Forward-looking — earns its keep once the local-model path lands.

---

---

## Retrieval-economy benchmark (token cost, not just correctness)

- **Source**: https://github.com/sverklo/sverklo
- **Surveyed**: 2026-06-19

The benchmark runs canonical queries and scores **tokens consumed** (estimated `text.length / 3.5`) by grep vs. semantic search — measuring how much *context budget* a retrieval tool spends per task, not whether the answer is correct. Results are saved to a JSON artifact and the diff-audit can fail CI on regression.

**Why interesting**: the metric choice is the insight — in an agent loop the scarce resource is the context window, so "how many tokens did retrieval cost" is often the number that decides whether a tool helps or hurts, and almost nobody benchmarks it. Correctness benchmarks miss it entirely.

**Where to apply in techne**: techne has no benchmark for hook/recall *retrieval cost*. A token-economy harness would quantify whether a hook or relevance-tuning change improves or degrades context efficiency for downstream agents — a measurable guardrail for the structural thread heuristic and Tier-2 relevance work.

---

---

## Slot substitution over pool expansion (invariant-preserving specialist add)

- **Source**: https://github.com/juliangeymonat-jpg/mothrag
- **Surveyed**: 2026-06-19

New specialists **replace** a generic arm inside a fixed-size pool (the `N=4` pool size is held as an explicit axiom) via a `SlotDecision` protocol — they're wired in by *substitution, never addition*, and fall back to the generic arm when the specialist fails or its `applicable()` returns False. The pool-size invariant is enforced structurally, not by convention.

**Why interesting**: it keeps the arbitration weight math stable as the system grows — adding a specialist can't drift the pool size and silently rebalance every weight. The constraint is load-bearing and self-healing (graceful fallback to the generic slot), rather than a comment saying "keep the pool at 4."

**Where to apply**: cabal's persona/arm dispatch and arbitration — as specialist personas are added, substitution-not-addition keeps the arbitrator's weight math invariant. Directly composes with the named-bias ensemble ([[architectural-patterns#Named-bias solver ensemble (divergence is the signal, not noise)]]) and the colony model ([[project_colony_persona_model]]): a specialist persona swaps into a generic deliberation slot rather than enlarging the panel.

---

---

## Tool-description surface as an auditable attack surface (MCP prompt-injection checks)

- **Source**: https://isyourmcpready.com/
- **Surveyed**: 2026-06-19

The scanner audits the **content the model reads** — tool *descriptions* and *annotations* — as a security surface, not just transport/auth. Named checks: **Prompt-injection in Descriptions** (embedded directives in a tool's description), **Dangerous Tool Exposure**, and **Destructive Annotations** (does the server correctly set MCP's `destructiveHint` / `readOnlyHint` so a caller can tell a mutating tool from a read-only one).

**Why interesting**: most MCP security attention goes to auth and transport; this treats the description/annotation text — which flows straight into the agent's context — as an injection vector to be scanned. It's the server-side mirror of the instruction-source-boundary discipline (observed tool content is data, not commands).

**Where to apply**: limenal (gateway) could scan a backend's tool descriptions for injection *before* exposing them to a persona — a natural gateway-policy check. techne/rookery should audit their own tool descriptions and actually set `destructiveHint`/`readOnlyHint` on their MCP tools (e.g. `techne_store` writes, `rookery_send` sends — currently unannotated). Pairs with the transport-side origin/DNS-rebinding check we already track in [[reference_fastmcp_dns_rebinding]], now itself an industry readiness check.

---

---

## Transparent typed degradation (same return type, LLM or rule engine)

- **Source**: https://github.com/jaswanthsurya007-source/enterprise-decision-intelligence
- **Surveyed**: 2026-06-19

An LLM (Haiku) classifies intent via structured output; **if the call fails for any reason a keyword-matcher returns the *same* `Route` datatype**, so the caller sees no exception and the pipeline runs unchanged — transparent degradation, not an error state or sentinel. The output-side complement: when a turn hits a limit, `LoopState.mark_degraded(reason)` returns the answer anyway but tagged with a reduced-confidence flag and a machine-readable cause (`budget_exceeded` / `max_iterations` / `refusal` / `truncation`) that propagates to the audit log.

**Why interesting**: the fallback is a *full, semantically-equivalent* result of the same type, not a degraded error the caller must special-case — and degradation is a named typed state carrying *why*, so downstream can retry/escalate/surface rather than guess. It's the discipline that lets an LLM feature and its rule-based fallback be interchangeable to every caller.

**Where to apply**: load-bearing for us specifically — the auth tier ([[reference_anthropic_auth_tier]]) means *every* techne/cabal LLM feature needs a same-type deterministic fallback or it strands the operator on provisioning. The Librarian summarizer ([[project_librarian_card_architecture]]) already has this shape (LLM summary → native-fields card → `pending`); generalize it: LLM and rule paths return one type, and a `degraded(reason)` carries the cause. Composes with cabal's existing A2A reason codes (`core/a2a/codes.py`) and four-state scoring hygiene.

---

---

## Type-discriminated edges via JSON properties in one table

- **Source**: https://github.com/dpapathanasiou/simple-graph
- **Surveyed**: 2026-06-19

One `edges(source, target, properties TEXT)` table holds *all* relationship kinds; the edge type and any metadata live in the freeform JSON `properties` blob (`json_extract(properties, '$.type') = 'calls'`). `UNIQUE(source, target, properties) ON CONFLICT REPLACE` lets multiple typed edges exist between the same node pair (a `calls` and an `imports` edge from A→B) while keeping each idempotent on re-ingest.

**Why interesting**: avoids both an EAV property table and a proliferation of per-type edge tables, and the UNIQUE-on-`(source,target,properties)` trick is the non-obvious bit — it permits *parallel* typed edges between a pair without a synthetic edge id, while still deduping on re-walk. One table covers an open set of relationship kinds with zero migration as new edge types appear.

**Where to apply (Codex repo storage)**: Codex needs `calls` / `imports` / `defines` edges plus per-edge metadata (call-site line, import alias). A single `edges` table with a JSON `properties` blob and the parallel-edge UNIQUE handles all relationship kinds + metadata without schema churn as edge types grow. Pure SQLite — composes with the recursive-CTE reachability path (no native extension; see `evaluations/codex-repo-storage-evaluation.md`).

---

---

## Whitelist-anchored LLM grounding with post-hoc numeric verification

- **Source**: https://github.com/jaswanthsurya007-source/enterprise-decision-intelligence
- **Surveyed**: 2026-06-19

An `EvidenceBundle` **pre-computes every number the LLM is allowed to cite** (including human-readable rounded variants); the system prompt absolutely forbids the model from deriving arithmetic itself; after generation a verifier **extracts numeric tokens from the output and diffs them against the whitelist**, falling back to a deterministic template if any number isn't on the list. The grounding guarantee is enforced at three independent checkpoints — prompt constraint, whitelist construction, post-hoc extraction — so any single failure still yields a safe output.

**Why interesting**: defense-in-depth against hallucinated facts that does *not* rely on the prompt instruction holding. Most "grounding" is a single prompt rule; this makes the allowed-facts set a precomputed artifact and *verifies the output against it* after the fact, with a deterministic fallback. Belt, suspenders, and a post-hoc audit.

**Where to apply in techne**: any LLM-generated narrative over retrieved facts — most concretely the **Librarian card summaries** ([[project_librarian_card_architecture]]): a generated summary that cites figures/IDs from a Jira ticket or doc could fabricate them. Pre-compute the citable values from the source record, forbid derivation, verify the card against them, fall back to native-fields-only on mismatch. Same shape fits cabal agent responses that quote `techne_recall` results.

---

---

## No-alt-screen replay cache (native scrollback as a free side-effect)

- **Source**: https://github.com/rot1024/tuck
- **Surveyed**: 2026-06-20

The session server owns the PTY and accumulates all child output in a bounded (~1 MB) in-memory ring buffer. A late-joining client receives the entire buffer verbatim on connect — that replay *is* the reattach repaint. Because the raw byte stream is rendered by the client's own terminal (no alternate-screen save/restore, no inner screen reconstruction), the host terminal's native scrollback accumulates for free. Inverts tmux/screen, which snapshot screen state into an inner buffer and thereby hijack scrollback.

**Why interesting**: the non-obvious design choice is *not reconstructing the screen at all* — replay-the-byte-stream instead of snapshot-the-screen. Native scrollback (the thing tmux/screen lose) falls out as a side-effect rather than being engineered. The named trade-off: replay fidelity degrades as a long-lived session pushes history past the ring cap.

**Where to apply in techne**: a bounded replay buffer on MCP tool-output streams so a reconnecting agent (or human) catches up on recent output *without re-running tools* — the structured-output analogue of tuck's byte replay. Composes with the hook lifecycle and the thread heuristic. Also the reference design for cabal's attach-from-anywhere substrate ([[project_colony_persona_model]], [[feedback_cabal_standalone_terminal]]).

---

---

## Self-daemonizing single binary (Setsid + env-flag sentinel)

- **Source**: https://github.com/rot1024/tuck
- **Surveyed**: 2026-06-20

One binary is client, server, and daemon, disambiguated by an env-var sentinel (`TUCK_SERVER=1`). `tuck new` re-execs itself with `SysProcAttr{Setsid: true}` to become a new session leader orphaned from the parent TTY, then the parent polls for the Unix-domain socket and attaches as the first client. No separate daemon binary, no systemd, no double-fork dance.

**Why interesting**: collapses the client/server/daemon split into one shippable artifact selected at runtime by env flag; Setsid alone (not the classic double-fork) does the TTY-orphaning, and poll-for-socket-then-attach gives a clean startup barrier without a readiness protocol.

**Where to apply in techne**: cabal's persistent-session substrate can self-daemonize a per-persona agent process straight from the CLI invocation — no shipped daemon, no init system. The env-flag-sentinel re-exec is the concrete recipe ([[project_colony_persona_model]]).

---

---

## Speculative-forward escape detection (bail-out that survives a stdin-hungry child)

- **Source**: https://github.com/rot1024/tuck
- **Surveyed**: 2026-06-20

A three-state machine (`afterNewline` → `sawEscapeChar` → detach) intercepts the SSH-style `~.` detach sequence in raw mode without line-buffering. The hard case: the child (e.g. Claude Code) consumes most control keys, so the escape char is **forwarded speculatively** to the child and only *retroactively* treated as a detach trigger when the next byte is `.` — late-cancel rather than swallow-up-front.

**Why interesting**: the correct protocol answer to "intercept a control sequence the child also wants" is speculative forward + late cancel, not pre-emptive consumption — otherwise either the detach key or the child's keybind breaks. Copy-paste-ready state machine.

**Where to apply**: cabal's A2A layer or limenal's gateway need a clean "bail out of the current agent/tool" escape that works even while the child persona is consuming stdin — this is a ready reference for that primitive.

---

---

## Surveyed (no patterns worth saving)

- https://github.com/1mcp-app/agent — surveyed 2026-06-12 — flat aggregation, all backends in one namespace, upstream-auth passthrough
- https://github.com/AntonOsika/gpt-engineer — surveyed 2026-06-12 — CLI-first code-gen; single-agent, no orchestration model
- https://github.com/AxDSan/Mnemosyne — surveyed 2026-06-12 — SQLite + sqlite-vec + FTS5; mirror of techne's stack, validated the choice. Fixed-weight 50/30/20 fusion noted under RRF entry above
- https://github.com/ClaudioDrews/memory-os — surveyed 2026-06-12 — 7-layer Qdrant+SQLite+Redis stack with "Fabric tools"; impressive but architecture is opaque and decay scanner pattern is already in our notes
- https://github.com/MemTensor/MemOS — surveyed 2026-06-12 — multi-layer memory w/ async scheduler; "self-evolving importance weighting" is the headline but the algorithm isn't documented in the OSS code
- https://github.com/agentscope-ai/ReMe — surveyed 2026-06-12 — 4-tier memory + async summarization; absorbed into bi-temporal + hybrid-rank composite, no unique pattern beyond
- https://github.com/camel-ai/camel — surveyed 2026-06-12 — role-playing personas + task scheduler; research-paper-adjacent, light on operator concerns
- https://github.com/huggingface/smolagents — surveyed 2026-06-12 — code-thinking single agent; orthogonal to cabal's broker pattern
- https://github.com/jo-inc/pi-mem — surveyed 2026-06-12 — markdown files in `~/.pi/`; charming low-barrier design but the recency-tiered injection is essentially passive-adr lite
- https://github.com/langroid/langroid — surveyed 2026-06-12 — chat-mailbox routing between ChatAgent subclasses; ad-hoc, not protocol-formalized
- https://github.com/mcp-router/mcp-router — surveyed 2026-06-12 — desktop UI for workspace-scoped tool groups, not server-side architecture
- https://github.com/microsoft/autogen — surveyed 2026-06-12 — peer-to-peer message-passing multi-agent; in-memory state, no protocol formalization
- https://github.com/milisp/mcp-linker — surveyed 2026-06-12 — local client-config sync utility, not a gateway
- https://github.com/neo4j-labs/agent-memory — surveyed 2026-06-12 — POLE+O entity extraction in Neo4j; valuable if you need a knowledge graph but architecturally inverse to techne's single-file SQLite ethos
- https://github.com/open-webui/mcpo — surveyed 2026-06-12 — 1:1 MCP→OpenAPI translation; useful as a shim but no novel architecture
- https://github.com/openai/swarm — surveyed 2026-06-12 — experimental handoff-style; explicitly not production
- https://github.com/ptbsare/mcp-proxy-server — surveyed 2026-06-12 — discovery + dispatch via config file, all standard idioms
- https://github.com/redis/agent-memory-server — surveyed 2026-06-12 — Redis-backed working+LT memory; standard pattern, nothing novel beyond Redis-as-backend
- https://github.com/sparfenyuk/mcp-proxy — surveyed 2026-06-12 — pure transport bridge (stdio ↔ HTTP), no dispatch logic
- https://github.com/topoteretes/cognee — surveyed 2026-06-12 — ontology + Kuzù graph; same inversion as Neo4j-labs
- https://www.npmjs.com/package/fss-link — surveyed 2026-06-13 — hard fork of QwenCode (Gemini CLI lineage); standard SQLite-backed CLI agent, nothing novel beyond what's already in our notes
- https://github.com/naveenayalla1-CS50/mcp-security-toolkit — surveyed 2026-06-17 — static regex+JSON-tree MCP config scanner; 0 stars, 3 commits, day-of-release; descriptive name will plausibly resurface but all 4 surfaced patterns were standard practice (negative fixtures, exit-code-as-severity, `mkdtemp` isolation, key-name traversal)
- https://github.com/maximhq/bifrost — surveyed 2026-06-18 — **FULL-EVAL** (see `evaluations/bifrost-evaluation.md`): Go LLM gateway, 23+ providers, MCP client+server, but no A2A and no CEL — overlaps agentgateway's LLM layer and MCP federation but does not replace it. Decision: audit agentgateway's built-in LLM gateway first; use bifrost as LLM-only layer if agentgateway's provider coverage is insufficient. One pattern lifted (code-mode Starlark below).
