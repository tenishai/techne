"""SQLite database schema and connection management for techne."""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - techne targets macOS/Linux
    fcntl = None

SCHEMA_VERSION = 9

# Schema evolution convention — when to bump SCHEMA_VERSION vs. edit SCHEMA_SQL only:
# See CONTRIBUTING.md for the full decision tree and real examples.
#
# Short version:
#   _migrate()        — data backfill, destructive change (DROP/rebuild), or a migration
#                       step that must assume a prior structural precondition. Bump
#                       SCHEMA_VERSION; update SCHEMA_SQL to match post-migration state.
#   SCHEMA_SQL only   — purely additive: new nullable column, new table, new index, new
#                       trigger. The reconciler (_reconcile_tables / _reconcile_columns /
#                       _reconcile_triggers) diffs SCHEMA_SQL against the live DB on every
#                       connect and applies the delta automatically.

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sessions
-- summary           = manual override (user-set, highest priority)
-- summary_llm       = LLM-derived (e.g. Claude Code ai-title), middle priority
-- summary_heuristic = auto-detector output (thread breakdown), fallback
-- Read sites should prefer manual > LLM > heuristic; see effective_summary().
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    project_path TEXT,
    agent_identity TEXT,
    platform TEXT DEFAULT 'claude-code',
    summary TEXT,
    summary_llm TEXT,
    summary_heuristic TEXT,
    summary_llm_source TEXT
);

