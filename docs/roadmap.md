# Techne Roadmap

> **SUPERSEDED for 0.8.0+ work.** Release history is in [`CHANGELOG.md`](../CHANGELOG.md); current architectural direction is in [`docs/context-architecture.md`](context-architecture.md). This document covers the 0.6.x–0.7.0 arc and earlier; it is kept for historical context only.

**Created**: 2026-06-17 (Raven + Richard). **Updated**: 2026-06-20 (un-shadowing arc → promotion → Librarian → **db-per-domain across 3 domains**). main `f2c884a`.
**Scope**: techne only — cabal (#11–14) parked as a separate track.
**Canonical remote**: `origin` = github.com/tenishai/techne. **gitea is archive — do not push there.**

Status legend: ☐ todo · ◐ in progress · ☑ done · ⏸ parked/blocked

> **Markers reconciled against GitHub 2026-08-06 (#425).** Every `#N` in this file was checked with `gh issue view N --json state,stateReason`; 11 markers said todo/parked for issues since closed as COMPLETED (#19, #20, #56, #76, #80–#86) and were corrected to ☑. Two are genuinely still open and were left alone (#75 parked, #89 in progress). **One exception:** #73 is ☑ but closed `NOT_PLANNED`, not `COMPLETED` — its own line says so ("CLOSED as largely-addressed") and is left as written.
> **The first pass of this reconciliation MISSED the "Recommended sequence" section** (caught in review, #495): the sweep matched a bulleted `- ☐ **#N` pattern, so a NUMBERED list was invisible to it, and the file briefly contradicted itself — #19/#20 marked ☑ above while that section still called them blocked/parked. A sweep is only as good as the shapes its pattern can see; when reconciling this file again, enumerate every `#N` in the raw text FIRST and only then look at what marker precedes it.
> **Why this mattered:** the header below already says this document is historical, and a reviewer read it anyway and cited closed #84 as an open risk, building an argument on a wrong premise (#424). A `☐` reads as live status regardless of what the surrounding prose claims — a stale marker is worse than no marker, because it reads as authoritative.
> **The dated PROSE in each review entry is deliberately NOT rewritten** — those are records of what was believed on that date and are correct as such. Only the status markers, which assert something about *now*, were touched. Markers drift as issues close; treat any marker as accurate only as of the date above.

---

## ☑ Session 2026-06-20 — un-shadowing → promotion → Librarian → db-per-domain

A long arc: techne went from "the understudy" (CC native memory silently carried the lead) to a memory system that captures **what was done** (tool calls → knowledge) *and* **what was said** (transcripts → Librarian), then split domains into their own db files.

### The un-shadowing arc (all closed)
- ☑ **#93 — egress / context-projection.** Tier-0 selective injection put techne in the push path. Then fixed a real **recency-starvation bug**: a large static feedback tier exhausted the char budget before fresh results were reached, so "what changed since last session" was the most likely to drop (the bug that made me boot blind to a passed test). `recent_reserve_frac` reserves budget for fresh decisions/evaluations. Closed.
- ☑ **#94 — memory-attribution probe.** Differential-canary harness — seed a store-exclusive canary, fresh session, check provenance. `off-test` (native+pull off → proves egress push) + `differential` (native ON + native-only control → proves techne is the *lead*, not masked) + `pull`. CC + **opencode** adapters (`techne probe --runtime`). Both proven live. Closed.
- ☑ **#95 — self-healing hooks.** Loud/persistent/fail-safe SessionStart; health flags in `techne_status`. Closed.
- ☑ **#92 — the understudy.** Both halves solved: injection (#93/#94) + **promotion** (below). Two silent bugs that were actively killing automated promotion, found by dogfooding, fixed: (1) candidate `source_thread_id` FK built as `{session}-{num}` but heuristic threads are content-addressed → every candidate insert silently FK-failed (`thread_row_id` now the single source of truth); (2) identity-candidate promotion self-deadlocked (2nd connection vs an open write) → identity table stuck empty (`set_value` takes the caller's conn). Closed.

### Agent-assisted promotion (the no-API-key path)
- The reflect pass is prompt-in / JSON-candidates-out with the LLM step behind an injectable `generate_fn` → run the inference in the **agent loop** instead of a headless API call. `techne reflect emit | ingest` + the **`/techne-promote`** skill (CC command). Dogfooded; honest finding: the observation backlog is mostly ephemera (durable knowledge already entered via import/backfill/dual-write), so the value is forward per-session promotion, not back-mining. (#92/#20.)

### Librarian — recall the conversation, not just tool calls (#91 closed)
- ☑ Transcript indexing: each CC JSONL transcript's prose → a Librarian document (FTS + fetch-on-demand). The observation lens only sees tool calls; decisions live in the dialogue.
- ☑ **Modular `TranscriptSource`** (canonical `Transcript` shape + neutral indexer; CC is one adapter). Other-client adapters → **#96**.
- ☑ **Option D document cards**: a `document_card` (gist + pointer) rides personal recall in `knowledge`; full content fetched on demand. Pull-only. Agent-assisted prose gist (`library cards emit/ingest`) for single-topic docs; **thread-index gist** (deterministic, from thread detection) for long multi-topic transcripts.
- ☑ Markdown-dir source + shared `index_document` unit; indexed our own `docs/` (and the 18 design docs in `~/working/claude/docs`).

### db-per-domain — a db file per persona (the big architecture call)
Decision (knowledge#327): `techne.db` = personal memory; each domain/persona gets its own db file behind a `StoreAdapter`. Design doc `docs/db-per-domain.md`. Reframed #88 (delete domain tables from core, don't register them) and converged #87/#89. **#88 closed 2026-06-20 — delivered**: domain tables gone from core, search no longer names them, and every domain routes through `load_store(config, domain)` (#102) — adding a domain is zero core edits.
- ☑ **#98 — store registry + SQLite `StoreAdapter`.** `load_store(config, domain)`, `stores:` config, `plain_connect` (domain dbs get ONLY their own schema). Closed.
- ☑ **#99 — Librarian → `librarian.db`.** Documents left core `SCHEMA_SQL`; cards stay in techne.db and resolve **cross-db** (keyed get, no ATTACH); 6 `search.py` hooks removed; `search_documents`/`techne library search` is the domain search. Live cutover: 57 docs moved, techne.db documents dropped, librarian.db rebuilt documents-only. Closed.
- ☑ **#87 — Architect → `architect.db`.** Pattern store: `investigations` (lens-aware dedup, url×surveyed_by×lens) + `patterns` + relations/applies + FTS; `ingest_patterns_md` seeds from the corpus. Team-scope (queried directly, not carded); persona-agnostic (`surveyed_by`=string, no cabal coupling). **P1** (store + schema) and **P2** (write-path, wikilink/applies graph, retire flat `type=pattern` rows, MCP `pattern_search`/`pattern_related`) shipped. **P3 (Postgres adapter) retired** — techne is SQLite-only by decision (techne#135); the `StoreAdapter` portability seam was unused and removed.
- ◐ **#89 P1 — Codex → `codex.db`.** Code graph: `symbols` + `edges` + FTS; `reachable()` = transitive blast-radius via **recursive CTE** (graph queries without a graph DB — the v1 plan). Tree-sitter parser + graphqlite (optional `techne[codex]`) deferred to when the Codex persona is scoped.

**Chain:** `techne.db` (memory) → `librarian.db` (#99) → `architect.db` (#87 P1) → `codex.db` (#89 P1) — each behind a StoreAdapter, each clean per-db-schema. Routing collapsed onto the registry in **#102** (the keystone: `config.stores` dsn overrides now take effect; `resolve_card` exercises the `StoreAdapter.get` Protocol seam).

### Staff review (2026-06-20) — all fixed + closed
Staff Python + architect review of the db-per-domain session (knowledge#337). Five issues, all shipped: **#100** (migration verify-by-identity — no silent doc drop on re-run), **#101** (github_issues → librarian.db, stop core-polluting the domain db), **#103** (empty-query FTS guard), **#102** (the keystone above), **#104** (LIKE-escaping + pre-drop backup + `techne library doctor`). 826 tests green; v0.7.0.

### Issues
- **Closed:** #88, #91, #92, #93, #94, #95, #98, #99, #100, #101, #102, #103, #104.
- **Opened:** #96 (other-client transcript adapters), #97 (platform-plugin authoring guide), #98, #99, #100–#104 (staff review).
- **Advanced (open):** #89 (graphqlite storage layer; tree-sitter parser shipped in #128), #20 (promotion mechanism shipped via the agent-assisted path). #87 fully shipped (P3 retired, techne#135).

---

## ☑ Shipped this session (merged + pushed to github, main `2bdf408`)

- ☑ **#69 — AWS credential redaction.** AKIA/ASIA access-key + labelled-secret patterns; merge review caught & fixed a JSON/SDK-form leak (`"SecretAccessKey": "…"`). Closed.
- ☑ **#70 — recall returns empty (FTS implicit-AND).** OR-of-terms fallback when the AND pass returns zero rows; verified on a live-db copy. Closed.
- ☑ **#71 — version + package_path in `techne_status`.** Answers "is the running server current?" from a normal tool call. Closed.
- ☑ **#72 — thread lifecycle never demotes.** Dropped the dead `resume_points` gate in `age_threads`; idle threads now complete→stale on the time knobs. Closed.
- ☑ **#68 — eval discipline (M1+M2).** 2-arm recall-quality harness, frozen Phase-0 baseline (techne=1.0/bare=0.0), enforced kill-gate (≥0.85 floor + ≥0.30 margin, `pytest -m eval`). Closed; v2 (cold/warm arm + running-model LLM-judge) tracked in **#75**. Docs: `docs/eval.md`, `tests/eval/EVAL.md`, `PHASE0.md`.

**Operational follow-through:** MCP server still needs to restart to run the merged code (editable venv) — `/mcp` reconnect this session, automatic next launch. On the first session-end after relaunch, ~65 long-idle threads complete in one pass (#72 backfill).

---

## Tier 1 — quick wins / active degradation

- ☑ #71, ☑ #72 shipped (above).
- ☑ **#78 — harden auto-migration for multi-machine use.** Done (`34c7a75`). Cross-process file lock (flock on `<db>.migrate.lock`) serializes the concurrent-hook race so the migration body runs once; WAL-safe pre-migration backup to `backups/`; stderr announce. Degrades to legacy path where fcntl absent. Tests incl. 6-way concurrent open → migrates exactly once. **Land this before the team pulls the v9 merge.**
- ☑ **#76 — SessionStart health banner.** Done on branch `feat/76-session-start-health-banner` (`72233db`), not yet pushed/merged. Factored `core/health.py::collect_health/format_banner` as the single source shared by MCP `techne_status` + the login banner. 607 tests pass, eval gate green. Renders at next session restart. Filed 2026-06-17.

## ✅ Merged to main 2026-06-17 (now `9d75f57`, v0.6.2)

#74 + #76 + #77 + #78 merged + pushed; GitHub auto-closed them. Version bumped 0.6.1 → 0.6.2. **#79 (outbox gate + purge) shipped on top** — see below. ✅ Server reconnected via `/mcp` and confirmed on HEAD: `techne_status` reports v0.6.2 / `build:live` / `pending_outbox:0`. 632 tests pass, eval gate green.

### ☑ #79 — outbox grew undrainable backlog under noop adapter
Enqueue wrote a `thread_summary` row every session_end regardless of adapter, but the noop adapter never drained (and the sink stub returns `ok=False` by design) → 1199 pending, ~N² growth from re-enqueued evolved summaries. Fix: gate enqueue on `_has_live_consumer` (`sync.adapter != "noop"`) in `run_session_end` + `redetect_threads`; add `outbox.purge_pending` + `techne outbox purge`. Purged live DB 1199→0 (140 synced preserved). Closed. **Note:** does *not* moot #51 — that's the dual-flush race + bridge ambiguity, both untouched here (re-eval'd, #51 stays open).

> ✅ Fixed the venv: it had been created in `infra-techne` and copied here, leaving 24 console-script shebangs (pip/pytest/litellm/…) pointing at the old absolute path. Rewrote them to `…/techne/.venv/bin/python` + corrected `pyvenv.cfg`. `.venv/bin/pip` etc. work now. (The `python` symlink was relative so it always worked; `ruff` is a compiled binary, no shebang.)

## Tier 2 — search-relevance cluster — **MERGED** (#68 gate live, so changes are measurable)

- ☑ **#74 — FTS bare-operator/backslash queries crash.** Done (`6b975ca`). `_sanitize_fts_query` now quotes every term unconditionally + `_fts_terms` strips backslashes; shared with #70 OR-builder. Parametrized regression tests at helper + `search_fts` level. 611 tests pass, eval green.
- ☑ **#77 — FTS5 Porter stemming** (the #70 deferred piece). Done (`0799714`). `tokenize='porter unicode61'` on all three FTS tables; v9 migration DROP+CREATE+repopulates from content tables. `TestV8ToV9Migration` covers tokenizer/data-survival/stemmed-match/idempotency. 616 tests pass, eval green. **⚠️ Live `~/.techne/techne.db` auto-migrated to v9 the instant the migration code was saved** — the hooks run from the editable venv, so saving schema code applied it to the live DB pre-commit. Verified intact (2785 obs, FTS repopulated). See memory note.
- ☑ **#73 — rank favors long blobs — CLOSED as largely-addressed.** Premise structurally mitigated: `post_tool_use.py:33` truncates obs to `[:2000]` (live DB maxes 3112 chars, 0 over 5KB), BM25 length-norm works (longer obs rank *below* shorter), no fixture reproduces the symptom. Real recall win was #77 (stemming), backstopped by the #68 gate. Reopen with a failing eval fixture if a concrete case ever surfaces → then add a `_rerank` conciseness tie-breaker.

## Tier 3 — strategic gates (schedule deliberately)

- ☑ **#68 — eval discipline** shipped + closed (see above). Its gate now backstops the Tier-2 relevance work.
- ☑ **#57 — memory portability across AI clients. CLOSED 2026-06-17 (validated + shipped).** Memory captured under opencode now lands in the same `~/.techne/techne.db` the Claude Code client reads — demonstrated end-to-end. Shipped: A.1 JSONL parser decoupling (`sync/jsonl_records.py`, closes #21 leak), `techne_observe` MCP tool (model-facing capture), `record_observation` core + `techne observe` CLI (plugin-facing capture), `integrations/opencode/` TS plugin (maps `tool.execute.after` → `techne observe`, stamps `platform=opencode`). Verified vs opencode 1.17.5 built-in `big-pickle` model (no external key needed). **Gate findings:** MCP read path = zero coupling (Config.load→~/.techne); only coupling was capture (MCP tool unreachable from a plugin → CLI bridge); pipx CLI drift bites the plugin bin (TECHNE_BIN→venv workaround). **Shipped within #57:** ✅ A.1 (jsonl_records, #21 leak) · ✅ `techne_observe` + `record_observation` + `techne observe` CLI · ✅ opencode plugin (gate passed) · ✅ pipx→editable (drift killed) · ✅ **A.2** (`13b1c30` — single-sourced the platform enum from the Literal so config-validator + CLI choices can't drift; added `opencode` to the enum; neutralized the import_md docstring).
**Also shipped:** ✅ opencode prompt + session-end capture (`a584edf` — `chat.message`→UserPrompt, `session.idle`→`techne session-end` CLI = thread detection/summaries; opencode sessions now surface in resume/threads/banner, full CC parity). Validated end-to-end.
**Remaining sub-work** (file as sub-issues): formally close/skip the PlatformHookContract Protocol (MCP-tool + CLI reframe won the capture path → likely just close); coupling #4 — *route* on `sessions.platform` (e.g. reranker same-platform weighting) or accept "stamped is enough"; optional: opencode context-injection on `experimental.chat.system.transform` (inject memory into opencode's prompt like the CC banner does). The original "paths assume Claude Code" coupling (#3) is now addressed (the real leak was #21/A.1; what's left is the legit CC-specific transcript-import command + sensible `platform="claude-code"` defaults).

## Tier 4 — deferred / blocked / low

- ☑ **#51 — outbox flush()/flush_sinks race. CLOSED 2026-06-17 (`31431bf`).** Fixed via target namespaces (Approach A): `sync:<name>` adapter-bound, `sink:<name>` sink-bound; enqueue rejects un-namespaced targets; the two flush paths filter disjoint prefixes so ownership is a permanent row property, never dependent on registry state at flush time. Removed the `_register_lifecycle_sinks`/`ok=False` stub. Regression test proves no cross-draining. **Unblocks cabal#14** (Archivist registers `sink:archivist`). 666 tests pass.
- ☑ **#56 — minor backlog tracker (06-16 staff reviews).** Opportunistic.

### Staff review 2026-06-17 (`e41d204..7d45067`, #57/#51 work) — verdicts SHIP-WITH-FIXES / SOUND-WITH-CAVEATS
Blocker fixed inline (`7d45067`): `thread_boundaries` grew unboundedly on per-turn `run_session_end` (live: 72k rows, one session 37k) — DELETE-then-insert now, test guards boundary count. Filed:
- ☑ **#80 — migrate un-namespaced pending outbox rows** (post-#51; live DB unaffected, defensive). Recommend.
- ☑ **#81 — `sessions.platform` is write-only** — wire a read path (`techne status` breakdown) + decide cross-client resume-context scoping.
- ☑ **#82 — `link_threshold` YAML fallback is the unreachable 0.85** (pre-existing v0.5.0 bug; configs with a `threads:` block get zero links).
- ☑ **#83 — minor backlog bundle** (purge_pending doc, chat.message role filter, inject escaping, jsonl_records→hooks smell, PORTING.md).
- **Open op task (awaiting go):** dedup the live DB's 72k stale `thread_boundaries` (classifier-blocked; code fix stops future growth).

### Staff Linux portability review 2026-06-17 — verdict PORTS-WITH-FIXES
Core (FTS-only default) runs on Debian/Ubuntu with **zero code changes**; paths are `Path.home()`-relative, hooks wire via `sys.executable`, CI already on ubuntu-latest. Filed:
- ☑ **#84 — `search[vectors]` `enable_load_extension` crash** (HIGH, code) on Linux Python without loadable-extension support; only bites the `[vectors]` extra. Fix = broaden the except.
- ☑ **#85 — add a Dockerfile** (the "containerizable" goal has no artifact; working minimal one in the issue).
- ☑ **#86 — Linux `techne watch` polish** (systemd unit + inotify watch-limit docs).
- ☑ **#20 — end-of-session reflect pass.** (Was: parked — mid 2-week observation window on `experiment/reflect-pass`.) Since CLOSED/COMPLETED.
- ☑ **#19 — tag inference + MCP-sampling fact extraction.** (Was: blocked — no API key, no client supports MCP sampling; needed a local-model fallback first.) Since CLOSED/COMPLETED — the agent-loop `generate_fn` path removed the API-key blocker.
- ⏸ **#75 — eval v2 (cold/warm arm + running-model LLM-judge).** Deferred — cold/warm needs a freshness feature; LLM-judge must use the running model (never an API key). Filed from #68 close-out.

---

## Recommended sequence (as of 2026-06-20; items reconciled 2026-08-06)
1. ✅ #71 → #72 → #68 (M1+M2) — shipped.
2. ✅ Tier-2 relevance cluster (#74 + #77 stemming) + #73 dispositioned (closed) — shipped/closed.
3. ✅ #79 outbox gate/purge — shipped.
4. ☑ **#57 — memory portability.** CLOSED — opencode full parity (read+write). Optional follow-ups in the issue close comment. **NEXT within #57:** A.2 path de-hardcoding + decide PlatformHookContract's fate; then opencode prompt/session-end capture. Aider remains a later SourceAdapter test (markdown history; no MCP/hooks).
5. ✅ Opportunistic: confirm-and-close **#51** (likely moot post-#79); **#56** staff-review backlog. — both CLOSED/COMPLETED.
6. Blocked/parked: #75 (needs freshness feature) — **still open**. ✅ #19 (was: no API key / no MCP-sampling client) and ✅ #20 (was: mid reflect-experiment) have since CLOSED/COMPLETED; the blockers named here were real at the time and were subsequently cleared.

## Parked: cabal (separate track)
#13 three-tier memory · #14 archivist outbox sink · #12 A2A streaming · #11 AbortGraph. After techne core is trustworthy, unless a launch milestone needs cabal.
