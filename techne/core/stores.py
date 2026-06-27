"""Domain stores — one db file per domain/persona, behind a StoreAdapter (techne#88/#98).

db-per-domain (`docs/db-per-domain.md`): `techne.db` is personal memory; each domain
(librarian, architect, codex) gets its own store, routed by config via `load_store()`.
SQLite is the day-one backend; the `StoreAdapter` Protocol is shaped so Postgres /
MariaDB / ClickHouse (techne#87) slot in later without touching callers — those
backends are NOT built here.

The first consumer is the Librarian migration (techne#99): it moves `documents` out
of techne core into `librarian.db` via a `SqliteStore`. Domains with bespoke SQL
(documents' FTS5 + supersede logic) use `SqliteStore.connect()`; backend-agnostic
domains use the Protocol's `add`/`get`/`search`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from techne.config import Config


@runtime_checkable
class StoreAdapter(Protocol):
    """Primary persistence for one domain — the 4th adapter shape (techne#87).

    Backend-agnostic surface only. SQLite-specific access (a raw connection for
    FTS5/bespoke SQL) is an extra on the SQLite impl, not part of the Protocol, so
    a Postgres store can satisfy this without exposing a sqlite connection.
    """

    domain: str

    def migrate(self, schema_sql: str) -> None: ...
    def health(self) -> bool: ...
    def add(self, table: str, record: dict, *, key: str = "id") -> str: ...
    def get(self, table: str, row_id, *, key: str = "id") -> dict | None: ...
    def search(self, table: str, query: str, *, limit: int = 20) -> list[dict]: ...


class SqliteStore:
    """A domain store backed by a single SQLite file. Reuses techne's `connect()`
    (WAL, busy_timeout, row_factory), so a domain db behaves like the core db.

    Table names passed to add/get/search come from internal domain code, never user
    input — they're interpolated into SQL (values are always parameterized)."""

    def __init__(self, domain: str, db_path: Path):
        self.domain = domain
        self.db_path = Path(db_path)

    # SQLite-specific escape hatch — bespoke domain SQL (FTS5 triggers, supersede)
    # runs against this. Not in the Protocol; backend-agnostic domains use add/get.
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

    def add(self, table: str, record: dict, *, key: str = "id") -> str:
        cols = ", ".join(record)
        placeholders = ", ".join("?" * len(record))
        conn = self.connect()
        try:
            cur = conn.execute(
                f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
                tuple(record.values()),
            )
            conn.commit()
            return str(record.get(key, cur.lastrowid))
        finally:
            conn.close()

    def get(self, table: str, row_id, *, key: str = "id") -> dict | None:
        conn = self.connect()
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (row_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def search(self, table: str, query: str, *, limit: int = 20) -> list[dict]:
        """FTS over <table>_fts if it exists; else a LIKE on a `content` column."""
        if not query.strip():  # FTS5 MATCH '' is a syntax error (#103)
            return []
        conn = self.connect()
        try:
            has_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (f"{table}_fts",),
            ).fetchone()
            if has_fts:
                # Sanitize before MATCH so operator-bearing input (`foo-`, `a OR`,
                # an unbalanced quote) can't raise fts5 syntax errors (#74) on this
                # public StoreAdapter surface — mirrors documents/patterns search.
                from techne.core.search import _sanitize_fts_query

                match = _sanitize_fts_query(query)
                if not match:  # all-punctuation/backslash input → no terms; MATCH '' errors (#103)
                    return []
                rows = conn.execute(
                    f"""SELECT t.* FROM {table}_fts f JOIN {table} t ON t.id = f.rowid
                        WHERE {table}_fts MATCH ? LIMIT ?""",
                    (match, limit),
                ).fetchall()
            else:
                from techne.core.database import like_escape

                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    (f"%{like_escape(query)}%", limit),
                ).fetchall()
            return [dict(r) for r in rows]
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
    domain works out of the box at <data_dir>/<domain>.db). Non-sqlite adapters are
    declared but not yet implemented (techne#87).
    """
    spec = config.stores.get(domain)
    adapter = spec.adapter if spec else "sqlite"
    if adapter == "sqlite":
        return SqliteStore(domain, store_path(config, domain))
    raise ValueError(
        f"Store adapter '{adapter}' for domain '{domain}' is not built yet "
        f"(built-in: {_BUILTIN_ADAPTERS}; postgres/mariadb/clickhouse land in techne#87)."
    )


def migrated_store(config: Config, domain: str, schema_sql: str, seen: set) -> StoreAdapter:
    """`load_store` + apply the domain schema once per (process × store identity).

    The single resolution point every domain accessor (librarian/architect/codex)
    routes through — so the registry (`config.stores` + `store_path`) is the sole
    source of a domain's path + adapter, and a `dsn:` override actually takes
    effect (techne#102). `schema_sql` is the domain's own DDL (CREATE IF NOT
    EXISTS), idempotent; we still gate it on `seen` to skip re-running executescript
    on every op. `seen` is the caller module's process-lifetime memo.
    """
    store = load_store(config, domain)
    key = getattr(store, "db_path", domain)
    if key not in seen:
        store.migrate(schema_sql)
        seen.add(key)
    return store
