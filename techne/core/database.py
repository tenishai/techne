"""SQLite database schema and connection management for techne."""

import re
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 7

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
-- event_at: actual real-world event timestamp (from transcript record). NULL means
--           treat `timestamp` (ingestion time) as the event time.
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

-- FTS5 indexes
CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5(
    label, summary, resume_points, content=threads, content_rowid=rowid
);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    content, content=observations, content_rowid=id
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, tags, content=knowledge, content_rowid=id
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
    INSERT INTO observations_fts(rowid, content)
    VALUES (NEW.id, NEW.content);
END;
CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content)
    VALUES ('delete', OLD.id, OLD.content);
END;
CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content)
    VALUES ('delete', OLD.id, OLD.content);
    INSERT INTO observations_fts(rowid, content)
    VALUES (NEW.id, NEW.content);
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
"""


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
    _ensure_initialized(conn)
    return conn


def _ensure_initialized(conn: sqlite3.Connection) -> None:
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
            _migrate(conn, existing_version, SCHEMA_VERSION)
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
    conn.commit()
