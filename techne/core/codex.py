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

import hashlib
import logging
import re
import shutil
from pathlib import Path

from techne.config import Config
from techne.core import migrator
from techne.core.repo import find_repo_root
from techne.core.stores import (
    SqliteStore,
    StoreAdapter,
    load_store,
    plain_connect,
    store_path,
)

log = logging.getLogger(__name__)

CODEX_VERSION = 3

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
    conn.executescript("CREATE TABLE IF NOT EXISTS codex_meta (key TEXT PRIMARY KEY, value TEXT);")


#: Path segments whose contents `_walk_repo` refuses to index. Kept HERE as SQL
#: patterns rather than imported from `techne.codex.source._SKIP_DIRS` on purpose:
#: this migration must run without the optional `techne[codex]` extra installed,
#: and it encodes what the skip list meant AT v3 — a migration that silently
#: changes meaning when a constant is edited later is not a migration.
_V3_PURGE_PATTERNS = (
    "site-packages/%",
    "%/site-packages/%",
    "dist-packages/%",
    "%/dist-packages/%",
)


def _codex_v2_to_v3(conn) -> None:
    """codex.db v2 → v3: purge rows for files the walker no longer indexes (#604).

    WITHOUT THIS, A POLLUTED INDEX SURVIVES THE FIX AND THEN CERTIFIES ITSELF.
    Indexing is incremental and the only DELETE is scoped to the file being
    re-indexed, so vendored rows written before `site-packages` was skipped are
    never revisited — nothing walks them, so nothing removes them. Measured in
    #605's review: pollute → fix → re-index leaves the vendored rows in place, and
    `index_freshness` then reports `stale: 0, missing: 0, coverage: "complete"`.
    The surface built to stop the index lying (techne#595) vouches for the
    pollution, because every one of those files really is present and unchanged on
    disk. It is not stale. It should not be there at all.

    Deleting exactly the excluded rows rather than truncating the whole graph is
    deliberate: a full rebuild costs a re-index of every repo and discards correct
    data to remove incorrect data. `guarded_migrate` takes a backup first.
    """
    for pattern in _V3_PURGE_PATTERNS:
        # Edges first — they reference symbols(id), and an orphaned edge is a
        # dangling FK that outlives the symbol it described.
        conn.execute(
            "DELETE FROM edges WHERE from_symbol IN (SELECT id FROM symbols WHERE file LIKE ?)",
            (pattern,),
        )
        conn.execute(
            "UPDATE edges SET to_symbol = NULL "
            "WHERE to_symbol IN (SELECT id FROM symbols WHERE file LIKE ?)",
            (pattern,),
        )
        conn.execute("DELETE FROM symbols WHERE file LIKE ?", (pattern,))


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
    if current < 3:
        _codex_v2_to_v3(conn)
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


def codex_store(config: Config, db_path: Path | None = None) -> StoreAdapter:
    """The Codex store (codex.db), schema applied and migrated.

    Default (``db_path=None``): the single global store, resolved via the
    registry so a ``config.stores['codex']`` dsn override takes effect
    (techne#102); default ``<data_dir>/codex.db``.

    Per-repo (``db_path`` given, techne#209): a store at that explicit path —
    used by ``codex_db_for_repo`` so each repo gets its own graph. The migration
    machinery is keyed by db_path (``_migration_done``/``_migrated`` sets), so
    multiple per-repo stores in one process each migrate independently.

    Ordering guarantee: migration runs BEFORE schema application so that
    executescript(CODEX_SCHEMA) never encounters a v0 table missing new columns.
    """
    store = SqliteStore("codex", db_path) if db_path is not None else load_store(config, "codex")
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
# Per-repo db router (techne#209) — db-per-repo, mirroring db-per-domain.
#
# Each repo gets its own codex db at <data_dir>/codex/<slug>-<hash8>.db, so
# indexing a second repo can never collide with, overwrite, or bleed into the
# first. The path derivation is deliberately PURE and STABLE (repo_root → path,
# deterministic) so the future cross-repo federation layer (techne#210) can key
# its links store on repo_root and re-derive each repo's db path.
# ---------------------------------------------------------------------------


