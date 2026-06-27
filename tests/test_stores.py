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