-- Threads (the unit of continuity)
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_num INTEGER NOT NULL,
    label TEXT NOT NULL,
    summary TEXT NOT NULL,
    resume_points TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    parent_thread_id TEXT REFERENCES threads(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_threads_session ON threads(session_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_parent ON threads(parent_thread_id);

-- Observations (tool use captures)
-- tags: whitespace-separated inferred tag tokens (embedding-based, see core/inference.py)
-- kind: highest-scoring kind label (decision-shaped / fact-shaped / question-shaped)
-- timestamp: when the tool-use event fired (or when the record was emitted by the
--            upstream source). Set automatically to the DB write time; for live
--            captures (hook → DB write is sub-second) this is a faithful event time.
-- event_at: optional override for the real-world subject-event time. Set explicitly
--           by ingest paths when known (e.g., importing a transcript dated last year
--           — `timestamp` is the import time, `event_at` is the transcript's actual
--           session date). NULL means `timestamp` is the canonical event time.
--           Query layer uses COALESCE(event_at, timestamp) for event-time ordering.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT REFERENCES threads(id),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_at TIMESTAMP,
    tool_name TEXT,
    content TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot',
    tags TEXT,
    kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_thread ON observations(thread_id);
CREATE INDEX IF NOT EXISTS idx_observations_tier ON observations(tier);

-- Knowledge (durable facts, decisions, patterns)
-- `confidence` (text enum: low/medium/high) is the legacy field; readers keep
-- using it for display. `confidence_numeric` (0..1) is the canonical value the
-- decay-aware effective_confidence() reads. `last_confirmed_at` powers decay:
-- score = confidence_numeric * 0.5^(days_since_confirmed / half_life_days).
-- A re-affirmation bumps last_confirmed_at without creating a new revision.
-- `event_at`: actual real-world event timestamp. NULL means treat `created_at`
--             as the event time. Use COALESCE(event_at, created_at) for ordering.
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    confidence_numeric REAL NOT NULL DEFAULT 0.6,
    last_confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES knowledge(id),
    event_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by);

-- Identity (key-value store for agent identity)
CREATE TABLE IF NOT EXISTS identity (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'config',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Outbox (local queue for team sync)
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    data_json TEXT NOT NULL,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'synced' | 'dead'
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    synced_at TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_dedup
    ON outbox(content_hash) WHERE status = 'pending';

-- Checkpoints (pre-compaction state snapshots)
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    trigger TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);

-- Thread boundaries (per-pair scores from the heuristic detector).
-- One row per consecutive observation pair within a session. Persisted
-- alongside threads so the audit surface can show why each split happened.
CREATE TABLE IF NOT EXISTS thread_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    obs_index_a INTEGER NOT NULL,
    obs_index_b INTEGER NOT NULL,
    gap_seconds REAL NOT NULL,
    family_a TEXT NOT NULL,
    family_b TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    total_score REAL NOT NULL,
    is_boundary INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_thread_boundaries_session
    ON thread_boundaries(session_id, obs_index_a);

-- Extension state (durable key-value sidecar for hooks and adapters).
-- Thin JSON payload per named extension; upserted on write, read on start.
CREATE TABLE IF NOT EXISTS extension_state (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Knowledge candidates (reflection-pass proposals awaiting promotion).
-- Populated by the end-of-session reflect pass when config.reflect.enabled.
-- While config.reflect.write_to='candidates' the live knowledge / identity
-- tables are never touched — these rows are inspected via the CLI and either
-- promoted (status='promoted', promoted_to_id set) or discarded.
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                       -- 'knowledge' | 'identity'
    type TEXT NOT NULL,                        -- knowledge VALID_TYPES, or identity key path
    content TEXT NOT NULL,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    source_session_id TEXT NOT NULL REFERENCES sessions(id),
    source_thread_id TEXT REFERENCES threads(id),
    model_used TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',    -- 'pending' | 'promoted' | 'discarded'
    promoted_to_id INTEGER,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_candidates(status);
CREATE INDEX IF NOT EXISTS idx_kc_session ON knowledge_candidates(source_session_id);

-- FTS5 indexes. tokenize='porter unicode61' applies Porter stemming on top of
-- the unicode61 tokenizer (#77) so morphological variants match: a query for
-- "optimize" hits "optimization", "index" hits "indexing", "log" hits "logs".
-- Existing DBs are upgraded to this tokenizer by the v9 migration.
CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5(
    label, summary, resume_points, content=threads, content_rowid=rowid,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    content, tags, kind, content=observations, content_rowid=id,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, tags, content=knowledge, content_rowid=id,
    tokenize='porter unicode61'
);

-- Documents (Librarian domain — external reference corpora).
-- Each row is one fetched document (e.g. a GitHub issue, arXiv paper,
-- runbook page). `source_url` is the dedup key: re-fetching the same URL
-- supersedes the prior row when content has changed, leaving the old row
-- linked via `superseded_by` for audit. Documents live in their own db
-- (a library db); they are decoupled from knowledge by design — no FK to
-- knowledge, provenance is carried in `source_url`.
--
-- Deferred: document_chunks (per-chunk vectors for long content); synthesis
-- operation (documents → knowledge with source_url provenance); other corpus
-- adapters (arXiv, Jira, Confluence, runbooks); load_source_adapter() factory
-- + entry-point registration; CLI commands for library management.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    meta TEXT,
    owned_by TEXT NOT NULL DEFAULT 'librarian',
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES documents(id)
);
-- Non-unique index on source_url for fast dedup lookups. Uniqueness of the
-- active row per source_url is enforced programmatically in store_document()
-- (SELECT + INSERT in the same transaction) rather than via a partial unique
-- index, because a partial unique index would collide with the FK constraint
-- during the supersession swap (both rows temporarily have superseded_by IS NULL
-- at statement time before the UPDATE fires). See documents.py: store_document.
CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents(source_url);
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_superseded ON documents(superseded_by);

-- FTS5 index for documents — content + title, porter stemming.
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    content, title, content=documents, content_rowid=id,
    tokenize='porter unicode61'
);
"""

# Triggers to keep FTS5 indexes in sync with content tables.
FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS threads_ai AFTER INSERT ON threads BEGIN
    INSERT INTO threads_fts(rowid, label, summary, resume_points)
    VALUES (NEW.rowid, NEW.label, NEW.summary, NEW.resume_points);
END;
CREATE TRIGGER IF NOT EXISTS threads_ad AFTER DELETE ON threads BEGIN
    INSERT INTO threads_fts(threads_fts, rowid, label, summary, resume_points)
    VALUES ('delete', OLD.rowid, OLD.label, OLD.summary, OLD.resume_points);
END;
CREATE TRIGGER IF NOT EXISTS threads_au AFTER UPDATE ON threads BEGIN
    INSERT INTO threads_fts(threads_fts, rowid, label, summary, resume_points)
    VALUES ('delete', OLD.rowid, OLD.label, OLD.summary, OLD.resume_points);
    INSERT INTO threads_fts(rowid, label, summary, resume_points)
    VALUES (NEW.rowid, NEW.label, NEW.summary, NEW.resume_points);
END;

CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, content, tags, kind)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''), COALESCE(NEW.kind, ''));
END;
CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, tags, kind)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''), COALESCE(OLD.kind, ''));
END;
CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, tags, kind)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''), COALESCE(OLD.kind, ''));
    INSERT INTO observations_fts(rowid, content, tags, kind)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''), COALESCE(NEW.kind, ''));
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, content, tags)
    VALUES (NEW.id, NEW.content, NEW.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, OLD.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, OLD.tags);
    INSERT INTO knowledge_fts(rowid, content, tags)
    VALUES (NEW.id, NEW.content, NEW.tags);
END;

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, content, title)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.title, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, content, title)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.title, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, content, title)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.title, ''));
    INSERT INTO documents_fts(rowid, content, title)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.title, ''));
END;
"""


def _trigger_statements() -> list[tuple[str, str]]:
    """Parse FTS_TRIGGERS_SQL and return (trigger_name, create_sql) pairs.

    Each statement spans from ``CREATE TRIGGER IF NOT EXISTS <name>`` through
    its closing ``END;``.  Multi-line bodies are handled by scanning for the
    ``END;`` terminator rather than relying on single-line detection.
    """
    # Each trigger block: CREATE TRIGGER ... END;
    pattern = re.compile(
        r"(CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+(\w+).+?END\s*;)",
        re.IGNORECASE | re.DOTALL,
    )
    return [(m.group(2), m.group(1).strip()) for m in pattern.finditer(FTS_TRIGGERS_SQL)]


def _trigger_source_table(trigger_name: str) -> str | None:
    """Return the content-table name that trigger_name fires on.

    Derived by extracting ``AFTER {INSERT|UPDATE|DELETE} ON <table>`` from
    the trigger SQL. Returns None if the table can't be determined (defensive
    fallback — caller will attempt the CREATE and let SQLite surface the error).
    """
    for name, create_sql in _trigger_statements():
        if name != trigger_name:
            continue
        m = re.search(r"AFTER\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+(\w+)", create_sql, re.IGNORECASE)
        return m.group(1) if m else None
    return None


def _reconcile_triggers(conn: sqlite3.Connection) -> None:
    """Create any triggers present in FTS_TRIGGERS_SQL but absent from the live DB.

    Uses ``CREATE TRIGGER IF NOT EXISTS`` so double-execution is harmless, but
    we still diff against sqlite_master first to avoid unnecessary work and to
    emit accurate log lines.

    Skips triggers whose source table does not yet exist (e.g. ``documents_ai``
    when ``documents`` hasn't been created yet on an older DB). Those triggers
    will be installed on the next connect() after ``_reconcile_tables()`` adds
    the source table.
    """
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for name, create_sql in _trigger_statements():
        if name in existing:
            continue
        src = _trigger_source_table(name)
        if src is not None and src not in existing_tables:
            # Source table absent — skip; _reconcile_tables() will create it,
            # then the next reconcile pass installs the trigger.
            continue
        conn.executescript(create_sql)
        conn.commit()
        print(f"[techne] reconcile: CREATE TRIGGER {name}", file=sys.stderr)


_FTS_BACKFILL: dict[str, tuple[str, list[str]]] = {
    # fts_table: (source_table, [columns to SELECT in (rowid, col…) order])
    "observations_fts": (
        "observations",
        ["id", "content", "COALESCE(tags, '')", "COALESCE(kind, '')"],
    ),
    "threads_fts": ("threads", ["rowid", "label", "summary", "resume_points"]),
    "knowledge_fts": ("knowledge", ["id", "content", "COALESCE(tags, '')"]),
    "documents_fts": ("documents", ["id", "content", "COALESCE(title, '')"]),
}

_FTS_INSERT_COLS: dict[str, list[str]] = {
    "observations_fts": ["rowid", "content", "tags", "kind"],
    "threads_fts": ["rowid", "label", "summary", "resume_points"],
    "knowledge_fts": ["rowid", "content", "tags"],
    "documents_fts": ["rowid", "content", "title"],
}


def _reconcile_fts_backfill(conn: sqlite3.Connection) -> None:
    """Backfill any FTS table whose index is empty while the source table has rows.

    Uses the FTS5 shadow ``_docsize`` table to detect an empty index; content-
    table FTS5 VTs report COUNT(*) from the source table even when the index
    has never been populated, so COUNT(*) is not a reliable signal here.

    Safe to re-run: once FTS _docsize is non-empty, subsequent calls skip.
    Called after _reconcile_triggers() so triggers exist for future inserts.
    """
    for fts_table, (src_table, src_cols) in _FTS_BACKFILL.items():
        # _docsize has one row per indexed document; 0 means the index is empty.
        fts_indexed = conn.execute(f"SELECT COUNT(*) FROM {fts_table}_docsize").fetchone()[0]
        if fts_indexed > 0:
            continue
        src_count = conn.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()[0]
        if src_count == 0:
            continue
        insert_cols = ", ".join(_FTS_INSERT_COLS[fts_table])
        select_cols = ", ".join(src_cols)
        conn.execute(
            f"INSERT INTO {fts_table}({insert_cols}) SELECT {select_cols} FROM {src_table}"
        )
        conn.commit()
        print(
            f"[techne] reconcile: backfill {fts_table} ({src_count} rows from {src_table})",
            file=sys.stderr,
        )


def _schema_table_statements() -> list[tuple[str, str]]:
    """Parse SCHEMA_SQL and return (table_name, create_sql) pairs.

    Each pair includes the CREATE [VIRTUAL] TABLE statement plus any
    immediately-following CREATE INDEX / CREATE UNIQUE INDEX lines that
    belong to that table (i.e., appear before the next CREATE TABLE).

    Virtual tables (FTS5) are included — they match ``CREATE VIRTUAL TABLE``.

    Note: associated triggers (FTS_TRIGGERS_SQL) are NOT included here; they
    live in FTS_TRIGGERS_SQL and are only created on fresh databases.
    Index reconciliation (ensuring indexes exist on upgraded DBs) is out of
    scope for this PR — see follow-up issue.
    """
    # Split SCHEMA_SQL into logical chunks separated by blank lines or new
    # CREATE TABLE / CREATE VIRTUAL TABLE boundaries.  We walk line-by-line,
    # collecting lines that belong to the current table block.
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

    for line in SCHEMA_SQL.splitlines():
        m = table_re.match(line.strip())
        if m:
            flush()
            current_name = m.group(1)
            current_lines = [line]
        elif current_name is not None:
            # Append index lines that follow the current table block.
            if index_re.match(line.strip()) or current_lines:
                # Stop accumulating for this table when we hit a comment that
                # starts a new section (lone '--' separator) only if we already
                # have a complete statement (closing semicolon seen).
                stmt_so_far = "\n".join(current_lines)
                if line.strip().startswith("--") and ";" in stmt_so_far:
                    # A comment after the table is done — belongs to next section.
                    flush()
                    current_name = None
                    current_lines = []
                else:
                    current_lines.append(line)

    flush()
    return results


def _reconcile_tables(conn: sqlite3.Connection) -> None:
    """Create any tables present in SCHEMA_SQL but absent from the live DB.

    Runs BEFORE _reconcile_columns() so newly-created tables don't then need
    a separate column-reconciliation pass for columns they already define.

    Virtual tables (FTS5) are handled alongside regular tables.
    Index reconciliation is out of scope for this PR.
    """
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    for name, create_sql in _schema_table_statements():
        if name not in existing:
            conn.executescript(create_sql)
            conn.commit()
            print(f"[techne] reconcile: CREATE TABLE {name}", file=sys.stderr)


def _schema_columns(table: str) -> set[str]:
    """Parse column names for `table` out of SCHEMA_SQL using regex."""
    # Match the CREATE TABLE block for this table name.
    pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);"
    m = re.search(pattern, SCHEMA_SQL, re.DOTALL)
    if not m:
        return set()
    block = m.group(1)
    cols = set()
    for line in block.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(
            ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES", "--")
        ):
            continue
        col_name = line.split()[0]
        cols.add(col_name)
    return cols


