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
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - techne targets macOS/Linux
    fcntl = None

from techne.config import Config
from techne.core.stores import StoreAdapter, load_store, plain_connect

CODEX_VERSION = 1

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
"""

_migrated: set[Path] = set()
_migration_done: set[Path] = set()


def _apply_codex_migration(db_path: Path) -> None:
    """Run pending codex schema migrations under a cross-process file lock.

    Mirrors database.py's _guarded_migrate pattern: fcntl.flock serialises
    concurrent process races; a double-checked version read under the lock
    makes the loser a safe no-op.

    v0 → v1
    -------
    * ALTER TABLE symbols ADD COLUMN file_hash TEXT  (if absent)
    * Rebuild edges: create edges_v1 with (from_symbol, to_name, to_symbol,
      kind, src_file_hash, resolution_status); migrate existing rows deriving
      to_name from the target symbol's name with status 'import_resolved';
      DROP old table, RENAME, re-create indexes.
    * PRAGMA user_version = 1

    Fresh DBs (no symbols table yet) are a no-op here — CODEX_SCHEMA
    applied afterwards by codex_store() creates the v1 tables directly.
    """
    lock_path = db_path.with_suffix(db_path.suffix + ".migrate.lock")

    def _do() -> None:
        conn = plain_connect(db_path)
        try:
            # Fast path: nothing here yet — codex_store() will apply full schema.
            has_symbols = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbols'"
            ).fetchone()
            if not has_symbols:
                conn.execute(f"PRAGMA user_version = {CODEX_VERSION}")
                conn.commit()
                return

            # Double-checked under lock.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= CODEX_VERSION:
                return

            print("[techne/codex] migrating schema v0 → v1", file=sys.stderr)

            # 1. Add file_hash to symbols (idempotent — check first).
            sym_cols = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
            if "file_hash" not in sym_cols:
                conn.execute("ALTER TABLE symbols ADD COLUMN file_hash TEXT")
                conn.commit()

            # 2. Rebuild edges with name-based shape.
            #    Check whether the new column already exists (race-safe double-check).
            edges_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
            if "to_name" not in edges_cols:
                # Snapshot before the destructive rebuild (parity with the core
                # db's _guarded_migrate), then rebuild ATOMICALLY: the whole
                # CREATE→INSERT→DROP→RENAME runs inside one BEGIN/COMMIT, so a
                # crash can never leave `edges` dropped-but-not-renamed.
                # DROP IF EXISTS guards a re-run after a crash mid-rebuild.
                _backup_codex_db(db_path)
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

            conn.execute(f"PRAGMA user_version = {CODEX_VERSION}")
            conn.commit()
        finally:
            conn.close()

    if fcntl is None:  # pragma: no cover
        _do()
        return
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _do()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _backup_codex_db(db_path: Path) -> None:
    """Snapshot codex.db before a destructive migration step (parity with the
    core db's _guarded_migrate / #78). Best-effort: a failed backup is logged
    but does not block the migration — the rebuild is itself atomic, so the
    backup is belt-and-suspenders for an already-populated store."""
    if not db_path.exists():
        return
    import shutil
    from datetime import datetime, timezone

    backups = db_path.parent / "backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db_path, backups / f"{db_path.stem}-v0-to-v1-{stamp}.db.bak")
    except OSError as exc:
        print(f"[techne/codex] pre-migration backup failed: {exc}", file=sys.stderr)


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
