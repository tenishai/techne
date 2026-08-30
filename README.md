# techne

**Persistent memory for AI agents — local-first, one file, and it runs on the model already in the room.**

*Technē* (τέχνη) is the ancient Greek word for **craft**: practical, applied know-how — the knowledge you build by *doing*, as opposed to *epistēmē*, abstract theoretical knowledge. That's the idea here. techne is the craft knowledge an agent accumulates across sessions — what it did, what it learned, who it works with — so it gets better at the work over time instead of starting cold every session.

Concretely: a SQLite-backed memory store whose core is a **single local file** (`techne.db`) — yours, offline, inspectable with `sqlite3`. It gives an agent three things to be useful over time: a structured record of *what it did* (sessions + auto-detected threads), a curated record of *what it learned* (knowledge), and a stable record of *who it is and who it works with* (identity). Its self-maintenance — distilling sessions into durable memory, deduping, evidence-checking claims — runs on **the agent's own model** (no API key, no extra inference bill). Larger or team-scoped domains — documents, patterns, code — each get their own SQLite file behind a `StoreAdapter` (db-per-domain).

> **New here? Start with the brochure:** [**Welcome to your new memory**](docs/welcome.md) — the friendly first-timer tour (the memory loop, the good bits you'd never find on your own, and the five things worth turning on). This README is the reference manual; read the welcome guide first.
>
> **Tour the pieces:** [`docs/modules.md`](docs/modules.md) — a short sales-pitch for each module (what it does, why it's different, how to enable it).

---

## The four functions

techne has four knowledge domains, and you address each one **by name**:

| | The question it answers | Ask it |
|---|---|---|
| **archivist** | *What does the team know?* | `archivist_search` · `techne archivist query` |
| **librarian** | *What is written down?* | `librarian_search` · `techne librarian search` |
| **architect** | *What is worth borrowing?* | `architect_search` · `techne architect search` |
| **codex** | *What does the code look like?* | `codex_search` · `techne codex search` |

The codex needs `techne codex index <repo>` run once before it can answer.

Every answer says **what it resolved to** — a `binding` (`absent` / `local` / `store` — not every domain can reach every value)
and the concrete `target` path — so the call stays the same while the infrastructure
underneath it changes. `techne bindings` reports all four at once.

The point of learning four names rather than four file paths is that the names
outlive this tool: an operator learns *archivist, librarian, architect, codex* here
and keeps them when the work grows into liminal and cabal, where a local librarian may
become a persona and later a deployed service.

**→ [The four functions](docs/four-functions.md)** — the tutorial: ask each one, read
the `binding`, and watch the same call answer from a different store. Start there.

`techne_recall` remains the default for *"what do we know about X"* — it escalates
across all four. Address a domain by name when you already know which function you
want.

---

```bash
git clone https://github.com/tenishai/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
techne init
```

> **Just want to _run_ techne, not develop it?** Install a pinned release straight
> from git — no clone, non-editable, and the Claude Code hook launcher self-heals
> across interpreter changes:
>
> ```bash
> pipx install "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.16.1"
> techne init
> ```
>
> `[recommended]` pulls semantic recall + the CVE pre-install audit (`vectors` + `security`).
> Drop it for a **core-only** install, or swap in any other set — see the
> [extras matrix](#extras--what-to-install) for the full possibility space.
>
> Upgrade later with `pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@<newer-tag>"`
> (a plain `pipx install` on an already-installed techne refuses without `--force`). **After an
> upgrade, run `techne skills install`** — an upgrade does not re-run `techne init`, so the
> bundled procedures below are not refreshed on their own.
> The `git clone` + `pip install -e .` flow above is for **developing** techne (live local edits).

> **Bundled procedures.** `techne init` also installs two operator procedures as slash
> commands: **`/self-care`** (hygiene pass — sweep the outbox, prune backups and snapshots,
> drain the rails, reclaim disk) and **`/techne-promote`** (turn accumulated observations
> into reviewed knowledge without an API key). They ship *with* techne rather than from a
> skills marketplace because they drive its CLI and know the store layout of the version
> they came with — a procedure written for 0.12 can be wrong for 0.15. `techne skills status`
> shows what you have; a copy you have edited is reported, never silently overwritten.
> Install without them via `techne init --no-skills`.

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
| **Domain stores** | Where [the four functions](#the-four-functions) keep their data — the *layout*, not the mental model. `techne.db` is personal memory and answers as the **archivist** when unbound; larger or team-scoped domains get their own db file behind a `StoreAdapter` (db-per-domain) — **librarian** (`librarian.db`, documents), **architect** (`architect.db`, patterns), **codex** (code graph, *experimental* — **one db per repo** at `codex/<slug>-<hash>.db`). Each db carries only its own schema. Address the domain by name; the db path is what it happens to resolve to today, and the answer's `target` tells you. See [Domain stores](#domain-stores-db-per-domain) and `docs/db-per-domain.md`. |

The schema and triggers all live in `techne/core/database.py`. Schema migrations are tracked in a `schema_version` table; `connect()` is lazy-init so library callers don't need to remember to call `init_db()` first.

---

## Install

techne is in alpha — installed from source. Public PyPI distribution is on the roadmap.

**Requires:** Python 3.11+, git.

Use a virtual environment — required on Debian/Ubuntu (PEP 668), recommended everywhere else:

```bash
git clone https://github.com/tenishai/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # core only
techne init
```

### Extras — what to install

Everything past core is an opt-in extra. Nothing here is required — core (capture, FTS
recall, threads, knowledge, identity) runs standalone, and every extra **degrades
gracefully** when absent (vector search returns empty, the CVE gate fails open, `watch`/
`codex` subcommands are unavailable). Pick deliberately — this is the complete option space.

| Install                 | Enables                                                        | Pulls                                              | Reach for it when |
|-------------------------|---------------------------------------------------------------|----------------------------------------------------|-------------------|
| `.`                     | Core: capture, FTS5 recall, threads, knowledge, identity      | `click`, `pyyaml`, `structlog` (lightweight)       | Always — the baseline. |
| `.[vectors]`            | Semantic (vector) recall alongside FTS                        | `sqlite-vec`, `numpy`, + `model2vec` (py≥3.14) / `fastembed` (py<3.14) | You want similarity search, not just keyword match. |
| `.[security]`           | CVE pre-install audit for the shell gate (`shell_guard`)      | `pip-audit`                                         | Running the anti-chaining shell gate and you want the audit sharpened (fails open without). |
| `.[watch]`              | `techne watch <dir>` — continuous filesystem-change ingest    | `watchdog`                                          | You want a background daemon ingesting file changes (vs per-session hook capture). |
| `.[llm]`                | Operator-model path for autonomous/headless reflect/consolidate | `litellm`                                        | Running headless/autonomous loops that can't use the agent's own model. |
| `.[codex]`              | `techne codex index <repo>` — Tree-sitter code graph (experimental) | `tree-sitter`, `tree-sitter-python`, `tree-sitter-typescript`, `watchdog` | Indexing a repo's symbols/edges for reachability/blast-radius queries. |
| **`.[recommended]`**    | **`vectors` + `security`** as one bundle                      | union of those two                                 | **The blessed default** — semantic recall + the safety audit, skipping situational/experimental deps. |
| `.[dev]`                | Test/lint toolchain                                            | `pytest`, `pytest-cov`, `ruff`, `litellm`          | Developing techne itself. |

Combine freely — extras compose: `pip install -e ".[vectors,watch,llm]"`. For the
non-editable pinned install, use the direct-reference form:
`pipx install "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.16.1"`.

**Python 3.14 note:** `fastembed` pulls in `onnxruntime` which has no 3.14 wheel yet. `techne` auto-selects `model2vec` (a numpy-only static embedding backend, no onnxruntime/torch) as the vector backend on 3.14+ instead. FTS5 search works fully on every supported Python (3.11+); vector search works via whichever backend is available.

**Upgrading an existing `[vectors]` DB to Python 3.14:** switching interpreters swaps the active embedding backend (fastembed/MiniLM-384 → model2vec/potion-256) — the two are not comparable vector spaces, so existing embeddings are dropped on next backfill and semantic search silently degrades to FTS-only until you re-embed. Run `techne vectors status` to check, then `techne vectors backfill` to restore the vector channel.

**The backend is your choice, not just the interpreter's.** Auto-selection is a default, not a constraint — set it explicitly and it wins on any Python:

```yaml
search:
  embedding_backend: model2vec   # or: fastembed   ("" = auto-select, the default)
  embedding_model: ""            # optional; defaults per backend
```

`model2vec` works on **every supported Python (3.11+)**, not only 3.14 — the `<3.14` fastembed default exists because `onnxruntime` had no 3.14 wheel, which is a fact about *onnxruntime*, not a judgment that fastembed is better below it. Verified 2026-08-17: `model2vec` 0.9.0 installs and runs on 3.13.

**It costs disk, and the difference is large.** Measured on a clean venv with the blessed command:

| backend | install | why |
|---|---|---|
| `fastembed` (py<3.14 default) | **290 MB** | `onnxruntime` 74M + `sympy` 72M + `numpy` 37M + `PIL` 15M + `tokenizers` 8.8M |
| `model2vec` (py3.14 default) | **85 MB** | numpy-only static embeddings; no onnxruntime, no sympy, no PIL |

The fastembed row is re-measured at **v0.15.0 on py3.13** (`techne[recommended] @ v0.15.0`, 290 MB — it was 289 MB at v0.14.0). The model2vec row is **still the v0.14.0 py3.14 measurement and has not been reproduced**: a py3.13 venv with `[security]` + `sqlite-vec` + `model2vec` came to 127 MB, which is a DIFFERENT construction (different interpreter, different extras) and so is not a like-for-like replacement for 85 MB. Two numbers measured differently do not belong in one column, so the older one stands until someone re-runs it on py3.14.

The techne wheel itself is **676 KB**; effectively all of the difference is the embedding backend's dependency tree.

**Choosing `model2vec` on py<3.14 is a real decision with a real cost:** it is 256-d against fastembed's 384-d, so the same "not comparable vector spaces" warning above applies — an existing store must be re-embedded (`techne vectors backfill`) or semantic search silently degrades to FTS-only. Set it before you build up a vector channel, or budget for the re-embed. On a fresh install it is free.

Note the smaller model was chosen on merit rather than only availability: `potion-base-8M` (256-d) measured *better* than `potion-retrieval-32M` (512-d) for natural-language paraphrase queries, which is how recall is actually used.

`embedding_backend: ""` means **auto-select, not disabled** — so read the live backend from `techne vectors status` rather than inferring it from an empty config value.

---

## Quick start — Claude Code integration

The primary way to use techne is as a memory backend for [Claude Code](https://claude.com/claude-code). One command sets up the database, registers Claude Code hooks (session start, end, post-tool-use, pre/post-compact), and registers the MCP server so the agent can query its own memory.

```bash
techne init --platform claude-code
techne status
```

From there, every Claude Code session is captured, thread-summarized at session end, and queryable via the CLI or MCP tools.

`techne status` also **self-checks hook registration** — if a hook is enabled in config but not actually wired into Claude Code's `settings.json`, it prints a `⚠ Hook registration drift` warning; `techne init` reconciles it. (Claude Code loads hooks at session *start*, so a newly-registered hook — e.g. opt-in [grep-assist](docs/welcome.md) — fires from the *next* session, not the current one.)

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

## MCP tools

The complete agent-facing surface — 33 tools, grouped by purpose — is documented in
**[`docs/mcp-tools.md`](docs/mcp-tools.md)**, generated from the live registry and
pinned by a test so a new tool cannot ship undocumented.

## CLI reference

```
techne init [--platform claude-code|codex|standalone] [--identity-dir DIR]
techne status                              # session and storage stats
techne capabilities [--json]               # what techne can do on THIS install and what is
                                           # switched off, grounded in your own store — the
                                           # operator twin of the techne_capabilities MCP tool
techne serve                               # run as a stdio MCP server (non-Claude-Code clients)

# Threads
techne threads [--active] [--topic QUERY] [--limit N]
techne threads show SESSION_ID [--no-color] [--detector auto|live|stored]
                                           # per-pair score breakdown for one session:
                                           # which boundaries the detector saw and why
techne threads redetect SESSION_ID [--sync]
                                           # re-run heuristic thread detection on a finished
                                           # session. Refreshes summary_heuristic; leaves
                                           # ended_at, summary, and summary_llm alone.
                                           # --sync re-enqueues thread_summary outbox entries.
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
techne outbox flush-sinks                  # flush pending rows to registered sinks
techne outbox prune-orphans                # mark pending rows for unregistered targets as dead
techne outbox purge [--target T] [--yes]   # delete drainable-but-unconfigured pending rows

# Sinks
techne sinks list                          # all registered sinks: name, description, schema fields
techne sinks describe NAME                 # full schema field list for a named sink

# Backfill + re-analysis
techne import [PATH] [--no-prompts] [--force]
                                           # ingest historical Claude Code JSONL transcripts as
                                           # observations + session rows. PATH defaults to
                                           # ~/.claude/projects/<slug>/ for the current cwd.
                                           # Idempotent — skips sessions that already have
                                           # observations unless --force.

# Capture + integration helpers (for non-Claude-Code runtimes)
techne observe --session-id ID [--content TEXT] [--tool-name NAME] [--platform P]
                                           # record an observation (platform-agnostic capture;
                                           # CLI sibling of the techne_observe MCP tool)
techne session-end SESSION_ID [--summary TEXT]
                                           # run end-of-session processing: detect threads,
                                           # summarize, age. Idempotent. Used by opencode plugin.
techne context [--project-path PATH] [--limit N]
                                           # emit a compact memory block for injecting into a
                                           # client's system prompt (e.g. opencode transform)

# Instruments — measuring whether the read path earns its tokens
techne audit [--since-days N] [--json]     # utilization audit (techne#329): which injection
                                           # sources fire, at what token cost, and whether the
                                           # injected item was used. Attribution is a PROXY and
                                           # biased in both directions — read the caveat it prints
techne profile [...]                       # profile the hot paths: import cost, thread detection
techne prompts [...]                       # view/edit the maintenance prompts (reflect,
                                           # consolidate, dream) — operator-owned, techne#219
techne deposit [...]                       # deposit a raw observation into a FOREIGN techne store
                                           # (cross-store handoff; not the normal capture path)

# Memory hygiene (reflect · evidence · claims — no API key; uses your own model)
#
# REFLECTION DEBT IS DURABLY QUEUED as of 0.13.0. session_end used to COUNT the debt and
# enqueue nothing, so reflection survived only the polite exit path and every crash or
# timeout dropped it silently. One reflection_queue row per emittable thread now records
# what is owed; the SessionStart banner reads that ledger instead of re-deriving it from
# recency, so debt that ages out of the recency window is no longer permanently invisible.
techne reflect                             # show pending / promoted / discarded candidate counts
techne reflect review [--status pending|promoted|discarded|all] [--limit N]
techne reflect emit [--session ID] [--limit N] [-o FILE]
                                           # emit per-thread reflection prompts for agent to answer
techne reflect ingest RESPONSES_FILE       # feed agent-answered prompts back; writes candidates
techne reflect promote CANDIDATE_ID [--by WHO] [--reason TEXT]
techne reflect discard CANDIDATE_ID [--by WHO] [--reason TEXT]
techne reflect stats                       # candidate counts as JSON

techne evidence SESSION_ID [--since ISO_TS]
                                           # evidence gate (#105): show what an agent actually did
                                           # (artifacts, commits, actions, test results) — verify
                                           # claims against the observation ledger before asserting

# CC memory reconciliation (techne#107)
techne memory doctor [--since N] [--claims] [--json]
                                           # diff CC's curated memory against the deed-log.
                                           # --claims uses the resident model to judge each
                                           # discrete fact (no API key required).
techne memory sync                         # drain CC curated memory files into techne.db

# Maintenance
techne compact                             # age threads (active → completed → stale)
techne migrate-summaries [--dry-run]       # one-shot: reclassify pre-v4 sessions.summary
                                           # content that's heuristic-shaped into the typed
                                           # summary_heuristic column (so the manual slot
                                           # stops winning the priority read against newer
                                           # LLM/heuristic signals). Safe to re-run.

# Operations (cross-DB integrity)
techne fsck                                # run every cross-DB consistency check in one pass;
                                           # exits non-zero on any inconsistency (CI-friendly)
techne cards repair [--apply]              # retype document_cards that never had a doc pointer
                                           # (they were mistyped knowledge, not cards). Dry-run by
                                           # default. REFUSES to touch a card whose pointer exists
                                           # but doesn't resolve — that needs a human
techne reconcile                           # force the schema drift-reconcile passes on techne.db;
                                           # use when you suspect schema drift or after an
                                           # additive SCHEMA_SQL change at unchanged SCHEMA_VERSION
techne backup [--out DIR] [--keep N]       # snapshot ALL present domain DBs together into one
                                           # timestamped set (cross-DB invariants require
                                           # backup/restore of all DBs as a unit). DIR is the
                                           # backups ROOT, and a set is created INSIDE it —
                                           # it is NOT the set, so it is not what you pass to
                                           # `restore` (changed in 0.16.0). --keep N prunes to
                                           # the newest N sets; exit 3 = backup OK, prune failed
techne restore BACKUP_DIR [--yes] [--no-pre-backup]
                                           # restore all domain DBs from ONE backup set —
                                           # `<DIR>/backup-<stamp>`, not `<DIR>`; validates
                                           # the full set before touching any live file

# Diagnostics
techne doctor                              # subsystem liveness — is techne actually WORKING,
                                           # not just installed. Each check reports LIVE /
                                           # DORMANT / STALE / UNOBSERVABLE with a remedy you
                                           # can act on. A check that cannot say what to do
                                           # about a failure is a bug, not a warning.
techne watch                               # exits 78 (EX_CONFIG) on a startup precondition
                                           # restarting cannot fix — e.g. a missing [watch]
                                           # extra. The shipped systemd unit sets
                                           # RestartPreventExitStatus=78, so it stops on the
                                           # FIRST failure instead of respawning forever.
                                           # BOTH halves are required; either alone loops.
techne probe [--runtime claude-code|opencode] [--mode off-test|pull|differential]
                                           # differential-canary memory-attribution probe (#94):
                                           # seeds a canary, spawns a fresh session, checks
                                           # whether techne carried the codeword into context

# Domain stores — db-per-domain (each in its own db file beside techne.db)
# Librarian (librarian.db) — documents: transcripts + markdown; recall the gist, fetch full on demand
techne librarian                           # document + card counts
techne librarian index-transcripts [--platform claude-code] [--dir DIR]
techne librarian index-markdown DIR [--source-type docs]
techne librarian search QUERY              # full-text over document content
techne librarian show DOC_ID               # full document (the fetch-on-demand half)
techne librarian card CARD_ID              # gist + resolve the document cross-db
techne librarian cards emit|ingest|list|gist-transcripts
                                           # agent-assisted gist fill (no API key)
techne librarian doctor                    # cross-db health check: dangling or stale cards
techne librarian migrate-store             # one-time: move documents techne.db → librarian.db

# Architect (architect.db) — the architectural-patterns corpus
techne architect                           # pattern / investigation counts
techne architect add --name N --source URL [--surveyed YYYY-MM-DD] [--body T | --body-file F]
                                           # add ONE pattern from its fields — the feeder
                                           # that ships. No markdown to hand-write, so no
                                           # format to get wrong; body also reads stdin.
                                           # The richer procedures (/investigate-url,
                                           # /survey-field) live in tenishai/skills
techne architect ingest PATTERNS_MD [--verdict skip|skip-with-flag|patterns-only|full-eval]
                                           # seed from an architectural-patterns.md corpus.
                                           # --verdict records YOUR judgment of the
                                           # investigation; omitted, it records
                                           # 'patterns-only' (the modal outcome).
techne architect reverdict URL --verdict X # correct one investigation's verdict without
                                           # re-ingesting. Leaves surveyed_at alone:
                                           # correcting a judgment is not a re-survey.
techne architect sweep-legacy-verdicts --before DATE [--apply]
                                           # retire pre-#428 auto-assigned verdicts to
                                           # 'unknown'. Dry-run by default, date-scoped.
techne architect search QUERY
techne architect show PATTERN_ID
techne architect export [-o FILE]          # regenerate shareable architectural-patterns.md
techne architect related PATTERN_ID        # patterns linked via [[wikilink]] edges
techne architect applies TARGET            # patterns tagged as applying to TARGET
techne architect backfill                  # re-derive the relation/applies graph (idempotent)
techne architect retire-legacy [--dry-run] # delete superseded type='pattern' rows from techne.db

# Codex — code graph (symbols + call/import/def-use edges), ONE db PER repo (db-per-repo, #209)
techne codex index PATH                    # ingest a repo (Tree-sitter; pip install 'techne[codex]')
                                           #   → writes codex/<slug>-<hash>.db for THAT repo
techne codex index-all                     # re-index every repo that already has a codex db
techne codex stats [--repo PATH] [--all]   # symbol / edge counts — one repo, or every indexed repo
techne codex search QUERY [--repo PATH]    # symbols in the repo for cwd, or --repo
techne codex callers SYMBOL_ID
techne codex blast-radius SYMBOL_ID [--direction out|in] [--depth N]
techne codex watch REPO [--language LIST] [--debounce-ms N]
                                           # watch a repo for file changes; re-indexes
                                           # incrementally on each change (Ctrl-C to stop)
```

---

## Domain stores (db-per-domain)

`techne.db` holds **personal memory** (sessions, threads, observations, knowledge, identity). Larger or team-scoped domains live in their **own db file** beside it, behind a `StoreAdapter` — kept clean (each db carries only its own schema). Configure non-default locations under `stores:` in `~/.techne/config.yaml`; the default is `<data_dir>/<domain>.db`. Full design: [`docs/db-per-domain.md`](docs/db-per-domain.md).

- **Librarian** (`librarian.db`) — **documents**: your session *transcripts* (the conversation prose, not just tool calls) and *markdown directories*. Recall returns a compact **gist card** that rides normal knowledge recall; the full document is fetched on demand via the card's pointer (a cross-db lookup). Long multi-topic transcripts get a deterministic **thread-index** gist; single-topic docs get an agent-written prose gist (`cards emit/ingest`, no API key). **New here, or setting up a teammate's machine? → [Using the Librarian](docs/librarian-guide.md)** (the documents↔cards model, getting your transcripts in, and an agent-instructions block).
- **Architect** (`architect.db`) — the **architectural-patterns** corpus: `investigations` (lens-aware dedup, keyed `url × surveyed_by × lens`, so two reviewer lenses on one URL is a join) + the `patterns` lifted from them. Team-scope — queried directly, not folded into personal recall. **Bootstrap your store from the shared corpus:** `techne architect ingest patterns/architectural-patterns.md` (the corpus is a generated export — `architect export` regenerates it; see [`patterns/README.md`](patterns/README.md) for the one-writer/many-readers workflow).
- **Codex** — **a code graph (experimental):** `symbols` + call/import/def-use `edges`. Reachability and blast-radius are recursive CTEs (graph queries without a graph DB; `graphqlite` is an optional future upgrade, techne#89). Real-repo ingest works — a Tree-sitter `SourceAdapter` (`techne codex index <repo>`, Python+TS, `pip install 'techne[codex]'`). **One db per repo (db-per-repo, techne#209):** each indexed repo lives at `<data_dir>/codex/<slug>-<hash8>.db`, derived deterministically from the repo's absolute root — indexing a second repo can never collide with or bleed into the first. A pre-#209 single `codex.db` migrates to its per-repo home automatically the first time any consumer resolves a codex db. **Experimental:** indexing is **manual** — the graph goes stale as code changes; re-run `codex index` (or `codex index-all`) to refresh (auto-refresh is the next cycle).

Personal `techne.db` deliberately stays raw-SQLite and local-only. techne is **SQLite-only by decision** (techne#135): the domains are isolated db files behind a thin path/schema registry, not a backend-portability seam.

---

## Configuration

Lives at `~/.techne/config.yaml`. Example:

> **Operators: there is a second, higher-authority layer for the keys that decide where
> memory can GO.** See [Operator-owned config](#operator-owned-config-egress-selecting-keys)
> below before deploying techne somewhere the agent should not be able to redirect its own
> egress. Installs that do not deploy one are unaffected — `config.yaml` keeps full
> authority.

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
  codex_grep_assist: false    # opt-in (#206): a PreToolUse hook that augments the agent's
                              #   own Grep with matching Codex symbols (needs an indexed repo).
                              #   Re-run 'techne init' after flipping; restart the session to load it.
  codex_grep_assist_limit: 8  # max symbols injected per Grep
  codex_edit_rail: false      # opt-in (#335): a PostToolUse hook that, after an Edit, injects the
                              #   edited symbol's blast-radius (its dependents — "what breaks if I
                              #   change this"), but only when it has enough dependents to matter.
                              #   Re-run 'techne init' after flipping; restart the session to load it.
  codex_edit_rail_min_deps: 3 # min transitive dependents before the edit rail fires (skip leaf edits)
  codex_edit_rail_limit: 10   # max dependents injected per edit (closest-first by depth)
  per_turn_recall: false      # opt-in (#109/#277): a UserPromptSubmit hook injects the durable
                              #   facts most relevant to each prompt — the DYNAMIC per-turn
                              #   complement to the STABLE SessionStart block. Re-run 'techne init'
                              #   after flipping; loads from the next session.
  per_turn_recall_limit: 5    # max knowledge rows injected per turn
  per_turn_recall_chars: 200  # max characters per injected row (#826). The cut lands on a
                              #   sentence or word boundary and a shortened row ends in '…',
                              #   so a truncated fact is distinguishable from a whole one.
                              #   Raising this raises per-prompt token cost on EVERY turn.
                              #   CEILING: search() already truncates knowledge rows to 500
                              #   (unmarked), so this is CLAMPED to 499. A value at or above
                              #   500 is not a no-op — unclamped it would suppress the '…'
                              #   and report a severed row as whole (#842).
  timeout_s: 5.0              # wall-clock budget for a hook (grep-assist / per-turn recall degrade to no-op if slow)

sync:
  adapter: noop               # or 'filesystem'; custom adapters via entry points
```

---

### Operator-owned config (egress-selecting keys)

The keys that choose **where memory can go** — `sync`, `connections`, `llm_api_base`,
`llm_api_key`, `model`, `reflect.model`, `conflict.model`, `archivist_db_path`,
`standalone`, `stores` — can be pinned in a `config.operator.yaml` that techne honors **only
while the file is provably not writable by the process reading it**. Resolved in order:

```
/etc/techne/config.operator.yaml     # the only real lock
$TECHNE_OPERATOR_CONFIG              # convenience; an agent can bypass with `env -u`
<techne_home>/config.operator.yaml   # convenience; bypassable via TECHNE_HOME
```

A layer that exists but **is writable by us fails CLOSED** to the built-in defaults rather
than deferring to `config.yaml`. `config.yaml` keeps its own values on disk either way — an
enforcing layer never destroys the file it overrules. `techne doctor`'s `config:trust` check
names the tier in force and the lever that actually applies to it.

> **Pin the WHOLE set or not at all.** An omitted key reverts to its **built-in** value; it
> does not fall back to `config.yaml`. Watch `stores` especially: an omitted store DSN does
> not error — it opens a new empty database at `<data_dir>/<domain>.db`, so domain memory
> *looks* intact and is not.

**Removed in 0.13.0:** `search.mmr_lambda`. It round-tripped through `config.yaml` and was
never passed to any search function, so setting it was a silent no-op. Existing configs
carrying the key still load; it is ignored, as it was in effect before.

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

Semantic versioning. **Full release history: [`CHANGELOG.md`](CHANGELOG.md).**

<details>
<summary>Pre-0.8.0 history (now canonical in CHANGELOG.md)</summary>

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

</details>

---

## License

AGPL-3.0-or-later. See `LICENSE` file.
