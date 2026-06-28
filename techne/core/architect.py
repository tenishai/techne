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

ARCHITECT_VERSION = 1  # PRAGMA user_version on architect.db

ARCHITECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id           TEXT PRIMARY KEY,            -- hash(url, surveyed_by, lens)
    url          TEXT NOT NULL,
    surveyed_by  TEXT NOT NULL,               -- raven | rook | architect | …
    lens         TEXT NOT NULL,               -- infra | sre | product | …
    surveyed_at  TEXT NOT NULL,
    verdict      TEXT NOT NULL,               -- skip | skip-with-flag | patterns-only | full-eval
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
    superseded_by    TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_patterns_investigation ON patterns(investigation_id);
CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status);

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

CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
    name, body, content=patterns, content_rowid=rowid,
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS patterns_ai AFTER INSERT ON patterns BEGIN
    INSERT INTO patterns_fts(rowid, name, body) VALUES (NEW.rowid, NEW.name, NEW.body);
END;
CREATE TRIGGER IF NOT EXISTS patterns_ad AFTER DELETE ON patterns BEGIN
    INSERT INTO patterns_fts(patterns_fts, rowid, name, body)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.body);
END;
CREATE TRIGGER IF NOT EXISTS patterns_au AFTER UPDATE ON patterns BEGIN
    INSERT INTO patterns_fts(patterns_fts, rowid, name, body)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.body);
    INSERT INTO patterns_fts(rowid, name, body) VALUES (NEW.rowid, NEW.name, NEW.body);
END;
"""

# Process-global memoization keyed on db_path: prevents re-running the migration
# step on every architect_store() call within a process lifetime.
_migrated: set[Path] = set()
_reconciled: set[Path] = set()


def _architect_v0_to_v1(conn, current: int, target: int) -> None:
    """architect.db v0 → v1 run-step (techne#136; reshape introduced in #87 P2).

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
    migrator.user_version_io.write(conn, target)
    conn.commit()


def _reconcile_relations(db_path: Path) -> None:
    """Versioned `pattern_relations` shape migration (techne#136).

    Now routed through the shared engine, gaining the cross-process lock + online
    backup it previously lacked (the old hand-rolled version had neither, justified
    by the table being provably empty — true, but unnecessary now that the engine
    makes locking + backup free). Kept as a named entrypoint (tests call it).

    A fresh or already-migrated db — `pattern_relations` already has `dst_name`, or
    the table doesn't exist yet (schema-apply creates it new-shape) — just gets its
    version stamped, quietly. An old-shape db takes the guarded, backed-up reshape.
    """
    conn = plain_connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pattern_relations)")}
        if not cols or "dst_name" in cols:
            migrator.user_version_io.write(conn, ARCHITECT_VERSION)
            conn.commit()
            return
        migrator.guarded_migrate(
            conn,
            db_path,
            target_version=ARCHITECT_VERSION,
            read_version=migrator.user_version_io.read,
            run_steps=_architect_v0_to_v1,
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
