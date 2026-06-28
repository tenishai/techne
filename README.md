# techne

**Persistent memory for AI agents — local-first, one file, and it runs on the model already in the room.**

*Technē* (τέχνη) is the ancient Greek word for **craft**: practical, applied know-how — the knowledge you build by *doing*, as opposed to *epistēmē*, abstract theoretical knowledge. That's the idea here. techne is the craft knowledge an agent accumulates across sessions — what it did, what it learned, who it works with — so it gets better at the work over time instead of starting cold every session.

Concretely: a SQLite-backed memory store whose core is a **single local file** (`techne.db`) — yours, offline, inspectable with `sqlite3`. It gives an agent three things to be useful over time: a structured record of *what it did* (sessions + auto-detected threads), a curated record of *what it learned* (knowledge), and a stable record of *who it is and who it works with* (identity). Its self-maintenance — distilling sessions into durable memory, deduping, evidence-checking claims — runs on **the agent's own model** (no API key, no extra inference bill). Larger or team-scoped domains — documents, patterns, code — each get their own SQLite file behind a `StoreAdapter` (db-per-domain).

> **Tour the pieces:** [`docs/modules.md`](docs/modules.md) — a short sales-pitch for each module (what it does, why it's different, how to enable it).

```bash
git clone https://github.com/dimartin/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
techne init
```

---

## What's inside

| Layer | Role |
|---|---|
| **Sessions** | Discrete agent runs. Each session accumulates observations and ends with auto-detected topic threads. Carries three typed summary columns by provenance — `summary` (manual override), `summary_llm` (e.g. Claude Code's `ai-title`), `summary_heuristic` (auto-detector output). Read priority is manual > LLM > heuristic. |
| **Threads** | Distinct topics within a session, detected by a structural heuristic (default) or by LLM. Boundary signals: temporal gap, tool-family change, repo change, file/entity overlap, **and user prompts** — `UserPrompt` observations don't break alone but amplify other weak signals. Threads link across sessions (`parent_thread_id`) so a chain of work is traceable over weeks. |
| **Observations** | Tool calls and content captures during a session. Includes user prompts (`tool_name = UserPrompt`) when ingested via the historical transcript backfill. Source material for thread detection. |
| **Knowledge** | Durable facts, decisions, patterns, evaluations, and procedures. Supersession-aware (revisions tracked). |
| **Identity** | Key-value store for who-the-agent-is and who-it-works-with. Ingests structured markdown (`contacts.md`, `invariants.md`) and YAML on init. |
| **Outbox** | Pluggable sync for shipping curated knowledge to a team-memory backend. Default adapter is `noop`; ships with a `filesystem` adapter; the protocol is open for custom adapters via entry points. |
| **Search** | FTS5 across all content layers. Optional vector search via `sqlite-vec` + `fastembed` for semantic similarity. |
| **Domain stores** | `techne.db` is personal memory; larger/team-scoped domains get their own db file behind a `StoreAdapter` (db-per-domain) — **Librarian** (`librarian.db`, documents), **Architect** (`architect.db`, patterns), **Codex** (`codex.db`, code graph — *experimental*). Each db carries only its own schema. See [Domain stores](#domain-stores-db-per-domain) and `docs/db-per-domain.md`. |

The schema and triggers all live in `techne/core/database.py`. Schema migrations are tracked in a `schema_version` table; `connect()` is lazy-init so library callers don't need to remember to call `init_db()` first.

---

## Install

techne is in early alpha — installed from source. Public PyPI distribution is on the roadmap.

**Requires:** Python 3.11+, git.

Use a virtual environment — required on Debian/Ubuntu (PEP 668), recommended everywhere else:

```bash
git clone https://github.com/dimartin/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                  # core: capture, FTS recall, threads, knowledge, identity (lightweight)
pip install -e ".[vectors]"       # semantic recall — sqlite-vec + fastembed (FTS works without it)
pip install -e ".[codex]"         # code-graph (experimental) — tree-sitter symbol/call extraction
pip install -e ".[llm]"           # OPTIONAL operator-model path for autonomous/headless runs;
                                  #   the default memory-hygiene loop uses the agent's own model — no extra
pip install -e ".[security]"      # CVE pre-install audit for the shell gate (fails open without it)
pip install -e ".[watch]"         # continuous filesystem ingest (techne watch)
```

**Python 3.14 note:** `fastembed` pulls in `onnxruntime` which has no 3.14 wheel yet. `pip install -e ".[vectors]"` succeeds on 3.14 (installs `sqlite-vec` only); semantic search returns empty gracefully until the ecosystem catches up. FTS5 search works fully on every supported Python (3.11+).

---

## Quick start — Claude Code integration

The primary way to use techne is as a memory backend for [Claude Code](https://claude.com/claude-code). One command sets up the database, registers Claude Code hooks (session start, end, post-tool-use, pre/post-compact), and registers the MCP server so the agent can query its own memory.

```bash
techne init --platform claude-code
techne status
```

From there, every Claude Code session is captured, thread-summarized at session end, and queryable via the CLI or MCP tools.

Inspect what's been captured:

```bash
techne status                              # session count, thread count, db location
techne history --days 7                    # recent sessions with thread labels
techne threads --active                    # threads with unfinished resume points
techne search "auth migration"             # full-text across threads, observations, knowledge
```

---

## Quick start — Library use

techne also exposes a stable public API for embedding in custom agent runtimes — long-running Python processes, notebooks, or alternative platforms. The hook business logic was extracted into `techne.lifecycle` for exactly this case.

```python
import techne

# Configure: defaults read from ~/.techne/config.yaml; override programmatically
config = techne.Config()
config.db_path = "/path/to/your/agent/memory.db"
config.platform = "standalone"
config.model = "claude-haiku-4-5-20251001"  # or any LiteLLM model ID

# Start a session
session_id = techne.run_session_start(
    config,
    project_path="/where/the/agent/is/working",
    agent_identity="my-custom-agent",
)

# … your agent does its work, capturing tool uses as it goes …
techne.capture_tool_use(
    config, session_id,
    tool_name="Edit",
    tool_input={"path": "src/auth.py"},
    tool_output="modified 12 lines",
)

# End the session — detects threads, links them across sessions,
# saves to DB, ages old threads, enqueues outbox sync. Returns a summary dict.
result = techne.run_session_end(config, session_id)
# {'threads': 2, 'linked': 1, 'aged_completed': 0, 'aged_stale': 0, 'queued': 2}
```

### Knowledge store

Knowledge entries are first-class — curated facts, decisions, patterns, evaluations, and procedures that survive across sessions. With supersession, you keep revision history without losing the original.

```python
# Store
entry_id = techne.knowledge.store(
    config.db_path,
    "We chose Postgres over MongoDB for the orders table because of join semantics.",
    type="decision",
    tags="storage,architecture",
    confidence="high",
)

# Update (supersession — links new entry to the old via superseded_by)
new_id = techne.knowledge.supersede(
    config.db_path, entry_id,
    "Postgres confirmed in production; we explored MongoDB and rejected for join cost.",
)

# Query
entries = techne.knowledge.list_entries(
    config.db_path,
    type="decision",
    tags="storage",
)

# Or full-text search across everything
results = techne.search.search(config.db_path, "Postgres", table="knowledge", limit=10)
```

### Identity registry

Identity is a key-value store keyed by dotted paths. `sync_from_markdown()` ingests `contacts.md`, `invariants.md`, and any YAML files in a directory:

```python
techne.identity.set_value(config.db_path, "agent.role", "code reviewer")
techne.identity.set_value(config.db_path, "team.lead.email", "alice@example.com")

# Or load from markdown:
techne.identity.sync_from_markdown(config.db_path, "/path/to/identity/")

print(techne.identity.list_entries(config.db_path, prefix="team."))
```

---

## CLI reference

```
techne init [--platform claude-code|codex|standalone] [--identity-dir DIR]
techne status                              # session and storage stats

# Threads
techne threads [--active] [--topic QUERY] [--limit N]
techne threads show SESSION_ID [--no-color] [--detector heuristic|llm]
                                           # per-pair score breakdown for one session:
                                           # which boundaries the detector saw and why
techne history [--days N] [--limit N]

# Knowledge
techne knowledge list [--type fact|decision|pattern|evaluation|procedure] [--tags T] [--limit N]
techne knowledge add CONTENT [--type T] [--tags T] [--confidence high|medium|low]

# Identity
techne identity show [--prefix PREFIX]

# Search
techne search QUERY [--table all|threads|observations|knowledge] [--limit N]

# Outbox (sync)
techne outbox status                       # pending / synced / dead counts
techne outbox flush                        # ship pending entries via configured adapter
techne outbox dead                         # entries that failed permanently

# Backfill + re-analysis
techne import [PATH] [--no-prompts] [--force]
                                           # ingest historical Claude Code JSONL transcripts as
                                           # observations + session rows. PATH defaults to
                                           # ~/.claude/projects/<slug>/ for the current cwd.
                                           # Idempotent — skips sessions that already have
                                           # observations unless --force.
techne threads redetect SESSION_ID [--sync]
                                           # re-run heuristic thread detection on a finished
                                           # session. Refreshes summary_heuristic; leaves
                                           # ended_at, summary, and summary_llm alone.
                                           # --sync re-enqueues thread_summary outbox entries.

# Maintenance
techne compact                             # age threads (active → completed → stale)
techne migrate-summaries [--dry-run]       # one-shot: reclassify pre-v4 sessions.summary
                                           # content that's heuristic-shaped into the typed
                                           # summary_heuristic column (so the manual slot
                                           # stops winning the priority read against newer
                                           # LLM/heuristic signals). Safe to re-run.

# Domain stores — db-per-domain (each in its own db file beside techne.db)
# Librarian (librarian.db) — documents: transcripts + markdown; recall the gist, fetch full on demand
techne library                             # document + card counts
techne library index-transcripts [--platform claude-code] [--dir DIR]
techne library index-markdown DIR [--source-type docs]
techne library search QUERY                # full-text over document content
techne library show DOC_ID                 # full document (the fetch-on-demand half)
techne library card CARD_ID                # gist + resolve the document cross-db
techne library cards emit|ingest|list|gist-transcripts
                                           # agent-assisted gist fill (no API key)
techne library migrate-store               # one-time: move documents techne.db → librarian.db

# Architect (architect.db) — the architectural-patterns corpus
techne architect                           # pattern / investigation counts
techne architect ingest PATTERNS_MD        # seed from an architectural-patterns.md corpus
techne architect search QUERY
techne architect show PATTERN_ID

# Codex (codex.db) — code graph (symbols + call/import/def-use edges)
techne codex index PATH                    # ingest a repo (Tree-sitter; pip install 'techne[codex]')
techne codex                               # symbol / edge counts
techne codex search QUERY
techne codex callers SYMBOL_ID
techne codex blast-radius SYMBOL_ID [--direction out|in] [--depth N]
```

---

## Domain stores (db-per-domain)

`techne.db` holds **personal memory** (sessions, threads, observations, knowledge, identity). Larger or team-scoped domains live in their **own db file** beside it, behind a `StoreAdapter` — kept clean (each db carries only its own schema). Configure non-default locations under `stores:` in `~/.techne/config.yaml`; the default is `<data_dir>/<domain>.db`. Full design: [`docs/db-per-domain.md`](docs/db-per-domain.md).

- **Librarian** (`librarian.db`) — **documents**: your session *transcripts* (the conversation prose, not just tool calls) and *markdown directories*. Recall returns a compact **gist card** that rides normal knowledge recall; the full document is fetched on demand via the card's pointer (a cross-db lookup). Long multi-topic transcripts get a deterministic **thread-index** gist; single-topic docs get an agent-written prose gist (`cards emit/ingest`, no API key). **New here, or setting up a teammate's machine? → [Using the Librarian](docs/librarian-guide.md)** (the documents↔cards model, getting your transcripts in, and an agent-instructions block).
- **Architect** (`architect.db`) — the **architectural-patterns** corpus: `investigations` (lens-aware dedup, keyed `url × surveyed_by × lens`, so two reviewer lenses on one URL is a join) + the `patterns` lifted from them. Team-scope — queried directly, not folded into personal recall. **Bootstrap your store from the shared corpus:** `techne architect ingest patterns/architectural-patterns.md` (the corpus is a generated export — `architect export` regenerates it; see [`patterns/README.md`](patterns/README.md) for the one-writer/many-readers workflow).
- **Codex** (`codex.db`) — **a code graph (experimental):** `symbols` + call/import/def-use `edges`. Reachability and blast-radius are recursive CTEs (graph queries without a graph DB; `graphqlite` is an optional future upgrade, techne#89). Real-repo ingest works — a Tree-sitter `SourceAdapter` (`techne codex index <repo>`, Python+TS, `pip install 'techne[codex]'`). **Experimental:** indexing is **manual** — the graph goes stale as code changes; re-run `codex index` to refresh (auto-refresh is the next cycle).

Personal `techne.db` deliberately stays raw-SQLite and local-only. techne is **SQLite-only by decision** (techne#135): the domains are isolated db files behind a thin path/schema registry, not a backend-portability seam.

---

## Configuration

Lives at `~/.techne/config.yaml`. Example:

```yaml
db_path: ~/.techne/techne.db
data_dir: ~/.techne
platform: claude-code
model: claude-haiku-4-5-20251001   # only used if threads.summarizer is "llm"

threads:
  detector: heuristic         # 'heuristic' (default, no LLM) or 'llm'
  summarizer: heuristic       # 'heuristic' (default, no LLM) or 'llm' (one call per thread)
  min_observations: 15        # below this the LLM detector collapses to single-thread; heuristic always runs
  link_threshold: 0.85        # BM25-derived similarity threshold for cross-session linking
  complete_after_hours: 48    # active → completed if no resume_points and stale
  stale_after_days: 30        # completed → stale
  # Extend the heuristic detector with your own MCP server families.
  # Keys are tool names; values are family strings.
  tool_families:
    mcp__claude_ai_Datadog__search_logs: "mcp:datadog"
    mcp__slack__send_message: "mcp:slack"
  # Extra regex patterns added to entity extraction (URL hostnames + file
  # basenames are extracted by default).
  entity_patterns:
    - "service:([a-z0-9-]+)"   # Datadog services
    - "C[A-Z0-9]{8,12}"        # Slack channel IDs

hooks:
  post_tool_use: true         # capture tool use as observations
  capture_bash: false         # include Bash tool calls
  capture_read: false         # include Read tool calls

sync:
  adapter: noop               # or 'filesystem'; custom adapters via entry points
```

---

## Model use — the agent's own model by default

techne's "thinking" features — reflecting a session into durable memory, deduping/merging knowledge ("sleep"), evidence-checking claims — **default to the model already running the agent.** techne renders a prompt, the agent answers it with its own model, and feeds the result back (the `*_emit` / `*_ingest` MCP tools). **No API key, no second model, no extra inference bill.** This is the recommended path and needs no extra dependency.

### Optional operator-model path (`[llm]`)

For **autonomous or headless runs** — a scheduled consolidation, a batch backfill, a background service — there's no live agent to answer the prompt, so techne can drive a model *itself* via [LiteLLM](https://github.com/BerriAI/litellm). Install the extra and set `model`:

```bash
pip install -e ".[llm]"
```

```yaml
model: claude-haiku-4-5-20251001      # any LiteLLM model id (Anthropic / OpenAI / Ollama / Bedrock / Vertex …)
llm_api_base: http://litellm:4000     # optional: route through a LiteLLM proxy for budget/observability
llm_api_key: sk-your-virtual-key      # optional; omit to use the provider's env var (e.g. ANTHROPIC_API_KEY)
```

Without the `[llm]` extra, the operator path degrades gracefully — the resident (agent's-own-model) path covers everything an interactive session needs. Switch providers by changing the one `model` line.

---

## Embedding techne in a custom agent runtime

The `techne.lifecycle` module is the high-level library entry point for runtimes that aren't Claude Code — your own agent process, a notebook, a server-side service. Two patterns:

### Auto-generated session IDs (default)

```python
import techne
from techne.lifecycle import run_session_start, run_session_end

config = techne.Config.load()
session_id = run_session_start(config, project_path="/my/project")
# ... agent does work, observations get added ...
run_session_end(config, session_id)
```

### Caller-supplied session IDs

If your runtime has its own conversation / task / dispatch identifiers (an A2A Task ID, a Slack thread ID, an issue number), bind them 1:1 onto techne sessions:

```python
from techne.lifecycle import init_session_for_external_id, run_session_end

# Idempotent: safe to call multiple times for the same id (e.g. on agent restart).
session_id = init_session_for_external_id(
    config,
    external_id="a2a-task-abc-123",
    project_path="/my/project",
    agent_identity="rook-primus",
)
# ... agent does work ...
run_session_end(config, session_id)
```

Caller is responsible for namespacing external IDs across runtimes that share a techne DB.

---

## Custom sync adapters

The outbox accepts arbitrary backends via entry points. To ship knowledge to your own team-memory service, write a class implementing the `SyncAdapter` Protocol and register it:

```toml
# in your package's pyproject.toml
[project.entry-points."techne.sync_adapters"]
mybackend = "mypackage.mybackend:MyBackendAdapter"
```

Then in techne config:

```yaml
sync:
  adapter: mybackend
  url: https://my-team-memory.example.com/api
```

`techne outbox flush` will route through your adapter. The `FilesystemAdapter` in `techne/sync/filesystem.py` is the simplest reference implementation.

---

## Versioning

Semantic versioning. Tagged releases on the repo:

- **v0.7.0** — db-per-domain + staff review:
  - **One db file per domain, behind a `StoreAdapter`.** `techne.db` stays personal memory; documents → `librarian.db` (#99), patterns → `architect.db` (#87 P1), code graph → `codex.db` (#89 P1). Domain tables left core's `SCHEMA_SQL` — a memory-only db no longer gets empty unused tables.
  - **Store registry** (#98) — `load_store(config, domain)`, a `stores:` config block, `dsn:` overrides honored. Adding a domain is a module + `StoreAdapter`, **zero core edits** (umbrella #88 delivered).
  - **Option D document cards** — a `document_card` (gist + pointer) rides personal recall in `knowledge`; full content fetched cross-db by key (no ATTACH → backend-portable). Transcript + markdown-dir sources via a shared `index_document` unit; a `techne library` command group + `library doctor` cross-db health check.
  - **Staff-review batch (#100–#104):** migration verifies by (id+content) identity — no silent document drop (#100); `github_issues` source writes to `librarian.db` and stops core-polluting its db (#101); domains route through the registry so `config.stores` `dsn:` actually takes effect, and `resolve_card` exercises the `StoreAdapter.get` Protocol seam (#102, keystone); empty/whitespace query guard across domain searches (#103); LIKE-metachar escaping + reversible pre-drop backup (#104).
- **v0.6.2** — reliability + recall pass:
  - **FTS5 hardening** — query sanitizer always-quotes + strips backslashes (#74, kills `fts5: syntax error` on bare `AND`/`OR`/`NOT`/hyphens); Porter stemming (`tokenize='porter unicode61'`) + a v9 rebuild migration (#77).
  - **SessionStart health banner** + shared `core/health.py::collect_health`, reused by `techne_status` (#76); `version` + `package_path` added to `techne_status` so callers can tell if the server is current (#71).
  - **Migration hardening** (#78) — flock-serialized, WAL-safe pre-migration backup to `backups/`, stderr announce (lands ahead of #77 so a first v9 migration is backup-protected).
  - **Outbox gate + purge** (#79) — `enqueue()` gated on a live consumer (`sync.adapter != "noop"`); `outbox.purge_pending` + `techne outbox purge` for regenerable backlog.
  - Thread-lifecycle fix (#72), FTS OR-fallback for empty implicit-AND recall (#70), AWS redaction incl. JSON/SDK form (#69), and the eval-discipline harness (#68 — 2-arm recall eval + enforced kill-gate via `pytest -m eval`).
- **v0.6.1** — correctness + hygiene pass (Fable review):
  - **FTS crash fix** — double-quoted search terms no longer raise `OperationalError`; embedded `"` are escaped before wrapping.
  - **Search provenance fix** — `search()` no longer stamps `source = "fts"/"vec"` over the original knowledge provenance; match source is returned in a separate `match_source` field.
  - **Supersede guard** — re-superseding an already-superseded entry raises a clear error instead of silently orphaning the revision chain.
  - **Atomic redetect / session-end** — `redetect_threads` and `run_session_end` wrapped in umbrella transactions; a crash mid-operation leaves original history intact. `INSERT OR REPLACE` in `save_threads` replaced with `INSERT … ON CONFLICT UPDATE` that excludes `created_at`.
  - **Public API contract tests** — CI job asserts that `search.search`, `init_db`, `knowledge.store`, and `lifecycle` module exist with the right signatures. A breaking rename fails CI.
  - **`VALID_TYPES` exported** from `techne.__init__` so consumers (`limenal.handlers.memory`) import the canonical list instead of duplicating it.
  - **`lifecycle` module in `__all__`** — callers can now `import techne; techne.lifecycle.run_session_end(...)`.
  - **`schema_version` race fix** — `UNIQUE` constraint on `version` column + `INSERT OR IGNORE` prevents double-insert on concurrent `connect()` calls.
  - **`llm_api_key` file permission** — `config.save()` now `chmod 0600`s the YAML after writing when `llm_api_key` is present, matching the filesystem sync adapter.
- **v0.6.0** — embedding techne in custom agent runtimes:
  - **`llm_api_base` + `llm_api_key` config fields** for routing LLM calls through a LiteLLM proxy server (centralized budget enforcement, observability, model routing). Unset → falls back to LiteLLM's standard env-var-based provider routing.
  - **Caller-supplied session IDs** via `start_session(session_id=...)` and `run_session_start(session_id=...)` — runtimes with their own conversation / task IDs (e.g., A2A Tasks, Slack threads) can map them 1:1 onto techne sessions.
  - **`techne.lifecycle.init_session_for_external_id(config, external_id, ...)`** — idempotent helper for the caller-supplied-id pattern. Safe to call repeatedly (e.g., on agent restart mid-dispatch).
  - **`cabal` platform** added to the `Platform` enum.
  - **`run_session_start` now passes `config.platform` through to `start_session`** (was always defaulting to `claude-code`; harmless but wrong for non-Claude-Code runtimes).
- **v0.5.1** — review pass: 2 blockers (`session_end` exit code, `NoopAdapter` synced=0), 5 high-priority bugs (MCP version + ping, `db_stats` outbox query, SQL allowlist asserts, `busy_timeout` PRAGMA, `techne_store` content validation), 8 medium-priority cleanups (Filesystem leading-consecutive contract + chmod 0600, outbox failed-without-error handling, LIKE escape, Config sync round-trip, pre_compact utf-8 + stderr log, FTS sanitizer hyphen). +22 net new tests.
- **v0.5.0** — first public release. Major additions over v0.4.x:
  - **JSONL backfill** (`techne import [PATH] [--no-prompts] [--force]`). Historical Claude Code transcripts at `~/.claude/projects/<slug>/` can be replayed into the database. Captures tool calls and user prompts; idempotent by default with `--force` for partial-overlap sessions.
  - **Dual-column session summary** with provenance routing. `sessions.summary` (manual override), `sessions.summary_llm` (LLM-derived, e.g. Claude Code's `ai-title`), `sessions.summary_heuristic` (auto-detector output). `effective_summary()` returns the highest-priority non-empty slot. Schema bumped to **v4**.
  - **User-prompt-anchored thread detection**. `UserPrompt` observations contribute a `USER_PROMPT_SCORE` signal (default 0.3) that amplifies other boundary signals without crossing the threshold alone. `family_change` is suppressed when crossing into a `UserPrompt` to avoid double-counting.
  - **`techne threads redetect <session>`** to re-run detection on a finished session without changing `ended_at` or touching manual/LLM summary slots. Default `--no-sync` so iterative tuning doesn't flood the outbox.
  - **`techne migrate-summaries`** one-shot to reclassify legacy `summary` content (heuristic-shaped) into the typed column.
  - **Platform code consolidated**: `techne/platforms/claude_code.py` → `techne/platforms/claude_code/` (package). Public import paths unchanged via `__init__.py` re-export.
  - **Cross-platform**: slug encoding uses `PurePosixPath` and skips `.resolve()` so macOS APFS firmlinks don't corrupt project slugs; JSONL reads pin `encoding="utf-8"`.
  - **Read-time provenance helper**: `effective_summary()` resolves the manual > LLM > heuristic priority chain; `end_session` and `set_session_summary` now require an explicit `source` (raises `ValueError` on unknown).
- **v0.4.1** — fix: hooks were registered in the legacy flat shape `{type, command}`; Claude Code's current validator (`/doctor`) requires the wrapped shape `{matcher, hooks: [{type, command}]}`. Symptom: every event silently skipped, sessions captured zero observations. `init` now writes the wrapped shape and replaces legacy entries on upgrade. Also: `_parse_timestamp` normalizes tz-aware ISO strings (e.g. `...Z`) to UTC-naive so they don't crash when compared against `datetime.now()` in resume-point rendering.
- **v0.4.0** — heuristic thread detection: structural signals (temporal gap, tool family, repo, entity overlap) replace per-session LLM calls for detecting topic boundaries. `ANTHROPIC_API_KEY` is no longer required for the default install. New `techne threads show <session>` audit view exposes the per-pair score breakdown. Config: `threads.detector` (`heuristic` | `llm`, default heuristic), `threads.summarizer` (`heuristic` | `llm`, default heuristic), `threads.tool_families` (user override for MCP tool naming), `threads.entity_patterns` (user-supplied regex extractors). Schema v3 adds `thread_boundaries` table.
- **v0.3.1** — fix: `init` writes hook + MCP commands as `sys.executable` (was bare `python`, broke on hosts without a `python` alias); `load_adapter` drops `None` kwargs so `NoopAdapter` and other zero-arg adapters work with default config
- **v0.3.0** — argument-order standardization on `should_capture` and `add_observation` (breaking)
- **v0.2.2** — bug fixes: `capture_read` and `post_tool_use=False` now actually work; `FilesystemAdapter.pull()` symmetric with push
- **v0.2.1** — polish: hook test coverage, contacts.md parser hardening, public API surface in `__init__.py`
- **v0.2.0** — LiteLLM as default LLM router, lazy schema init, outbox status refactor, `techne.lifecycle` module for library use
- **v0.1.0** — initial release

---

## License

Apache-2.0. See `LICENSE` file.
