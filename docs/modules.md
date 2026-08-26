# techne — the modules

*One memory substrate, composable in layers. Install only what you need; each piece earns its place.*

techne is a **local-first, SQLite-backed memory system for AI agents**, delivered over MCP so any agent (Claude Code, opencode, anything that speaks MCP) can use it. The core is a single file. Everything else is opt-in.

A guiding principle worth stating up front: **techne's "thinking" features use the agent's own model** — the model already in the room — via a render-a-prompt / answer-it / feed-it-back loop. No second API key, no extra inference bill, no background sampling. (An optional operator-model path exists for headless runs; see *Memory hygiene*.)

---

## Core memory — `techne.db`  ·  *always on, zero-config, one file*

The substrate. `pip install techne && techne init` and an agent has durable memory across sessions — in **one local SQLite file**, no server, no cloud.

It records three things an agent needs to be useful over time:
- **what it did** — sessions, auto-detected topic *threads*, and an observation ledger of tool calls;
- **what it learned** — curated *knowledge* (facts, decisions, evaluations, procedures), supersession-aware;
- **who it is and who it works with** — an *identity* layer seeded from your `contacts.md` / `invariants.md`.

**Why it's different:** it's local-first to the metal. Your memory is a file you own, not a SaaS row. It works fully offline, and the whole thing is inspectable with `sqlite3`.

---

## Recall & search  ·  *find the right memory at the right moment*

Full-text search (SQLite FTS5) across every layer, out of the box. Add the `vectors` extra for **semantic recall** (embeddings via `fastembed` + `sqlite-vec`) so "how did we handle auth retries" finds the memory even when the words don't match.

Two retrieval shapes:
- **`recall`** — topic-scoped context for *right now* (incl. the identity layer).
- **`search`** — query across sessions / threads / knowledge.

**Why it's different:** git-anchored memories carry a **freshness grade** — a fact tied to a file that has since changed/moved/been deleted is flagged `modified/moved/deleted/unknown`, so the agent spends verification effort only where the ground actually shifted. Memories whose basis was superseded are surfaced as **uncertain** and down-ranked instead of asserted as fact.

```bash
pip install "techne[vectors]"   # semantic recall (FTS works without it)
```

---

## Threads  ·  *a week of work, traceable*

Within a session, techne detects distinct topics with a **structural heuristic** (no LLM required): temporal gaps, tool-family shifts, repo changes, file/entity overlap, and user-prompt signals. Threads **chain across sessions**, so a line of work is followable over days or weeks — not lost at each `/clear`.

**Why it's different:** the default detector is deterministic and free. You *can* opt into an LLM detector/summarizer, but you never have to.

---

## Memory hygiene — reflect · consolidate · dream · evidence · claims  ·  *uses the agent's own model*

The part most memory systems skip. techne keeps memory *honest and lean* through **five distinct passes**, each driven by **the live agent's own model** (the `emit` / `ingest` pattern — techne renders a prompt, the agent answers, techne stores the result). **No API key. No extra inference cost.**

