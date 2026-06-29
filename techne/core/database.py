"""SQLite database schema and connection management for techne."""

import sqlite3
from pathlib import Path

from techne.core import migrator

SCHEMA_VERSION = 12

# Schema evolution convention — when to bump SCHEMA_VERSION vs. edit SCHEMA_SQL only:
# See CONTRIBUTING.md for the full decision tree and real examples.
#
# Short version:
#   _migrate()        — data backfill, destructive change (DROP/rebuild), or a migration
#                       step that must assume a prior structural precondition. Bump
#                       SCHEMA_VERSION; update SCHEMA_SQL to match post-migration state.
#   SCHEMA_SQL only   — purely additive: new nullable column, new table, new index, a
#                       brand-NEW trigger. The reconciler (_reconcile_tables /
#                       _reconcile_columns / _reconcile_triggers) diffs SCHEMA_SQL against
#                       the live DB on every connect and applies the delta automatically.
#                       POST-#140 CAVEAT: connect()-time reconcile is sentinel-gated and
#                       skips already-reconciled DBs. An additive SCHEMA_SQL change at an
#                       UNCHANGED SCHEMA_VERSION will NOT reach already-reconciled databases.
#                       Use `techne reconcile` to force re-run, or bump SCHEMA_VERSION.
#   CHANGING a trigger BODY — NOT additive. _reconcile_triggers is name-only (so it can't
#                       clobber a user-modified trigger), so an edited body never reaches
#                       existing DBs. Edit FTS_TRIGGERS_SQL *and* ship a numbered _migrate
#                       step that DROPs + re-CREATEs the trigger (see step 12, techne#169).

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
    -- Dependency-driven staleness (techne#117). `current` is the default. A
    -- reconcile-time cascade flips a row to `uncertain` when a fact it depends on
    -- (via knowledge_deps) is superseded — "stop trusting this until reconfirmed".
    -- Orthogonal to superseded_by (direct replacement). Recall annotates/down-ranks
    -- `uncertain`, sharing the #90 staleness gate. reconfirm() clears it.
    status TEXT NOT NULL DEFAULT 'current',
    event_at TIMESTAMP,
    -- Evidence anchor (techne#90): a memory that points into a repo carries the
    -- file path (+ optional symbol after "::"), the sha256 of the file's bytes
    -- at write time, and the git HEAD sha then. All NULL for non-anchored
    -- (behavioral/relational) memory, which is the majority — those rows are
    -- never staleness-checked. See core/anchor.py for classification at recall.
    anchor_path TEXT,
    anchor_symbol TEXT,
    content_hash TEXT,
    anchor_git_sha TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by);

-- Dependency edges between knowledge entries (techne#117). A junction table, NOT
-- a graph engine. `dependent_id` is derived from / depends on `trigger_id` — when
-- the trigger is superseded, the cascade marks the dependent `uncertain`. Edges are
-- written explicitly (add_dependency) at extraction/reconcile time against the
-- CURRENT revision (they do not auto-follow a supersession chain), never inferred
-- on the hot ingest path. idx on trigger_id powers the cascade lookup.
-- (No semicolons in this comment — the SCHEMA_SQL parser's stop heuristic, #56.)
CREATE TABLE IF NOT EXISTS knowledge_deps (
    dependent_id INTEGER NOT NULL REFERENCES knowledge(id),
    trigger_id   INTEGER NOT NULL REFERENCES knowledge(id),
    kind         TEXT NOT NULL DEFAULT 'derived_from',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dependent_id, trigger_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_deps_trigger ON knowledge_deps(trigger_id);

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
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Sleep/dream consolidation (techne#108). Additive (reconciled, no migration).
    -- action: what promote does with this proposal — one of the reflect._APPLIERS
    -- dispatch keys (add | supersede | merge), which is the single source of truth.
    -- An unknown action raises at promote-time in promote_candidate(). Deliberately
    -- NOT a SQL CHECK: that would duplicate the _APPLIERS key list in the schema (a
    -- second source of truth to drift) and shift enforcement off the dispatch.
    -- target_ids: JSON array of the knowledge.id rows it acts on (NULL for plain
    -- adds). origin: which pass made it.
    action TEXT NOT NULL DEFAULT 'add',
    target_ids TEXT,
    origin TEXT NOT NULL DEFAULT 'reflect',     -- reflect | consolidate | dream
    -- Decision-provenance: who/what reviewed and why (free-text, not a DB enum).
    decided_by TEXT,
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_candidates(status);
CREATE INDEX IF NOT EXISTS idx_kc_session ON knowledge_candidates(source_session_id);
CREATE INDEX IF NOT EXISTS idx_kc_action_status ON knowledge_candidates(action, status);

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

-- Curated memory drain (techne#107 PR2) — persisted mirror of CC's
-- ~/.claude/projects/<slug>/memory/*.md files.  Content-hash deduped and
-- superseding on change (the same 3-outcome dance as knowledge/documents).
-- Queried by PR3 claim-gate via live_curated_memory().
-- (No semicolons in this comment -- the SCHEMA_SQL parser stop heuristic, #56.)
CREATE TABLE IF NOT EXISTS curated_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    slug TEXT,
    title TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'disk_walk',
    session_id TEXT,
    meta TEXT,
    superseded_by INTEGER REFERENCES curated_memory(id),
    captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_curated_memory_path ON curated_memory(source_path);
CREATE INDEX IF NOT EXISTS idx_curated_memory_slug ON curated_memory(slug);
CREATE INDEX IF NOT EXISTS idx_curated_memory_superseded ON curated_memory(superseded_by);

-- NOTE: the `documents` domain moved out of core to its own db (librarian.db)
-- under db-per-domain (techne#99). Its schema now lives in core/librarian.py
-- (LIBRARIAN_SCHEMA), applied via the store registry -- NOT here. techne.db is
-- personal memory only.
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

-- knowledge_* use COALESCE(tags,'') to match the value the backfill/rebuild paths
-- index ('' — see step 9) and to stay consistent with observations_* above. This
-- honours the FTS5 external-content contract (the 'delete' command should pass the
-- same values that were indexed). In practice NULL and '' tokenize identically for
-- this column, so the prior bare NEW.tags/OLD.tags caused no demonstrable corruption
-- — this is consistency/contract hygiene, not a fix for a reproduced bug (techne#169).
-- NOTE: trigger-BODY changes here do NOT reach existing DBs — reconcile_triggers is
-- name-only (intentional, so it can't clobber a user-modified trigger). A body change
-- MUST also ship a numbered migration step that DROPs + re-CREATEs the trigger (step 12).
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, content, tags)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''));
    INSERT INTO knowledge_fts(rowid, content, tags)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''));
END;
-- documents_* triggers moved to core/librarian.py (LIBRARIAN_SCHEMA) — the
-- documents domain lives in librarian.db now (techne#99).
"""


def _reconcile_triggers(conn: sqlite3.Connection) -> None:
    """Core shim → migrator.reconcile_triggers against FTS_TRIGGERS_SQL (techne#136)."""
    migrator.reconcile_triggers(conn, FTS_TRIGGERS_SQL)


_FTS_BACKFILL: dict[str, tuple[str, list[str]]] = {
    # fts_table: (source_table, [columns to SELECT in (rowid, col…) order])
    "observations_fts": (
        "observations",
        ["id", "content", "COALESCE(tags, '')", "COALESCE(kind, '')"],
    ),
    "threads_fts": ("threads", ["rowid", "label", "summary", "resume_points"]),
    "knowledge_fts": ("knowledge", ["id", "content", "COALESCE(tags, '')"]),
}

_FTS_INSERT_COLS: dict[str, list[str]] = {
    "observations_fts": ["rowid", "content", "tags", "kind"],
    "threads_fts": ["rowid", "label", "summary", "resume_points"],
    "knowledge_fts": ["rowid", "content", "tags"],
}


def _reconcile_fts_backfill(conn: sqlite3.Connection) -> None:
    """Core shim → migrator.reconcile_fts_backfill (techne#136)."""
    migrator.reconcile_fts_backfill(conn, _FTS_BACKFILL, _FTS_INSERT_COLS)


def _reconcile_tables(conn: sqlite3.Connection) -> None:
    """Core shim → migrator.reconcile_tables against SCHEMA_SQL (techne#136)."""
    migrator.reconcile_tables(conn, SCHEMA_SQL)


def _strip_sql_comment(line: str) -> str:
    """Core shim → migrator._strip_sql_comment (techne#136; kept for test import)."""
    return migrator._strip_sql_comment(line)


def _reconcile_columns(conn: sqlite3.Connection) -> None:
    """Core shim → migrator.reconcile_columns against SCHEMA_SQL (techne#136)."""
    migrator.reconcile_columns(conn, SCHEMA_SQL)


def _reconcile_indexes(conn: sqlite3.Connection) -> None:
    """Core shim → migrator.reconcile_indexes against SCHEMA_SQL (techne#138)."""
    migrator.reconcile_indexes(conn, SCHEMA_SQL)


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


def _guarded_migrate(conn: sqlite3.Connection, db_path: Path, existing_version: int) -> None:
    """Core shim → migrator.guarded_migrate with the `schema_version` table
    accessor (techne#136). `run_steps=_migrate` owns version-writing (it inserts a
    schema_version row per step), and the engine's in-lock double-check makes a
    racing loser a true no-op."""

    def _read(c: sqlite3.Connection) -> int:
        # Literal move of the old double-check read (MAX(version) always returns a
        # row, so this is row[0]); existing_version is the documented fallback.
        row = c.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row else existing_version

    migrator.guarded_migrate(
        conn,
        db_path,
        target_version=SCHEMA_VERSION,
        read_version=_read,
        run_steps=_migrate,
        label="schema",
    )


# Sentinel recording the SCHEMA_VERSION at which the reconcile passes last ran for
# THIS db (techne#140). The reconcile passes are an idempotent drift safety-net, but
# they run inside connect() — i.e. on the hot capture path — and include COUNT(*)
# probes over the FTS shadow tables. Running them unconditionally on every connect is
# wasteful once a db is already current+reconciled. We stamp this sentinel after a
# reconcile and skip the passes while it matches SCHEMA_VERSION. Reconcile still runs
# (a) after any migration and (b) once for any db that reaches the current version
# without a matching stamp (e.g. a pre-sentinel db). For suspected drift on an
# already-stamped db, `techne reconcile` (force_reconcile) re-runs the passes
# unconditionally — the operator escape hatch now that the every-connect self-heal
# is gated.
_RECONCILE_SENTINEL = "reconcile:schema_version"


def _reconciled_version(conn: sqlite3.Connection) -> int:
    from techne.core.extension_state import get_state

    try:
        st = get_state(conn, _RECONCILE_SENTINEL)
    except sqlite3.OperationalError:
        # extension_state not present yet (a db older than that table) → treat as
        # un-reconciled so the passes run and create + stamp it.
        return 0
    return st.get("version", 0) if st else 0


def _run_reconcile_passes(conn: sqlite3.Connection) -> None:
    """Run every drift-reconcile pass, then stamp the sentinel at SCHEMA_VERSION."""
    from techne.core.extension_state import set_state

    _reconcile_tables(conn)
    _reconcile_triggers(conn)
    _reconcile_fts_backfill(conn)
    _reconcile_columns(conn)
    _reconcile_indexes(conn)  # last: an index may reference a just-added column (#138)
    set_state(conn, _RECONCILE_SENTINEL, {"version": SCHEMA_VERSION})
    conn.commit()


def force_reconcile(db_path: Path) -> None:
    """Force the drift-reconcile passes on *db_path*, bypassing the sentinel (techne#140).

    The connect()-time passes are gated to skip an already-stamped, current-version db
    (the hot-path optimisation). This is the operator escape hatch: re-run all passes
    unconditionally to repair suspected schema drift (a dropped index/trigger/column,
    FTS desync) on a db that would otherwise be skipped. Re-stamps the sentinel.
    """
    conn = connect(db_path)
    try:
        _run_reconcile_passes(conn)
    finally:
        conn.close()


def _ensure_initialized(conn: sqlite3.Connection, db_path: Path) -> None:
    """Initialize the schema if absent. Safe to call repeatedly."""
    from techne.core.extension_state import set_state

    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if existing is None:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        # A freshly-created schema is correct by construction — no reconcile needed;
        # stamp the sentinel so the next connect skips the passes (techne#140).
        set_state(conn, _RECONCILE_SENTINEL, {"version": SCHEMA_VERSION})
        conn.commit()
    else:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        existing_version = row[0] if row else 0
        if existing_version < SCHEMA_VERSION:
            # Schema is changing — reconcile is mandatory afterwards.
            _guarded_migrate(conn, db_path, existing_version)
            _run_reconcile_passes(conn)
        elif _reconciled_version(conn) != SCHEMA_VERSION:
            # Already at the current version but not yet reconciled at it (a pre-sentinel
            # db, or one that reached this version another way) — reconcile once, stamp.
            _run_reconcile_passes(conn)
        # else: current and already reconciled at SCHEMA_VERSION → skip the hot-path passes.


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize (or open) the techne database. Returns an open connection.

    Schema initialization is now idempotent and happens on every connect().
    This function is kept for backward compat and for the CLI `init` command
    which expects to return a connection.
    """
    return connect(db_path)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN`` (techne#169).

    ``executescript`` runs each DDL statement in autocommit, so the ALTER is
    committed independently of the per-step ``INSERT INTO schema_version`` (which
    only commits at the end of ``_migrate``). A crash in that window leaves the
    column present but the version unrecorded → the next startup re-runs the step
    and dies permanently with "duplicate column name". Guarding on
    ``PRAGMA table_info`` makes the add a safe no-op on replay. (Matches the
    guard step 11 already used.)
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection, from_version: int, to_version: int):
    """Run incremental migrations. Each branch is gated on both
    ``from_version`` (the migration applies) and ``to_version`` (the caller
    asked to reach at least this version)."""
    if from_version < 2 and to_version >= 2:
        _add_column_if_missing(conn, "outbox", "status", "TEXT NOT NULL DEFAULT 'pending'")
        _add_column_if_missing(conn, "outbox", "error_message", "TEXT")
        conn.executescript("""
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
        _add_column_if_missing(conn, "sessions", "summary_llm", "TEXT")
        _add_column_if_missing(conn, "sessions", "summary_heuristic", "TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    if from_version < 5 and to_version >= 5:
        # Add numeric confidence + last_confirmed_at so recall can decay
        # entries that haven't been re-confirmed. Legacy text `confidence`
        # stays for display + backward compat.
        _add_column_if_missing(conn, "knowledge", "confidence_numeric", "REAL NOT NULL DEFAULT 0.6")
        _add_column_if_missing(conn, "knowledge", "last_confirmed_at", "TIMESTAMP")
        conn.executescript("""
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
        _add_column_if_missing(conn, "observations", "tags", "TEXT")
        _add_column_if_missing(conn, "observations", "kind", "TEXT")
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
    if from_version < 10 and to_version >= 10:
        # Rewrite bare (un-namespaced) pending outbox targets to the sync: lane.
        # Pre-#51 rows used bare names like "thread_summary"; after #51 both
        # flush paths filter by prefix and such rows are permanently undrainable.
        # Only rewrite `pending` rows — `synced`/`dead` are terminal and never
        # re-read by any flush path, so touching them is unnecessary churn.
        # Already-namespaced rows (sync:* or sink:*) are left alone.
        # Guard: in some migration paths (e.g. reconcile from a pre-outbox
        # schema) the outbox table is created later by schema reconciliation,
        # so it may not exist yet here — and an absent table has no rows to
        # migrate. Skip the UPDATE in that case; the version bump still applies.
        outbox_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'"
        ).fetchone()
        if outbox_exists:
            conn.execute("""
                UPDATE outbox
                   SET target = 'sync:' || target
                 WHERE status = 'pending'
                   AND target NOT LIKE 'sync:%'
                   AND target NOT LIKE 'sink:%'
            """)
        conn.execute("INSERT INTO schema_version (version) VALUES (10)")
    if from_version < 11 and to_version >= 11:
        # Evidence-anchored memory (techne#90): add the nullable anchor columns
        # so a memory pointing into a repo can store its file path/symbol plus a
        # content hash + git HEAD sha at write time. All adds are purely additive
        # and NULL-defaulting — non-anchored rows are unaffected and never
        # staleness-checked. Guard on the knowledge table existing (some migration
        # paths reconcile it later); the version bump still applies if absent.
        knowledge_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
        ).fetchone()
        if knowledge_exists:
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
            for col in ("anchor_path", "anchor_symbol", "content_hash", "anchor_git_sha"):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE knowledge ADD COLUMN {col} TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (11)")
    if from_version < 12 and to_version >= 12:
        # Bring the knowledge_ai/ad/au FTS triggers into line with observations_*
        # (and the backfill/rebuild paths): COALESCE(tags, '') instead of bare
        # NEW.tags/OLD.tags. This honours the FTS5 external-content contract — the
        # 'delete' command's values should match what was indexed — where the backfill
        # indexes '' but the old trigger passed NULL. (techne#169.)
        #
        # HONESTY: I could NOT reproduce actual corruption from the mismatch — for the
        # `tags` column NULL and '' both tokenize to zero tokens, so FTS5 treats them
        # identically and the index stays consistent in every case I tried. So this is
        # a consistency / contract-compliance / future-proofing fix, NOT a repair for a
        # demonstrated live bug — hence no index rebuild, just the trigger redeploy.
        #
        # The redeploy is still required: reconcile_triggers is name-only, so editing
        # FTS_TRIGGERS_SQL alone never reaches an existing DB — DROP + CREATE here.
        knowledge_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
        ).fetchone()
        if knowledge_exists:
            conn.executescript("""
                DROP TRIGGER IF EXISTS knowledge_ai;
                DROP TRIGGER IF EXISTS knowledge_ad;
                DROP TRIGGER IF EXISTS knowledge_au;
                CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
                    INSERT INTO knowledge_fts(rowid, content, tags)
                    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''));
                END;
                CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
                    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''));
                END;
                CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags)
                    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''));
                    INSERT INTO knowledge_fts(rowid, content, tags)
                    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''));
                END;
            """)
        conn.execute("INSERT INTO schema_version (version) VALUES (12)")
    conn.commit()
