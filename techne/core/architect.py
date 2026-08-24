"""Architect domain — the pattern store, in its own db file (techne#87).

db-per-domain (`docs/db-per-domain.md`, proven by the Librarian #99): the
architectural-patterns corpus lives in `architect.db`, NOT techne.db and NOT the
operator's `~/.claude` markdown. Owned by an "architect" persona; team-scope, so —
unlike the Librarian — patterns are queried DIRECTLY from this store (no gist cards
in personal recall).

Data model (spec): `investigations` is the dedup+verdict+lens unit (one row per
url×surveyed_by×lens — two lenses on a URL = a join, not a diff); `patterns` hang
off it; `pattern_relations` (the [[wikilink]] graph) and `pattern_applies` (which
projects) are first-class but populated later (P2 write-path).

Persona-agnostic: `surveyed_by` is a plain string id ('raven'/'rook'/'architect') —
no cabal↔techne coupling. Backend: SQLite via the #98 store; Postgres is P3.
"""

from __future__ import annotations

from pathlib import Path

from techne.config import Config
from techne.core import migrator
from techne.core.stores import StoreAdapter, migrated_store, plain_connect

ARCHITECT_VERSION = 6  # PRAGMA user_version on architect.db

_PATTERNS_FTS_DDL = """
-- `source_url` IS INDEXED; its absence was a silent false negative (techne#430).
-- The dedup step of investigate-url / survey-field asks `architect search "<url>"`
-- before spending a subagent on a repo. With only (name, body) indexed that query
-- STRUCTURALLY could not match a pattern by its source, and returned a confident
-- "No patterns matched" — so an already-surveyed URL got re-investigated, and the
-- caller was told the corpus had nothing. `unicode61` splits on `/`, `:` and `-`,
-- so a full URL and a bare `owner/repo` slug both tokenise into matchable terms.
CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
    name, body, source_url, content=patterns, content_rowid=rowid,
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS patterns_ai AFTER INSERT ON patterns BEGIN
    INSERT INTO patterns_fts(rowid, name, body, source_url)
    VALUES (NEW.rowid, NEW.name, NEW.body, NEW.source_url);
END;
CREATE TRIGGER IF NOT EXISTS patterns_ad AFTER DELETE ON patterns BEGIN
    INSERT INTO patterns_fts(patterns_fts, rowid, name, body, source_url)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.body, OLD.source_url);
END;
CREATE TRIGGER IF NOT EXISTS patterns_au AFTER UPDATE ON patterns BEGIN
    INSERT INTO patterns_fts(patterns_fts, rowid, name, body, source_url)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.body, OLD.source_url);
    INSERT INTO patterns_fts(rowid, name, body, source_url)
    VALUES (NEW.rowid, NEW.name, NEW.body, NEW.source_url);
END;"""

#: The FTS block is defined ONCE, above, and interpolated here — the v5→v6 step
#: recreates it from the same constant, so the live schema and the migration cannot
#: drift into two derivations of one DDL (techne#430).
ARCHITECT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS investigations (
    id           TEXT PRIMARY KEY,            -- hash(url, surveyed_by, lens)
    url          TEXT NOT NULL,
    surveyed_by  TEXT NOT NULL,               -- raven | rook | architect | …
    lens         TEXT NOT NULL,               -- infra | sre | product | …
    surveyed_at  TEXT NOT NULL,
    -- Operator's judgment. Vocabulary is enforced in patterns.VERDICTS, not by a
    -- CHECK here (adding one to an existing table needs a full rebuild), and
    -- `unknown` marks rows auto-assigned before techne#428 made this settable.
    verdict      TEXT NOT NULL,               -- skip | skip-with-flag | patterns-only
                                              -- | full-eval | unknown
    summary      TEXT,
    skip_reason  TEXT,
    maturity     TEXT,
    UNIQUE(url, surveyed_by, lens)
);
CREATE INDEX IF NOT EXISTS idx_investigations_url ON investigations(url);

