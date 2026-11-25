"""techne#1253: a vector-channel failure reaches techne_recall, techne_search and
`techne search`, not only the hooks.

The channel degrades to FTS rather than raising, so without a marker a thin
FTS-only answer over a broken index reads as "nothing known".
"""

from __future__ import annotations

import sqlite3

import pytest

import techne.core.search as search_mod
from techne import mcp_server
from techne.config import Config
from techne.core.database import init_db
from techne.core.knowledge import store

ERR = "vector index query failed: Dimension mismatch; run techne vectors backfill"


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path).close()
    store(cfg.db_path, "the deploy uses blue green cutover", type="fact")
    return cfg


@pytest.fixture
def broken_vec(monkeypatch):
    """The vector channel fails during the call, the way a broken index does."""

    def _fail(query):
        search_mod._VEC_CHANNEL_ERROR = ERR
        return None

    monkeypatch.setattr(search_mod, "_query_embedding", _fail)


def test_vec_channel_rows_sets_the_flag_on_operational_error():
    """The ANN handler swallowed OperationalError into [] without setting the flag."""

    class _Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("Dimension mismatch for query vector")

    search_mod.take_vec_channel_error()
    rows = search_mod._vec_channel_rows(
        _Broken(), b"", "threads", 5,
        include_archived=False, include_uncertain=False, row_chars=None,
    )
    assert rows == []
    err = search_mod.take_vec_channel_error()
    assert err and "Dimension mismatch" in err


def test_recall_reports_degraded(config, broken_vec):
    out = mcp_server._techne_recall({"topic": "deploy cutover"}, config)
    assert out["degraded"] == ERR
    assert search_mod.take_vec_channel_error() is None  # consumed, not left standing


def test_search_reports_degraded(config, broken_vec):
    out = mcp_server._techne_search({"query": "deploy cutover"}, config)
    assert out["degraded"] == ERR


def test_healthy_recall_has_no_degraded_and_ignores_a_stale_flag(config, monkeypatch):
    monkeypatch.setattr(search_mod, "_query_embedding", lambda q: None)
    search_mod._VEC_CHANNEL_ERROR = "left over from an earlier call"
    out = mcp_server._techne_recall({"topic": "deploy cutover"}, config)
    assert "degraded" not in out


def test_cli_search_reports_degraded(config, broken_vec, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))
    res = CliRunner().invoke(main, ["search", "deploy cutover"])
    assert res.exit_code == 0, res.output
    assert f"degraded: {ERR}" in res.output
