# db-per-domain: one store file per persona/domain

**Status**: Draft for review (Raven + Richard, 2026-06-20). Pre-implementation.
**Tracking**: reframes [#88](https://github.com/dimartin/techne/issues/88) (was: in-db domain registration), ties in [#87](https://github.com/dimartin/techne/issues/87) (StoreAdapter / patterns) and [#89](https://github.com/dimartin/techne/issues/89) (codex graph).
**Decision recorded**: techne knowledge#327.

---

## TL;DR

Each domain/persona gets its **own db file**, behind a `StoreAdapter`:

| db | owner | holds |
|---|---|---|
| `techne.db` | personal memory (you) | sessions, threads, observations, **knowledge** (incl. document *cards*), identity, outbox |
| `librarian.db` | Librarian | `documents` + fts/vec (full content of transcripts, docs) |
| `architect.db` | Architect | patterns (the architectural-patterns corpus) |
| `codex.db` | Codex | repo/graph domain (nodes/edges) |

Core (`techne.db`) stops carrying domain tables. Domains are routed by a small **store registry** (config: domain → adapter + dsn). This replaces #88's "register in-db domains" with "route to the domain's own store," and generalizes #87's `StoreAdapter` from patterns-only to *every* domain.

## Problem

The Library MVP (`1f72aa7`) added the `documents` domain by editing techne **core**: `documents`/`documents_fts`/`documents_vec` live in the shared `SCHEMA_SQL` (every db — even a memory-only one — gets them), and the search engine names `documents` directly in 6 places (`SearchTable` Literal, the FTS + vec branches, `backfill_vectors`, `_VEC_TABLE_NAMES`). A domain leaked into the substrate.

Two ways out were on the table:
- **#88** — keep domains in `techne.db`, register them so core stops hardcoding names.
- **#87** — domains are *separate backend-portable stores* (own file, blast radius, team-grantable, not personal `~/.techne`).

The pattern-store spec already chose *separate* for patterns. The persona-domain model has the Librarian owning its domain exactly as the Architect owns patterns — so the Librarian "should" be separate too, but it's sitting in the personal db. **Decision: go separate for all domains.** Personal data (memory) stays in `techne.db`; domain/team data gets its own file.

## The scope rule

The cut is **scope**, not size:
- **Personal-scope** → `techne.db`: your session history, threads, observations, curated personal knowledge, identity. Local, SQLite-to-the-metal, never synced to a team.
- **Domain/team-scope** → own db: documents, patterns, codex. Separate blast radius, grantable, backend-portable, optionally team-synced.

A document's **gist card** is personal recall (it's *your* memory that this thing exists) → stays in `techne.db`. The document's **full content** is domain data → `librarian.db`.

## Architecture

### Store registry + StoreAdapter

`techne.db` is the implicit personal store (`config.db_path`, unchanged). Domains are configured:

```yaml
stores:
  librarian: { adapter: sqlite, dsn: "sqlite:///~/.techne/librarian.db" }
  architect: { adapter: sqlite, dsn: "sqlite:///~/.techne/architect.db" }   # → postgres later, by config
  codex:     { adapter: sqlite, dsn: "sqlite:///~/.techne/codex.db" }
```