def _reconcile_columns(conn: sqlite3.Connection) -> None:
    """Add any columns present in SCHEMA_SQL but absent from the live table."""
    # Only reconcile regular (non-virtual) tables that appear in SCHEMA_SQL.
    table_pattern = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", re.IGNORECASE)
    for m in table_pattern.finditer(SCHEMA_SQL):
        table = m.group(1)
        live_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not live_cols:
            continue  # table doesn't exist yet; CREATE TABLE IF NOT EXISTS will handle it
        for col in _schema_columns(table) - live_cols:
            block_m = re.search(
                rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);",
                SCHEMA_SQL,
                re.DOTALL,
            )
            col_def = col  # fallback: bare name (SQLite infers NULL affinity)
            if block_m:
                for line in block_m.group(1).splitlines():
                    stripped = line.strip().rstrip(",")
                    parts = stripped.split()
                    if parts and parts[0] == col:
                        col_def = stripped
                        break
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            conn.commit()
            print(f"[techne] reconcile: ALTER TABLE {table} ADD COLUMN {col_def}", file=sys.stderr)


def like_escape(s: str) -> str:
    """Escape `%`, `_`, and `\\` so user input matches literally in a LIKE
    pattern. Pair with `ESCAPE '\\'` on the LIKE clause. Without this, a tag
    or key prefix containing `%` matches anything and one containing `_`
    matches any single character — surprises ranging from harmless wrong
    results to denial-of-service on large tables."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a lock before raising "database is locked". Multiple
    # hooks (post_tool_use, session_end) can fire concurrently when Claude Code
    # streams events, and WAL still serializes writes — without a busy timeout
    # the concurrent writer raises immediately instead of waiting its turn.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_initialized(conn, db_path)
    return conn


def _backup_db(conn: sqlite3.Connection, db_path: Path, from_v: int, to_v: int) -> Path | None:
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


def _guarded_migrate(conn: sqlite3.Connection, db_path: Path, existing_version: int) -> None:
    """Run pending migrations exactly once, even under concurrent processes.

    Claude Code fires session_start + post_tool_use near-simultaneously, so on
    the first open after a code bump several processes can all see a stale
    version and race into the migration body. A cross-process file lock
    serializes them; the loser re-checks the version under the lock and no-ops.
    We back up first and announce on stderr (#78).

    The lock is filesystem-level (not a SQLite transaction) because the
    migration's ``executescript`` calls issue their own COMMITs and can't be
    held inside one outer transaction. Per-machine locking is sufficient —
    each machine has its own ``~/.techne/techne.db``.
    """
    lock_path = db_path.with_suffix(db_path.suffix + ".migrate.lock")

    def _do() -> None:
        # Double-checked: a process we raced may have migrated while we waited.
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row else existing_version
        if current >= SCHEMA_VERSION:
            return
        backup = _backup_db(conn, db_path, current, SCHEMA_VERSION)
        note = f" (backup: {backup})" if backup else ""
        print(f"[techne] migrating schema v{current} → v{SCHEMA_VERSION}{note}", file=sys.stderr)
        _migrate(conn, current, SCHEMA_VERSION)

    if fcntl is None:  # no file locking available — run unguarded (legacy behaviour)
        _do()
        return
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _do()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _ensure_initialized(conn: sqlite3.Connection, db_path: Path) -> None:
    """Initialize the schema if absent. Safe to call repeatedly."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if existing is None:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    else:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        existing_version = row[0] if row else 0
        if existing_version < SCHEMA_VERSION:
            _guarded_migrate(conn, db_path, existing_version)
        _reconcile_tables(conn)
        _reconcile_triggers(conn)
        _reconcile_fts_backfill(conn)
        _reconcile_columns(conn)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize (or open) the techne database. Returns an open connection.

    Schema initialization is now idempotent and happens on every connect().
    This function is kept for backward compat and for the CLI `init` command
    which expects to return a connection.
    """
    return connect(db_path)


def _migrate(conn: sqlite3.Connection, from_version: int, to_version: int):
    """Run incremental migrations. Each branch is gated on both
    ``from_version`` (the migration applies) and ``to_version`` (the caller
    asked to reach at least this version)."""
    if from_version < 2 and to_version >= 2:
        conn.executescript("""
            ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
            ALTER TABLE outbox ADD COLUMN error_message TEXT;

            -- Backfill: synced_at NULL = pending (already default); 'DEAD:...' = dead; else synced
            UPDATE outbox SET status = 'synced'
                WHERE synced_at IS NOT NULL AND synced_at NOT LIKE 'DEAD:%';
            UPDATE outbox SET status = 'dead',
                              error_message = SUBSTR(synced_at, 6),
                              synced_at = NULL
                WHERE synced_at LIKE 'DEAD:%';

            -- Replace the dedup index (old version may rely on synced_at)
            DROP INDEX IF EXISTS idx_outbox_dedup;
            CREATE UNIQUE INDEX idx_outbox_dedup
                ON outbox(content_hash) WHERE status = 'pending';
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    if from_version < 3 and to_version >= 3:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS thread_boundaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                obs_index_a INTEGER NOT NULL,
                obs_index_b INTEGER NOT NULL,
                gap_seconds REAL NOT NULL,
                family_a TEXT NOT NULL,
                family_b TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                is_boundary INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_thread_boundaries_session
                ON thread_boundaries(session_id, obs_index_a);
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    if from_version < 4 and to_version >= 4:
        # Split sessions.summary into typed columns by provenance. Existing
        # `summary` content is left in place (it predates the split and may be
        # manual, LLM-derived, or heuristic — undifferentiable now), and the
        # effective_summary() read rule treats it as the highest-priority
        # "manual" slot. New writers route to the typed columns instead.
        conn.executescript("""
            ALTER TABLE sessions ADD COLUMN summary_llm TEXT;
            ALTER TABLE sessions ADD COLUMN summary_heuristic TEXT;
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    if from_version < 5 and to_version >= 5:
        # Add numeric confidence + last_confirmed_at so recall can decay
        # entries that haven't been re-confirmed. Legacy text `confidence`
        # stays for display + backward compat.
        conn.executescript("""
            ALTER TABLE knowledge ADD COLUMN confidence_numeric REAL NOT NULL DEFAULT 0.6;
            ALTER TABLE knowledge ADD COLUMN last_confirmed_at TIMESTAMP;

            -- Backfill confidence_numeric from the legacy enum
            UPDATE knowledge SET confidence_numeric = 0.9 WHERE confidence = 'high';
            UPDATE knowledge SET confidence_numeric = 0.6 WHERE confidence = 'medium';
            UPDATE knowledge SET confidence_numeric = 0.3 WHERE confidence = 'low';

            -- last_confirmed_at defaults to created_at for existing rows
            UPDATE knowledge SET last_confirmed_at = created_at WHERE last_confirmed_at IS NULL;
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")
    if from_version < 6 and to_version >= 6:
        # Reflection-pass output: candidate knowledge / identity entries the
        # end-of-session reflect pass proposes. Separate from `knowledge` so
        # the live store is untouched while the feature is under evaluation.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                confidence TEXT NOT NULL DEFAULT 'medium',
                source_session_id TEXT NOT NULL REFERENCES sessions(id),
                source_thread_id TEXT REFERENCES threads(id),
                model_used TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                raw_response TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                promoted_to_id INTEGER,
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_candidates(status);
            CREATE INDEX IF NOT EXISTS idx_kc_session ON knowledge_candidates(source_session_id);
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (6)")
    if from_version < 7 and to_version >= 7:
        # Add inferred tag + kind columns to observations.
        # tags: whitespace-separated token list from embedding-based inference.
        # kind: highest-scoring kind label (decision-shaped / fact-shaped /
        #        question-shaped / observation-shaped).
        conn.executescript("""
            ALTER TABLE observations ADD COLUMN tags TEXT;
            ALTER TABLE observations ADD COLUMN kind TEXT;
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (7)")
    if from_version < 8 and to_version >= 8:
        # Rebuild observations_fts to index tags + kind (Gap 1 fix: #50).
        # Virtual table schema changes require DROP + CREATE + reindex; the old
        # single-column definition is replaced by (content, tags, kind).
        # Guard: observations table may not exist yet if this DB is being
        # migrated from a schema that never had it (_reconcile_tables runs after
        # _migrate). In that case the DROP+CREATE is still safe; INSERT is
        # skipped — _reconcile_fts_backfill handles it once tables exist.
        obs_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()[0]
        conn.executescript("""
            DROP TABLE IF EXISTS observations_fts;
            CREATE VIRTUAL TABLE observations_fts USING fts5(
                content, tags, kind, content=observations, content_rowid=id
            );
        """)
        if obs_exists:
            conn.execute(
                "INSERT INTO observations_fts(rowid, content, tags, kind) "
                "SELECT id, content, COALESCE(tags, ''), COALESCE(kind, '') FROM observations"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (8)")
    if from_version < 9 and to_version >= 9:
        # Recreate all FTS tables with Porter stemming (#77). Changing the
        # tokenizer requires DROP + CREATE + reindex (same constraint as the v8
        # rebuild). Each rebuild is guarded on its source table existing; when
        # absent the CREATE still lands and _reconcile_fts_backfill repopulates
        # once the source table appears (tables are reconciled after _migrate).
        _fts_rebuild = {
            "threads_fts": (
                "label, summary, resume_points, content=threads, content_rowid=rowid",
                "INSERT INTO threads_fts(rowid, label, summary, resume_points) "
                "SELECT rowid, label, summary, resume_points FROM threads",
                "threads",
            ),
            "observations_fts": (
                "content, tags, kind, content=observations, content_rowid=id",
                "INSERT INTO observations_fts(rowid, content, tags, kind) "
                "SELECT id, content, COALESCE(tags, ''), COALESCE(kind, '') FROM observations",
                "observations",
            ),
            "knowledge_fts": (
                "content, tags, content=knowledge, content_rowid=id",
                "INSERT INTO knowledge_fts(rowid, content, tags) "
                "SELECT id, content, COALESCE(tags, '') FROM knowledge",
                "knowledge",
            ),
        }
        for fts_table, (cols, insert_sql, src) in _fts_rebuild.items():
            src_exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (src,),
            ).fetchone()[0]
            conn.executescript(
                f"DROP TABLE IF EXISTS {fts_table};\n"
                f"CREATE VIRTUAL TABLE {fts_table} USING fts5(\n"
                f"    {cols},\n"
                f"    tokenize='porter unicode61'\n"
                f");"
            )
            if src_exists:
                conn.execute(insert_sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (9)")
    conn.commit()
