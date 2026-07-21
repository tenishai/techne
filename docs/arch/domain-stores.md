---
card: arch-card:techne-domain-stores
updated: 2026-07-20
anchor: techne/core/stores.py sha=fe749946756940d5109ac45a8d7d8d2b795caf59
---
# domain-stores — per-domain SQLite registry

SEAM: StoreAdapter Protocol (domain:str, migrate(schema_sql), health(), get(table,row_id,key='id')) + default SqliteStore. A path+schema registry PER DOMAIN.
NOT A PORTABILITY SEAM: the earlier Postgres/ClickHouse-agnostic add/search Protocol was REMOVED (techne#135) — every domain bypassed it to raw SQLite FTS5/BM25/recursive-CTEs, zero callers. Don't reintroduce backend abstraction here.
WHY plain_connect: SqliteStore.connect() → plain_connect() (WAL+pragmas+row_factory, NOT core SCHEMA_SQL) so each domain db carries only its own schema; core connect() (core/database.py) would core-init a domain db — this avoids that (techne#99).
OPS: load_store(config, domain) routes by config `stores:` dsn → one db file per domain:
  techne.db (personal memory/core) | librarian.db (documents) | architect.db (patterns) | codex/<slug>-<hash>.db (one per repo).
  SqliteStore.get() = the one cross-domain keyed lookup for cross-db resolution WITHOUT ATTACH (e.g. Librarian card federation in core/cards.py).
CONSUMERS: core/codex.py, core/library.py+librarian.py, core/architect.py+patterns.py — all via load_store.
INVARIANTS: table names to get() come only from internal domain code (never user input) — interpolated into SQL; row VALUES always parameterized. health() swallows exceptions → returns False, never raises.