- **Reflect** — at session end, distill a finished session's observations into durable knowledge *candidates* (add-only; opt-in, off by default; enable via `reflect.enabled`).
- **Consolidate** ("sleep") — dedup and merge near-duplicate live knowledge, fighting memory bloat over time.
- **Dream** (recombine) — surface *non-obvious* connections across knowledge entries (A + B ⇒ C) as LOW-confidence, review-gated candidates. *Speculative* — for discovery, not assertion.
- **Evidence gate** (#105) — verify an agent's *structured* claims (committed / tested / wrote-file) against the observation ledger *deterministically* (`backed` / `unbacked` / `unverifiable`). **Caveat:** commit/test/action claims come from Bash output, which is off by default (`capture_bash`), so those return `unverifiable` — not `unbacked` — until enabled.
- **Claims reconciliation** (#107) — use the agent's own model to judge free-text claims in CC's curated memory against the deed-log digest. *Distinct from the evidence gate:* #105 is deterministic + structured; #107 is model-driven + free-text.

**Why it's different:** memory that *curates itself using the model already running*, and proposes rather than overwrites. For headless/background runs with no agent in the loop, an optional **operator-model** path (`techne[llm]`) can drive the same passes autonomously — but it's strictly opt-in; the default is the agent's own model.

```bash
pip install "techne[llm]"   # OPTIONAL: autonomous/headless model passes
```

---

## Archivist — *what does the team know?*  ·  `techne.db`, or a team store

The first of the four names, and the one this tour was missing entirely. Ask it what is
*known* — decisions, facts, corrections, the reasoning you would hate to redo — as opposed
to what is written down (Librarian), what is worth stealing (Architect), or what the code
looks like (Codex).

**Why it's different:** the archivist is the only one of the four whose binding changes
whose knowledge you are asking about. Bound `local` — the default — it resolves to *this
instance's own* memory and there is no second store. Bound to a team store it answers for
the team, and the same call reports which it resolved to, so the caller never has to guess.

---

## Librarian — *what is written down?*  ·  `librarian.db`

Personal recall stays fast because it ranks **gist cards**, not full documents. The Librarian holds the **full content** of transcripts and docs in its own db, with its own **FTS index** — one virtual table, `documents_fts`. Document search is full-text by construction; the `[vectors]` extra adds vector search to the **archivist**, not here.

**Why it's different — federation as a keyed `get`, not a federated search.** A document's gist lives in your personal recall; when you actually need the full text, the card's pointer resolves cross-db to the Librarian. You never rank across databases at query time — the rankable summary is already local, and the boundary is crossed only to fetch one known document.

---

## Architect — *what is worth stealing?*  ·  `architect.db`

A curated corpus of **architectural patterns** lifted from other projects' code — design decisions and trade-offs worth reusing — linked into a `[[wikilink]]` relation graph (`pattern_search`, `pattern_related`). Team-scoped: its own file, grantable, shareable.

**Why it's different:** patterns are a *team* asset with its own blast radius, not personal memory — so it lives in its own db and is queried deliberately, never auto-injected into your private recall.

---

## Codex — *what does the code look like?*  ·  `codex.db`  ·  **experimental**

Point it at a repo (`techne codex index`) and it extracts a **symbol + call/import graph** with tree-sitter (Python + TypeScript today). The agent can then ask **`codex_search`** (find a function/class) and **`codex_blast_radius`** (what does this reach / what reaches this — transitive callers/callees over the graph).

**Why it's different:** real graph queries — `callers`, `callees`, BFS reachability — not just grep.

> **Status: experimental (opt-in).** Indexing is **manual today** — the graph goes stale as code changes; re-run `index` to refresh. Auto-refresh and deeper integration are the next cycle. Ships behind the `codex` extra; nothing else depends on it.

> **When to reach for it (and when not):** see [`codex-usage.md`](./codex-usage.md) — Codex is a *relationship-mapping engine* (code is adapter #1), best for graphs too big to eyeball or needing transitive queries; for ad-hoc "where is X" and small graphs, grep/memory win. What decides whether such a graph lives or rots isn't persistence — it's having an automatic, high-value consumer.

```bash
pip install "techne[codex]"
techne codex index .
```

---

## Sync & team memory  ·  *opt-in, pluggable, off by default*

Personal memory is yours and local. When you *want* to share or ingest, techne has a clean adapter seam:
- **SourceAdapters** pull external material *in* — Claude Code's curated memory, full session transcripts, GitHub issues, the filesystem.
- **SyncAdapters** push curated knowledge *out* to a team backend via an outbox (default `noop` — nothing leaves your machine unless you wire it).

**Why it's different:** team memory is a deliberate, configured act with its own boundary — never a silent default. A personal install stays a personal install.

---

## Operations — fsck · backup · restore  ·  *db-per-domain, kept consistent*

Because domains live in separate files, cross-db invariants are maintained by reconciliation, not a shared transaction — so techne ships the tools to keep the constellation honest:
- **`techne fsck`** — one pass over every cross-db check; exits non-zero on any inconsistency (CI-friendly).
- **`techne backup` / `restore`** — snapshot and restore **all** domain dbs *together*, as a unit, with a pre-restore safety snapshot and corruption-safe WAL handling.

**Why it's different:** operational integrity is a first-class, tested feature, not an afterthought.

---

## Security gate  ·  *defensive, fail-open*

`shell_guard` blocks command-chaining (`;`, `|`, backticks, `$()`, …) and, with the `security` extra, runs a **CVE pre-install audit** (`pip-audit`) before a package install proceeds.

**Why it's different:** the audit **fails open** — absent the auditor, the anti-chaining gate still holds; the CVE check only ever *sharpens* it, never blocks the basics.

```bash
pip install "techne[security]"   # adds the CVE pre-install audit
```

---

## Delivery — MCP server + platform hooks  ·  *client-agnostic*

techne is delivered as an **MCP server**, so its tools (`store`, `recall`, `search`, `resume`, the domain tools) work in **any MCP-capable client**. On **Claude Code** it adds lifecycle hooks for a frictionless experience: **automatic capture** (every turn), **recall pushed** into the session banner at start, and a **reflection nudge** to consolidate finished work. The platform layer is a thin, swappable adapter — a second client implements the same seams.

**Why it's different:** the substrate is portable; the convenience automation is a thin platform shim, not a lock-in.

---

## Install map (footprint follows function)

| You want… | Install | Adds |
|---|---|---|
| Just agent memory (capture, FTS recall, threads, knowledge) | `pip install techne` | core (lightweight) |
| Semantic recall | `techne[vectors]` | `fastembed` + `sqlite-vec` |
| Autonomous/headless model passes | `techne[llm]` | operator-model gateway |
| Code-graph | `techne[codex]` | tree-sitter + grammars |
| CVE pre-install audit | `techne[security]` | `pip-audit` |
| Continuous filesystem ingest | `techne[watch]` | `watchdog` |

*The memory-hygiene passes (reflect/consolidate/dream/evidence/claims) need **no** extra — they run on the agent's own model.*
