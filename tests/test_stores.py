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


def test_add_get_search(cfg):
    store = load_store(cfg, "librarian")
    store.migrate(_SCHEMA)
    store.add("notes", {"id": 1, "content": "decided to use SQLite for the store"})
    store.add("notes", {"id": 2, "content": "unrelated note about cabbages"})

    got = store.get("notes", 1)
    assert got["content"] == "decided to use SQLite for the store"
    assert store.get("notes", 99) is None

    hits = store.search("notes", "SQLite")
    assert [h["id"] for h in hits] == [1]


def test_search_blank_query_returns_empty(cfg):
    """#103: blank/whitespace must not crash (FTS5 MATCH '' is a syntax error)."""
    store = load_store(cfg, "librarian")
    store.migrate(_SCHEMA)
    store.add("notes", {"id": 1, "content": "decided to use SQLite for the store"})
    assert store.search("notes", "") == []
    assert store.search("notes", "   ") == []


@pytest.mark.parametrize("q", ["foo-", "a OR", 'unbalanced "quote', "NOT", "a AND", "*("])
def test_search_operator_bearing_query_does_not_crash(cfg, q):
    """techne#133: the FTS branch must sanitize, so operator/punctuation input
    can't raise an fts5 syntax error on this public StoreAdapter surface."""
    store = load_store(cfg, "librarian")
    store.migrate(_SCHEMA)
    store.add("notes", {"id": 1, "content": "SQLite store notes"})
    # Must return a list (possibly empty), never raise.
    assert isinstance(store.search("notes", q), list)


def test_search_like_fallback_without_fts(cfg):
    store = load_store(cfg, "x")
    store.migrate("CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, content TEXT);")
    store.add("docs", {"id": 1, "content": "alpha beta"})
    assert [h["id"] for h in store.search("docs", "beta")] == [1]


def test_search_like_fallback_escapes_metachars(cfg):
    """#104: % and _ in a LIKE-fallback query must match literally, not as wildcards."""
    store = load_store(cfg, "x")
    store.migrate("CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, content TEXT);")
    store.add("docs", {"id": 1, "content": "alpha beta"})
    store.add("docs", {"id": 2, "content": "100% done"})
    # '%' must not behave as a wildcard (would match everything if unescaped)
    assert [h["id"] for h in store.search("docs", "%")] == [2]
    # '_' must match a literal underscore, not any single char
    assert store.search("docs", "a_p") == []


def test_unknown_adapter_raises(cfg):
    cfg.stores["architect"] = StoreSpec(adapter="postgres", dsn="postgresql://localhost/x")
    with pytest.raises(ValueError, match="not built yet"):
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
