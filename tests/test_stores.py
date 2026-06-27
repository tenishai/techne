"""Tests for the domain store registry + SQLite StoreAdapter (techne#98)."""

from __future__ import annotations

import pytest

from techne.config import Config, StoreSpec
from techne.core.stores import SqliteStore, StoreAdapter, load_store, store_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, content TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
  USING fts5(content, content='notes', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;
"""


@pytest.fixture()
def cfg(tmp_path):
    c = Config()
    c.data_dir = tmp_path
    return c


def test_load_store_defaults_to_sqlite_under_data_dir(cfg, tmp_path):
    store = load_store(cfg, "librarian")
    assert isinstance(store, SqliteStore)
    assert isinstance(store, StoreAdapter)  # satisfies the Protocol
    assert store.db_path == tmp_path / "librarian.db"
    assert store.domain == "librarian"


def test_store_path_honors_configured_dsn(cfg, tmp_path):
    cfg.stores["architect"] = StoreSpec(adapter="sqlite", dsn=f"sqlite:///{tmp_path}/custom.db")
    assert store_path(cfg, "architect") == tmp_path / "custom.db"


def test_migrate_and_health_and_separate_file(cfg, tmp_path):
    store = load_store(cfg, "librarian")
    assert not store.db_path.exists()
    store.migrate(_SCHEMA)
    assert store.db_path.exists()  # its own file, not techne.db
    assert store.db_path != cfg.db_path
    assert store.health() is True


def test_get_keyed_lookup(cfg):
    """`get` is the one cross-domain read on the Protocol (the cards federation).
    Domains do their own writes via connect(); add/search were removed (techne#135)."""
    store = load_store(cfg, "librarian")
    store.migrate(_SCHEMA)
    conn = store.connect()
    try:
        conn.execute("INSERT INTO notes (id, content) VALUES (1, 'a domain row')")
        conn.commit()
    finally:
        conn.close()

    got = store.get("notes", 1)
    assert got["content"] == "a domain row"
    assert store.get("notes", 99) is None


def test_storeadapter_protocol_surface_is_minimal(cfg):
    """The Protocol is the honest cross-domain surface only — no portable
    add/search (techne#135)."""
    store = load_store(cfg, "librarian")
    assert isinstance(store, StoreAdapter)
    assert not hasattr(store, "add")
    assert not hasattr(store, "search")
    for m in ("migrate", "health", "get", "connect"):
        assert hasattr(store, m)


def test_unknown_adapter_raises(cfg):
    """A non-sqlite adapter is a config error — techne is SQLite-only (techne#135)."""
    cfg.stores["architect"] = StoreSpec(adapter="postgres", dsn="postgresql://localhost/x")
    with pytest.raises(ValueError, match="SQLite-only"):
        load_store(cfg, "architect")


def test_config_parses_stores_block():
    raw = {
        "stores": {
            "librarian": {"adapter": "sqlite", "dsn": "sqlite:///~/.techne/librarian.db"},
            "architect": {"adapter": "postgres", "dsn": "postgresql://db/x"},
        }
    }
    cfg = Config._from_dict(raw)
    assert cfg.stores["librarian"].adapter == "sqlite"
    assert cfg.stores["librarian"].dsn.endswith("librarian.db")
    assert cfg.stores["architect"].adapter == "postgres"


def test_stores_roundtrip_through_save(tmp_path):
    cfg = Config()
    cfg.stores["librarian"] = StoreSpec(adapter="sqlite", dsn="sqlite:///x/librarian.db")
    p = tmp_path / "config.yaml"
    cfg.save(p)
    loaded = Config.load(p)
    assert loaded.stores["librarian"].adapter == "sqlite"
    assert loaded.stores["librarian"].dsn == "sqlite:///x/librarian.db"


def test_migrated_store_reconcile_adds_missing_column_and_index(cfg):
    """techne#136: migrated_store(reconcile=True) converges an upgraded domain db
    toward the schema — a column/index added to the DDL lands on an existing db
    that the bare CREATE IF NOT EXISTS would have skipped."""
    from techne.core.stores import migrated_store

    # Seed an "old" domain db: a docs table missing the `kind` column + its index.
    store = load_store(cfg, "demo")
    store.migrate("CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, content TEXT);")

    cols_before = {r[1] for r in store.connect().execute("PRAGMA table_info(docs)")}
    assert "kind" not in cols_before

    # The full schema adds a `kind` column AND an index on it (multi-line CREATE
    # TABLE, as real schemas are — _schema_columns parses the `…\n);` block). A
    # blanket executescript would fail ("no such column: kind") because the index
    # runs before the column is added — reconcile_schema does columns first.
    full_schema = (
        "CREATE TABLE IF NOT EXISTS docs (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    content TEXT,\n"
        "    kind TEXT\n"
        ");\n"
        "CREATE INDEX IF NOT EXISTS idx_docs_kind ON docs(kind);\n"
    )
    migrated_store(cfg, "demo", full_schema, set(), reconcile=True)

    conn = store.connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert "kind" in cols
    assert "idx_docs_kind" in idx


def test_reconcile_schema_fresh_creates_fts_vtable_and_triggers(cfg):
    """The invariant PR4 rests on (techne#136): reconcile_schema fully provisions a
    FRESH db — FTS virtual table + triggers + indexes — equivalent to the old
    executescript(LIBRARIAN_SCHEMA), and the FTS path actually works end-to-end."""
    from techne.core import migrator
    from techne.core.librarian import LIBRARIAN_SCHEMA

    store = load_store(cfg, "lib_fresh")
    conn = store.connect()
    try:
        migrator.reconcile_schema(conn, LIBRARIAN_SCHEMA, triggers_sql=LIBRARIAN_SCHEMA)
        objs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for name in (
            "documents",
            "documents_fts",
            "documents_ai",
            "documents_ad",
            "documents_au",
            "idx_documents_source_url",
            "idx_documents_source_type",
            "idx_documents_superseded",
        ):
            assert name in objs, f"reconcile_schema did not create {name}"
        # End-to-end: insert fires documents_ai → FTS populated → MATCH finds it.
        conn.execute(
            "INSERT INTO documents (source_type, source_url, content) "
            "VALUES ('web', 'http://x', 'hello world from the librarian')"
        )
        conn.commit()
        hit = conn.execute(
            "SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'librarian'"
        ).fetchone()
        assert hit is not None
    finally:
        conn.close()
