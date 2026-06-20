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
from techne.core.stores import StoreAdapter, migrated_store

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
    src_pattern_id TEXT REFERENCES patterns(id),
    dst_pattern_id TEXT REFERENCES patterns(id),
    kind           TEXT NOT NULL,             -- composes-with | duplicates | supersedes
                                              -- | contradicts | refines | related
    PRIMARY KEY (src_pattern_id, dst_pattern_id, kind)
);

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

_migrated: set[Path] = set()


def architect_store(config: Config) -> StoreAdapter:
    """The Architect's store (architect.db), schema applied. Patterns route through
    here, not techne.db. Resolved via the registry, so a `config.stores['architect']`
    dsn override takes effect (techne#102); default <data_dir>/architect.db."""
    return migrated_store(config, "architect", ARCHITECT_SCHEMA, _migrated)


def architect_db(config: Config) -> Path:
    return architect_store(config).db_path
