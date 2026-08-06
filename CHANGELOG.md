# Changelog

All notable changes to techne are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.

## [Unreleased]

### Added

- **Operator-owned config layer for egress-selecting keys** (#393) — the keys that choose where memory can GO (`sync`, `connections`, `llm_api_base`, `llm_api_key`, `model`, `reflect.model`, `conflict.model`, `archivist_db_path`, `standalone`) can now be pinned in a `config.operator.yaml` that techne honors only while the file is provably not writable by the process reading it. Resolved from `/etc/techne/config.operator.yaml` → `$TECHNE_OPERATOR_CONFIG` → `<techne_home>/config.operator.yaml`, gated once in `Config.load()`. A layer that exists but is writable by us FAILS CLOSED to the built-in defaults rather than deferring to `config.yaml`. **Installs with no operator layer are unaffected** — `config.yaml` keeps full authority, and the new `config:trust` check reports that state rather than implying a boundary nobody deployed.

- **`techne architect ingest --verdict`** (#428) — record the operator's actual judgment (`skip` / `skip-with-flag` / `patterns-only` / `full-eval`) for the URLs an ingest covers.
- **`techne architect reverdict URL --verdict X`** (#428) — correct one investigation's verdict without re-ingesting its pattern block. Leaves `surveyed_at` alone: correcting a judgment is not a re-survey.
- **`techne architect sweep-legacy-verdicts --before DATE [--apply]`** (#428) — retire pre-#428 auto-assigned verdicts to `unknown`. Dry-run by default and date-scoped, so a genuine post-fix verdict is never clobbered.

### Fixed

- **The investigation verdict is no longer hardcoded** (#428) — `ingest_patterns_md` wrote `full-eval` for any URL group carrying patterns and `skip-with-flag` for a no-lift bullet, and the CLI could not set one, so the column recorded *whether the markdown had patterns* rather than a judgment and `patterns-only` was unreachable by any code path. The live corpus had 202 `full-eval` / 70 `skip-with-flag` / 0 `patterns-only` against 19 eval docs on disk. Verdicts are now validated against a vocabulary, and an ingest that does not state one records `patterns-only` (the documented modal outcome) instead of `full-eval`.

## [0.12.0] — 2026-07-24

### Added

- **Inter-session coordination mailbox** (#376, #378, #379) — live Claude sessions on one host can message each other over a `coord` bus (`techne_send` / `techne_inbox` / `techne_threads`): broadcast + direct addressing, session aliases, threaded replies, and a collision warning when two sessions claim the same branch or worktree. Phase 1 (core mailbox) + Phase 2 (aliases, threads, collision warning).
- **Architecture cards** (#383, #384) — an in-repo repo card (`ARCHITECTURE.md`) plus per-seam cards (`docs/arch/*.md`) with a git-anchored freshness gate and `AGENTS.md` wiring, so an agent grasps a subsystem from its card before reading source.
- **Session-end Bash backfill** (#371, #375) — capture can backfill Bash activity at session end behind a shared read-only denylist, restoring partial visibility for otherwise-uncaptured Bash-only sessions.

### Changed

- **Honest edit-rail attribution** (#377) — utilization attributes edits at symbol granularity, turn-windowed, for truthful read-vs-write accounting.

## [0.11.0] — 2026-07-16

### Added

- **Base Archivist — closed team-memory loop** (#346 PR 2) — a non-standalone
  instance now egresses live durable knowledge to the Archivist (`sink:archivist_knowledge`)
  at session end, and recall automatically federates over its `archivist.db` as a
  `team` connection. So a fact stored by one agent is read back as team memory by
  another sharing the Archivist — locally, no cabal, no git. Re-pushing the same
  fact doesn't duplicate it (the handler skips one already live in the Archivist).
  Known v1 gaps, both closed by the #223 export ledger: superseded facts leave a
  stale revision live in the Archivist (append-only, no supersession propagation),
  and every session re-enqueues all live knowledge (the outbox's partial unique
  index only collapses still-pending duplicates).

- **Base Archivist — write target + topology lane selector** (#346 PR 1, #224) — a
  `standalone` config flag selects the outbox lane for thread summaries: `true`
  (default, behaviour-preserving) keeps the `sync:` adapter lane; `false` routes to
  `sink:archivist`, a techne-native Archivist that writes pushed team memory into a
  techne-schema `archivist.db` (`archivist_db_path`, default `<data_dir>/archivist.db`).
  `_has_live_consumer` is now lane-aware (sync adapter vs. registered sink). techne
  never touches git in either lane. The federation read-back loop is PR 2.

- **Operator-editable maintenance prompts** (#219, completes the Coherence track of
  #212) — the reflect / consolidate / dream prompts that drive knowledge maintenance
  are now overridable per-install: drop a file at `<data_dir>/prompts/<name>.md` and
  the change is picked up on the next run — no code edit, no restart. A broken
  override (bad `{placeholder}`) falls back to the built-in default with a warning,
  so it can never break the maintenance loop. `techne prompts export` scaffolds the
  defaults to edit from; `techne prompts path` prints the directory. Contract in
  `docs/prompts.md`.

- **Codex edit-rail telemetry hygiene** (#342, #343) — two tuning fixes so the
  edit-rail data is trustworthy for the fix-or-cut read (#338). (1) Per-symbol
  -per-session throttle: a symbol's blast radius is injected at most once per
  session, so re-edits don't spam context or inflate utilization counts. (2) The
  injection log now records `total_hits` — the true pre-cap blast-radius size —
  alongside `hit_count` (the shown/cost count), so `techne audit` shows a
  `radius` column distinguishing a 252-dependent hub from a 10-dependent leaf.
  `context_injections` self-migrates the new nullable column.

- **Recall returns memory + librarian documents** (#340) — `techne_recall` now
  folds the top library documents (from `librarian.db`, resolved locally) into
  its result set alongside memory. Every row is labelled `origin` (`memory` vs
  `library`) so results stay clearly sourced; documents come as capped snippets
  with `source_url` for full retrieval. Capped by `search.recall_documents_limit`
  (default 3; 0 disables). Best-effort — a missing/broken `librarian.db` degrades
  to memory-only. New `librarian.recall_documents()`. Previously the 62-document
  store was reachable only via the `techne library` CLI, invisible to recall.

- **`techne audit`** (#329) — a utilization audit with two descriptive panels:
  the injection read-path (per-source events / hits / tokens / sessions /
  empty-fires over `context_injections`) and the ledger read/write ratios per
  memory domain (store-vs-recall, codex-reads-vs-edits, architect + librarian).
  `--since N` windows to the last N days; `--json` for machine output.
  Attribution ("was the injected item used") is out of scope — see #105.

- **Codex edit rail** (#335) — an opt-in PostToolUse hook (`hooks.codex_edit_rail`,
  default off) that, after an Edit/MultiEdit, maps the edit to its enclosing
  symbol and injects that symbol's blast-radius (transitive dependents — "what
  breaks if I change this") into context. Threshold-gated
  (`codex_edit_rail_min_deps`, default 3) so low-risk leaf edits stay silent, and
  capped (`codex_edit_rail_limit`, default 10, closest-first by depth). Logs every
  injection to `context_injections` (`source=codex_edit_rail`) so its utilization
  is measurable against the other read-paths (#329). New `codegraph.symbol_at()`
  maps a file+line to the innermost enclosing symbol via its span.

- **Config-driven tag inference threshold — tag revival** (#352) — `tag_threshold`
  is now a `Config` field (default `0.42`). The old hardcoded `0.6` sat above the
  model2vec (potion-base-8M) cosine ceiling (~0.59), so `infer()` tagged 0% of
  rows — the embedding stack was revived (#356) but tags stayed empty. `infer()`
  resolves the threshold from config when none is passed explicitly; the ceiling is
  backend-dependent, so it's a dial. Re-running `vectors backfill --tags` took the
  live store from 147 to 8.4k / 11.4k tagged. (Prototype over-fire on `todo` /
  `context` is tracked in #368.)

- **Per-category memory fill-state in the banner** (#334) — the session-start banner
  breaks its opaque `knowledge` total into per-type counts (+ identity) on a
  `memory:` line, so the agent can see WHERE memory accumulates and self-regulate
  what it writes. `db_stats` gains `knowledge_by_type`; non-canonical types are
  appended after the canonical ones so the line always accounts for the full total.

- **`techne profile`** (#359) — a repeatable hot-path profiling command (read-only,
  never mutates). `profile_import_cost()` measures the cold `import techne.config`
  price every hook pays before any work (#351); `profile_thread_detection()` times
  heuristic detection over the largest session and surfaces the top functions by
  cumulative time (#350). Latency-at-scale had no harness; now it does.

- **Hook budget-burn is observable** (#362) — `bounded_hook` hard-killed on timeout
  and otherwise exited 0 with no signal, so a hook creeping toward (or blowing) its
  budget was invisible — the #350 pathology where thread detection sat at 4.45s
  under a 5s budget and silently dropped detection every turn. It now warns on
  stderr at `warn_ratio` (default 0.7) of budget, and more loudly on an actual
  timeout, naming the hook + elapsed/budget.

- **Vector-channel degradation in the banner** (#358) — a missing embedding backend
  silently downgrades recall to FTS-only and a backend/model swap leaves the vec
  index stale, both with zero signal (this is how the embedding stack sat dead in
  prod, #352). `collect_health` folds `vector_channel_status` into a `vectors`
  summary and the banner alarms (⚠️) only when recall has actually degraded —
  stored vectors but no usable backend, or a stale index after a swap.

- **Schema-drift detector + v18 migration heal** (#364) — reconcile is deliberately
  additive (`CREATE IF NOT EXISTS`), so a redefinition under an existing name
  applies silently never and drift goes unnoticed. A detector now compares live
  trigger/index definitions (from `sqlite_master`) against canonical via DDL
  normalization and reports mismatches, and a v18 migration heals the two drifts
  surfaced in the perf review — without overturning the additive convention.

### Fixed

- **Codex blast-radius direction + fixture noise** (#325, PR #336) — `blast-radius`
  now defaults to dependents (`direction=in`, "what breaks if I change this")
  instead of dependencies, and `tests/fixtures/**` name-collision phantom nodes are
  excluded by default (`--include-fixtures` / `include_fixtures` to opt back in).

- **Evidence gate: passive inspection can't back a completion claim** (#105, PR #354)
  — the completion check backed on `any_tool_call`, set by ANY captured tool
  including read-only inspection (Read/Grep/Glob/WebFetch), so an agent that only
  *looked* at a file and claimed "done" got a green "backed" — the gate affirming
  work that never happened, the worst failure direction for a "no unbacked word"
  check. Split out `active_tool_call` (a deed — anything outside a new
  `PASSIVE_TOOLS` set) and back completion on `active_tool_call or artifacts`.

- **Federation: relative connection db_path resolves against TECHNE_HOME** (#332) —
  a federation connection's `db_path` had to be absolute, making a committed
  `config.yaml` machine-specific. A relative path now resolves against
  `techne_home()` at open time (absolute paths and `~` unchanged), so a shared
  config can carry `db_path: team-mirror.db` and each machine resolves it locally.
  Resolved at the open site, not config load, so the portable string survives
  save round-trips.

### Performance

- **Hook hot-path fixes + embedding/tag revival** (#356, staff review) — steady-state
  hook cost: `find_repo()` gained a per-pass path cache (thread detection was making
  ~126k `stat()` calls on a 1695-obs session by re-walking the same recurring
  paths), and feature extraction now runs once per pass. Also restored the embedding
  stack — the pipx runtime was missing `model2vec` / `sqlite-vec`, so recall had
  silently degraded to FTS-only and observations stored `tags=NULL`; vectors are
  backfilled here (the tag half lands via #352).

## [0.10.0] — 2026-07-13

### Added

- **Codex LSP-grounded call graph** — a reusable LSP client foundation (#320, PR
  #326) plus pyright-resolved call edges so callers are ground-truth rather than
  heuristic (#321, PR #327). Symbol resolution now accepts bare names and errors
  hard on an unresolvable symbol id (#300, PR #302), applied to the MCP
  blast-radius tool as well (#303, PR #304). The codex index reindexes the edited
  file on Write/Edit for mid-session freshness (#301, PR #314).

- **Architect curation** — topic-index + supersession curation (#308, PR #313),
  validity-windowed supersession (#306, PR #317), and the relation graph rendered
  as Mermaid/DOT (#305, PR #316).

- **Per-turn memory-seed nudge (#277, PR #309)** — closes the write half of the
  recall loop begun in 0.9.0. SessionStart injection is instrumented and unified
  with per-turn recall into a single `context_injections` surface, and per-turn
  recall injections are logged so the READ path is auditable (#329, PRs #330/#331).

- **Memory/skill eval harness — engine + seed corpus (#118, Phase 1, PR #312).**

- **Content-addressed spawn contracts (prototype, #318)** — handoff drift
  detection between spawner and spawned agent.

### Fixed

- architect: create the topic index in the migration, not the base schema (#315).

### Docs

- README "run vs develop" install note documenting the pinned pipx path (#297).

## [0.9.0] — 2026-07-10

### Added

- **`techne doctor` — core-DB health audit (#295, PR #296)** — a new top-level
  command that flags silent-accumulation bugs (duplicate rows a table shouldn't
  have) and reports reclaimable freelist space, with `--vacuum` to reclaim it in
  place. Exits non-zero on duplication debt, so it's safe to run in CI or a
  scheduled audit. Extensible via a `_DEDUP_CHECKS` registry of `(table, key)`.

- **Per-turn recall injection (#109 / #277)** — an opt-in `UserPromptSubmit` hook
  (`hooks.per_turn_recall`, default off) that runs a knowledge search scoped to each
  submitted prompt and injects the most relevant durable facts via
  `hookSpecificOutput.additionalContext` — the **dynamic** per-turn complement to the
  **stable** SessionStart injection. Strictly best-effort: never blocks the turn
  (always exit 0), no-ops on flag-off / empty prompt / no match / timeout, and
  excludes soft-archived rows (mirrors egress discipline). `per_turn_recall_limit`
  (default 5) caps rows per turn. The stdin prompt-field name is undocumented in the
  CC hooks reference, so the hook defensively accepts `prompt` / `user_prompt` / `text`
  and no-ops safely otherwise. (Note: whether Claude Code exploits Anthropic prompt
  caching across the stable/dynamic boundary is harness-controlled and undocumented —
  this delivers the per-turn recall capability regardless; any cache benefit is a bonus
  techne can't set from a hook.)

- **Federation brick #1: deposit-and-drain (#221)** — the first step toward
  one-distiller-many-sources memory federation.

  **Pattern:** an ephemeral producer (a short-lived agent, a CI job, a sidecar)
  deposits raw observations into a techne store it doesn't own by calling
  `record_observation(config, ...)` with a `Config` whose `db_path` points at
  the foreign store.  A single distiller (`drain.py`) then drains that store
  asynchronously — no LLM call, no blocking on the deposit path (accept-fast /
  distill-async).

  **Primitives:** the deposit and drain primitives pre-existed; this brick
  proves the round-trip with an acceptance test suite and adds a discoverability
  CLI surface.

  **Idempotency (two signals):**
  - *Signal A* — `knowledge_candidates.source_session_id`: once a session has
    been distilled (candidates written), drain.py's `NOT IN` worklist query
    excludes it on subsequent fire-ups.  Tested end-to-end.
  - *Signal B* — `.drained.json` fingerprint: drain.py filesystem state for
    empty-yield sessions (no candidates written).  Handled by drain.py; noted
    in the test comments.

  **Sovereignty:** deposit writes raw observations only; distillation stays
  the distiller's async job.  The local caller's store is never touched.

  **New CLI command:** `techne deposit --target <foreign techne home>
  --session-id <sid> --content "..."` — the explicit "deposit into a store you
  don't own" surface.  Mirrors `techne observe` in option shape; targets an
  independent foreign store.

  **Follow-up:** candidates land in the foreign store; Archivist-promotion
  (surfacing them to the owning agent's curated memory) is tracked in #222.

- **Codex db-per-repo (#209, PR #286)** — each indexed repo now gets its **own**
  code-graph db at `<data_dir>/codex/<slug>-<hash8>.db`, derived deterministically
  from the repo's resolved absolute root, replacing the single shared `codex.db`.
  Indexing a second repo can no longer collide with, overwrite, or bleed into the
  first. A pre-#209 single `codex.db` migrates to its per-repo home automatically
  (WAL-checkpointed move) the first time any consumer — CLI, hook, or MCP — resolves
  a codex db. New API: `repo_db_path` / `codex_db_for_repo` /
  `resolve_repo_db_for_query` / `list_codex_repos` / `migrate_legacy_codex_db`. New
  CLI: `techne codex index-all` (refresh every indexed repo) and
  `techne codex stats --all|--repo`. Read consumers (MCP query tools, grep-assist)
  scope to the repo under cwd, or `--repo`.

- **grep-assist — graph-augment the agent's own Grep (#206, PR #208)** — an opt-in
  `PreToolUse` hook (`hooks.codex_grep_assist`, default off) that runs a Codex symbol
  search for a Grep's pattern and injects the matching symbols (name, signature, file,
  freshness marker) into the model's context via `additionalContext`. Never blocks the
  tool; degrades to a no-op on any error, a missing/empty index, or a wall-clock
  timeout. Scopes symbols to the repo actually being searched (via the #209 db-per-repo
  router), fixing the single-global-graph signal/noise problem. `codex_grep_assist_limit`
  (default 8) caps injected symbols. Claude Code loads hooks at session start, so it
  fires from the next session after `techne init`.

- **Hook-registration drift check (#287, PR #289)** — `check_hook_drift(config)`
  compares the techne hooks enabled in config against what's actually wired into Claude
  Code's `settings.json`; `techne status` surfaces a `⚠ Hook registration drift — run
  'techne init' to reconcile` warning when they diverge (e.g. grep-assist ON in config
  but the `PreToolUse` group missing after a re-init or stray edit).

- **Test suite no longer clobbers the real `~/.claude` (#288, PR #289)** — a
  `conftest.py` guard points hook-registration tests at a throwaway home, so running the
  suite can't strip or rewrite a developer's real `settings.json`.

### Fixed

- **`thread_boundaries` duplicate-row debt (#295, PR #296)** — a schema **v17**
  migration collapses the duplicate `(session_id, obs_index_a, obs_index_b)` rows
  (keeps the most recent per triple) that had accumulated because `run_session_end`
  re-inserts a session's full boundary set every turn, and no backfill had shipped
  after the delete-before-insert write-path fix. A new `UNIQUE` index makes any future
  non-atomic regression fail loudly instead of silently re-accumulating; the redundant
  prefix index is dropped (the unique index serves `load_boundaries` as a prefix). On
  the reference DB this collapsed 76,343 rows → 9,438 and reclaimed ~26% after `VACUUM`.

## [0.8.0] — 2026-06-28

The "memory that maintains itself, honestly" release: a self-curating memory loop
that runs on the agent's own model, a matured db-per-domain architecture, a full
data-integrity audit, and a leaner, self-teaching install.

### Highlights
- **Memory hygiene on the agent's own model** — reflect, consolidate ("sleep"), and
  claim-evidence reconciliation now run via a render-prompt / answer / ingest loop using
  the model already in the room. No API key, no extra inference.
- **litellm is now optional** (`[llm]` extra) — the minimal `pip install techne` drops from
  ~100 MB to ~10–15 MB.
- **MCP self-teaching** — the server now ships an `instructions` block (the memory loop) so
  any MCP client's agent knows *when* to store/recall, not just that the tools exist.
- **Full data-integrity + robustness audit** (#165–#170).

### Added
- **Memory hygiene loop (agent's own model, no key):** resident-model fact extraction over
  MCP (#19), reflect candidate-model foundation + decision provenance (#108), consolidate
  "sleep" dedup/merge pass (#108), claim-level evidence reconciliation (#107).
- **Curated-memory drain (#107):** ephemeral memory doctor (file-reference reconciliation),
  persistent drain, and an init coexistence posture (`spool-primary`).
- **Knowledge freshness:** 4-state graded staleness for git-anchored memories (#90);
  cascade-to-uncertain dependency staleness + a recall gate that annotates and down-ranks
  uncertain rows (#117).
- **Codex code-graph domain — EXPERIMENTAL (#89):** tree-sitter symbol/edge extraction
  (Python + TypeScript), `callers`/`callees`/reachability/blast-radius graph queries, and
  `codex_search` / `codex_blast_radius` MCP tools. Indexing is manual (the graph can go
  stale; re-run `codex index`). Behind the `[codex]` extra.
- **Architect:** pattern relation/applies graph + `pattern_search` / `pattern_related` MCP
  tools (#87).
- **Operations:** cross-DB `fsck` + all-DBs-together `backup`/`restore` (#139).
- **Security:** shell-execution gates — command-chaining block + fail-open CVE pre-install
  audit (#120, `[security]` extra).
- **Eval harness:** deterministic MeME task taxonomy + observability plumbing, plus an LLM
  answerer/judge for negative-polarity tasks (#118).
- **MCP server `instructions`** (the four-step memory loop) + trigger-led `store`/`recall`/
  `search` tool descriptions.
- **sessions.platform read path (#81):** `techne status` platform breakdown and per-thread
  provenance surfaced at injection (cross-platform by design — surfaced, never filtered).
- **Deploy/runtime:** Dockerfile + `.dockerignore` (#123), systemd user unit + inotify
  watch-limit docs (#122), `TECHNE_HOME` to externalize the root (#114), honor
  `CLAUDE_CONFIG_DIR` when reading host files (#115).
- `docs/modules.md` (per-module tour) and `docs/context-architecture.md` (CC-coupling /
  durable-context decision record).

### Changed
- **litellm → optional `[llm]` extra** (was a core dependency). The default hygiene loop uses
  the agent's own model; `[llm]` is only for autonomous/headless operator-model runs. Dropped
  the unused `[anthropic]` extra. Minimal install ~100 MB → ~10–15 MB.
- **db-per-domain matured:** unified migration engine across core/librarian/architect/codex
  (#136, closes #138); `StoreAdapter` is SQLite-only — dead portability surface removed
  (#135, closes #87); personal/team data boundary enforced at write time (#137).
- Retired the hand-edited markdown patterns corpus in favor of `architect.db` (#121).
- Shared resident-model JSON-parse + prompt-hash util `core/_resident.py` (#164);
  `_get_embedding_model` moved to a neutral `core/embeddings.py`.
- Holistic README rewrite (technē framing, full extras map, agent's-own-model model section).

### Fixed
- **Data-integrity + robustness audit (#165–#170):** thread-ID construction unified through
  `thread_row_id` (#165); hook/process hardening against config + input failures (#166);
  `config.save` security + injection/staleness persistence (#167); retrieval robustness —
  empty-FTS, superseded-vec over-fetch, egress band, prototype cache (#168); migration
  idempotency + redeployed COALESCE knowledge FTS triggers (#169); codex edge corruption +
  three SELECT→INSERT TOCTOUs + sync correctness (#170).
- Empty-FTS-query crash closed in one shared guard (#143).
- CC auto-memory dir resolved via git-root slug + `autoMemoryDirectory` (#175).
- Migration cross-process race + anchor too-large degradation, now tested (#141).
- Outbox: bare pending targets migrated to the `sync:` namespace (#80); v10 migration guarded
  when the outbox table is absent (#127).
- Hooks: bounded SIGALRM timeout + exit-0 degradation on all sidecars (#126).
- `enable_load_extension` guard broadened to catch AttributeError/sqlite3 errors (#125);
  reflect persisted-thread FK + candidate-insert guard (#106); config thread fallbacks read
  dataclass defaults (#82); health capture-gap alarm keyed on last observation; staff-review
  bug sweep — supersede, reconciler, FTS, redaction (#131–#134).
- **Low-severity backlog cleanups (#188):** removed dead `hook_error`/`session_id`; documented
  the process-global `_migrated` memo; cached the fastembed availability check in `infer()`;
  `format_resume_context` trust-boundary note + closing-tag neutralisation; filled two test
  gaps (dead-row purge, mixed-batch flush). Trackers #140/#56/#83 triaged.

### Security
- `shell_guard`: anti-command-chaining gate plus an optional `pip-audit` CVE pre-install audit
  that fails open (the basic gate holds even without the auditor) — #120.

### Known limitations
- **Codex is experimental:** indexing is manual; the graph goes stale as code changes
  (auto-refresh is planned). Cross-file edges resolve for ordinary calls; some remain
  unresolved.
- **Vector/semantic recall** requires `[vectors]`; on Python 3.14 it is not yet installable
  (no `onnxruntime` wheel) and degrades to FTS-only.
- The curated-memory drain (#107) is inert when Claude Code auto-memory is disabled.

---

## Earlier releases

Condensed; see the linked issues/PRs and git history for detail.

- **v0.7.0** — db-per-domain: one db file per domain behind a `StoreAdapter` (librarian/
  architect/codex), store registry (#98), Option-D document cards, staff-review batch
  (#100–#104).
- **v0.6.2** — reliability + recall: FTS5 hardening + Porter stemming (#74/#77), SessionStart
  health banner (#76), flock-serialized WAL-safe migrations (#78), outbox gate + purge (#79),
  eval-discipline harness (#68).
- **v0.6.1** — correctness/hygiene (Fable review): FTS double-quote crash fix, search
  provenance separation, supersede guard, atomic redetect/session-end, public-API contract
  tests, `schema_version` race fix, `llm_api_key` chmod 0600.
- **v0.6.0** — embedding techne in custom runtimes: `llm_api_base`/`llm_api_key`,
  caller-supplied session IDs, `init_session_for_external_id`, `cabal` platform.
- **v0.5.1** — review pass: 2 blockers + 5 high + 8 medium fixes, +22 tests.
- **v0.5.0** — first public release: JSONL backfill, dual-column session summary (schema v4),
  user-prompt-anchored thread detection, `threads redetect`, platform package consolidation.
- **v0.4.1** — hooks rewritten to Claude Code's wrapped `{matcher, hooks:[...]}` shape.
- **v0.4.0** — heuristic thread detection (no `ANTHROPIC_API_KEY` for the default install);
  schema v3 `thread_boundaries`.
- **v0.3.1** — `init` writes `sys.executable`; `load_adapter` drops `None` kwargs.
- **v0.3.0** — argument-order standardization on `should_capture`/`add_observation` (breaking).
- **v0.2.2** — `capture_read` / `post_tool_use=False` fixes; symmetric `FilesystemAdapter.pull()`.
- **v0.2.1** — hook test coverage, `contacts.md` parser hardening, public API surface.
- **v0.2.0** — LiteLLM as default LLM router, lazy schema init, `techne.lifecycle` module.
- **v0.1.0** — initial release.

[0.8.0]: https://github.com/tenishai/techne/releases/tag/v0.8.0
