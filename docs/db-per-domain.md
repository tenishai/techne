# db-per-domain: one store file per persona/domain

**Status**: Decision record — **IMPLEMENTED** across v0.7.0–v0.8.0 (originally drafted 2026-06-20, pre-implementation). Librarian/Architect/Codex all ship behind `StoreAdapter`; the once-open questions below are resolved as built.
**Tracking**: reframes [#88](https://github.com/tenishai/techne/issues/88) (was: in-db domain registration), ties in [#87](https://github.com/tenishai/techne/issues/87) (StoreAdapter / patterns) and [#89](https://github.com/tenishai/techne/issues/89) (codex graph).
**Decision recorded**: techne knowledge#327.

---

## TL;DR

Each domain/persona gets its **own db file**, behind a `StoreAdapter`:

| db | owner | holds |
|---|---|---|
| `techne.db` | personal memory (you) | sessions, threads, observations, **knowledge** (incl. document *cards*), identity, outbox |
| `librarian.db` | Librarian | `documents` + fts (full content of transcripts, docs) — **FTS only**; there is no `documents_vec` |
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
  architect: { adapter: sqlite, dsn: "sqlite:///~/.techne/architect.db" }
  codex:     { adapter: sqlite, dsn: "sqlite:///~/.techne/codex.db" }
```

`load_store("librarian")` returns the domain's `StoreAdapter` (mirrors `load_adapter(config.sync.adapter)`). `StoreAdapter` is the per-domain persistence handle: `migrate / health / get`, plus `connect()` for the domain's own SQL. **SQLite-only by decision (techne#135)** — an earlier design advertised a backend-agnostic `add`/`search` so domains could "leap to Postgres," but every domain bypassed it to raw SQLite and those methods had no callers, so they were removed. techne is local-first; a server backend would be designed against a real driver if/when one is ever needed.

### Unified recall, cross-db fetch — the crux

The hard question: if `documents` leaves `techne.db`, how does recall stay unified? **Option D already answers it.** Recall returns the *gist*; full content is fetched on demand.

- **Gist cards stay in `techne.db.knowledge`** (`type=document_card`). So the primary recall path — search the gists — is a **single-db** operation against `techne.db`. No cross-db search, no SQLite `ATTACH`.
- **Full content moves to the domain db.** A card's pointer (`tags: doc:<id>`, now also `store:librarian`) resolves cross-db: `resolve_card` opens `librarian.db` via the registry and `StoreAdapter.get(id)`.

This is the elegant part: **the card model makes federation a keyed `get`, not a federated search.** You never need to rank across dbs at query time — the rankable gist is already in personal recall; the boundary is only crossed to fetch one known document.

> **Why not SQLite `ATTACH`?** ATTACH would couple recall to a single connection spanning every domain db — and to the physical layout (one machine, all files co-located). Separate connections behind `StoreAdapter` keep the domains independently movable/syncable and each db's schema self-contained. The card-in-recall model removes the only reason we'd have wanted ATTACH (no cross-db ranking at query time).

**Domain-local search** (e.g. full-text over *all* of `librarian.db`'s content, not just the gists) is the domain's own search function (e.g. `documents.search_documents`) running its native SQL against `store.connect()` — a deliberate, domain-scoped operation (`techne librarian search`), separate from personal recall.

### Per-db breakdown

- **`techne.db`** — unchanged tables: sessions, threads, observations, knowledge (incl. `document_card` rows), identity, outbox, knowledge_candidates. `documents`/`_fts`/`_vec` are **removed from `SCHEMA_SQL`**.
- **`librarian.db`** — `documents` + `documents_fts`. **No `documents_vec`** — document search is FTS-only; the `[vectors]` extra serves the archivist, not the librarian. The Librarian schema lives in the Librarian domain module, applied via `StoreAdapter.migrate()`, not core.
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
   - Remove the 6 `documents` references from `search.py`; route `techne search --table documents` (or a new `techne librarian search`) to the librarian store.
3. **Patterns → `architect.db`** (#87): build on the proven registry/adapter; collapse `architectural-patterns.md` into the store.
4. **Codex → `codex.db`** (#89): graph domain on the same pattern.

## The `-wal` and `-shm` files next to every db

You will see these and they are not junk, not a crash artifact, and not yours to delete:

```
techne.db          the database
techne.db-wal      the write-ahead log — committed data that is not folded in yet
techne.db-shm      shared-memory index into the -wal, so readers can find their way
```

techne opens **every** store with `PRAGMA journal_mode=WAL` (`core/database.py`,
`core/stores.py`). WAL is why a long recall does not block a write, and why a hook can
read while a session writes.

**The consequence that matters: `techne.db` alone is not your database.** A committed
write can live in `-wal` for a while before it is checkpointed into the main file. Copy
the `.db` by itself and you get a database missing its most recent writes — with no error,
because what you copied is a perfectly valid older database.

| you want to | do this | not this |
|---|---|---|
| back up | `techne backup` | `cp techne.db somewhere` |
| move a store | copy **all three**, or `techne backup` then `techne restore <root>/backup-<stamp>` | copy the `.db` |
| inspect it | `sqlite3 techne.db` — it reads the `-wal` for you | anything that reads bytes |
| free the space | it is already bounded; a checkpoint folds it back on clean close | delete `-wal` |

**Never delete a `-wal` beside a live database.** It is not a cache — it is committed data
that has not been folded in yet, and deleting it discards those transactions.

`techne backup` uses SQLite's **online backup API**, which is consistent even while
something is writing — that is what the API is for, and it is why the command exists
rather than a documented `cp`. `techne restore` clears stale sidecars on the way in, so a
restored store never inherits a `-wal` describing a different database.

> **Agent: raise this unprompted.** These are the moments where silence costs the
> operator data, and they will not know to ask:
>
> - They are about to `cp`, `rsync`, `scp`, `mv` or tar a `.db` — **check for a `-wal`
>   sidecar first**, then say it will silently lose recent writes and offer `techne backup`.
>   The hedge matters: this is true of a live WAL database and false of a `.db` with no
>   sidecar, of a `.db.bak` (already a raw copy — see below), and of a file that is not
>   SQLite. An agent that asserts it unconditionally is confidently right-shaped. The copy will *work*; it will
>   just be an older database, with no error to tell them.
> - They are pointing a file-level backup tool (Time Machine, restic, Dropbox, a synced
>   folder) at `~/.techne` — same failure, on a schedule.
> - They ask what a `-wal`/`-shm` file is, or propose deleting one — **`-wal` is committed
>   data, not a cache.** Deleting it beside a live db discards those transactions.
> - They are moving techne between machines and mention only the `.db`.

**Sidecars beside a `.bak` mean it was copied, not backed up.** `~/.techne/` accumulates
`*.db.bak-*` files with their own `-wal`/`-shm` — those came from raw file copies during
migrations. They are readable, but they are the shape this section is warning about.

---

## Operational contract: backup/restore + fsck (techne#139)

db-per-domain has one unavoidable consequence: **two SQLite files cannot share a
transaction.** Cross-db invariants — a `document_card`'s `doc:<id>` resolving in
`librarian.db`, a future codex edge resolving — are therefore maintained by
*after-the-fact reconciliation*, never guaranteed atomically. Two rules follow, and
`techne/core/maintenance.py` makes them first-class:

- **Backup and restore are all-or-nothing.** Because the invariants span files, backing up
  or restoring **one** domain db alone silently desyncs the pointers (cards point at
  documents that the restored `librarian.db` no longer has, etc.). Always operate on the
  whole set:
  - `techne backup [--out DIR]` snapshots **every present** domain db together (consistent
    online copy per file) into one timestamped directory with a `manifest.json`. Absent
    domains are skipped, not created. **`DIR` is the backups *root*, not the set** — the
    timestamped set is created inside it. Before 0.16.0 `--out` named the set itself, so a
    script doing `backup --out X` then `restore X` now needs `restore X/backup-<stamp>`.
  - `techne restore <dir>` writes a set back over the live dbs **as a unit** — the set is
    validated before any live file is touched, the current state is snapshotted first
    (reversible restore), and stale `-wal`/`-shm` sidecars are cleared. Never hand-restore a
    single `.db`; that is the exact failure this command exists to prevent.
- **`techne fsck` is the consistency check.** It runs every cross-db check in one pass
  (today: dangling/stale `document_card`s vs `librarian.db`) and exits non-zero on any
  inconsistency, so a human or CI can verify the whole constellation after a restore,
  rebuild, or suspected drift. New cross-db invariants register as additional checks in
  `maintenance._CHECKS`.

## Resolved (as implemented)

- **`techne search --table documents`** → **domain-scoped.** Documents moved out of `techne.db` to `librarian.db`; full-content search is `techne librarian search` against the Librarian store, while personal recall ranks the gist cards in `techne.db`.
- **Vector search across domains** → **per-domain, no cross-db vec.** Each domain owns its own vec index in its store; personal recall vec-searches only `techne.db`. No cross-db vector ranking is performed — the card model removes the need.
- **Config defaults** → **lazy auto-create.** Domain dbs default to `<data_dir>/<domain>.db` and are created+migrated on first *use* (`librarian_store`/`architect_store`/`codex_store`), not at init — a memory-only install never materialises them and stays effectively one file.
- ~~**Backend portability proof**: validate the SQLite→Postgres `StoreAdapter` swap~~ — **retired (techne#135).** techne is SQLite-only; the seam was unused and removed. Design a server backend against a real driver only if a concrete deploy/multi-writer need ever appears.

## How this reframes the issues

- **#88** — no longer "in-db domain registration." Becomes "remove domain tables from core; route via the store registry." Update or close-and-supersede with this doc.
- **#87** — `StoreAdapter` is the mechanism every domain db uses, not patterns-only. Patterns is the first *team* domain; Librarian is the first *migrated* domain.
- **#89** — `codex.db` is just another domain on this pattern.