`load_store("librarian")` returns the domain's `StoreAdapter` (mirrors `load_adapter(config.sync.adapter)`). `StoreAdapter` is the per-domain persistence handle: `migrate / health / get`, plus `connect()` for the domain's own SQL. **SQLite-only by decision (techne#135)** — an earlier design advertised a backend-agnostic `add`/`search` so domains could "leap to Postgres," but every domain bypassed it to raw SQLite and those methods had no callers, so they were removed. techne is local-first; a server backend would be designed against a real driver if/when one is ever needed.

### Unified recall, cross-db fetch — the crux

The hard question: if `documents` leaves `techne.db`, how does recall stay unified? **Option D already answers it.** Recall returns the *gist*; full content is fetched on demand.

- **Gist cards stay in `techne.db.knowledge`** (`type=document_card`). So the primary recall path — search the gists — is a **single-db** operation against `techne.db`. No cross-db search, no SQLite `ATTACH`.
- **Full content moves to the domain db.** A card's pointer (`tags: doc:<id>`, now also `store:librarian`) resolves cross-db: `resolve_card` opens `librarian.db` via the registry and `StoreAdapter.get(id)`.

This is the elegant part: **the card model makes federation a keyed `get`, not a federated search.** You never need to rank across dbs at query time — the rankable gist is already in personal recall; the boundary is only crossed to fetch one known document.

> **Why not SQLite `ATTACH`?** ATTACH would couple recall to a single connection spanning every domain db — and to the physical layout (one machine, all files co-located). Separate connections behind `StoreAdapter` keep the domains independently movable/syncable and each db's schema self-contained. The card-in-recall model removes the only reason we'd have wanted ATTACH (no cross-db ranking at query time).

**Domain-local search** (e.g. full-text over *all* of `librarian.db`'s content, not just the gists) is the domain's own search function (e.g. `documents.search_documents`) running its native SQL against `store.connect()` — a deliberate, domain-scoped operation (`techne library search`), separate from personal recall.

### Per-db breakdown

- **`techne.db`** — unchanged tables: sessions, threads, observations, knowledge (incl. `document_card` rows), identity, outbox, knowledge_candidates. `documents`/`_fts`/`_vec` are **removed from `SCHEMA_SQL`**.
- **`librarian.db`** — `documents` + `documents_fts` + `documents_vec`. The Librarian schema lives in the Librarian domain module, applied via `StoreAdapter.migrate()`, not core.
- **`architect.db`** — patterns (per #87 spec). Pure team knowledge, queried directly from the store; *not* carded into personal recall by default (it isn't your personal memory).
- **`codex.db`** — repo/graph domain (#89): nodes/edges (graphqlite). Same pattern.

## Migration plan

Sequenced; the Librarian goes first because it's already built and working — the cleanest proof of the pattern.

1. **Store registry + SQLite `StoreAdapter`** — `load_store(domain)`, config `stores:` block, a SQLite adapter that opens/migrates a domain db file. (Smallest possible — `migrate / health / get` + `connect()`.)
2. **Librarian → `librarian.db`**:
   - Move the `documents`/`_fts`/`_vec` schema out of core `SCHEMA_SQL` into the Librarian domain (applied to `librarian.db`).
   - `documents.py` (`store_document`/`get_document`/search) operate on the librarian store, not `config.db_path`.
   - Cards stay in `techne.db`; extend the pointer with `store:librarian`; `resolve_card` fetches via the librarian store.
   - **Backfill**: copy the 57 existing `documents` rows from `techne.db` → `librarian.db`; drop the tables from `techne.db`.
   - Remove the 6 `documents` references from `search.py`; route `techne search --table documents` (or a new `techne library search`) to the librarian store.
3. **Patterns → `architect.db`** (#87): build on the proven registry/adapter; collapse `architectural-patterns.md` into the store.
4. **Codex → `codex.db`** (#89): graph domain on the same pattern.

## Open questions

- **`techne search --table documents`**: keep it (route to the librarian store) or move to a domain-scoped `techne library search`? (Leaning: domain-scoped; personal recall is the cards.)
- **Vector search** across domains: each domain owns its own vec index in its store; personal recall vec-searches only `techne.db`. Confirm no cross-db vec need.
- **Config defaults**: do domain dbs default to existing (`~/.techne/<domain>.db`) and auto-create on first use, or require explicit enable? (Leaning: explicit enable, so a memory-only install stays one file.)
- **Backend portability proof**: validate the SQLite→Postgres `StoreAdapter` swap on `architect.db` (per #87) once a domain actually needs it — don't build Postgres day one.

## How this reframes the issues

- **#88** — no longer "in-db domain registration." Becomes "remove domain tables from core; route via the store registry." Update or close-and-supersede with this doc.
- **#87** — `StoreAdapter` is the mechanism every domain db uses, not patterns-only. Patterns is the first *team* domain; Librarian is the first *migrated* domain.
- **#89** — `codex.db` is just another domain on this pattern.
