"""Codex domain — the repo/code-graph store, in its own db file (techne#89).

db-per-domain (`docs/db-per-domain.md`, proven by Librarian #99 + Architect #87): a
codebase is files → symbols → call/import/def-use edges. Stored RELATIONALLY on the
SQLite substrate (NOT a graph DB — that breaks the single-file/portable substrate).
Graph queries (reachability / blast-radius) are hand-rolled recursive CTEs in v1
(dep-free); graphqlite (Cypher + PageRank/Louvain as a SQLite UDF) is an optional
`techne[codex]` upgrade later, not core.

Scope here is the persona-agnostic STORAGE substrate — schema + the graph data layer.
The Tree-sitter SourceAdapter that parses real repos lives in techne/codex/source.py.
Graph-boosted ranking, graphqlite, and code vectors are deferred.

Schema versions (PRAGMA user_version on codex.db):
  0 — original: edges(from_symbol, to_symbol NOT NULL, kind); no file_hash on symbols
  1 — v1 extraction: edges name-based (to_name, to_symbol nullable); symbols.file_hash
  2 — codex_meta KV table: persists repo_root (and future per-db metadata)
"""

from __future__ import annotations

from pathlib import Path

from techne.config import Config
from techne.core import migrator
from techne.core.stores import StoreAdapter, load_store, plain_connect

CODEX_VERSION = 2

# v1 shape — the authoritative schema for fresh databases.
# Incremental: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS are
# safe no-ops on tables that already exist.  Columns that were added in a
# migration (file_hash, to_name, etc.) are present here so brand-new DBs
# get them without running the migration; old DBs get them via
# _apply_codex_migration() (called before store.migrate() — see codex_store).
#
# Edge indexes for new columns (to_name, to_symbol, src_file_hash) are omitted
# from this string because on a pre-migration v0 DB those columns don't exist
# yet when executescript() runs — they're created by the migration instead.
# After migration completes the columns exist, so subsequent schema applications
# succeed. idx_edges_from references from_symbol which exists in both schemas.
CODEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id         TEXT PRIMARY KEY,          -- stable id (file::name::kind::line)
    file       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- function | class | method | module | …
    signature  TEXT,
    span       TEXT,                      -- "start:end" line range
    language   TEXT,
    file_hash  TEXT                       -- SHA-256 of file bytes at index time (v1)
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE TABLE IF NOT EXISTS edges (
    from_symbol       TEXT NOT NULL REFERENCES symbols(id),
    to_name           TEXT NOT NULL,      -- callee / imported name string (v1: always known)
    to_symbol         TEXT REFERENCES symbols(id),  -- resolved id; NULL until a resolver pass
    kind              TEXT NOT NULL,      -- call | import | def-use
    src_file_hash     TEXT,              -- hash of the REFERENCING file (incremental delete key)
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
    PRIMARY KEY (from_symbol, to_name, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_symbol);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, signature, content=symbols, content_rowid=rowid, tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, signature)
    VALUES (NEW.rowid, NEW.name, COALESCE(NEW.signature, ''));
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, signature)
    VALUES ('delete', OLD.rowid, OLD.name, COALESCE(OLD.signature, ''));
END;
CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, signature)
    VALUES ('delete', OLD.rowid, OLD.name, COALESCE(OLD.signature, ''));
    INSERT INTO symbols_fts(rowid, name, signature)
    VALUES (NEW.rowid, NEW.name, COALESCE(NEW.signature, ''));
END;

CREATE TABLE IF NOT EXISTS codex_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Process-global memoization keyed on db_path: prevents re-running the migration
# step on every codex_store() call within a process lifetime.
_migrated: set[Path] = set()
_migration_done: set[Path] = set()