CREATE TABLE IF NOT EXISTS patterns (
    id               TEXT PRIMARY KEY,        -- content_hash
    investigation_id TEXT REFERENCES investigations(id),
    name             TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    source_path      TEXT,
    lens_kind        TEXT,                    -- code | product | process
    body             TEXT NOT NULL,
    confidence       TEXT DEFAULT 'medium',
    status           TEXT DEFAULT 'active',   -- active | superseded
    superseded_by    TEXT,                    -- id of the pattern that replaced this one
    topic            TEXT,                    -- problem-solved grouping key (techne#597 topic-index)
    absorbed         TEXT,                    -- salvage note: what this pattern contributed before being folded in
    valid_at         TEXT,                    -- when this pattern became current (NULL = since created_at) — techne#306
    invalid_at       TEXT,                    -- when it stopped being current / was superseded (NULL = still current) — techne#306
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_patterns_investigation ON patterns(investigation_id);
CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status);
-- NOTE: the index on `topic` is created by the v1→v2 migration step
-- (_step_v1_to_v2_topic_absorbed), NOT here. This schema string is executescript'd
-- on every open BEFORE the numbered migration runs; an index referencing `topic`
-- would crash the apply on an existing pre-v2 db where the column isn't added yet.

CREATE TABLE IF NOT EXISTS pattern_relations (
    src_pattern_id    TEXT NOT NULL REFERENCES patterns(id),
    dst_name          TEXT NOT NULL,           -- heading from [[architectural-patterns#<name>]]
    dst_pattern_id    TEXT REFERENCES patterns(id),  -- resolved target; NULL while dangling
    kind              TEXT NOT NULL DEFAULT 'related',  -- related | composes-with | refines
                                                        -- | duplicates | supersedes | contradicts
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',  -- resolved | unresolved
    PRIMARY KEY (src_pattern_id, dst_name, kind)
);
CREATE INDEX IF NOT EXISTS idx_pattern_relations_dst ON pattern_relations(dst_pattern_id);

CREATE TABLE IF NOT EXISTS pattern_applies (
    pattern_id TEXT REFERENCES patterns(id),
    target     TEXT NOT NULL,                 -- cabal | techne | limenal | agent-infra | …
    PRIMARY KEY (pattern_id, target)
);

CREATE TABLE IF NOT EXISTS pattern_outcomes (
    pattern_id  TEXT REFERENCES patterns(id),
    ref         TEXT NOT NULL,                -- 'tenishai/techne#385' | commit sha | doc path
    kind        TEXT NOT NULL DEFAULT 'issue',-- issue | pr | commit | decision
    state       TEXT NOT NULL DEFAULT 'filed',-- filed | shipped | rejected
    recorded_at TEXT NOT NULL,
    note        TEXT,
    PRIMARY KEY (pattern_id, ref)
);
CREATE INDEX IF NOT EXISTS idx_pattern_outcomes_state ON pattern_outcomes(state);

{_PATTERNS_FTS_DDL}
"""

# Process-global memoization keyed on db_path: prevents re-running the migration
# step on every architect_store() call within a process lifetime.
_migrated: set[Path] = set()
_reconciled: set[Path] = set()


def _step_v0_to_v1_reshape_relations(conn) -> None:
    """v0 → v1 step (techne#136; reshape introduced in #87 P2).

    Reshape `pattern_relations` from the resolved-only `(src, dst_pattern_id, kind)`
    to the unresolved-edge model (`dst_name` + nullable `dst_pattern_id` +
    `resolution_status`, the shape codex#89 uses for call edges). Idempotency is
    STRUCTURAL (the `dst_name`-presence guard), not version-based — a
    retrofit-versioned db reads `user_version = 0` regardless of shape. The table
    ships EMPTY in every existing db (no code path wrote a relation before #87 P2),
    so the DROP+recreate is data-safe; the engine takes a backup first regardless.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pattern_relations)")}
    if cols and "dst_name" not in cols:
        # ATOMIC (one BEGIN/COMMIT, matching codex's edges rebuild) so a crash can
        # never leave the table dropped-but-not-recreated.
        conn.executescript(
            "BEGIN;\n"
            "DROP TABLE pattern_relations;\n"
            "CREATE TABLE pattern_relations (\n"
            "    src_pattern_id    TEXT NOT NULL REFERENCES patterns(id),\n"
            "    dst_name          TEXT NOT NULL,\n"
            "    dst_pattern_id    TEXT REFERENCES patterns(id),\n"
            "    kind              TEXT NOT NULL DEFAULT 'related',\n"
            "    resolution_status TEXT NOT NULL DEFAULT 'unresolved',\n"
            "    PRIMARY KEY (src_pattern_id, dst_name, kind)\n"
            ");\n"
            "CREATE INDEX IF NOT EXISTS idx_pattern_relations_dst "
            "ON pattern_relations(dst_pattern_id);\n"
            "COMMIT;"
        )


def _step_v1_to_v2_topic_absorbed(conn) -> None:
    """v1 → v2 step (techne#308; topic-index redesign, decision techne#597).

    Add the `topic` grouping key and the `absorbed` supersession-salvage note to
    `patterns`. Structurally idempotent via a column-presence guard, so it is safe
    on a retrofit-versioned db (reads `user_version = 0` regardless of real shape)
    and on a re-run. Additive `ALTER TABLE … ADD COLUMN` — existing rows keep NULL.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(patterns)")}
    if "topic" not in cols:
        conn.execute("ALTER TABLE patterns ADD COLUMN topic TEXT")
    if "absorbed" not in cols:
        conn.execute("ALTER TABLE patterns ADD COLUMN absorbed TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_topic ON patterns(topic)")


def _step_v2_to_v3_validity_window(conn) -> None:
    """v2 → v3 step (techne#306; validity-windowed supersession).

    Add nullable `valid_at`/`invalid_at` timestamps to `patterns` so supersession
    carries a temporal window: a superseded pattern records WHEN it stopped being
    current (`invalid_at`), letting the read-path ask "what was current as of T?".
    Binary status is preserved — existing rows keep NULL (an open-ended window).
    Structurally idempotent (column-presence guard). No index in the base schema
    (the #315 lesson) and none here either — point-in-time filtering is rare."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(patterns)")}
    if "valid_at" not in cols:
        conn.execute("ALTER TABLE patterns ADD COLUMN valid_at TEXT")
    if "invalid_at" not in cols:
        conn.execute("ALTER TABLE patterns ADD COLUMN invalid_at TEXT")


def _step_v3_to_v4_canonicalise_liminal(conn) -> None:
    """v3 → v4 step (techne#674; collapse the pre-rename `limenal` target).

    `liminal` was renamed from `limenal` on 2026-08-11 and `_KNOWN_TARGETS` was
    not updated, so `backfill_graph` could only ever emit the OLD spelling while
    the direct-tag path wrote the new one. The live store ended up with the same
    11 patterns listed under BOTH targets — one project, two rows, and a read
    that answers half the truth depending on which spelling you ask for.

    Fixing the extractor alone does not repair existing rows: backfill re-derives
    from pattern BODIES, and those still carry the old word. So the rows are
    rewritten here, once.

    `INSERT OR IGNORE`-then-`DELETE` rather than `UPDATE`: the primary key is
    (pattern_id, target), so a straight UPDATE collides on every pattern that
    already carries both spellings — which is all 11 of them. Idempotent by
    construction; a second run finds no `limenal` rows and does nothing.

    TABLE-PRESENCE GUARD, not decoration: this ladder is re-applied from
    `user_version = 0` on retrofit-versioned stores, so the step can run against
    a db predating `pattern_applies` entirely. Every other step guards on column
    presence for the same reason; this one guards on the table."""
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pattern_applies'"
    ).fetchone()
    if not have:
        return
    conn.execute(
        "INSERT OR IGNORE INTO pattern_applies (pattern_id, target) "
        "SELECT pattern_id, 'liminal' FROM pattern_applies WHERE target = 'limenal'"
    )
    conn.execute("DELETE FROM pattern_applies WHERE target = 'limenal'")


def _step_v4_to_v5_pattern_outcomes(conn) -> None:
    """v4 → v5 step (techne#426; bidirectional pattern provenance).

    Adds `pattern_outcomes` — the return path the corpus never had. A pattern
    already records where it came FROM (`source_url`, `investigation_id`); this
    records what it BECAME: the issue/PR/commit it turned into and whether that
    shipped. Without it the loop is one-way — downstream work cites its pattern,
    the pattern learns nothing back — so dedup cannot ask "has this already been
    filed or built?" and "which investigations actually paid?" is unanswerable.

    A junction, not columns on `patterns`, because the relation is many-to-many:
    one pattern can spawn issues in several repos, and one issue can cite several
    patterns. Mirrors the existing `pattern_applies` shape.

    Structurally idempotent (CREATE TABLE/INDEX IF NOT EXISTS), so it is safe on
    a retrofit-versioned db and on re-run, per the ladder's contract."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pattern_outcomes ("
        " pattern_id  TEXT REFERENCES patterns(id),"
        " ref         TEXT NOT NULL,"
        " kind        TEXT NOT NULL DEFAULT 'issue',"
        " state       TEXT NOT NULL DEFAULT 'filed',"
        " recorded_at TEXT NOT NULL,"
        " note        TEXT,"
        " PRIMARY KEY (pattern_id, ref))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pattern_outcomes_state ON pattern_outcomes(state)")


def _step_v5_to_v6_index_source_url(conn) -> None:
    """Add `source_url` to patterns_fts and rebuild (techne#430).

    AN fts5 EXTERNAL-CONTENT TABLE CANNOT GAIN A COLUMN IN PLACE — there is no
    ALTER for a virtual table, and the triggers that feed it name their columns
    positionally. So the table and all three triggers are dropped and recreated
    from SCHEMA_SQL, then `rebuild` repopulates the index from `patterns`.

    REBUILD IS THE POINT, not a tidy-up: recreating the table alone leaves it EMPTY
    on an existing store, which would turn a silent false negative into a total one.
    Structurally idempotent — dropping what may not exist and recreating from the
    canonical DDL is safe on a retrofit-versioned db that re-runs the ladder.
    """
    # THE LADDER RUNS FROM WHEREVER THE STORE IS, INCLUDING v0, so `patterns` may not
    # carry `source_url` yet when this step is reached — an external-content fts5 table
    # resolves its columns against the content table at REBUILD time, and a missing one
    # fails with `no such column: T.source_url`. Column-presence guard, matching the
    # style the rest of the ladder uses: if the content column is not there, the store
    # predates it and there is nothing to index.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(patterns)")}
    if "source_url" not in cols:
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='patterns_fts'"
    ).fetchone()
    if row and "source_url" in (row[0] or ""):
        return  # already the v6 shape
    # ATOMIC (one BEGIN/COMMIT, matching _step_v0_to_v1_reshape_relations above and
    # codex's edges rebuild) — and here the reason is sharper than "never leave the
    # table dropped". `plain_connect` runs DDL in autocommit, so without this the DROPs
    # and CREATE are durable the instant they execute while the `rebuild` INSERT waits
    # on the runner's commit. An interruption in that window leaves the NEW-SHAPED
    # table durable and EMPTY — and the shape guard above then short-circuits every
    # retry, so `user_version` reaches 6 over an index holding nothing.
    #
    # MEASURED by Prudence across both crash windows: `search url: 0` AND
    # `search name: 0` — not a partial degradation, the whole pattern index gone,
    # permanently, with the store claiming it is migrated. The guard cannot repair it
    # because it keys on the table's SHAPE while the invariant that matters is that the
    # index is POPULATED, and those two come apart exactly when the step is interrupted.
    conn.executescript(
        "BEGIN;\n"
        "DROP TRIGGER IF EXISTS patterns_ai;\n"
        "DROP TRIGGER IF EXISTS patterns_ad;\n"
        "DROP TRIGGER IF EXISTS patterns_au;\n"
        "DROP TABLE IF EXISTS patterns_fts;\n"
        + _PATTERNS_FTS_DDL
        + "\nINSERT INTO patterns_fts(patterns_fts) VALUES ('rebuild');\n"
        "COMMIT;\n"
    )


def _architect_run_steps(conn, current: int, target: int) -> None:
    """Apply architect.db migration steps from *current* up to *target*.

    Each step is individually, STRUCTURALLY idempotent (column-presence guards),
    so a retrofit-versioned db (`user_version = 0` regardless of real shape)
    re-applies the ladder safely. Per the `guarded_migrate` contract, the runner
    owns the single version stamp — written once here after all steps succeed.
    """
    if current < 1:
        _step_v0_to_v1_reshape_relations(conn)
    if current < 2:
        _step_v1_to_v2_topic_absorbed(conn)
    if current < 3:
        _step_v2_to_v3_validity_window(conn)
    if current < 4:
        _step_v3_to_v4_canonicalise_liminal(conn)
    if current < 5:
        _step_v4_to_v5_pattern_outcomes(conn)
    if current < 6:
        _step_v5_to_v6_index_source_url(conn)
    migrator.user_version_io.write(conn, target)
    conn.commit()


def _reconcile_relations(db_path: Path) -> None:
    """Run architect.db schema migrations through the shared engine (techne#136, #308).

    Routed through `guarded_migrate`: cross-process lock + online backup, and a
    double-checked version guard that no-ops the moment the db is already at
    `ARCHITECT_VERSION`. Steps are structurally idempotent. Kept under this name
    because `architect_store()` and the tests call it directly.
    """
    conn = plain_connect(db_path)
    try:
        migrator.guarded_migrate(
            conn,
            db_path,
            target_version=ARCHITECT_VERSION,
            read_version=migrator.user_version_io.read,
            run_steps=_architect_run_steps,
            label="architect schema",
        )
    finally:
        conn.close()


def architect_store(config: Config) -> StoreAdapter:
    """The Architect's store (architect.db), schema applied. Patterns route through
    here, not techne.db. Resolved via the registry, so a `config.stores['architect']`
    dsn override takes effect (techne#102); default <data_dir>/architect.db."""
    store = migrated_store(config, "architect", ARCHITECT_SCHEMA, _migrated)
    if store.db_path not in _reconciled:
        _reconcile_relations(store.db_path)
        _reconciled.add(store.db_path)
    return store


def architect_db(config: Config) -> Path:
    return architect_store(config).db_path
