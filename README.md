# techne

**Persistent memory for AI agents — local-first, one file, and it runs on the model already in the room.**

*Technē* (τέχνη) is the ancient Greek word for **craft**: practical, applied know-how — the knowledge you build by *doing*, as opposed to *epistēmē*, abstract theoretical knowledge. That's the idea here. techne is the craft knowledge an agent accumulates across sessions — what it did, what it learned, who it works with — so it gets better at the work over time instead of starting cold every session.

Concretely: a SQLite-backed memory store whose core is a **single local file** (`techne.db`) — yours, offline, inspectable with `sqlite3`. It gives an agent three things to be useful over time: a structured record of *what it did* (sessions + auto-detected threads), a curated record of *what it learned* (knowledge), and a stable record of *who it is and who it works with* (identity). Its self-maintenance — distilling sessions into durable memory, deduping, evidence-checking claims — runs on **the agent's own model** (no API key, no extra inference bill). Larger or team-scoped domains — documents, patterns, code — each get their own SQLite file behind a `StoreAdapter` (db-per-domain).

> **New here? Start with the brochure:** [**Welcome to your new memory**](docs/welcome.md) — the friendly first-timer tour (the memory loop, the good bits you'd never find on your own, the defaults, and what's worth turning on next). This README is the reference manual; read the welcome guide first.
>
> **Tour the pieces:** [`docs/modules.md`](docs/modules.md) — a short sales-pitch for each module (what it does, why it's different, how to enable it).
>
> **Running techne day to day, or across a fleet of machines?** [**The operator guide**](docs/operator-guide.md) — the recommended hook configuration (what's on by default, what it costs, how to turn it off) and the operating habits that came from running techne ourselves: the recall rule's limits, the knowledge-row/issue reference convention, the model-pin trap, and why CLAUDE.md imports need a fallback line.

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
and keeps them when a store moves from a local file to a team service, where a local
librarian may become a shared one.

**→ [The four functions](docs/four-functions.md)** — the tutorial: ask each one, read
the `binding`, and watch the same call answer from a different store. Start there.

`techne_recall` remains the default for *"what do we know about X"* — it escalates
across all four. Address a domain by name when you already know which function you
want.

---

On Debian/Ubuntu, `venv` isn't preinstalled: `sudo apt update && sudo apt install python3-venv`.

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
> On Debian/Ubuntu, `pipx` isn't preinstalled: `sudo apt update && sudo apt install pipx` (macOS: `brew install pipx`).
>
> ```bash
> pipx install "tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.18.2"
> pipx ensurepath
> ```
>
> Now open a new shell. `pipx` puts `techne` in `~/.local/bin`, which is not on PATH in
> the shell you ran `ensurepath` from, so `techne init` there fails with `command not found`.
> In the new shell:
>
> ```bash
> techne init
> ```
>
> **Install from git, not PyPI.** The PyPI package named `techne` is a different project;
> `pip install techne` gets you that one. It declares none of the extras below, so pip only
> warns about them and installs nothing extra.
>
> `[recommended]` pulls semantic recall + the CVE pre-install audit (`vectors` + `security`).
> With a vectors backend installed, `techne init` downloads the embedding model once (tens
> of MB, then cached) so search is semantic from the first session; `--no-vectors` skips it.
> Drop it for a **core-only** install, or swap in any other set — see the
> [extras matrix](#extras--what-to-install) for the full possibility space.
>
> Upgrade later with `pipx install --force "tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@<newer-tag>"`
> (a plain `pipx install` on an already-installed techne refuses without `--force`).
>
> **Upgrading from 0.18.1 or earlier:** the distribution was renamed from `techne` to
> `tenish-techne` in 0.18.2 (the `techne` command and import are unchanged). pipx keeps
> the old `techne` environment and its `techne` command, so remove it first, then
> install, then re-run `techne init` so the hooks point at the new interpreter:
> `pipx uninstall techne`, the install line above, `techne init`.
>
> **An upgrade replaces the code and nothing else. Run these afterwards:**
>
> ```sh
> techne init              # ONLY init writes the SessionStart hook chain — an upgrade
>                          #   leaves whatever chain was written last. It also rewrites a
>                          #   0.17-or-earlier config.yaml to the sparse form, downloads
>                          #   the embedding model and embeds what is stored, and
>                          #   refreshes the bundled skills, so the next line is
>                          #   belt-and-braces.
> techne skills install    # for anyone who skipped init (or passed `--no-skills`)
> techne agents install    # SAME flag, separate command — `--no-skills` skips the research
>                          #   sandbox too, and `skills install` does not bring it back
> ```
>
> Run `techne init` **before you change any config key.** A `config.yaml` written by 0.17.0
> or earlier pinned every key; until init rewrites it, each load un-pins any value that was
> once a default, so a `false` you type there may not take effect. `techne doctor` and the
> session-start banner name every such key. See [Configuration](#configuration).
>
> The third line is not belt-and-braces the way the second is. `techne guide investigating`
> tells an agent to do its web research inside the `research-readonly` sandbox — a
> definition with WebFetch and WebSearch and nothing else. Without the file that
> instruction is unenforceable: the agent reaches for a general research agent, which has a
> shell, and one that can both read attacker-controlled pages and write to the pattern
> store is one injection away from writing whatever a page tells it to. `techne agents
> status` reports each definition as unchanged / edited / **widened** / absent, and
> `widened` is called out separately from `edited` because a sandbox that gained tools is
> the failure that matters.
>
> `techne init` reports the embedding step as `Semantic recall ready: embedded N stored
> row(s).` Run `techne vectors backfill` yourself only if init printed a download failure
> instead, or you passed `--no-vectors`. That run worked only if it printed a JSON counts
> block. A `vector search unavailable` line means it embedded nothing and exited 0 — run
> `techne vectors status` to see whether a backend resolved at all, then re-run
> `techne vectors backfill` somewhere the model cache can be fetched. The bare command
> downloads by default; only the SessionStart hook passes `--no-download`. See the
> CHANGELOG's Upgrading section.
>
> **If you ever ran `techne init --posture observe` (or `--posture spool-primary`), pass it
> again.** `--posture` defaults to `auto` and `init` re-resolves it every run, so a bare
> `init` can flip a posture you chose — in either direction. It is machine-wide, and `init`
> reports the resolved state rather than announcing a change.
> The `git clone` + `pip install -e .` flow above is for **developing** techne (live local edits).

> **Bundled procedures.** `techne init` also installs two operator procedures as slash
> commands: **`/self-care`** (hygiene pass — sweep the outbox, prune backups and snapshots,
> drain the rails, reclaim disk) and **`/techne-promote`** (turn accumulated observations
> into reviewed knowledge without an API key). They ship *with* techne rather than from a
> skills marketplace because they drive its CLI and know the store layout of the version
> they came with — a procedure written for 0.12 can be wrong for 0.15. `techne skills status`
> shows what you have; a copy you have edited is reported, never silently overwritten.
> A copy that matches an earlier release byte for byte is recognised as unedited, so
> `techne skills install` refreshes it rather than keeping it as yours (#1072).
>
> init also installs two research stubs as Claude Code skills, **`investigate-url`** and
> **`survey-field`**, in `~/.claude/skills/<name>/SKILL.md`. Each holds one line that loads
> the matching bundled guide (`techne guide investigating` / `techne guide surveying`), so
> the procedure lives in one place and the stub cannot drift from it (#1040).
> Install without any of them via `techne init --no-skills`.

---

## What's inside

| Layer | Role |
|---|---|
| **Sessions** | Discrete agent runs. Each session accumulates observations and ends with auto-detected topic threads. Carries three typed summary columns by provenance — `summary` (manual override), `summary_llm` (e.g. Claude Code's `ai-title`), `summary_heuristic` (auto-detector output). Read priority is manual > LLM > heuristic. |
| **Threads** | Distinct topics within a session, detected by a structural heuristic (default) or by LLM. Boundary signals: temporal gap, tool-family change, repo change, file/entity overlap, **and user prompts** — `UserPrompt` observations don't break alone but amplify other weak signals. Threads link across sessions (`parent_thread_id`) so a chain of work is traceable over weeks. |
| **Observations** | Tool calls and content captures during a session. Includes user prompts (`tool_name = UserPrompt`) when ingested via the historical transcript backfill. Source material for thread detection. |
| **Knowledge** | Durable facts, decisions, patterns, evaluations, and procedures. Supersession-aware (revisions tracked). |
| **Identity** | Key-value store for who-the-agent-is and who-it-works-with. Ingests structured markdown (`contacts.md`, `invariants.md`) and YAML on init. |
| **Outbox** | Pluggable sync for shipping curated knowledge to a team-memory backend. Default adapter is `noop`; ships with a `filesystem` adapter. Installed packages can add sync adapters, outbox sinks and recall sources through entry points; see [Extension points](#extension-points). |
| **Search** | FTS5 across all content layers. Optional vector search via `sqlite-vec` + `fastembed` for semantic similarity. |
| **Domain stores** | Where [the four functions](#the-four-functions) keep their data — the *layout*, not the mental model. `techne.db` is personal memory and answers as the **archivist** when unbound; larger or team-scoped domains get their own db file behind a `StoreAdapter` (db-per-domain) — **librarian** (`librarian.db`, documents), **architect** (`architect.db`, patterns), **codex** (code graph, *experimental* — **one db per repo** at `codex/<slug>-<hash>.db`). Each db carries only its own schema. Address the domain by name; the db path is what it happens to resolve to today, and the answer's `target` tells you. See [Domain stores](#domain-stores-db-per-domain) and `docs/db-per-domain.md`. |

The schema and triggers all live in `techne/core/database.py`. Schema migrations are tracked in a `schema_version` table; `connect()` is lazy-init so library callers don't need to remember to call `init_db()` first.

---

## Install

techne is in alpha — installed from source. The PyPI name `techne` belongs to an unrelated
project, so a PyPI release would need a different package name; until then, install from git.

**Requires:** Python 3.11+, git. CI runs the suite on 3.12 for pull requests and on 3.12
and 3.13 for main, and the vector eval on 3.14; 3.11 is supported but not in CI.

Use a virtual environment — required on Debian/Ubuntu (PEP 668), recommended everywhere else.
On Debian/Ubuntu, `venv` isn't preinstalled: `sudo apt update && sudo apt install python3-venv`.

```bash
git clone https://github.com/tenishai/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # core only
techne init
```

You can run `techne init` before Claude Code is installed. It registers the hooks, skills
and MCP server anyway and prints `Claude Code not found; techne's hooks are registered and
take effect when you install it`, so installing Claude Code later needs no second init. For
no Claude Code integration at all, run `techne init --platform standalone`.

If no embedding backend is installed, init's closing lines give the command that adds
semantic recall, matched to how techne was installed: `pip install -e ".[recommended]"` in
the checkout for an editable install, `uv tool install --force` for a uv tool, that venv's
own pip for any other venv, and `pipx install --force` otherwise (#1124). `techne
capabilities` prints the same command.

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
`pipx install "tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.18.2"`.

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

The primary way to use techne is as a memory backend for [Claude Code](https://claude.com/claude-code). One command sets up the database, registers Claude Code hooks (session start, end, post-tool-use, pre/post-compact, plus UserPromptSubmit for per-turn recall and PreToolUse for grep-assist, both on by default), and registers the MCP server so the agent can query its own memory.

```bash
techne init --platform claude-code
techne status
```

From there, every Claude Code session is captured, thread-summarized at session end, and queryable via the CLI or MCP tools.

`techne status` also **self-checks hook registration** — if a hook is enabled in config but not actually wired into Claude Code's `settings.json`, it prints a `⚠ Hook registration drift` warning; `techne init` reconciles it. (Claude Code loads hooks at session *start*, so a newly-registered hook — e.g. [grep-assist](docs/welcome.md), on by default — fires from the *next* session, not the current one.)

`techne doctor` goes further and asks whether each subsystem is actually **working**, which
includes two freshness checks added in 0.17.0 (#937). `codex:freshness` compares the repo's
recorded `indexed_head` against the current HEAD — a stale code index answers "not found" with
the same confidence as a correct miss, so it now says so on the way in:

```
! codex:freshness   stale   techne was indexed at 21813c1, HEAD is now d0a42a5 — codex
                            may answer 'not found' for code that exists
                              remedy: techne codex index /path/to/repo
✓ cards:freshness   live    20 anchor(s) match the card vouching for them
```

`cards:freshness` does the same for architecture cards: every `anchor: <file> sha=<blob>` line
in `ARCHITECTURE.md` and `docs/arch/*.md` is compared against the seam it vouches for, using the
same globs as the pre-push gate so the banner and the gate cannot disagree. Both are cheap by
construction rather than deferred — 32 checks in 0.149s against the hook's 5s budget, measured at 0.17.0.

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

The complete agent-facing surface — 39 tools, grouped by purpose — is documented in
**[`docs/mcp-tools.md`](docs/mcp-tools.md)**, generated from the live registry and
pinned by a test so a new tool cannot ship undocumented.

With `connections` configured, `techne_recall` merges rows from the other stores into the
local page. Each federated row names its source in `store` (the connection name, or
`local`) and `role`. The underscore forms `_store` / `_role` still appear with the same
values but are deprecated; read `store` / `role` (#1102). The merge keeps the local page's
order and its reserved head slots, and interleaves each remote store's rows in their own
order (#1089).

## CLI reference

```
techne init [--platform claude-code|codex|opencode|standalone|cabal] [--identity-dir DIR]
            [--posture auto|observe|spool-primary] [--skills/--no-skills] [--force-skills]
            [--vectors/--no-vectors] [--allow-worktree]
                                           # create the store, register hooks + MCP server,
                                           # install skills, download the embedding model.
                                           # Only claude-code registers anything; the other
                                           # platforms are recorded in config and stamped on
                                           # sessions, and standalone prints embedding steps
                                           # and embed what is stored (--no-vectors skips).
                                           # Also rewrites a pre-0.18 config.yaml to the
                                           # sparse form (backup: config.yaml.bak-<stamp>)
techne status                              # session and storage stats
techne --version                           # the installed version, and — on a build installed
                                           #   from a git ref — the commit it was built from,
                                           #   read from PEP 610 direct_url.json (#965). A build
                                           #   carrying unreleased merges no longer reports the
                                           #   last tag as if that were what is running.
techne capabilities [--json]               # what techne can do on THIS install and what is
                                           # switched off, grounded in your own store — the
                                           # operator twin of the techne_capabilities MCP tool
techne capabilities ack FLAG [--reason T]  # mark FLAG as off on purpose: it stops counting
                                           # toward the session-start invitation (no expiry)
techne capabilities unack FLAG             # undo that
techne serve                               # run as a stdio MCP server (non-Claude-Code clients)
techne guide [NAME]                        # print a bundled guide, or list them (no checkout needed)
techne bindings [--json]                   # what each of the four domains resolved to

# Bundled skills and subagents (see "Bundled procedures" above)
techne skills install [--force]            # /self-care, /techne-promote + the research stubs
techne skills status                       # current / outdated / user-modified / absent
techne skills remove                       # remove only the copies techne wrote and still owns
techne agents install [--force]            # the research-readonly subagent definition
techne agents status                       # unchanged / edited / widened / absent
techne agents remove                       # remove only the copies techne wrote and still owns

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
techne knowledge list [--type fact|decision|evaluation|procedure|document|document_card] [--tags T] [--limit N]
techne knowledge add CONTENT [--type T] [--tags T] [--confidence high|medium|low]
techne knowledge supersede OLD_ID CONTENT  # new revision; OLD_ID stops being recalled
techne knowledge supersede OLD_ID --by ID  # retire OLD_ID into an existing entry
                                           # (patterns go to `techne architect ingest`)
techne knowledge archive-scan [--floor F] [--window DAYS] [--dry-run]
                                           # soft-archive stale, uncertain, unreaffirmed
                                           # entries (hidden from injection, never deleted)
techne knowledge unarchive ENTRY_ID        # restore an archived entry as 'uncertain'
techne knowledge export [-o DIR] [--include-superseded]
                                           # live knowledge as a git-diffable markdown tree

# Identity
techne identity show [--prefix PREFIX]

# Search
techne search QUERY [--table all|threads|observations|knowledge] [--limit N]
techne vectors status                      # embedding backend, stamp, rows not yet embedded
techne vectors backfill [--no-download] [--tags [--all-tags]]
                                           # embed un-embedded rows; re-embed all if the
                                           # backend changed. init already runs this once

# Outbox (sync)
techne outbox status                       # pending / synced / dead counts
techne outbox flush                        # ship pending entries via configured adapter
techne outbox dead                         # entries that failed permanently
techne outbox flush-sinks                  # flush pending rows to registered sinks
techne outbox prune-orphans [--target T]   # mark pending rows for unregistered targets as dead;
                                           # refuses while a sink entry point is refused or
                                           # failed, unless --target names what to prune
techne outbox purge [--target T] [--yes]   # delete drainable-but-unconfigured pending rows
techne outbox sweep [--retain-days N] [--apply]
                                           # drop delivered rows nothing reads again (#518):
                                           # collapse ledger lanes, age out the rest (default
                                           # 7 days); never pending or dead rows. Dry run
                                           # unless --apply

# Sinks
techne sinks list                          # all registered sinks: name, description, schema
                                           # fields; also refused and failed entry points
techne sinks describe NAME                 # full schema field list for a named sink

# Backfill + re-analysis
techne import [PATH] [--no-prompts] [--force]
                                           # ingest historical Claude Code JSONL transcripts as
                                           # observations + session rows. PATH defaults to
                                           # ~/.claude/projects/<slug>/ for the current cwd.
                                           # Idempotent — skips sessions that already have
                                           # observations unless --force.
techne usage-backfill [--force]            # fill sessions.tokens_* (in/out/cache write/cache
                                           # read, subagents included) from transcripts still on
                                           # disk. Only NULL rows unless --force. Idempotent.

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
techne audit [--since N] [--json]          # utilization audit (techne#329): which injection
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
                                           # emit per-thread reflection prompts for agent to answer;
                                           # no --session = the newest session with unreflected
                                           # work, of any age, one session per call
techne reflect ingest RESPONSES_FILE       # feed agent-answered prompts back; writes candidates
techne reflect promote CANDIDATE_ID [--by WHO] [--reason TEXT]
techne reflect discard CANDIDATE_ID [--by WHO] [--reason TEXT]
techne reflect stats                       # candidate counts as JSON
techne reflect reconcile [--apply]         # retire queue rows left by a replaced thread
                                           # detection; park below-threshold ones (dry run
                                           # by default)
techne reflect undecline --session ID [--thread ID]
                                           # offer declined threads again
techne custodian status                    # what each emit→ingest rail is bound to, and
                                           # what it last did
techne custodian run [--rail R] [--limit N] [--dry-run] [--json]
                                           # run the rails headlessly with the bound model
                                           # (what a scheduler calls)

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
techne scrub-credentials [--apply] [--json]
                                           # re-apply current credential redaction to rows
                                           # already stored (observations, knowledge, summaries,
                                           # coord, reminders, candidates, ...). Dry-run counts
                                           # only; --apply rewrites in one transaction. Does not
                                           # reach vectors, librarian.db, backups or remote sinks
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
                                           # Also lists the keys your config.yaml pins.
                                           # hook:pre_compact / hook:post_compact read
                                           # DORMANT ("idle: no compaction since …") when
                                           # nothing has compacted, not STALE (#1075)
techne watch                               # exits 78 (EX_CONFIG) on a startup precondition
                                           # restarting cannot fix — e.g. a missing [watch]
                                           # extra. The shipped systemd unit sets
                                           # RestartPreventExitStatus=78, so it stops on the
                                           # FIRST failure instead of respawning forever.
                                           # BOTH halves are required; either alone loops.
techne probe [--runtime claude-code|opencode|pi] [--mode off-test|pull|differential]
                                           # differential-canary memory-attribution probe (#94):
                                           # seeds a canary, spawns a fresh session, checks
                                           # whether techne carried the codeword into context

# Domain stores — db-per-domain (each in its own db file beside techne.db)
# Archivist — read-only SQL over the team (or local) knowledge store
techne archivist schema                    # tables and columns `archivist query` can read
techne archivist query SQL [--limit N] [--json]
                                           # one guarded read-only SELECT
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
                                           # The richer procedures ship as techne's own
                                           # guides — `techne guide investigating` /
                                           # `techne guide surveying` — with thin
                                           # `investigate-url` / `survey-field` stub
                                           # skills installed by `techne init --skills`.
techne architect ingest PATTERNS_MD [--verdict skip|skip-with-flag|patterns-only|full-eval]
                                           # seed from an architectural-patterns.md corpus.
                                           # --verdict records YOUR judgment of the
                                           # investigation; omitted, it records
                                           # 'patterns-only' (the modal outcome).
techne architect reverdict URL --verdict X # correct one investigation's verdict without
                                           # re-ingesting. Leaves surveyed_at alone:
                                           # correcting a judgment is not a re-survey.
techne architect sweep-legacy-verdicts --before DATE [--spare-with-eval-doc DIR]
    [--skip-url URL] [--apply]             # retire pre-#428 auto-assigned verdicts to
                                           # 'unknown'. Dry-run by default; --apply needs
                                           # a spare option, since a date alone also
                                           # catches real full-evals (#549).
techne architect search QUERY
techne architect show PATTERN_ID
techne architect export [-o FILE]          # regenerate shareable architectural-patterns.md
techne architect related PATTERN_ID        # patterns linked via [[wikilink]] edges
techne architect applies TARGET            # patterns tagged as applying to TARGET
techne architect backfill                  # re-derive the relation/applies graph (idempotent)
techne architect retire-legacy [--dry-run] # delete superseded type='pattern' rows from techne.db

# Codex — code graph (symbols + call/import/def-use edges), ONE db PER repo (db-per-repo, #209)
techne codex index PATH                    # ingest a repo (Tree-sitter; needs the [codex] extra)
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
- **Architect** (`architect.db`) — the **architectural-patterns** corpus: `investigations` (lens-aware dedup, keyed `url × surveyed_by × lens`, so two reviewer lenses on one URL is a join) + the `patterns` lifted from them. Team-scope — queried directly, not folded into personal recall. A fresh store starts empty: add one pattern with `techne architect add`, or follow `techne guide investigating` to mine a repo for them. `techne architect export` writes a shareable markdown corpus and `techne architect ingest FILE.md` loads one (see [`patterns/README.md`](patterns/README.md)).
- **Codex** — **a code graph (experimental):** `symbols` + call/import/def-use `edges`. Reachability and blast-radius are recursive CTEs (graph queries without a graph DB; `graphqlite` is an optional future upgrade, techne#89). Real-repo ingest works — a Tree-sitter `SourceAdapter` (`techne codex index <repo>`, Python+TS, needs the `[codex]` extra). **One db per repo (db-per-repo, techne#209):** each indexed repo lives at `<data_dir>/codex/<slug>-<hash8>.db`, derived deterministically from the repo's absolute root — indexing a second repo can never collide with or bleed into the first. A pre-#209 single `codex.db` migrates to its per-repo home automatically the first time any consumer resolves a codex db. **Experimental:** the first index is **manual** (`codex index`). After that, `hooks.codex_reindex_on_edit` (on by default) re-indexes each file the agent edits; changes made outside the agent still need `codex index` / `codex index-all`, or `codex watch`.

Personal `techne.db` deliberately stays raw-SQLite and local-only. techne is **SQLite-only by decision** (techne#135): the domains are isolated db files behind a thin path/schema registry, not a backend-portability seam.

---

## Configuration

Lives at `~/.techne/config.yaml`. It holds only the keys you set: `techne init` writes a
key only when you changed it or the file already set it, so every other key follows the
defaults of the version you run, including defaults a later release changes. `techne
doctor` lists the keys your file pins. A file from 0.17.0 or earlier, which pinned every
key, is migrated on upgrade: run `techne init` once to rewrite it before you change any
key, or a value that was once a default is un-pinned again on every load. See the
CHANGELOG's Upgrading note (#1070).

A typical file is a few lines — only what you changed:

```yaml
hooks:
  capture_bash: false         # everything else follows the defaults
reflect:
  enabled: true
```

The [operator guide](docs/operator-guide.md#recommended-configuration) says why each
default-on hook is on, what it costs, and how to turn it off.

> **Operators: there is a second, higher-authority layer for the keys that decide where
> memory can GO.** See [Operator-owned config](#operator-owned-config-egress-selecting-keys)
> below before deploying techne somewhere the agent should not be able to redirect its own
> egress. Installs that do not deploy one are unaffected — `config.yaml` keeps full
> authority.

**Reference, not a template.** The block below lists the common keys with their defaults,
plus example `tool_families` and `entity_patterns` entries. Don't copy it into your config:
a copied default is pinned, so it stops following the defaults of later releases, which is
the problem the sparse file exists to fix. Set only the keys you change.

```yaml
# db_path / data_dir: omit to follow TECHNE_HOME (the default, ~/.techne).
# Set them only to keep the store somewhere else; an absolute path here wins
# over TECHNE_HOME. When TECHNE_HOME is exported and this path is outside it,
# techne warns on stderr (techne#947).
# db_path: /Volumes/data/techne.db
platform: claude-code
model: claude-haiku-4-5-20251001   # only used if threads.summarizer is "llm"
model_engine: litellm              # or 'claude-code': run your own `claude -p` (no API key)

threads:
  detector: heuristic         # 'heuristic' (default, no LLM) or 'llm'
  summarizer: heuristic       # 'heuristic' (default, no LLM) or 'llm' (one call per thread)
  min_observations: 15        # below this the LLM detector collapses to single-thread; heuristic always runs
  link_threshold: 0.05        # BM25-derived similarity threshold for cross-session linking
  complete_after_hours: 48    # active → completed if no resume_points and stale
  stale_after_days: 30        # completed → stale
  # Thread-summary sync on Stop, which fires every turn (#1187): re-send a thread
  # only after this much growth and time. SessionEnd and PreCompact ignore both.
  checkpoint_min_new_observations: 10
  checkpoint_min_interval_minutes: 5
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

search:
  recency_weight: 0.3         # how much a recent row is favoured in the fused rank
  bm25_weight: 0.7            # ...against lexical relevance
  embedding_backend: ""       # "" means AUTO-SELECT, not disabled — read the live backend
                              #   from `techne vectors status`, not from this being empty.
  embedding_model: ""         # "" = the backend's default
  recall_documents_limit: 3   # librarian document cards allowed into a recall
  curated_head_slots: 3       # head positions a table="all" page reserves for the best
                              #   CURATED rows (#946). Without it a knowledge row competed
                              #   with the whole observation ledger for a slot, and an
                              #   evaluator measured gold answers ranking 1/2/7 in
                              #   table="knowledge" and dropping off the page entirely at
                              #   table="all". The page does not grow — reserved rows
                              #   displace the weakest non-curated head rows one for one,
                              #   and the tail floor is untouched. 0 restores the previous
                              #   page exactly; capped at a third of the head.
  identifier_head_slots: 2    # head positions for rows matching a RARE identifier-shaped
                              #   query token (#1078): `emit_max_chars`, `b2dcdd2`,
                              #   `reflect-drain`. Vec-first used to order those exact rows
                              #   by cosine distance, anywhere from rank 2 to off the page.
                              #   Displaces head rows like curated_head_slots; 0 restores
                              #   the previous page. A query with no RARE identifier-shaped
                              #   token is unaffected (hyphenated words and dates can be
                              #   identifier-shaped; rarity keeps most of them out).
  identifier_rare_fraction: 0.002  # a token is "rare" when fewer than this fraction of the
                              #   searched tables' rows match it (floor 5): e.g. on a
                              #   63k-row store the cutoff is ~127 rows at table="all",
                              #   so a common word like `techne` never qualifies.
                              #   Word-like terms (no digit or underscore: `well-known`,
                              #   `reflect-drain`) need half that

hooks:
  post_tool_use: true         # capture tool use as observations
  capture_bash: true          # include Bash tool calls (default on since #1020). It's what lets
                              #   techne_check_evidence confirm a claim like "tests passed" in the
                              #   same turn it's made. Checked per call; applies immediately.
  capture_read: false         # include Read tool calls. Stays opt-in — Read fires far more
                              #   often than Bash, so capturing it inflates the observation
                              #   ledger without a matching gain.
  sweep_interval_minutes: 30  # min gap between the Stop hook's sweeps for abandoned and quiet
                              #   sessions (#1218); SessionEnd always sweeps. 0 = every Stop.
  codex_grep_assist: true     # default on (#206, #1020): a PreToolUse hook that augments the
                              #   agent's own Grep with matching Codex symbols. No-op outside an
                              #   indexed repo. Re-run 'techne init' after changing it.
  codex_grep_assist_limit: 8  # max symbols injected per Grep
  codex_edit_rail: true       # default on (#335, #1020): a PostToolUse hook that, after an Edit,
                              #   injects the edited symbol's blast-radius (its dependents — "what
                              #   breaks if I change this"), only when enough depend on it and only
                              #   in an indexed repo. Checked inside PostToolUse, so a change
                              #   applies on the next edit; no 'techne init' needed.
  codex_edit_rail_min_deps: 3 # min transitive dependents before the edit rail fires (skip leaf edits)
  codex_edit_rail_limit: 10   # max dependents injected per edit (closest-first by depth)
  codex_edit_rail_exclude_globs: [tests/*, */tests/*, test_*.py, ...]  # test files the rail never injects (#406)
  codex_reindex_on_edit: true # default on (#301, #1020): reindexes just the edited file, so the
                              #   two codex rails above don't answer from a graph gone stale
                              #   mid-session. Checked inside PostToolUse; applies on the next edit.
  per_turn_recall: true       # default on (#109/#277, #1020): a UserPromptSubmit hook injects the
                              #   durable facts most relevant to each prompt — the DYNAMIC per-turn
                              #   complement to the STABLE SessionStart block. Searches typed
                              #   prompts, pasted content (on its question lines) and subagent
                              #   hand-backs (on the report, harness frame stripped). Tool results
                              #   are covered only by inbound_recall below; see
                              #   docs/operator-guide.md for the recall rule.
                              #   Re-run 'techne init' after changing it; loads from the next session.
  per_turn_recall_limit: 5    # max knowledge rows injected per turn
  per_turn_recall_chars: 1000 # max characters per injected row (#826, #1020). The cut lands on a
                              #   sentence or word boundary and a shortened row ends in '…',
                              #   so a truncated fact is distinguishable from a whole one.
                              #   Raising this raises per-prompt token cost on EVERY turn.
                              #   Governs at any value since #842. It used to be capped:
                              #   search() pre-truncated knowledge rows to 500, so a higher
                              #   setting changed nothing and a row landing exactly on the
                              #   cap reported itself as whole. Both are fixed; 500 is now
                              #   only the default when nothing is asked for.
  per_turn_recall_long_prompt_chars: 600 # pasted content is searched on its question lines
                              #   (#1018); a paste block always counts, text with lines
                              #   starting ❯ and ⏺ once it is longer than this. Typed
                              #   prompts are searched as is.
  per_turn_recall_floor_vec_distance: null  # relevance floor (#414): drop a hit farther than
                              #   this. null = no floor, so every search injects up to the
                              #   limit. Unset on purpose until a replay measurement sets it.
  inbound_recall: false       # opt-in (#1018): PostToolUse runs the same recall on the results
                              #   of inbound_recall_tools and on coord messages it injects —
                              #   content that arrives mid-turn, with no prompt to trigger
                              #   per-turn recall. At most once per user prompt. DORMANT until
                              #   inbound_recall_floor_vec_distance is also set; `techne doctor`
                              #   says so.
  inbound_recall_tools: [Agent, Task, WebFetch, mcp__techne__techne_inbox, mcp__techne__techne_coord_threads]
                              #   Bash, Read, Edit, Grep and other file tools are refused even
                              #   if listed.
  inbound_recall_limit: 2     # rows per inbound delivery (lands mid-turn, on top of the tool result)
  inbound_recall_floor_vec_distance: null  # the inbound rail's own floor; unset = rail dormant.
                              #   Not measured for inbound text yet, so there is no default.
  per_turn_seed: false        # opt-in (#277): nudge the agent each turn to techne_store a durable
                              #   fact the moment it emerges
  startup_directive: false    # opt-in (#643): on the first prompt of a session, tell the agent
                              #   how to use techne's tools, once
  timeout_s: 5.0              # wall-clock budget for a hook (grep-assist / per-turn recall degrade to no-op if slow)

conflict:                     # the contradiction rail — which candidate rows reach your model
  model: ""                   # "" = answered by the resident agent, no API key. Egress-selecting;
                              #   see Operator-owned config below.
  max_comparison_candidates: 20  # how many matched rows are compared per check
  min_shared_keywords: 1      # floor on shared content tokens for the no-tags fallback path
                              #   (#933). 1 is the implicit floor it replaces, so leaving it
                              #   alone changes nothing. Below 1 is REFUSED rather than coerced:
                              #   it does not loosen the filter, it removes it. Raising it makes
                              #   the fallback stricter — fewer, more topical candidates.
  max_items_per_emit: 20      # cap on items in one emit payload
  log_raw_response: true      # keep the model's unparsed reply for debugging

sync:
  adapter: noop               # or 'filesystem'; custom adapters via entry points
  allow_unlisted_adapter: false  # true permits a non-built-in adapter (default deny)
  allowed_sinks: []           # `techne.outbox_sinks` entry-point names allowed to load
  allow_unlisted_sinks: false # true loads every installed sink
  flush_budget_s: 0           # seconds the session-end hook may spend delivering to sinks;
                              #   0 = unbounded. Set it (e.g. 2) when a sink makes a network
                              #   call per row; undelivered rows wait for the next session end.

archivist_publish_tags: []    # publish lane (#1298): live knowledge with any of these tags is
                              #   enqueued to `sink:archivist_publish` for a network archivist.
                              #   [] = off. Needs a package that registers that sink.
archivist_query_heap_bytes: 67108864  # SQLite heap cap for the techne_archivist_query worker (#1313)
archivist_query_timeout_s: 10         # the worker is killed after this many seconds

recall:                       # which sources recall consults, in what order (#1170)
  order: []                   # [] = the built-in ladder, then any allowed plugged-in source
  allowed_sources: []         # `techne.recall_sources` entry-point names allowed to load
  allow_unlisted_sources: false
  fill: 10                    # a `fallback: true` source runs only below this many rows
  source_limit: 5             # rows asked of each plugged-in source
  source_timeout_s: 3.0       # bound on each plugged-in source call
```

Candidates are **ranked before truncation** as of 0.17.0 (#933) — by shared-token count, with an
overlap-coefficient tiebreak and an id tiebreak for determinism. Previously the SQL carried no
`ORDER BY`, so `max_comparison_candidates` kept whichever rows SQLite happened to return. This
changes the order reaching the model on every conflict check, not only when truncation bites.

---

### Operator-owned config (egress-selecting keys)

The keys that choose **where memory can go**, or what reaches the model's context — `sync`
(the whole block, so `sync.allow_unlisted_adapter`, `sync.allowed_sinks` and
`sync.allow_unlisted_sinks` ride with it), `connections`, `llm_api_base`, `llm_api_key`,
`model`, `model_engine`, `reflect.model`, `conflict.model`, `custodian.model`,
`custodian.rail_models`, `archivist_db_path`, `standalone`, `archivist`, `archivist_publish_tags`, `recall.allowed_sources`,
`recall.allow_unlisted_sources`, `stores` — can be pinned in a `config.operator.yaml` that techne honors **only
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

`recall.order`, `recall.fill`, `recall.source_limit` and `recall.source_timeout_s` are
deliberately not governed: they only rank and bound sources that are already admitted.

**Removed (#1074):** `search.expansion_ratio`, and the `sync.auth` / `sync.audience` fields.
None of them reached any code. Configs carrying them still load.

**Removed (#1262):** `sources` (`sources.claude_code_transcripts`). Nothing read it. Configs
carrying it still load, and the block is written back unchanged.

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

### Claude Code engine (`model_engine: claude-code`)

If you use Claude Code, techne can run its own model calls through your `claude` CLI
instead of LiteLLM, with no API key and no `[llm]` extra:

```yaml
model_engine: claude-code
model: haiku                    # a Claude alias or id, passed to `claude -p --model`
custodian:
  model: haiku                  # the per-rail model keys work the same way
```

This covers every call techne makes itself: LLM thread detection and summaries,
session-end reflection and the headless custodian rails. The resident path is unchanged.
Each call runs `claude -p`, normally on your Claude Code login, so it counts against
your plan's usage limits. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are not passed
to it even when they're set, because `claude -p` would otherwise bill that key instead
of the plan. Only those two are withheld: if your Claude Code is set up another way,
for example Bedrock, Vertex or Foundry (`CLAUDE_CODE_USE_*`), a
`CLAUDE_CODE_OAUTH_TOKEN`, a named `ANTHROPIC_PROFILE` or the federation variables,
these calls use that too.
Settings files are ignored, so an `apiKeyHelper` or a settings `env` key does not apply
either. techne never stores or logs your credentials. `llm_api_base` and
`llm_api_key` are LiteLLM settings and aren't used by this engine. The call is isolated so it
can't feed techne: user, project and local settings are ignored (so techne's hooks
don't fire), no MCP servers or tools load, no session transcript is written, and it
runs from a temporary directory.

`techne doctor` reports `model:engine`. It reads broken if `claude` is not on PATH,
and when `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set in its environment, it
names the variable and says it is withheld from these calls.
`model_engine` is egress-selecting, so it can be pinned in `config.operator.yaml`
(see [Operator-owned config](#operator-owned-config-egress-selecting-keys)).

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
    agent_identity="my-agent",
)
# ... agent does work ...
run_session_end(config, session_id)
```

Caller is responsible for namespacing external IDs across runtimes that share a techne DB.

---

## Extension points

An installed package can extend techne through three entry-point groups. Each is
refused by default, because each one is an egress or ingress decision; the allow keys
belong to the [operator layer](#operator-owned-config-egress-selecting-keys).

| Entry-point group | What it adds | Allowed by |
|---|---|---|
| `techne.sync_adapters` | A team-memory backend for `techne outbox flush` | `sync.adapter: <name>` plus `sync.allow_unlisted_adapter: true` |
| `techne.outbox_sinks` | An outbox sink that receives payloads | `sync.allowed_sinks: [<entry-point name>]`, or `sync.allow_unlisted_sinks: true` |
| `techne.recall_sources` | A read-only source for `techne_recall` | `recall.allowed_sources: [<entry-point name>]`, or `recall.allow_unlisted_sources: true` |

`techne doctor` (`sinks:entry-points`, `recall:sources`) shows which entry points
loaded; it and `techne sinks list` both show which were refused or failed, and why.

### Custom sync adapters

To ship knowledge to your own team-memory service, write a class implementing the
`SyncAdapter` Protocol and register it:

```toml
# in your package's pyproject.toml
[project.entry-points."techne.sync_adapters"]
mybackend = "mypackage.mybackend:MyBackendAdapter"
```

Then in techne config:

```yaml
sync:
  adapter: mybackend
  allow_unlisted_adapter: true   # a non-built-in adapter is refused without this
  url: https://my-team-memory.example.com/api
```

`techne outbox flush` will route through your adapter. The `FilesystemAdapter` in
`techne/sync/filesystem.py` is the simplest reference implementation.

An adapter whose class declares `supports_pull = True` also joins recall as the source
`team`, reading back through its `pull`. It is not governed by `recall.allowed_sources`,
since `sync.adapter` already admitted its code. With no `recall.order` set, `team` comes
after the built-in sources and recall groups results by source; to keep it out of
recall, set an order without it. See
[docs/mcp-tools.md](docs/mcp-tools.md#choosing-what-recall-searches-and-in-what-order).

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
- **v0.5.0** — major additions over v0.4.x:
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

## Who's who

The docs and `CHANGELOG.md` cite people and agents by first name:

- **Richard** (also "Rich") is tenishai's maintainer, cited for design and policy
  decisions ("Richard's call, 2026-08-17").
- **Prudence** and similar names refer to the maintainer's automated reviewers
  and paired agent sessions. A citation like "Prudence, #872 r2" means a
  specific finding from one automated review round on that pull request: evidence,
  not a person's opinion.

## How this was built

tenishai is a small team of engineers who build with AI coding agents. Most of
this code was written in pairing sessions with Claude models in Claude Code: a
person set the design, directed the work and reviewed what landed, and the
agent wrote much of the code and docs. Many commits name the agent in a
`Co-Authored-By:` trailer, either a Claude model or the name of the
agent one of us pairs with. A commit without a trailer may still
have had AI help.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the Developer Certificate of
Origin sign-off every commit needs, and the policy on AI tools. Everyone taking
part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md). For
questions and bug reports, see [SUPPORT.md](SUPPORT.md). Report security
problems privately, as [SECURITY.md](SECURITY.md) describes.

## License

Copyright (C) 2026 tenish and the techne contributors

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.

Full text: [LICENSE](LICENSE).
