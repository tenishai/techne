"""Unified schema-migration engine (techne#136).

ONE place for the lock + backup + versioned step-runner and the additive schema
reconciler that every db-per-domain store routes through. Extracted from
techne.db's mature machinery (`techne/core/database.py`); the domain stores
(codex / architect / librarian / generic) replace their hand-rolled, divergent
copies by calling these helpers.

Design contract (load-bearing — see techne#136):
  * The engine does NOT own version-writing. `run_steps` writes the version —
    core inserts a `schema_version` row per step (its append-log + partial-resume
    semantics); domains call their `VersionIO.write` at each step's end. This is
    why `guarded_migrate` takes `read_version` but never writes.
  * The fcntl lock is the ONLY serialization. Migrations are not wrapped in an
    outer transaction (`executescript` auto-commits; steps issue their own
    COMMITs). The in-lock double-check makes a racing loser a true no-op.
  * The reconciler is ADDITIVE only (tables / columns / triggers / fts-backfill).
    Destructive changes (DROP/rebuild) go through a numbered `run_steps`
    migration and must keep a STRUCTURAL idempotency guard (column-presence) —
    never gate a destructive step on the version number: a retrofit-versioned
    domain reads `user_version = 0` on every existing file, so version cannot
    distinguish fresh / old-shape / already-migrated.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    import fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - techne targets macOS/Linux
    fcntl = None


# --- version accessors ------------------------------------------------------


@dataclass(frozen=True)
class VersionIO:
    """How a db reads + writes its schema version. Pluggable so core keeps its
    audited `schema_version` table while domains use the zero-ceremony
    `PRAGMA user_version` (techne#136 — the version store is a parameter, not a
    fifth mechanism)."""

    read: Callable[[sqlite3.Connection], int]
    write: Callable[[sqlite3.Connection, int], None]


# `PRAGMA user_version` — a single int baked in the SQLite header. The default
# for domain stores (codex/architect/librarian); fresh files read 0.
user_version_io = VersionIO(
    read=lambda conn: conn.execute("PRAGMA user_version").fetchone()[0],
    write=lambda conn, v: conn.execute(f"PRAGMA user_version = {int(v)}"),
)


# --- backup -----------------------------------------------------------------


def backup_db(conn: sqlite3.Connection, db_path: Path, from_v: int, to_v: int) -> Path | None:
    """Snapshot the DB to ``<dir>/backups/`` before a migration rewrites it.

    Uses SQLite's online backup API so the copy is consistent even in WAL mode
    (a plain file copy can miss un-checkpointed WAL frames). Returns the backup
    path, or ``None`` when there's nothing worth saving (absent/empty DB).
    Migrations are forward-only, so this backup is the rollback path (#78).
    """
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{db_path.stem}-v{from_v}-to-v{to_v}-{ts}.db.bak"
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return backup


# --- guarded migration ------------------------------------------------------


def guarded_migrate(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    target_version: int,
    read_version: Callable[[sqlite3.Connection], int],
    run_steps: Callable[[sqlite3.Connection, int, int], None],
    backup: bool = True,
    label: str = "schema",
) -> None:
    """Run pending migrations exactly once, even under concurrent processes.

    Claude Code fires session_start + post_tool_use near-simultaneously, so on
    the first open after a code bump several processes can all see a stale
    version and race into the migration body. A cross-process file lock
    serializes them; the loser re-reads the version under the lock and no-ops.
    We back up first and announce on stderr (#78).

    The lock is filesystem-level (not a SQLite transaction) because a migration's
    ``executescript`` calls issue their own COMMITs and can't be held inside one
    outer transaction. Per-machine locking is sufficient — each machine has its
    own db file. ``run_steps(conn, current, target)`` owns version-writing.
    """
    lock_path = db_path.with_suffix(db_path.suffix + ".migrate.lock")

    def _do() -> None:
        # Double-checked: a process we raced may have migrated while we waited.
        current = read_version(conn)
        if current >= target_version:
            return
        note = ""
        if backup:
            bkp = backup_db(conn, db_path, current, target_version)
            note = f" (backup: {bkp})" if bkp else ""
        print(f"[techne] migrating {label} v{current} → v{target_version}{note}", file=sys.stderr)
        run_steps(conn, current, target_version)

    if fcntl is None:  # no file locking available — run unguarded (legacy behaviour)
        _do()
        return
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _do()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


# --- schema reconciler (additive convergence toward a DDL string) -----------
#
# Generalized from core's reconciler: the four passes take the target DDL as an
# argument instead of reading a module global, so any domain can converge its db
# toward its own schema. Order is load-bearing: tables → triggers → fts_backfill
# → columns (a trigger may reference a not-yet-created table; backfill needs the
# triggers in place for future writes). Index reconciliation lands last in a
# follow-up (techne#138).


def _strip_sql_comment(line: str) -> str:
    """Drop an inline ``-- …`` comment from a DDL line.

    Column lines may carry a trailing comment (e.g.
    ``status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'synced'``). The
    reconciler copies the column definition verbatim into an ``ALTER TABLE … ADD
    COLUMN``; leaving the comment (and the mid-line comma before it) in produces
    invalid DDL and crashes the universal ``connect()`` (techne#132).

    Naive line-level strip: assumes no column's ``DEFAULT``/``CHECK`` carries a
    literal ``--`` inside a string (e.g. ``CHECK (x <> 'a--b')``). Revisit if that
    ever changes.
    """
    return re.sub(r"\s*--.*$", "", line)


def _schema_table_statements(schema_sql: str) -> list[tuple[str, str]]:
    """Parse *schema_sql* and return (table_name, create_sql) pairs.

    Each pair includes the CREATE [VIRTUAL] TABLE statement plus any
    immediately-following CREATE INDEX / CREATE UNIQUE INDEX lines that belong to
    that table (i.e. appear before the next CREATE TABLE). Virtual tables (FTS5)
    are included. Associated triggers live in the triggers DDL, not here. Index
    reconciliation on already-existing tables is out of scope here (techne#138).
    """
    table_re = re.compile(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE
    )
    index_re = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+", re.IGNORECASE)

    results: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_name and current_lines:
            results.append((current_name, "\n".join(current_lines).strip()))

    for line in schema_sql.splitlines():
        m = table_re.match(line.strip())
        if m:
            flush()
            current_name = m.group(1)
            current_lines = [line]
        elif current_name is not None:
            if index_re.match(line.strip()) or current_lines:
                stmt_so_far = "\n".join(current_lines)
                if line.strip().startswith("--") and ";" in stmt_so_far:
                    flush()
                    current_name = None
                    current_lines = []
                else:
                    current_lines.append(line)

    flush()
    return results


def _schema_columns(table: str, schema_sql: str) -> set[str]:
    """Parse column names for *table* out of *schema_sql* using regex."""
    pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);"
    m = re.search(pattern, schema_sql, re.DOTALL)
    if not m:
        return set()
    block = m.group(1)
    cols = set()
    for line in block.splitlines():
        line = _strip_sql_comment(line).strip().rstrip(",")
        if not line or line.upper().startswith(
            ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES", "--")
        ):
            continue
        col_name = line.split()[0]
        cols.add(col_name)
    return cols


def _trigger_statements(triggers_sql: str) -> list[tuple[str, str]]:
    """Parse *triggers_sql* and return (trigger_name, create_sql) pairs.

    Each statement spans ``CREATE TRIGGER IF NOT EXISTS <name>`` through ``END;``.
    """
    pattern = re.compile(
        r"(CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+(\w+).+?END\s*;)",
        re.IGNORECASE | re.DOTALL,
    )
    return [(m.group(2), m.group(1).strip()) for m in pattern.finditer(triggers_sql)]


def _trigger_source_table(trigger_name: str, triggers_sql: str) -> str | None:
    """Return the content-table *trigger_name* fires on (``AFTER … ON <table>``)."""
    for name, create_sql in _trigger_statements(triggers_sql):
        if name != trigger_name:
            continue
        m = re.search(r"AFTER\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+(\w+)", create_sql, re.IGNORECASE)
        return m.group(1) if m else None
    return None


def reconcile_tables(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Create any tables present in *schema_sql* but absent from the live DB.

    Runs BEFORE reconcile_columns so newly-created tables don't then need a
    separate column pass. Virtual tables (FTS5) are handled alongside regular ones.
    """
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    for name, create_sql in _schema_table_statements(schema_sql):
        if name not in existing:
            conn.executescript(create_sql)
            conn.commit()
            print(f"[techne] reconcile: CREATE TABLE {name}", file=sys.stderr)


def reconcile_triggers(conn: sqlite3.Connection, triggers_sql: str) -> None:
    """Create any triggers present in *triggers_sql* but absent from the live DB.

    Skips triggers whose source table doesn't yet exist (e.g. ``documents_ai``
    before ``documents``); the next connect() installs them after the source
    table is reconciled.
    """
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for name, create_sql in _trigger_statements(triggers_sql):
        if name in existing:
            continue
        src = _trigger_source_table(name, triggers_sql)
        if src is not None and src not in existing_tables:
            continue
        conn.executescript(create_sql)
        conn.commit()
        print(f"[techne] reconcile: CREATE TRIGGER {name}", file=sys.stderr)


def reconcile_fts_backfill(
    conn: sqlite3.Connection,
    fts_backfill: dict[str, tuple[str, list[str]]],
    fts_insert_cols: dict[str, list[str]],
) -> None:
    """Backfill any FTS table whose index is empty while its source has rows.

    Uses the FTS5 shadow ``_docsize`` table to detect an empty index (a content-
    FTS5 VT reports the source COUNT(*) even when never populated, so COUNT on the
    FTS table is not a reliable signal). Safe to re-run; runs after triggers exist
    so future writes stay in sync. *fts_backfill*/*fts_insert_cols* are caller-
    supplied because the rowid source + COALESCE wrapping can't be derived from DDL.
    """
    for fts_table, (src_table, src_cols) in fts_backfill.items():
        fts_indexed = conn.execute(f"SELECT COUNT(*) FROM {fts_table}_docsize").fetchone()[0]
        if fts_indexed > 0:
            continue
        src_count = conn.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()[0]
        if src_count == 0:
            continue
        insert_cols = ", ".join(fts_insert_cols[fts_table])
        select_cols = ", ".join(src_cols)
        conn.execute(
            f"INSERT INTO {fts_table}({insert_cols}) SELECT {select_cols} FROM {src_table}"
        )
        conn.commit()
        print(
            f"[techne] reconcile: backfill {fts_table} ({src_count} rows from {src_table})",
            file=sys.stderr,
        )


def reconcile_columns(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Add any columns present in *schema_sql* but absent from the live table."""
    table_pattern = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", re.IGNORECASE)
    for m in table_pattern.finditer(schema_sql):
        table = m.group(1)
        live_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not live_cols:
            continue  # table doesn't exist yet; CREATE TABLE IF NOT EXISTS handles it
        for col in _schema_columns(table, schema_sql) - live_cols:
            block_m = re.search(
                rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);",
                schema_sql,
                re.DOTALL,
            )
            col_def = col  # fallback: bare name (SQLite infers NULL affinity)
            if block_m:
                for line in block_m.group(1).splitlines():
                    stripped = _strip_sql_comment(line).strip().rstrip(",")
                    parts = stripped.split()
                    if parts and parts[0] == col:
                        col_def = stripped
                        break
            # SQLite cannot ADD a NOT NULL column without a DEFAULT to an existing
            # (possibly populated) table. Skip + warn rather than crash connect();
            # such a column needs a numbered migration, not the additive path.
            upper = col_def.upper()
            if "NOT NULL" in upper and "DEFAULT" not in upper:
                print(
                    f"[techne] reconcile: SKIP {table}.{col} — NOT NULL without DEFAULT "
                    "cannot be added to an existing table; use a numbered migration",
                    file=sys.stderr,
                )
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            conn.commit()
            print(f"[techne] reconcile: ALTER TABLE {table} ADD COLUMN {col_def}", file=sys.stderr)


def _index_statements(schema_sql: str) -> list[tuple[str, str]]:
    """Parse *schema_sql* for standalone ``CREATE [UNIQUE] INDEX IF NOT EXISTS``
    statements (multi-line supported — a negated class matches across newlines up
    to the terminating ``;``). Returns (index_name, create_sql) pairs."""
    pattern = re.compile(
        r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)[^;]*;)",
        re.IGNORECASE,
    )
    return [(m.group(2), m.group(1).strip()) for m in pattern.finditer(schema_sql)]


def reconcile_indexes(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Create any index present in *schema_sql* but absent from the live DB
    (techne#138). The pre-#138 reconciler only emitted a table's indexes when the
    *table* was missing, so an index added to an already-existing table never
    landed on upgraded dbs. Runs LAST — after column reconciliation — so an index
    can reference a column the column pass just added. ``CREATE INDEX IF NOT
    EXISTS`` is idempotent; we diff first only to skip work + log accurately."""
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    for name, create_sql in _index_statements(schema_sql):
        if name in existing:
            continue
        conn.executescript(create_sql)
        conn.commit()
        print(f"[techne] reconcile: CREATE INDEX {name}", file=sys.stderr)
