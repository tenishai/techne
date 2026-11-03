"""SQLite database schema and connection management for techne."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from techne.core import migrator

SCHEMA_VERSION = 30

# THE ONE ON-DISK TIMESTAMP SHAPE (#515). Every TEXT timestamp column is compared
# LEXICALLY by SQLite, so the storage format IS the ordering primitive — a second
# format does not merely look untidy, it makes every range query silently wrong.
#
# This matches what SQLite's own CURRENT_TIMESTAMP default produces, which is why
# it is the target rather than ISO: the majority of rows already carry it and the
# column defaults to it, so normalising the other direction would mean rewriting
# the default and every row that used it.
#
# UTC, like CURRENT_TIMESTAMP. Verified on the operator's store rather than
# assumed: `date -u`, CURRENT_TIMESTAMP and the JSONL's Z-suffixed values all
# agreed. Had they NOT agreed, normalising the FORMAT alone would have baked a
# timezone skew into 18,327 rows while looking like a clean fix.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# VERIFIED AGAINST THE LIVE STORE, all 53 tables, 2026-08-08 — stated as an
# inventory rather than an unconditional invariant, because the unconditional
# version was FALSE when written: outbox.synced_at was 100% isoformat.
#
#   REPAIRED HERE (step 23): observations.timestamp / .event_at,
#     sessions.started_at / .ended_at, outbox.synced_at
#   ALREADY CONFORMING (CURRENT_TIMESTAMP-defaulted or written by the house
#     strftime helper, so conforming by construction): knowledge.created_at /
#     .updated_at / .last_confirmed_at / .event_at, threads.created_at /
#     .updated_at, outbox.created_at, coord_messages.created_at,
#     coord_reads.read_at, thread_boundaries, knowledge_candidates,
#     knowledge_deps.created_at, curated_memory (created_at / updated_at /
#     last_seen_at), reminders, identity.updated_at, checkpoints,
#     schema_version.applied_at
#   NOT IN SCHEMA_SQL AT ALL: context_injections is created lazily by the hook's
#     own _ensure_columns, so it has no CREATE TABLE here. An earlier version of
#     this list named it as verified-conforming, which read as a claim about a
#     table this file does not define.
#
# A NEW TIMESTAMP COLUMN BELONGS IN ONE OF THOSE TWO LISTS. Writing it with
# CURRENT_TIMESTAMP or `strftime(TIMESTAMP_FORMAT)` puts it in the second for
# free; `datetime.isoformat()` puts it in neither and is how synced_at drifted.


# The columns the transcript importers write a JSONL timestamp into, and so the
# only ones that can carry the ISO shape. MEASURED on the operator's store rather
# than guessed at — the issue speculated that knowledge and threads might share
# the split, and they do NOT: knowledge.created_at/updated_at (834 rows each),
# threads.created_at (1,650), outbox.created_at (3,544) and
# coord_messages.created_at (153) are uniformly space-separated, because every
# one of them is written by CURRENT_TIMESTAMP or by the house
# `strftime("%Y-%m-%d %H:%M:%S")` helper. Widening this map on suspicion would
# make the migration scan tables it has no reason to touch.
_TIMESTAMP_BACKFILL_COLUMNS = {
    "observations": ("timestamp", "event_at"),
    "sessions": ("started_at", "ended_at"),
    # 100% non-conforming (3,907 of 3,907), written by `datetime.isoformat()` at
    # three sites in sync/outbox.py — now fixed to use TIMESTAMP_FORMAT. It was
    # not WRONG (internally uniform, and every reader goes through
    # `checks._parse_ts` rather than a lexical compare), but the module comment
    # above declares "the one on-disk timestamp shape", and a column that is
    # entirely non-conforming makes that sentence false. Making the claim true
    # beats documenting an exception to it.
    "outbox": ("synced_at",),
}


def backfill_timestamp_formats(conn) -> int:
    """Rewrite non-conforming timestamps in place (#515 step 23).

    Returns the number of rows changed, across every table and column.

    SQL rather than a Python round trip, deliberately: this runs over the whole
    ledger (21k rows on the operator's store, unbounded in general) and the
    shapes are fixed and known.

    IDEMPOTENT, and testable as such: a normalised value has no 'T' at position
    11, so the WHERE clause stops matching after the first pass. That is also the
    safety — a value that is NOT `YYYY-MM-DDTHH:MM:SS[.fffZ]` is never matched,
    so an unrecognised shape survives intact rather than being mangled by a blind
    `substr()`.

    `sessions` is included because the same importer derives a session's bounds
    from the same JSONL records (4 `started_at` and 2 `ended_at` rows carried the
    ISO shape). It matters beyond tidiness: #516 resolves a moment to a session
    with `started_at <= T AND (ended_at IS NULL OR ended_at >= T)`, which is the
    lexical comparison this bug breaks.
    """
    changed = 0
    for table, columns in _TIMESTAMP_BACKFILL_COLUMNS.items():
        # A MIGRATION STEP CANNOT ASSUME AN ADDITIVE COLUMN EXISTS YET. `event_at`
        # is supplied by SCHEMA_SQL via the reconciler, which runs AFTER _migrate
        # — so on a database migrating up from an old version (the v3 fixtures do
        # exactly this) the column is genuinely absent here and a bare UPDATE
        # raises "no such column". Intersect with what is actually present rather
        # than ordering-by-luck. See the convention note at the top of this module.
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in present:
                continue
            cur = conn.execute(
                f"""UPDATE {table}
                       SET {column} = strftime('%Y-%m-%d %H:%M:%S', {column})
                     WHERE {column} IS NOT NULL
                       AND substr({column}, 11, 1) = 'T'
                       -- strftime CONVERTS an offset to UTC; it does not truncate.
                       -- An earlier version used substr(...,1,19)+replace and
                       -- therefore had to REFUSE offset-bearing rows, on the
                       -- premise that SQL could not convert them. False: strftime
                       -- agrees with normalize_timestamp() on every shape THIS
                       -- MIGRATION TOUCHES, including -04:00 and +05:30.
                       -- NOT a universal, and the counterexample is reachable:
                       -- '2026-08-08T24:00:00' — Python rolls it to 08-09
                       -- 00:00:00, SQLite leaves it as 08-08 24:00:00. Neither
                       -- writer emits hour 24, so nothing diverges today; the
                       -- claim is narrowed rather than left as a universal that
                       -- a future writer could falsify. The refusal was machinery
                       -- guarding a limitation that did not exist, and it was
                       -- silent AND permanent — _migrate discards the return
                       -- value and can never re-run, so a refused row stayed
                       -- non-conforming forever in a column whose whole purpose
                       -- is lexical ordering.
                       -- THIS GUARD IS LOAD-BEARING: strftime returns NULL on an
                       -- unparseable value ('not a timestamp', '2026-13-45T…'),
                       -- so without it the UPDATE would NULL those rows out —
                       -- worse than leaving them alone. With it, "an unrecognised
                       -- shape survives intact" still holds.
                       AND strftime('%Y-%m-%d %H:%M:%S', {column}) IS NOT NULL"""
            )
            changed += cur.rowcount
    return changed


def normalize_timestamp(value):
    """Coerce any timestamp we accept from outside into `TIMESTAMP_FORMAT` (#515).

    `observations.timestamp` held THREE shapes, all written concurrently across
    the whole history: `2026-06-09 23:25:10` (the column default, 18,327 rows),
    `2026-06-09T14:00:00` (18), and `2026-08-08T00:19:00.965Z` (3,439). Because
    'T' (0x54) sorts after ' ' (0x20) at position 10, every ISO row compared as
    LATER than every space-separated row on the same date — 782 returned where 770
    were correct, 12 phantom rows (~1.5%), on a live
    range query, with no error raised.

    NOTE WHAT THIS IMPLIES FOR A NAIVE FIX: replacing 'T' with ' ' is not enough.
    A row left as `2026-08-08 00:19:00.965Z` is 24 characters and still compares
    wrong against a 19-character bound. The fractional part and the zone suffix
    have to go too, which is why this parses rather than substitutes.

    Returns None for None so callers can pass an absent value straight through
    (the column is nullable and the default fills it). An UNPARSEABLE value is
    returned UNCHANGED rather than dropped or coerced to now(): inventing a
    timestamp would fabricate history, and dropping one loses the row's place in
    it. A future shape shows up as itself in the data instead of as a plausible
    lie — and `test_normalize_leaves_an_unparseable_value_alone` pins that.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return value
    try:
        # fromisoformat handles both separators; Z is not accepted before 3.11,
        # so map it to the explicit offset rather than relying on the version.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(TIMESTAMP_FORMAT)


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
    summary_llm_source TEXT,
    -- HOW ended_at GOT THERE (#516). 'clean' = run_session_end reached; NULL on
    -- a legacy row predating this column, treated as clean. Anything else
    -- ('derived') means the session died without running session_end and the
    -- value is LAST KNOWN ACTIVITY — a lower bound, not the true end.
    --
    -- Needed because deriving ended_at changes what the column MEANS, and a row
    -- could not otherwise tell you which meaning it holds. check_outbox_targets
    -- depends on the distinction: those lanes are fed AT SESSION END, so only a
    -- session that actually ENDED is evidence the producer is dead. A crashed
    -- session never had the opportunity to feed one.
    ended_at_source TEXT,
    -- API token usage (#689), summed from the Claude Code transcript's
    -- message.usage, main transcript plus its subagent transcripts, deduped on
    -- message.id. NULL = unknown (no transcript read yet), 0 = measured zero.
    -- Written at SessionEnd and by `techne usage-backfill`; never dollars.
    tokens_in INTEGER,
    tokens_out INTEGER,
    tokens_cache_write INTEGER,
    tokens_cache_read INTEGER,
    usage_updated_at TIMESTAMP
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
    kind TEXT,
    content_hash TEXT,
    -- The transcript message uuid this row came from (techne#962). A RESUMED
    -- session replays its parent's messages under a NEW session id while the
    -- records keep their ORIGINAL uuid, so import used to write the parent's
    -- turns a second time. Nullable: rows captured live by the hooks have no
    -- transcript record behind them, and every row written before v27 predates
    -- the column.
    source_uuid TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_source_uuid
    ON observations(source_uuid) WHERE source_uuid IS NOT NULL;
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
    -- Writer provenance (techne#553). Nullable, and the NULLs are the point: an
    -- unattributed row stays honestly unattributed rather than guessed at.
    -- `written_by` is the INSTANCE that pushed it, `origin_source` the local
    -- `source` that federated writes used to overwrite with 'team', and
    -- `origin_session` the session it was distilled in where recoverable.
    written_by TEXT,
    origin_source TEXT,
    origin_session TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    confidence_numeric REAL NOT NULL DEFAULT 0.6,
    -- NULLABLE, WITH NO DEFAULT, AND THAT MATCHES THE MIGRATION ON PURPOSE
    -- (techne#938). This was `NOT NULL DEFAULT CURRENT_TIMESTAMP` while the v5
    -- ALTER could only produce a plain nullable column here -- not because SQLite
    -- refuses NOT NULL via ALTER (four lines above, the same v5 block adds
    -- `confidence_numeric REAL NOT NULL DEFAULT 0.6`) but because it refuses a
    -- NON-CONSTANT default, and CURRENT_TIMESTAMP is one:
    --
    --   ADD COLUMN x TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    --     -> Cannot add a column with non-constant default
    --
    -- so a fresh store and a migrated
    -- one disagreed AT THE SAME schema_version, and 2,823 of 2,914 live rows on
    -- the operator's store had it NULL. Those rows take decay_score's
    -- no-timestamp branch, so the confidence-decay tiebreak never decayed for
    -- 96.9% of the curated layer, while the same rows DID decay on a fresh
    -- install. Identical code, identical schema_version, different retrieval
    -- order, decided by a store's provenance.
    --
    -- The value is now guaranteed by the WRITE PATH instead: every INSERT INTO
    -- knowledge sets it explicitly. A schema default cannot be made to agree
    -- across both paths; an explicit write can, and it is the only place that
    -- knows the row is being created rather than migrated.
    last_confirmed_at TIMESTAMP,
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
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Vector-index staleness flag (techne#382). Set to 1 by any path that edits
    -- this row's `content` in place (e.g. cards.fill_card_summary) — the plain
    -- connect() connection can't touch the vec0 table (techne#168), so it signals
    -- drift here instead. backfill_vectors re-embeds rows where vec_stale=1 (in
    -- addition to un-embedded rows) and clears the flag. Without this, an edited
    -- row keeps its OLD embedding forever and semantic recall matches stale text.
    vec_stale INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
-- techne#553. DECLARED HERE AS WELL AS IN THE v25 STEP, following idx_outbox_dedup's
-- convention: `_reconcile_indexes` diffs against SCHEMA_SQL, so an index that exists
-- only in a migration never lands on a fresh db and cannot be healed by
-- `force_reconcile`. A brand-new team archivist is exactly the install that wants the
-- contributor query, and it was the one getting a full scan.
CREATE INDEX IF NOT EXISTS idx_knowledge_written_by ON knowledge(written_by);
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by);

-- Dependency edges between knowledge entries (techne#117). A junction table, NOT
-- a graph engine. `dependent_id` is derived from / depends on `trigger_id` — when
-- the trigger is superseded, the cascade marks the dependent `uncertain`. Edges are
-- written via promote_candidate's dream-promote dispatch (_add_dep_in_conn, techne#215/#251)
-- against the CURRENT revision (they do not auto-follow a supersession chain).
-- idx on trigger_id powers the cascade lookup.
-- (No semicolons in this comment — the SCHEMA_SQL parser's stop heuristic, #56.)
CREATE TABLE IF NOT EXISTS knowledge_deps (
    dependent_id INTEGER NOT NULL REFERENCES knowledge(id),
    trigger_id   INTEGER NOT NULL REFERENCES knowledge(id),
    kind         TEXT NOT NULL DEFAULT 'derived_from',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dependent_id, trigger_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_deps_trigger ON knowledge_deps(trigger_id);

-- Agent self-lessons (techne#1195). One row per knowledge row of type lesson,
-- grant or charter, carrying the structure the knowledge row does not have.
-- `agent_id` is the stable agent the lesson belongs to (config instance_name),
-- NULL meaning every agent on this store. It is an ID, not a display name, so a
-- rename never orphans a lesson. `tier` is principle or entry for a lesson and
-- NULL for grant and charter. `rule` is the one line that gets injected.
-- `parent_id` links an entry to the principle whose family it belongs to, and is
-- repointed when that principle is refined. `loaded_when_failed` is set on an
-- entry that names its evidence session: 1 if the parent principle was injected
-- in that session, 0 if not, NULL if unknown.
-- (No semicolons in this comment — the SCHEMA_SQL parser's stop heuristic, #56.)
CREATE TABLE IF NOT EXISTS lesson_meta (
    knowledge_id       INTEGER PRIMARY KEY REFERENCES knowledge(id),
    agent_id           TEXT,
    tier               TEXT,
    rule               TEXT NOT NULL,
    parent_id          INTEGER REFERENCES knowledge(id),
    enforced_by        TEXT,
    evidence_session   TEXT,
    loaded_when_failed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lesson_meta_parent ON lesson_meta(parent_id);
CREATE INDEX IF NOT EXISTS idx_lesson_meta_agent ON lesson_meta(agent_id, tier);

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
-- Covering index for the per-target liveness check (techne#468). Without it that
-- check does `SCAN outbox` + `USE TEMP B-TREE FOR GROUP BY` over the whole table
-- — measured 103ms on a 60k-row outbox, inside the SessionStart hot path, and
-- GROWING FOREVER because nothing ever deletes a synced row (both
-- `DELETE FROM outbox` statements are `WHERE status = 'pending'`). With it the
-- same query is a covering scan at ~11ms. Columns ordered to serve
-- `GROUP BY target` with the aggregates read straight from the index.
CREATE INDEX IF NOT EXISTS idx_outbox_target_status
    ON outbox(target, status, synced_at, created_at);

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
-- The UNIQUE index enforces one row per (session, pair): run_session_end
-- deletes-then-reinserts a session's boundaries each turn, and the constraint
-- makes any future non-atomic regression fail loudly instead of silently
-- re-accumulating duplicates (the v17 migration; techne thread_boundaries debt).
-- It also serves load_boundaries' (session_id, obs_index_a) lookup as a prefix,
-- so no separate non-unique index is needed.
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_boundaries_triple
    ON thread_boundaries(session_id, obs_index_a, obs_index_b);

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
    decision_reason TEXT,
    -- Faithfulness gate (techne#217 / coherence-layer D4). Populated by the
    -- faithfulness emit/ingest pass on merge/supersede candidates. NULL means
    -- unchecked (candidate was not yet run through the pass, or action='add'
    -- which has no source rows to preserve). 1=faithful, 0=unfaithful (a
    -- load-bearing claim was dropped). faithfulness_warning carries the
    -- human-readable reason when unfaithful. The gate is ADVISORY — a flagged
    -- candidate still goes to the operator for decision; it is never auto-blocked.
    faithfulness_ok INTEGER,          -- NULL|1|0
    faithfulness_warning TEXT,        -- NULL when ok or unchecked
    -- Reviewer's edit (techne#952). NULL unless promoted with `--content`; promote
    -- then applies this instead of `content`. `content` is never edited: it is half
    -- of idx_kc_reflect_dedup, so rewriting it would let a re-ingest insert a fresh
    -- pending duplicate, and it is the record of what the model proposed.
    revised_content TEXT
);

CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_candidates(status);
CREATE INDEX IF NOT EXISTS idx_kc_session ON knowledge_candidates(source_session_id);
CREATE INDEX IF NOT EXISTS idx_kc_action_status ON knowledge_candidates(action, status);
-- Exactly-once reflect ingest (techne#236, schema v14). Partial UNIQUE on
-- (prompt_hash, content) so re-ingesting the same thread's answer is a no-op
-- (paired with INSERT ... ON CONFLICT DO NOTHING in reflect._insert_candidate).
-- Scoped to origin='reflect': the consolidate/dream/conflict/federation passes share
-- this table but carry their own re-proposal guards and reuse prompt_hash values, so
-- they must stay unconstrained. prompt_hash (NOT NULL) not source_thread_id (the FK
-- safety-net can NULL it, and SQLite treats NULLs as distinct — silently defeating the
-- dedup); content is in the key because one prompt yields many distinct candidates.
CREATE UNIQUE INDEX IF NOT EXISTS idx_kc_reflect_dedup
    ON knowledge_candidates(prompt_hash, content) WHERE origin = 'reflect';

-- Reflection debt queue (techne#514).
--
-- WHY IT EXISTS: session_end COUNTS unreflected work and enqueues nothing. It
-- even logs "403 obs, 19 threads" — so the runtime KNOWS the debt exists, which
-- is worse than not knowing, because the instrumentation makes the rail read as
-- healthy. Reflection therefore survives exactly ONE exit path, the polite one
-- (crestart asking a session to reflect before it exits). Every impolite exit —
-- and techne#490 records Remote Control dying after a ~10min network outage as
-- the NORMAL way a long session ends here — drops its debt silently.
--
-- THREAD IDS, NOT RENDERED PROMPTS (Richard's call, 2026-08-07). A stored prompt
-- embeds a point-in-time snapshot that drifts from the ledger it describes, and
-- the ledger is what the prompt renders FROM anyway — so re-rendering at drain
-- time is both cheaper and strictly fresher. The cost is that a thread must stay
-- reconstructible, which it is: prompts render from observations, which are
-- never deleted.
--
-- DECOUPLES PRODUCER FROM REFLECTOR, which is the actual fix. A peer session
-- reflecting another's threads is not a workaround — it happened by hand on
-- 2026-08-07 and worked, because answering a rendered prompt needs the thread's
-- observations, not the original session's context. `drained_at` is NULLABLE
-- rather than a status enum: pending is the absence of a drain, so there is no
-- second source of truth to disagree with the timestamp.
--
-- UNIQUE on thread_id alone (not the pair): threads.id is a global TEXT PK, so
-- the thread is the natural idempotency key — re-running session_end, or a peer
-- enqueueing the same thread, must not queue it twice.
-- ON DELETE CASCADE is required: re-detection deletes and re-inserts a session's
-- whole thread set in one transaction (lifecycle.py), so a plain reference makes
-- the queue BLOCK re-detection with a FOREIGN KEY failure.
--
-- IT IS NOT A COMPLETE ANSWER, AND BOTH HALVES OF THE ORIGINAL CLAIM HERE WERE
-- FALSE (#527 review). The thread does NOT cease to exist — it is re-inserted
-- with the same id — and session_end does NOT re-run for a redetect ("a
-- re-analysis, not a re-end"). So a caller that removes a threads row must
-- re-enqueue, or it silently destroys the debt.
--
-- TWO WAYS A ROW IS REMOVED, and only one of them looks like a delete:
--   1. An explicit DELETE. There is exactly ONE site in the tree —
--      `persist_detection`'s PRUNE (lifecycle.py), which deletes only
--      superseded, unreferenced, unpaid rows (techne#797). It is NEVER a
--      whole-session wipe; that form is what broke run_session_end on any store
--      with history. `redetect_threads` has no DELETE of its own — it calls
--      persist_detection and goes through the same prune. The prune re-enqueues
--      IN THE SAME TRANSACTION (#527 r5), so a failure rolls back rather than
--      stranding a half-applied detection.
--   2. INSERT OR REPLACE, whose conflict resolution deletes the existing row and
--      fires this CASCADE. OR IGNORE does not.
-- CORRECTED techne#797: this used to say `save_threads` PICKS between OR REPLACE
-- and OR IGNORE on whether the thread has a boundary_signal, and that
-- `run_session_end` therefore drops the debt for legacy threads. It no longer
-- picks — it is a single `ON CONFLICT(id) DO UPDATE` upsert, which fires neither
-- the CASCADE nor a delete, so route 2 has no caller in this tree today. It is
-- documented because a future caller can still reintroduce it.
-- Don't take SQLite's verb behaviour on trust from this comment — run it:
-- test_reflect.py::test_the_cascade_fires_on_or_replace_not_only_on_delete.
-- NOTE WHAT THAT TEST DOES AND DOES NOT WITNESS: it executes its own literal
-- OR REPLACE / OR IGNORE statements, so it pins SQLITE'S semantics and is green
-- regardless of which verb `save_threads` uses. For that, see
-- test_detection_replace.py::test_a_kept_threads_summary_still_refreshes.
-- A new caller doing either without re-enqueueing destroys debt with no error
-- and no trace. Check both verbs, not just the word DELETE.
CREATE TABLE IF NOT EXISTS reflection_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id) ON DELETE CASCADE,
    enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    drained_at TIMESTAMP,
    -- techne#870 (v28): set by `techne reflect reconcile --apply` on a row it has
    -- shown is below min_observations_per_thread. Still undrained and kept as the
    -- #646 evidence, but out of the banner's actionable debt. Cleared when the
    -- thread renders again. Plain nullable on both paths, per techne#938.
    parked_at TIMESTAMP,
    parked_reason TEXT
);
-- Partial index: the only hot query is "what is still pending, oldest first".
CREATE INDEX IF NOT EXISTS idx_reflection_queue_pending
    ON reflection_queue(id) WHERE drained_at IS NULL;

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

-- Durable due-dated reminders (techne#207).
-- status is constrained to 'active' | 'done' in the application layer.
-- due_at: ISO text timestamp (same TIMESTAMP convention as all other tables).
-- last_surfaced_at: stamped each time the post_tool_use hook injects this
--   reminder; NULL means never surfaced. Powers the ~3h repeat-throttle.
-- Hot-path SELECT is: status='active' AND due_at <= :now; index covers both.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note TEXT NOT NULL,
    due_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_surfaced_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reminders_status_due
    ON reminders(status, due_at);

-- Inter-session coordination mailbox (techne#376). Additive (reconciled, no
-- migration) — fresh DBs get these at init; an existing DB picks them up via
-- `techne reconcile` (the connect-time reconcile is sentinel-gated, techne#140).
-- Concurrent Claude Code sessions
-- converse without a human router: a message targets a channel (e.g. 'coord') or a
-- specific session_id, and is delivered via the post_tool_use injection rail.
-- Identity is the self-asserted session_id (no bearer token, local trust boundary).
CREATE TABLE IF NOT EXISTS coord_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_session TEXT NOT NULL,
    from_name TEXT,
    recipient TEXT NOT NULL,
    thread_id TEXT,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coord_messages_recipient ON coord_messages(recipient, id);

-- Per-reader read receipts, so a broadcast-channel message can be read independently
-- by each session. Unread for a session = messages to its channels or itself, not
-- authored by it, with no matching coord_reads row.
--
-- reader_session is a NAMESPACED identity, not a raw session id: 'name:<display>'
-- when the session has a -n alias, else 'uuid:<session-id>'. Built by
-- coord.reader_key(). The column holds two KINDS of identity and an unprefixed
-- value cannot say which, so they are tagged (techne#446, migration step 21).
-- Keying on the bare UUID was the bug: Claude Code mints one per launch, so every
-- relaunch re-read the entire backlog.
CREATE TABLE IF NOT EXISTS coord_reads (
    message_id INTEGER NOT NULL,
    reader_session TEXT NOT NULL,
    read_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, reader_session)
);

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


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open ``db_path`` in WAL mode, migrating it if it is behind.

    ``check_same_thread=False`` is for a caller that serialises use of the
    connection itself: the MCP server's long-lived read connection
    (``core/read_conn.py``, techne#1095) holds a lock around every use.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a lock before raising "database is locked". Multiple
    # hooks (post_tool_use, session_end) can fire concurrently when Claude Code
    # streams events, and WAL still serializes writes — without a busy timeout
    # the concurrent writer raises immediately instead of waiting its turn.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        _ensure_initialized(conn, db_path)
    except BaseException:
        conn.close()  # techne#1241: a failed init used to leak the connection
        raise
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
        return row[0] if row and row[0] is not None else existing_version

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


def _stored_version(conn: sqlite3.Connection) -> int | None:
    """MAX(version) from schema_version; None when the table has no rows."""
    return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]


def _first_init(conn: sqlite3.Connection, db_path: Path, *, half_built: bool) -> None:
    """Create the schema and stamp SCHEMA_VERSION, once across processes.

    Runs under the same flock as migrator.guarded_migrate, and re-checks under it,
    so two hooks opening a fresh TECHNE_HOME together don't both build it
    (techne#1241). SCHEMA_SQL and FTS_TRIGGERS_SQL are all IF NOT EXISTS, so
    finishing a half-built store is safe. A half-built store also gets the
    reconcile passes rather than the sentinel alone: it may predate this code, and
    the passes add whatever columns and indexes CREATE IF NOT EXISTS skipped."""
    from techne.core.extension_state import set_state

    def _do() -> None:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if has_table and _stored_version(conn) is not None:
            return  # another process finished it while we waited
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )
        if half_built:
            _run_reconcile_passes(conn)  # stamps the sentinel and commits
            return
        # A freshly-created schema is correct by construction — no reconcile needed;
        # stamp the sentinel so the next connect skips the passes (techne#140).
        set_state(conn, _RECONCILE_SENTINEL, {"version": SCHEMA_VERSION})
        conn.commit()

    migrator.with_migration_lock(db_path, _do)


def _ensure_initialized(conn: sqlite3.Connection, db_path: Path) -> None:
    """Initialize the schema if absent. Safe to call repeatedly."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if existing is None:
        _first_init(conn, db_path, half_built=False)
    elif _stored_version(conn) is None:
        # techne#1241: SCHEMA_SQL commits before the version row is inserted, so a
        # kill or a race there leaves the tables with an empty schema_version, and
        # MAX(version) is NULL. Treat it as an unfinished first init, never as v0:
        # running the migration ladder from 0 over a current schema would break it.
        _first_init(conn, db_path, half_built=True)
    else:
        existing_version = _stored_version(conn)
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
    if from_version < 13 and to_version >= 13:
        # Faithfulness gate (techne#217 / coherence-layer D4). Add two nullable
        # columns to knowledge_candidates to carry the pre-promotion faithfulness
        # annotation for merge/supersede candidates: faithfulness_ok (1/0/NULL) and
        # faithfulness_warning (human-readable reason when unfaithful). NULL means
        # the candidate was not yet checked (or is an action='add' with no sources).
        # The gate is ADVISORY — the operator still decides; no candidate is blocked.
        kc_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_candidates'"
        ).fetchone()
        if kc_exists:
            _add_column_if_missing(conn, "knowledge_candidates", "faithfulness_ok", "INTEGER")
            _add_column_if_missing(conn, "knowledge_candidates", "faithfulness_warning", "TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (13)")
    if from_version < 14 and to_version >= 14:
        # Exactly-once reflect ingest (techne#236). knowledge_candidates had no
        # uniqueness, so re-emitting/re-ingesting the same thread inserted a DUPLICATE
        # pending candidate. Add a partial UNIQUE index over (prompt_hash, content)
        # WHERE origin='reflect' so the ingest INSERT ... ON CONFLICT DO NOTHING is
        # idempotent. Scoped to origin='reflect' to leave the consolidate/dream/conflict/
        # federation passes (which share the table and reuse prompt_hash) unconstrained.
        kc_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_candidates'"
        ).fetchone()
        if kc_exists:
            # `origin` is an additive column the reconciler normally backfills AFTER
            # _migrate runs — but both the dedup and the partial index below reference it,
            # so ensure it exists first (same decl/default as SCHEMA_SQL; pre-origin rows
            # were all reflect-origin, so 'reflect' is the correct backfill).
            _add_column_if_missing(
                conn, "knowledge_candidates", "origin", "TEXT NOT NULL DEFAULT 'reflect'"
            )
            # Dedup pre-existing reflect duplicates BEFORE creating the UNIQUE index —
            # otherwise CREATE UNIQUE INDEX fails on a DB that already has dupes. Keep the
            # lowest id per (prompt_hash, content) group; scoped to origin='reflect' to
            # match the partial index's predicate (other origins are untouched).
            # MIN(id) is kept regardless of status: if an earlier identical candidate was
            # already discarded or promoted and a later identical one is pending, the pending
            # row is deleted. This is benign — the content is byte-identical to something
            # already judged, so no work is lost.
            conn.execute(
                """DELETE FROM knowledge_candidates
                     WHERE origin = 'reflect'
                       AND id NOT IN (
                           SELECT MIN(id) FROM knowledge_candidates
                            WHERE origin = 'reflect'
                         GROUP BY prompt_hash, content
                       )"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_kc_reflect_dedup "
                "ON knowledge_candidates(prompt_hash, content) WHERE origin = 'reflect'"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (14)")
    if from_version < 15 and to_version >= 15:
        # Durable due-dated reminders (techne#207). Purely additive new table,
        # so this is safe to re-run (CREATE TABLE IF NOT EXISTS). A plain
        # covering index on (status, due_at) covers the hot-path SELECT in the
        # post_tool_use hook — added here so existing DBs get it without
        # requiring `techne reconcile` (the reconcile sentinel skips additive
        # index-only changes on already-stamped DBs at the current version).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note TEXT NOT NULL,
                due_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_surfaced_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_status_due
                ON reminders(status, due_at);
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (15)")
    if from_version < 16 and to_version >= 16:
        # Audit content hash for redacted observations (techne#270). Purely
        # additive nullable column — existing rows get NULL, which is correct:
        # they were written before this feature existed and have no stored hash.
        # Hash is only computed+stored when redaction fires (content changed),
        # so NULL also naturally encodes "no redaction occurred, nothing to prove".
        # Guard on table existence: minimal-schema migration-test DBs may not
        # have the observations table yet (mirrors the v13/v14 kc_exists guard).
        obs_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        if obs_exists:
            _add_column_if_missing(conn, "observations", "content_hash", "TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (16)")
    if from_version < 17 and to_version >= 17:
        # thread_boundaries accumulated duplicate (session_id, obs_index_a, obs_index_b)
        # rows. run_session_end recomputes and re-inserts a session's FULL boundary set
        # once per turn; before the delete-before-insert fix (7d45067, made atomic in
        # 93890d5) that append had no dedup guard, so a session touched N turns kept N
        # copies — observed 8x avg / 85x max, ~30% of the DB. The write path is correct
        # now, but no backfill shipped, so existing DBs carry the debt forever. Collapse
        # duplicates keeping the most-recent row per triple (MAX(id) preserves the current
        # heuristic's scoring if it changed between recomputes), then add a UNIQUE index so
        # any future non-atomic regression fails loudly instead of silently re-accumulating,
        # and drop the now-redundant prefix index (the unique index covers
        # (session_id, obs_index_a) as a prefix for load_boundaries). Dedup BEFORE the
        # UNIQUE index or its creation fails on a DB that already has dupes (mirrors the
        # v14 kc_reflect_dedup idiom). Guard on table existence (mirrors the v8/v16 guards).
        tb_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_boundaries'"
        ).fetchone()
        if tb_exists:
            conn.execute(
                """DELETE FROM thread_boundaries
                     WHERE id NOT IN (
                         SELECT MAX(id) FROM thread_boundaries
                       GROUP BY session_id, obs_index_a, obs_index_b
                     )"""
            )
            conn.execute("DROP INDEX IF EXISTS idx_thread_boundaries_session")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_boundaries_triple "
                "ON thread_boundaries(session_id, obs_index_a, obs_index_b)"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (17)")
    if from_version < 18 and to_version >= 18:
        # #355 (the #360 drift class): on DBs created before observations_fts
        # indexed tags/kind, the observations_ai/ad/au triggers are stuck in their
        # OLD content-only form. reconcile_triggers is name-only, so the
        # FTS_TRIGGERS_SQL update never reached them — tags/kind are silently never
        # FTS-indexed. Redeploy the triggers to the canonical content+tags+kind
        # form (mirrors the v12 knowledge_* precedent), THEN rebuild
        # observations_fts so existing rows are re-indexed with tags/kind. Unlike
        # v12 (NULL vs '' both tokenize empty → no rebuild needed), this drift
        # genuinely dropped tags/kind from the index, so the rebuild is required.
        # Guard on table existence (mirrors the v16 obs guard).
        obs_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        if obs_exists:
            # SQL is flush-left (leading whitespace is insignificant to SQLite) so
            # the COALESCE VALUES lines stay under the line-length limit despite the
            # depth of this migration block.
            conn.executescript("""
DROP TRIGGER IF EXISTS observations_ai;
DROP TRIGGER IF EXISTS observations_ad;
DROP TRIGGER IF EXISTS observations_au;
CREATE TRIGGER observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, content, tags, kind)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''), COALESCE(NEW.kind, ''));
END;
CREATE TRIGGER observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, tags, kind)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''), COALESCE(OLD.kind, ''));
END;
CREATE TRIGGER observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, tags, kind)
    VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.tags, ''), COALESCE(OLD.kind, ''));
    INSERT INTO observations_fts(rowid, content, tags, kind)
    VALUES (NEW.id, NEW.content, COALESCE(NEW.tags, ''), COALESCE(NEW.kind, ''));
END;
""")
            fts_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations_fts'"
            ).fetchone()
            if fts_exists:
                conn.execute("INSERT INTO observations_fts(observations_fts) VALUES('rebuild')")
        # #353: drop the zombie idx_thread_boundaries_session on DBs where an older
        # SCHEMA_SQL reconcile recreated it AFTER v17 already dropped it (v17 won't
        # re-run on an already-migrated DB, so it lingers). Idempotent; the UNIQUE
        # triple index covers its (session_id, obs_index_a) prefix.
        conn.execute("DROP INDEX IF EXISTS idx_thread_boundaries_session")
        conn.execute("INSERT INTO schema_version (version) VALUES (18)")
    if from_version < 19 and to_version >= 19:
        # techne#382: vector-index staleness flag. An in-place content edit left
        # the row's OLD embedding resident (backfill only embeds MISSING rows), so
        # semantic recall matched stale text forever. Additive column; existing
        # rows default 0 (their current embedding matches their current content).
        # Guard on table existence: minimal migration-test fixtures build only a
        # subset of tables (a real techne DB always has `knowledge`).
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
        ).fetchone():
            _add_column_if_missing(conn, "knowledge", "vec_stale", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version) VALUES (19)")

    if from_version < 20 and to_version >= 20:
        # techne#468: covering index for the per-target outbox liveness check.
        # Existing stores are exactly the ones that need it — the cost scales
        # with accumulated synced rows, which are never deleted, so an old store
        # pays the most. Index-only; no data change, and IF NOT EXISTS makes it
        # a no-op on a store created from the current schema.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'"
        ).fetchone():
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_target_status "
                "ON outbox(target, status, synced_at, created_at)"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (20)")

    if from_version < 21 and to_version >= 21:
        # techne#446: coord read receipts were keyed on the session UUID, which
        # Claude Code mints fresh per launch — so a relaunched session inherited
        # zero receipts and re-read the whole backlog, and the unread count only
        # ever grew (97 messages, the bus's entire history, reported unread).
        # `reader_key()` now files receipts under `name:<display-name>` when the
        # session has one, falling back to `uuid:<session-id>`.
        #
        # THIS STEP ONLY NAMESPACES THE EXISTING ROWS. It does NOT rewrite them to
        # names, and that is deliberate rather than lazy: the UUID->name map lives
        # in Claude Code's per-session sidecars, and the sidecars for dead sessions
        # are gone. Inventing a mapping would fabricate the very "who read what"
        # claim the receipt exists to record. So old rows keep their UUID identity,
        # correctly tagged as such; each name drains the backlog once more and then
        # stops. Cost is bounded and one-time, and it is honest.
        #
        # Idempotent by construction — only bare values (neither prefix) match.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coord_reads'"
        ).fetchone():
            conn.execute(
                "UPDATE coord_reads SET reader_session = 'uuid:' || reader_session "
                "WHERE reader_session NOT LIKE 'uuid:%' AND reader_session NOT LIKE 'name:%'"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (21)")

    if from_version < 22 and to_version >= 22:
        # techne#514: the reflection_queue table. NO DDL HERE ON PURPOSE — the
        # table is purely additive, so _reconcile_tables creates it from
        # SCHEMA_SQL on connect. This step exists solely to BUMP THE VERSION,
        # and that bump is the load-bearing part: post-#140 the connect()-time
        # reconcile is sentinel-gated and skips already-reconciled DBs, so an
        # additive SCHEMA_SQL change at an UNCHANGED SCHEMA_VERSION reaches new
        # databases only and silently never reaches existing ones. Every live
        # store on this machine is an existing one.
        conn.execute("INSERT INTO schema_version (version) VALUES (22)")
    if from_version < 23 and to_version >= 23:
        # techne#515: observations.timestamp / .event_at held three formats at
        # once, written concurrently across the entire history — the column
        # default (`2026-06-09 23:25:10`), bare ISO, and ISO with fractional
        # seconds and a Z. TEXT columns compare lexically, so 'T' (0x54) sorting
        # after ' ' (0x20) made every ISO row read as later than every
        # space-separated row on the same date. Measured on the live store: 782
        # returned where 770 were correct — 12 phantom rows, ~1.5%, on a live
        # range query, silently.
        #
        # The writers are fixed at the seven insert sites; this backfills what
        # they already wrote. Both columns, because event_at carries the same
        # JSONL value through the same paths.
        #
        # DONE IN SQL RATHER THAN PYTHON, deliberately: these are millions of
        # comparisons on a large store and the shapes are fixed and known.
        # `substr(...,1,19)` truncates the fraction and zone, and replace() fixes
        # the separator — both safe because every non-conforming value observed
        # is `YYYY-MM-DDTHH:MM:SS` with an OPTIONAL `.fffZ` tail. A value that is
        # not that shape is left alone by the WHERE clause rather than mangled.
        #
        # The backfill is a NAMED FUNCTION rather than inline SQL so its
        # idempotence can be TESTED by calling it twice, instead of asserted in
        # a comment. `_migrate` itself cannot be re-run (the schema_version
        # insert is UNIQUE), so an inline version would have carried an
        # idempotence claim nothing could check.
        backfill_timestamp_formats(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (23)")
    if from_version < 24 and to_version >= 24:
        # techne#516: sessions.ended_at was 79% NULL and DEGRADING (52% of June
        # sessions had one, 19% of July, 7% of August), because `end_session` is
        # its only CLEAN writer and an unclean exit never reaches it. With ended_at
        # NULL the interval is open-ended, so "which session was live at T"
        # matched every session ever started before T — the replay index #516
        # exists to build could not discriminate at all.
        #
        # Backfills the historical NULLs from last known activity. The ongoing
        # fix is the sweep in core/state.py, called from lifecycle.run_session_start
        # and from the Stop/SessionEnd hook (NOT the SessionStart hook, which never
        # calls run_session_start; techne#1187); this step
        # only repairs what the old behaviour already lost.
        #
        from techne.core.state import close_abandoned_sessions_in_conn

        # THE NORMAL IDLE WINDOW, not 0. An earlier version passed 0 on the
        # reasoning that "every session in history is finished" — false whenever
        # a peer is live, and concurrent sessions are the normal case here.
        # `connect()` IS the migration trigger, so a post_tool_use hook writing an
        # observation runs this step with its own session's earlier observations
        # already in the past: measured, a peer whose last observation was five
        # seconds old got stamped. History is silent by definition, so the six-hour
        # window sweeps all of it anyway and costs nothing.
        # THE STEP MUST ENSURE ITS OWN COLUMN. `ended_at_source` is additive, so
        # SCHEMA_SQL's copy is created by the RECONCILER — which runs AFTER
        # _migrate. On a v3 fixture the column is genuinely absent here and the
        # sweep's UPDATE raises "no such column". THIRD instance of this exact
        # trap in this milestone (#515 hit it with event_at, then with the
        # observations table); a migration step owns every precondition it needs.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone():
            _add_column_if_missing(conn, "sessions", "ended_at_source", "TEXT")
        close_abandoned_sessions_in_conn(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (24)")
    if from_version < 25 and to_version >= 25:
        # techne#553: archivist.db held 1,094 rows and every one carried
        # source='team' and nothing else. At one contributor that is survivable; a
        # store whose whole purpose is sharing cannot name its second. Blocks
        # conflict resolution, trust weighting, "who should I ask", and retracting
        # one contributor's bad week.
        #
        # ONE SCHEMA EVERYWHERE (techne#220), so these land on techne.db as well,
        # where they are simply NULL. archivist.db IS a techne-schema db —
        # `archivist_db()` calls `init_db` — so an archivist-only column is not
        # available even in principle without forking the shape the read edge
        # depends on.
        #
        # DELIBERATELY NOT BACKFILLED. Existing rows are unattributable: the local
        # `source` was discarded at write time, not stored anywhere. Filling them
        # with the only instance we have ever run would encode a guess as a fact,
        # in the one table whose job is to say who said what.
        # TABLE-PRESENCE GUARD. Old fixtures (v9, v13, v15) reach this step before
        # `knowledge` exists — the reconciler creates it AFTER _migrate. The v24 step
        # above carries the same guard and calls itself "the THIRD instance of this
        # exact trap in this milestone"; this was the fourth, caught by 16 failing
        # migration tests. A migration step owns every precondition it needs.
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
        ).fetchone():
            conn.execute("INSERT INTO schema_version (version) VALUES (25)")
        else:
            _add_column_if_missing(conn, "knowledge", "written_by", "TEXT")
            _add_column_if_missing(conn, "knowledge", "origin_source", "TEXT")
            _add_column_if_missing(conn, "knowledge", "origin_session", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_written_by ON knowledge(written_by)"
            )
            conn.execute("INSERT INTO schema_version (version) VALUES (25)")
    if from_version < 26 and to_version >= 26:
        # techne#949: promotion wrote knowledge rows with origin_session NULL, so
        # the session a promoted fact came from was reachable only by joining
        # knowledge_candidates.promoted_to_id or by parsing the `source` string.
        # Promote now stamps it; this fills the rows written before that.
        #
        # UNLIKE v25's columns, THESE ROWS ARE ATTRIBUTABLE, which is why this
        # backfills where v25 deliberately did not. v25 refused because a federated
        # row's local source was discarded at write time. A promoted row's source
        # session is recorded on the candidate that produced it.
        #
        # NOT THE SLEEP SENTINEL (Prudence #961 r1). Consolidate merges, dream adds and
        # conflict supersedes are filed under `sleepdream.SYSTEM_SLEEP_SESSION`
        # ('system:sleep'), a placeholder for the FK, not a session. Those rows stay
        # NULL. The literal is pinned to the constant by
        # test_the_migration_sentinel_matches_sleepdream (no import: database.py sits
        # below sleepdream).
        #
        # ONLY ACTIONS THAT MINT A ROW. `retire` points promoted_to_id at an existing
        # survivor, which did not originate in the retiring session, so stamping it
        # would write a wrong session into the provenance column. A NULL action is a
        # legacy `add`.
        #
        # Both tables must exist; old fixtures reach this step before the reconciler
        # creates them (the guard v24 and v25 each needed).
        have = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('knowledge', 'knowledge_candidates')"
            )
        }
        if have == {"knowledge", "knowledge_candidates"}:
            _add_column_if_missing(conn, "knowledge", "origin_session", "TEXT")
            # `action` is reconciler-added (#108), so a pre-#108 fixture lacks it here.
            _add_column_if_missing(
                conn, "knowledge_candidates", "action", "TEXT NOT NULL DEFAULT 'add'"
            )
            conn.execute(
                """UPDATE knowledge
                      SET origin_session = (
                          SELECT c.source_session_id FROM knowledge_candidates c
                           WHERE c.promoted_to_id = knowledge.id
                             AND c.status = 'promoted'
                             AND c.source_session_id != 'system:sleep'
                             AND COALESCE(c.action, 'add') IN ('add', 'supersede', 'merge')
                             AND c.scope = 'knowledge'
                           ORDER BY c.id LIMIT 1)
                    WHERE origin_session IS NULL
                      AND id IN (
                          SELECT promoted_to_id FROM knowledge_candidates
                           WHERE status = 'promoted'
                             AND source_session_id != 'system:sleep'
                             AND COALESCE(action, 'add') IN ('add', 'supersede', 'merge')
                             AND scope = 'knowledge'
                             AND promoted_to_id IS NOT NULL)"""
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (26)")

    if from_version < 27 and to_version >= 27:
        # techne#962: a resumed session replays its parent's turns under a NEW
        # session id, and import idempotency is PER SESSION ("already has N
        # observations"), so the replayed turns imported a second time. Measured
        # on this machine before the fix: 52 transcript pairs share message
        # uuids, and of the 2 pairs where both halves had been imported, 44
        # observation rows were duplicated across the two session ids.
        # source_uuid gives import the identity the transcript already carries.
        #
        # DELIBERATELY NOT BACKFILLED. There is nothing to backfill FROM in the
        # DATABASE -- the uuid was never recorded and is not derivable from a
        # stored row. Unlike v26's origin_session, whose source WAS recoverable
        # from knowledge_candidates, inventing values here would be fabrication.
        #
        # THE COST OF THAT, STATED PRECISELY, BECAUSE AN EARLIER COMMENT SAID ONLY
        # "existing duplicates stay; this stops new ones" AND THAT IS TOO KIND.
        # Every pre-v27 row has source_uuid NULL, and NULL never matches `= ?`, so
        # the import-time skip cannot see it. A resume imported AFTER the upgrade
        # whose parent was imported BEFORE it still duplicates the parent in full
        # -- which is the issue's own scenario on every store that is not fresh.
        # Measured: parent + resume with the parent's uuids nulled gives 4 rows
        # where 2 are correct, replayed_skipped=0.
        #
        # What WOULD close it is a backfill keyed on the transcript that produced
        # the rows (re-read each imported session's own jsonl and stamp its uuids),
        # which invents nothing. That is a separate change with its own risks and
        # is filed as techne#978 rather than smuggled in here.
        #
        # THE ALTER AND THE CREATE TABLE DECLARE THE SAME THING, which is the
        # standing lesson of techne#938: last_confirmed_at is `NOT NULL DEFAULT
        # CURRENT_TIMESTAMP` on a fresh install and plain nullable on a migrated
        # one, AT THE SAME schema_version -- so a store's provenance silently
        # changed its retrieval order. Plain `TEXT` on both paths here.
        # GUARDED ON THE TABLE EXISTING, like v26 above. A migration step runs
        # against whatever shape the store actually has, and the reconciler path
        # brings a DB forward from as far back as v7 -- fixtures at that age may
        # carry no `observations` table at all, in which case `ALTER TABLE` dies
        # with "no such table" and takes the whole chain down. The table is
        # created by the schema script for any store that should have one, so
        # "absent here" means "this step has nothing to do", not "something is
        # wrong". Caught by 21 migration tests before this shipped.
        has_observations = bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone()
        )
        if has_observations:
            _add_column_if_missing(conn, "observations", "source_uuid", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_source_uuid "
                "ON observations(source_uuid) WHERE source_uuid IS NOT NULL"
            )
        conn.execute("INSERT INTO schema_version (version) VALUES (27)")

    if from_version < 28 and to_version >= 28:
        # techne#870: a third state for a reflection_queue row, between owed and
        # drained. Additive and nullable, so no backfill: every existing row is
        # unparked, which is exactly what it was before this column existed. A
        # numbered step rather than SCHEMA_SQL alone because the connect()-time
        # reconciler skips an already-reconciled store (#140), and the readers of
        # this column would fail on such a store with "no such column".
        # Guarded on the table existing, like v27: old fixtures may predate it.
        has_queue = bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reflection_queue'"
            ).fetchone()
        )
        if has_queue:
            _add_column_if_missing(conn, "reflection_queue", "parked_at", "TIMESTAMP")
            _add_column_if_missing(conn, "reflection_queue", "parked_reason", "TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (28)")
    if from_version < 29 and to_version >= 29:
        # techne#1195: agent self-lessons. A new table only, nothing backfilled:
        # existing self-lessons live in identity under ad hoc keys and moving them
        # is an operator decision, not a migration. A numbered step rather than
        # SCHEMA_SQL alone for the same reason as v28 -- an already-reconciled
        # store skips the reconciler, and lesson writes would fail on it.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lesson_meta (
                knowledge_id       INTEGER PRIMARY KEY REFERENCES knowledge(id),
                agent_id           TEXT,
                tier               TEXT,
                rule               TEXT NOT NULL,
                parent_id          INTEGER REFERENCES knowledge(id),
                enforced_by        TEXT,
                evidence_session   TEXT,
                loaded_when_failed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_lesson_meta_parent ON lesson_meta(parent_id);
            CREATE INDEX IF NOT EXISTS idx_lesson_meta_agent ON lesson_meta(agent_id, tier);
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (29)")
    if from_version < 30 and to_version >= 30:
        # #689: per-session token usage. Additive and nullable, no backfill here:
        # filling them means reading every transcript (gigabytes), which belongs in
        # `techne usage-backfill`, not in connect(). A numbered step for the same
        # reason as v28: an already-reconciled store skips the reconciler.
        has_sessions = bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
        )
        if has_sessions:
            for col, decl in (
                ("tokens_in", "INTEGER"),
                ("tokens_out", "INTEGER"),
                ("tokens_cache_write", "INTEGER"),
                ("tokens_cache_read", "INTEGER"),
                ("usage_updated_at", "TIMESTAMP"),
            ):
                _add_column_if_missing(conn, "sessions", col, decl)
        conn.execute("INSERT INTO schema_version (version) VALUES (30)")
    conn.commit()
