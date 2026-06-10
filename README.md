# techne

**Agentic memory and knowledge system.**

A single-file SQLite-backed memory store that gives AI agents persistent memory across sessions. Built around the idea that agents need three things to be useful over time: a structured record of *what they did* (sessions and threads), a curated record of *what they learned* (knowledge), and a stable record of *who they are and who they work with* (identity).

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
pip install -e .                  # core
pip install -e ".[vectors]"       # adds sqlite-vec + fastembed for semantic search
pip install -e ".[anthropic]"     # if you want to pin Anthropic SDK directly (LiteLLM handles routing by default)
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
```

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

## LLM routing

techne calls LLMs via [LiteLLM](https://github.com/BerriAI/litellm), which provides a unified interface to 140+ providers. The `model` field accepts any LiteLLM model identifier:

| Provider | Example model ID | Env var needed |
|---|---|---|
| Anthropic | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai/gpt-4o-mini` | `OPENAI_API_KEY` |
| Ollama (local) | `ollama/llama3` | — (runs locally) |
| Bedrock | `bedrock/anthropic.claude-3-haiku` | AWS creds |
| Vertex | `vertex_ai/claude-haiku@20251001` | GCP creds |

Switch providers by changing one config line.

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
