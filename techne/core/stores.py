"""Domain stores — one db file per domain/persona, behind a StoreAdapter (techne#88/#98).

db-per-domain (`docs/db-per-domain.md`): `techne.db` is personal memory; each domain
(librarian, architect, codex) gets its own store, routed by config via `load_store()`.

**SQLite-only, by decision (techne#135).** This is a per-domain *path + schema
registry*, NOT a backend-portability seam. An earlier design advertised a
backend-agnostic `add`/`search` Protocol so Postgres/MariaDB/ClickHouse could "slot
in later" — but every domain bypasses it to raw SQLite (FTS5, `bm25`, recursive
CTEs, triggers) and those methods had zero callers, so they were removed. techne is
local-first (no network/key/server); team domains share via files/git, not a server.
If a server-class backend ever becomes a real need, design the seam *then* against an
actual driver — guessing its shape now produced only indirection. See techne#135.

Domains run their own SQL against `SqliteStore.connect()` (a `plain_connect` that
applies ONLY the domain's schema, never techne core's). The Protocol carries the
genuinely cross-domain surface: schema lifecycle (`migrate`/`health`) and a keyed
`get` (the Librarian card federation in `cards.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from techne.config import Config


@runtime_checkable
class StoreAdapter(Protocol):
    """A domain's persistence handle (techne#88/#98) — schema lifecycle plus a keyed
    lookup. Deliberately minimal: only the surface that is actually shared across
    domains. Domains do their real reads/writes against `SqliteStore.connect()` with
    their own SQL (FTS5, supersede, graph CTEs), which is not — and need not be —
    portable. `add`/`search` were removed (techne#135): they had no callers and a
    portable full-text/ranking surface was never real.
    """

    domain: str

    def migrate(self, schema_sql: str) -> None: ...
    def health(self) -> bool: ...
    def get(self, table: str, row_id, *, key: str = "id") -> dict | None: ...


class SqliteStore:
    """A domain store backed by a single SQLite file. Reuses techne's `connect()`
    (WAL, busy_timeout, row_factory), so a domain db behaves like the core db.

    Table names passed to get() come from internal domain code, never user input —
    they're interpolated into SQL (values are always parameterized)."""

    def __init__(self, domain: str, db_path: Path):
        self.domain = domain
        self.db_path = Path(db_path)

    # The domain's working connection — all bespoke domain SQL (FTS5 triggers,
    # supersede, graph CTEs) runs against this.
    #
    # A PLAIN connection (pragmas + row_factory) — deliberately NOT core's connect(),
    # which would apply techne's core SCHEMA_SQL to the domain db. A domain store
    # gets ONLY its own schema (via migrate); that's the per-db-schema goal.
    def connect(self) -> sqlite3.Connection:
        return plain_connect(self.db_path)

    def migrate(self, schema_sql: str) -> None:
        conn = self.connect()
        try:
            conn.executescript(schema_sql)
            conn.commit()
        finally:
            conn.close()

    def health(self) -> bool:
        try:
            conn = self.connect()
            try:
                conn.execute("SELECT 1")
                return True
            finally:
                conn.close()
        except Exception:
            return False

    def get(self, table: str, row_id, *, key: str = "id") -> dict | None:
        conn = self.connect()
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (row_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def plain_connect(db_path: Path) -> sqlite3.Connection:
    """A pragma'd SQLite connection that does NOT apply techne's core schema.
    Use for domain dbs (librarian.db, etc.) so they get only their own schema —
    core.database.connect() would core-initialize them (db-per-domain, techne#99)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


_BUILTIN_ADAPTERS = ("sqlite",)


def store_path(config: Config, domain: str) -> Path:
    """Resolve a domain's SQLite path: the configured dsn, else <data_dir>/<domain>.db."""
    spec = config.stores.get(domain)
    if spec and spec.dsn:
        return _resolve_sqlite_dsn(spec.dsn)
    return Path(config.data_dir) / f"{domain}.db"


def _resolve_sqlite_dsn(dsn: str) -> Path:
    for prefix in ("sqlite:///", "sqlite://"):
        if dsn.startswith(prefix):
            return Path(dsn[len(prefix) :]).expanduser()
    return Path(dsn).expanduser()


def load_store(config: Config, domain: str) -> StoreAdapter:
    """Return the StoreAdapter for a domain, mirroring `load_adapter` for sync.

    Defaults to a SqliteStore even when the domain isn't in config.stores (so a
    domain works out of the box at <data_dir>/<domain>.db). SQLite is the only
    backend (techne#135); a non-sqlite `adapter` in config is a configuration error.
    """
    spec = config.stores.get(domain)
    adapter = spec.adapter if spec else "sqlite"
    if adapter == "sqlite":
        return SqliteStore(domain, store_path(config, domain))
    raise ValueError(
        f"Store adapter '{adapter}' for domain '{domain}' is not supported — techne "
        f"is SQLite-only (techne#135). Built-in: {_BUILTIN_ADAPTERS}."
    )


def migrated_store(
    config: Config, domain: str, schema_sql: str, seen: set, *, reconcile: bool = False
) -> StoreAdapter:
    """`load_store` + apply the domain schema once per (process × store identity).

    The single resolution point every domain accessor (librarian/architect/codex)
    routes through — so the registry (`config.stores` + `store_path`) is the sole
    source of a domain's path + adapter, and a `dsn:` override actually takes
    effect (techne#102). `schema_sql` is the domain's own DDL (CREATE IF NOT
    EXISTS), idempotent; we still gate it on `seen` to skip re-running executescript
    on every op. `seen` is the caller module's process-lifetime memo.

    `reconcile=True` (techne#136) additionally converges an EXISTING db toward
    `schema_sql` via `migrator.reconcile_schema` — adding any new table/column/
    index/trigger the bare `CREATE IF NOT EXISTS` would skip on an upgraded db. Use
    it for purely-additive domains (librarian). Do NOT use it for a domain with a
    destructive/numbered migration (architect/codex) — that runs its versioned step
    under `guarded_migrate` separately, and a structure-diffing reconcile would
    mangle an old-shape table the numbered step means to rebuild.
    """
    store = load_store(config, domain)
    key = getattr(store, "db_path", domain)
    if key not in seen:
        if reconcile:
            # reconcile_schema provisions a fresh db AND converges an existing one,
            # in the load-bearing order (columns before indexes). It REPLACES the
            # blanket executescript here on purpose: executescript runs the whole
            # DDL in text order, so a new `CREATE INDEX` over a column the upgraded
            # table doesn't have yet would fail before any reconcile could add it.
            from techne.core import migrator

            conn = store.connect()
            try:
                migrator.reconcile_schema(conn, schema_sql, triggers_sql=schema_sql)
            finally:
                conn.close()
        else:
            store.migrate(schema_sql)
        seen.add(key)
    return store