def _repo_slug(name: str) -> str:
    """Filesystem-safe, human-readable slug of a repo directory basename."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.").lower()
    return slug or "repo"


def repo_db_path(config: Config, repo_root: Path) -> Path:
    """Pure, stable derivation: repo_root → its per-repo codex db path (techne#209).

    ``<data_dir>/codex/<slug>-<hash8>.db``. The slug (repo basename) keeps the
    path human-readable; the 8-hex sha256 of the *resolved absolute* root keeps
    it collision-free across same-named repos in different locations. No I/O,
    no side effects — safe for the #210 links store to call.
    """
    resolved = repo_root.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return Path(config.data_dir) / "codex" / f"{_repo_slug(resolved.name)}-{digest}.db"


def codex_db_for_repo(config: Config, repo_root: Path) -> Path:
    """Resolve AND prepare (schema + migration) the per-repo codex db (techne#209).

    Creates ``<data_dir>/codex/`` if needed and applies the schema, so callers
    get a ready-to-use db path. Returns the path (mirrors ``codex_db``).
    """
    ensure_legacy_migrated(config)
    db_path = repo_db_path(config, repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    codex_store(config, db_path)  # applies schema + migration at this path
    return db_path


# Guards the one-time legacy migration to at most once per (process × data_dir).
# Keyed on data_dir (not a bare bool) so per-test temp dirs each migrate.
_legacy_checked: set[Path] = set()


def ensure_legacy_migrated(config: Config) -> None:
    """Run the one-time legacy-db migration at most once per process × data_dir.

    Safe to call from every consumer's resolution path (CLI, hook, MCP) so the
    legacy ``<data_dir>/codex.db`` moves to its per-repo home the first time any
    consumer resolves a codex db — after which all consumers see the same
    per-repo layout. Best-effort: failures are logged, never raised.
    """
    data_dir = Path(config.data_dir)
    if data_dir in _legacy_checked:
        return
    _legacy_checked.add(data_dir)
    try:
        migrate_legacy_codex_db(config)
    except Exception:  # noqa: BLE001
        log.warning("codex: legacy migration check failed", exc_info=True)


def resolve_repo_db_for_query(config: Config, start_path: Path | str) -> Path | None:
    """Resolve an EXISTING per-repo codex db for *start_path*, or None (techne#209).

    For read-only consumers (grep-assist hook, MCP query tools): runs the
    one-time legacy migration, finds the repo root by walking up from
    *start_path*, and returns that repo's db path IFF it already exists. Returns
    None when *start_path* is in no repo, or the repo has never been indexed —
    read consumers must no-op on None rather than create an empty db.
    """
    ensure_legacy_migrated(config)
    root = find_repo_root(Path(start_path))
    if root is None:
        return None
    db_path = repo_db_path(config, root)
    return db_path if db_path.exists() else None


def reindex_edited_file(config: Config, file_path: str | Path) -> int | None:
    """Incrementally reindex a single edited file into its repo's codex db —
    the mid-session freshness path (techne#301).

    The SessionStart `codex index-all` only refreshes at session start, so a
    file edited mid-session leaves the graph stale until the next session. This
    reindexes just the touched file after an edit.

    Only touches repos ALREADY indexed (never creates a db — mirrors
    resolve_repo_db_for_query's read-consumer posture) and only supported
    languages. Returns the symbol count indexed, or None when skipped
    (unsupported extension / not in a repo / repo not indexed). Cheap:
    index_file is incremental — an unchanged file is a no-op via its stored
    content hash.

    NOTE on latency (techne#301 review): index_file opens the db with
    busy_timeout=5000, so under a concurrent writer (an active `codex watch`)
    this can block up to ~5s before SQLite's file lock frees; bounded_hook's
    SIGALRM does not pre-empt SQLite's busy-wait, so busy_timeout is the real
    backstop. Bounded and rare, but the caller runs this best-effort.
    """
    from techne.codex.source import index_file, is_indexable_path

    p = Path(file_path)
    if not is_indexable_path(p):
        return None
    # Run the one-time legacy-layout migration first, for parity with
    # resolve_repo_db_for_query — else a legacy install's first edit would skip
    # until some other process triggers it (techne#301 review).
    ensure_legacy_migrated(config)
    root = find_repo_root(p)
    if root is None:
        return None
    db_path = repo_db_path(config, root)
    if not db_path.exists():
        return None  # repo never indexed → don't spawn an empty db
    return index_file(db_path, p, root)


def list_codex_repos(config: Config) -> list[tuple[str, Path]]:
    """Enumerate the per-repo codex dbs as ``(repo_root, db_path)`` pairs.

    Reads each ``<data_dir>/codex/*.db``'s ``codex_meta.repo_root``. Powers
    ``codex stats --all`` and ``codex index-all``. Dbs with no stored repo_root
    (never fully indexed) are skipped. Sorted by db path for stable output.
    """
    codex_dir = Path(config.data_dir) / "codex"
    if not codex_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for db_path in sorted(codex_dir.glob("*.db")):
        root = get_meta(db_path, "repo_root")
        if root:
            out.append((root, db_path))
    return out


def migrate_legacy_codex_db(config: Config) -> Path | None:
    """One-time move of the legacy single ``<data_dir>/codex.db`` to its per-repo
    path (techne#209). Idempotent; returns the destination path if it migrated,
    else ``None`` (nothing to do).

    NOT auto-invoked while consumers still read the legacy path — wiring it in
    is the job of the consumer-rewiring phase, so the move happens together with
    the switch to per-repo resolution rather than out from under a live reader.

    WAL safety: checkpoints (TRUNCATE) before moving so no committed pages are
    stranded in a ``-wal`` sidecar, then moves the main db and clears any stale
    sidecars (empty after the truncate).
    """
    legacy = store_path(config, "codex")
    if not legacy.exists():
        return None

    root = get_meta(legacy, "repo_root")
    if not root:
        log.warning("codex: legacy %s has no repo_root — leaving in place", legacy)
        return None

    dest = repo_db_path(config, Path(root))
    if dest.exists():
        log.info("codex: per-repo db already exists at %s — leaving legacy db in place", dest)
        return None

    # Flush WAL into the main db file so the move can't strand committed pages.
    conn = plain_connect(legacy)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(dest))
    # Sidecars are empty after a TRUNCATE checkpoint; drop any that linger.
    for suffix in ("-wal", "-shm"):
        stale = Path(str(legacy) + suffix)
        if stale.exists():
            stale.unlink()
    log.info("codex: migrated legacy db %s → per-repo %s (repo_root=%s)", legacy, dest, root)
    return dest


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
        row = conn.execute("SELECT value FROM codex_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_meta(db_path: Path, key: str, value: str) -> None:
    """Upsert *key* → *value* in codex_meta. Creates the table if absent (graceful)."""
    conn = plain_connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS codex_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO codex_meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()