def _codex_v0_to_v1(conn) -> None:
    """codex.db v0 → v1 structural changes: symbols.file_hash + name-based edges.

    Idempotency is STRUCTURAL (column-presence), not version-based — a
    retrofit-versioned db reads ``user_version = 0`` regardless of its actual
    shape, so a destructive rebuild must guard on the columns, never the version.
    """
    # 1. Add file_hash to symbols.
    sym_cols = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "file_hash" not in sym_cols:
        conn.execute("ALTER TABLE symbols ADD COLUMN file_hash TEXT")
        conn.commit()

    # 2. Rebuild edges to the name-based shape, ATOMICALLY (the whole
    #    CREATE→INSERT→DROP→RENAME runs inside one BEGIN/COMMIT script, so a crash
    #    can never leave `edges` dropped-but-not-renamed). DROP IF EXISTS guards a
    #    re-run after a crash mid-rebuild. The engine took the pre-migration backup.
    edges_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
    if "to_name" not in edges_cols:
        conn.executescript("""
            BEGIN;
            DROP TABLE IF EXISTS edges_v1;
            CREATE TABLE edges_v1 (
                from_symbol       TEXT NOT NULL REFERENCES symbols(id),
                to_name           TEXT NOT NULL,
                to_symbol         TEXT REFERENCES symbols(id),
                kind              TEXT NOT NULL,
                src_file_hash     TEXT,
                resolution_status TEXT NOT NULL DEFAULT 'unresolved',
                PRIMARY KEY (from_symbol, to_name, kind)
            );

            INSERT OR IGNORE INTO edges_v1
                   (from_symbol, to_name, to_symbol, kind, resolution_status)
            SELECT e.from_symbol, s.name, e.to_symbol, e.kind, 'import_resolved'
            FROM   edges e
            JOIN   symbols s ON s.id = e.to_symbol;

            DROP TABLE edges;
            ALTER TABLE edges_v1 RENAME TO edges;

            CREATE INDEX IF NOT EXISTS idx_edges_from      ON edges(from_symbol);
            CREATE INDEX IF NOT EXISTS idx_edges_to_name   ON edges(to_name);
            CREATE INDEX IF NOT EXISTS idx_edges_to_symbol ON edges(to_symbol);
            CREATE INDEX IF NOT EXISTS idx_edges_src_hash  ON edges(src_file_hash);
            COMMIT;
        """)
    else:
        # to_name already present (e.g. partial migration) — ensure indexes.
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_edges_to_name   ON edges(to_name);
            CREATE INDEX IF NOT EXISTS idx_edges_to_symbol ON edges(to_symbol);
            CREATE INDEX IF NOT EXISTS idx_edges_src_hash  ON edges(src_file_hash);
        """)


def _codex_v1_to_v2(conn) -> None:
    """codex.db v1 → v2: codex_meta KV table (repo_root + future db-level metadata).

    The table is also created via ``CREATE TABLE IF NOT EXISTS codex_meta`` in
    CODEX_SCHEMA (applied by ``store.migrate(CODEX_SCHEMA)`` after the migration
    step), so this step is intentionally a no-op structurally — its sole job is to
    bump user_version so the migration engine does not re-enter on subsequent opens.
    The ``IF NOT EXISTS`` guard makes it safe on both fresh and migrated DBs.
    """
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS codex_meta (key TEXT PRIMARY KEY, value TEXT);"
    )


def _run_codex_steps(conn, current: int, target: int) -> None:
    """Chain all codex migration steps from *current* to *target*.

    Each step guards itself structurally so it's safe to re-enter (idempotent).
    Version is written once at the end so a mid-chain crash re-runs all pending
    steps on the next open (they're all no-ops on already-applied changes).
    """
    if current < 1:
        _codex_v0_to_v1(conn)
    if current < 2:
        _codex_v1_to_v2(conn)
    migrator.user_version_io.write(conn, target)
    conn.commit()


def _apply_codex_migration(db_path: Path) -> None:
    """Run pending codex schema migrations via the shared engine (techne#136 —
    replacing the hand-rolled fcntl-lock + shutil.copy2 backup with
    migrator.guarded_migrate's lock + online backup).

    A fresh store (no symbols table) just gets its version stamped — CODEX_SCHEMA,
    applied next by codex_store(), creates the v1 tables directly. Kept as a named
    entrypoint (tests call it directly).
    """
    conn = plain_connect(db_path)
    try:
        # Fast path: nothing here yet — codex_store() applies the full v1 schema.
        has_symbols = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbols'"
        ).fetchone()
        if not has_symbols:
            migrator.user_version_io.write(conn, CODEX_VERSION)
            conn.commit()
            return
        migrator.guarded_migrate(
            conn,
            db_path,
            target_version=CODEX_VERSION,
            read_version=migrator.user_version_io.read,
            run_steps=_run_codex_steps,
            label="codex schema",
        )
    finally:
        conn.close()


def codex_store(config: Config) -> StoreAdapter:
    """The Codex store (codex.db), schema applied and migrated.

    Resolved via the registry, so a `config.stores['codex']` dsn override
    takes effect (techne#102); default <data_dir>/codex.db.

    Ordering guarantee: migration runs BEFORE schema application so that
    executescript(CODEX_SCHEMA) never encounters a v0 table missing new columns.
    Both operations are gated on process-lifetime `_migration_done` /
    `_migrated` sets — safe for concurrent callers within one process.
    """
    store = load_store(config, "codex")
    db_path = store.db_path

    if db_path not in _migration_done:
        _apply_codex_migration(db_path)
        _migration_done.add(db_path)

    if db_path not in _migrated:
        store.migrate(CODEX_SCHEMA)
        # After initial schema, ensure the new-column indexes exist (fresh DBs
        # get them from CODEX_SCHEMA; migrated old DBs already have them; this
        # handles any remaining gap gracefully).
        _conn = plain_connect(db_path)
        try:
            _conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_edges_to_name   ON edges(to_name);
                CREATE INDEX IF NOT EXISTS idx_edges_to_symbol ON edges(to_symbol);
                CREATE INDEX IF NOT EXISTS idx_edges_src_hash  ON edges(src_file_hash);
            """)
        finally:
            _conn.close()
        _migrated.add(db_path)

    return store


def codex_db(config: Config) -> Path:
    return codex_store(config).db_path


# ---------------------------------------------------------------------------
# codex_meta helpers — repo-level KV store
# ---------------------------------------------------------------------------


def get_meta(db_path: Path, key: str) -> str | None:
    """Return the value for *key* from codex_meta, or None if absent.

    Gracefully returns None when the codex_meta table doesn't exist yet (e.g.
    on a pre-v2 DB that hasn't been migrated), so callers don't need to guard.
    """
    conn = plain_connect(db_path)
    try:
        # Guard: table may be absent on a very old DB accessed without codex_store().
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_meta'"
        ).fetchone()
        if not has_meta:
            return None
        row = conn.execute(
            "SELECT value FROM codex_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_meta(db_path: Path, key: str, value: str) -> None:
    """Upsert *key* → *value* in codex_meta. Creates the table if absent (graceful)."""
    conn = plain_connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO codex_meta (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()
    finally:
        conn.close()
