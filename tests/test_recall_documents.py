"""Folding librarian documents into recall (techne#340).

recall_documents() pulls top library docs as labelled recall rows; _fold_in_documents
labels memory rows origin='memory' and appends them, so a recall request returns
memory + library content, each clearly sourced.
"""

from __future__ import annotations

import pytest

from techne.core.documents import store_document
from techne.core.librarian import librarian_db, recall_documents


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    return Config.load()


def _seed(config, url, content, title=None):
    store_document(
        librarian_db(config), source_type="test", source_url=url, content=content, title=title
    )


def test_recall_documents_labels_and_snippets(config):
    _seed(config, "u1", "alpha beta rate limiting content " * 50, title="Rate Limits")
    rows = recall_documents(config, "rate limiting")

    assert rows, "expected a document match"
    d = rows[0]
    assert d["origin"] == "library"  # sourced, distinct from memory
    assert d["type"] == "document"
    assert d["title"] == "Rate Limits"
    assert d["source_url"] == "u1"  # full-text retrievable via `techne library show`
    assert len(d["content"]) <= 501  # capped snippet (500 + ellipsis)
    assert d["content"].endswith("…")  # truncation is visible


def test_recall_documents_limit_zero_disables(config):
    _seed(config, "u1", "rate limiting stuff")
    config.search.recall_documents_limit = 0
    assert recall_documents(config, "rate limiting") == []


def test_recall_documents_respects_limit(config):
    for i in range(5):
        _seed(config, f"u{i}", f"shared keyword doc number {i}")
    config.search.recall_documents_limit = 2
    assert len(recall_documents(config, "shared keyword")) == 2


def test_recall_documents_empty_store_is_empty(config):
    assert recall_documents(config, "anything") == []


def test_fold_in_labels_memory_and_appends_library(config):
    from techne.mcp_server import _fold_in_documents

    _seed(config, "u1", "folding keyword content here", title="Doc")
    memory = [{"source_table": "knowledge", "id": 1, "content": "m"}]
    merged = _fold_in_documents(config, "folding keyword", memory)

    origins = [r["origin"] for r in merged]
    assert origins[0] == "memory"  # existing memory row labelled in place
    assert "library" in origins  # a document appended
    assert merged[0] is memory[0]  # memory rows preserved, not copied


def test_recall_documents_limit_round_trips_through_config(tmp_path, monkeypatch):
    """The new SearchConfig field must survive to_dict → from_dict (asymmetry bug guard)."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    cfg = Config.load()
    cfg.search.recall_documents_limit = 7
    path = tmp_path / "roundtrip.yaml"
    cfg.save(path)
    assert Config.load(path).search.recall_documents_limit == 7
