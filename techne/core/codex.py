"""Codex domain — the repo/code-graph store, in its own db file (techne#89).

db-per-domain (`docs/db-per-domain.md`, proven by Librarian #99 + Architect #87): a
codebase is files → symbols → call/import/def-use edges. Stored RELATIONALLY on the
SQLite substrate (NOT a graph DB — that breaks the single-file/portable substrate).
Graph queries (reachability / blast-radius) are hand-rolled recursive CTEs in v1
(dep-free); graphqlite (Cypher + PageRank/Louvain as a SQLite UDF) is an optional
`techne[codex]` upgrade later, not core.

Scope here is the persona-agnostic STORAGE substrate — schema + the graph data layer.
Deferred (need the Codex persona scoped / optional deps): the Tree-sitter code
SourceAdapter that parses real repos, graph-boosted ranking, graphqlite, code vectors.
"""

from __future__ import annotations

from pathlib import Path

from techne.core.stores import SqliteStore

CODEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id         TEXT PRIMARY KEY,          -- stable id (qualified name / content hash)
    file       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- function | class | method | variable | module | …
    signature  TEXT,
    span       TEXT,                      -- "start:end" line range
    language   TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE TABLE IF NOT EXISTS edges (
    from_symbol TEXT NOT NULL REFERENCES symbols(id),
    to_symbol   TEXT NOT NULL REFERENCES symbols(id),
    kind        TEXT NOT NULL,            -- call | import | def-use
    PRIMARY KEY (from_symbol, to_symbol, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_symbol);

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


def codex_store(techne_db_path: Path) -> SqliteStore:
    """The Codex store (codex.db, beside the techne db), schema applied. Code lives
    here, not techne.db. config.stores dsn override is a follow-up (path-derived)."""
    db = Path(techne_db_path).parent / "codex.db"
    store = SqliteStore("codex", db)
    if db not in _migrated:
        store.migrate(CODEX_SCHEMA)
        _migrated.add(db)
    return store


def codex_db(techne_db_path: Path) -> Path:
    return codex_store(techne_db_path).db_path
