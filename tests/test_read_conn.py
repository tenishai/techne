"""techne#1095 — the MCP server's long-lived, mmap'd read connection.

Each test goes through the real tool handlers with `mcp_server._READER` set the
way `main()` sets it, and checks `reader.opens`, so a pass cannot come from the
handler quietly opening a fresh connection instead of using the cached one.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from techne import mcp_server
from techne.config import Config
from techne.core import read_conn as read_conn_mod
from techne.core.database import connect, init_db
from techne.core.knowledge import store
from techne.core.maintenance import backup_all_dbs, restore_all_dbs
from techne.core.read_conn import MMAP_SIZE, ReadConnection


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path).close()
    return cfg


@pytest.fixture
def reader(monkeypatch):
    r = ReadConnection()
    monkeypatch.setattr(mcp_server, "_READER", r)
    yield r
    r.close()


def _count(config, query="alphamarker"):
    out = mcp_server._techne_search({"query": query, "table": "knowledge"}, config)
    return out["count"]


def _recall_contents(config, topic="alphamarker"):
    out = mcp_server._techne_recall({"topic": topic}, config)
    return sorted(r["content"] for r in out["search_results"] if r["source_table"] == "knowledge")


def test_server_reuses_one_connection_with_mmap_and_query_only(config, reader):
    store(config.db_path, "alphamarker one", type="fact")
    for _ in range(3):
        assert _count(config) == 1
    assert _recall_contents(config) == ["alphamarker one"]
    assert reader.opens == 1 and reader.fallbacks == 0
    conn = reader._conn
    assert conn.execute("PRAGMA mmap_size").fetchone()[0] == MMAP_SIZE
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_no_reader_outside_the_server():
    """Only `main()` sets it: the CLI, hooks and a handler called directly get None."""
    assert mcp_server._READER is None


# --- no staleness -----------------------------------------------------------


def test_second_call_sees_a_write_committed_between_calls(config, reader):
    store(config.db_path, "alphamarker one", type="fact")
    assert _count(config) == 1
    assert _recall_contents(config) == ["alphamarker one"]
    # A write through another connection, between two tool calls.
    store(config.db_path, "alphamarker two", type="fact")
    assert _count(config) == 2
    assert _recall_contents(config) == ["alphamarker one", "alphamarker two"]
    assert reader.opens == 1  # the same connection saw it: no reconnect hid staleness


def _checkpoint_truncates(db_path) -> bool:
    """True when a TRUNCATE checkpoint finishes, i.e. no reader pins a WAL snapshot."""
    w = connect(db_path)
    try:
        busy, log_frames, _ = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return busy == 0 and log_frames == 0
    finally:
        w.close()


def test_a_call_leaves_no_read_transaction_pinning_the_wal(config, reader):
    for i in range(5):
        store(config.db_path, f"alphamarker {i}", type="fact")
    assert _count(config) == 5
    store(config.db_path, "alphamarker after", type="fact")  # new WAL frames
    assert _checkpoint_truncates(config.db_path)


def test_a_half_read_cursor_does_not_pin_the_wal(config, reader):
    """A statement left mid-result holds its snapshot until the cursor is freed.
    `run` returns only after `fn` has, so the cursor is gone by then."""
    for i in range(5):
        store(config.db_path, f"alphamarker {i}", type="fact")
    first = reader.run(
        config.db_path, lambda c: c.execute("SELECT id FROM knowledge ORDER BY id").fetchone()[0]
    )
    assert first == 1
    store(config.db_path, "alphamarker after", type="fact")
    assert _checkpoint_truncates(config.db_path)
    assert reader.opens == 1


def test_a_transaction_left_open_is_rolled_back_before_the_next_call(config, reader):
    store(config.db_path, "alphamarker one", type="fact")

    def begin_and_read(c):
        c.execute("BEGIN")
        return c.execute("SELECT count(*) FROM knowledge").fetchone()[0]

    assert reader.run(config.db_path, begin_and_read) == 1
    store(config.db_path, "alphamarker two", type="fact")
    assert _count(config) == 2
    assert reader.opens == 1


# --- invalidation -----------------------------------------------------------


@pytest.mark.parametrize(
    "ddl", ["CREATE TABLE migration_probe (x INTEGER)", "PRAGMA user_version = 42"]
)
def test_reconnects_after_an_out_of_process_schema_change(config, reader, ddl):
    store(config.db_path, "alphamarker one", type="fact")
    assert _count(config) == 1 and reader.opens == 1
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
            "c.execute(sys.argv[2]); c.commit(); c.close()",
            str(config.db_path),
            ddl,
        ],
        check=True,
    )
    assert _count(config) == 1
    assert reader.opens == 2


def test_reconnects_when_a_restore_replaces_the_file(config, reader):
    """`techne restore` deletes the sidecars and `os.replace`-s a new file into
    place. A connection that kept the old one open would keep answering from the
    unlinked file: the row written after the backup would still be there."""
    store(config.db_path, "alphamarker kept", type="fact")
    backup = backup_all_dbs(config).dest
    store(config.db_path, "alphamarker dropped", type="fact")
    assert _count(config) == 2 and reader.opens == 1
    inode_before = os.stat(config.db_path).st_ino

    restore_all_dbs(config, backup, pre_restore_backup=False)
    assert os.stat(config.db_path).st_ino != inode_before

    assert _recall_contents(config) == ["alphamarker kept"]
    assert _count(config) == 1
    assert reader.opens == 2


def test_dropping_the_old_connection_leaves_the_new_files_wal_alone(config, reader):
    """Closing a connection whose file was replaced must not checkpoint into, or
    delete, the NEW file's -wal. A writer holds the new store open with frames in
    its WAL while the reader notices the swap and closes its old connection."""
    store(config.db_path, "alphamarker kept", type="fact")
    backup = backup_all_dbs(config).dest
    assert _count(config) == 1
    restore_all_dbs(config, backup, pre_restore_backup=False)

    writer = connect(config.db_path)
    try:
        writer.execute(
            "INSERT INTO knowledge (type, content, confidence) VALUES ('fact', ?, 'high')",
            ("alphamarker live",),
        )
        writer.commit()
        wal = config.db_path.with_name(config.db_path.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        assert _count(config) == 2  # drops the old connection, opens the new file
        assert reader.opens == 2
        reader.close()
        assert wal.exists()
    finally:
        writer.close()
    check = sqlite3.connect(config.db_path)
    try:
        rows = check.execute("SELECT content FROM knowledge ORDER BY id").fetchall()
    finally:
        check.close()
    assert [r[0] for r in rows] == ["alphamarker kept", "alphamarker live"]


def test_a_missing_store_uses_the_fresh_path(tmp_path, reader):
    calls = []
    reader.run(tmp_path / "absent.db", lambda c: calls.append(c))
    assert calls == [None] and reader.opens == 0


# --- failure ----------------------------------------------------------------


def test_a_dead_cached_connection_is_replaced_at_the_start_of_a_call(config, reader):
    store(config.db_path, "alphamarker one", type="fact")
    assert _count(config) == 1
    reader._conn.close()  # the cached connection dies between calls
    assert _count(config) == 1
    assert reader.opens == 2 and reader.fallbacks == 0


def test_an_error_mid_search_falls_back_to_a_fresh_connection(config, reader, monkeypatch):
    """`database is locked` from the cached connection, part-way through a real
    search: the tool call still answers, from a fresh connection, and the next
    call reopens the cache."""
    from techne.core import search as search_mod

    store(config.db_path, "alphamarker one", type="fact")
    assert _count(config) == 1
    real_fts = search_mod.search_fts
    cached = reader._conn

    def locked_on_cache(*a, conn=None, **kw):
        if conn is cached:
            raise sqlite3.OperationalError("database is locked")
        return real_fts(*a, conn=conn, **kw)

    monkeypatch.setattr(search_mod, "search_fts", locked_on_cache)
    assert _count(config) == 1
    assert reader.fallbacks == 1 and reader._conn is None
    assert _count(config) == 1
    assert reader.opens == 2


def test_database_locked_falls_back_and_the_call_still_answers(config, reader):
    seen = []

    def fn(c):
        seen.append(c)
        if c is not None:
            raise sqlite3.OperationalError("database is locked")
        return "fresh"

    assert reader.run(config.db_path, fn) == "fresh"
    assert seen[0] is not None and seen[1] is None
    assert reader._conn is None and reader.fallbacks == 1


def test_an_error_on_the_fresh_connection_still_propagates(config, reader):
    def fn(c):
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        reader.run(config.db_path, fn)


# --- threading --------------------------------------------------------------


def test_a_second_thread_gets_a_fresh_connection_instead_of_sharing(config, reader):
    entered, release = threading.Event(), threading.Event()
    seen = {}

    def slow(c):
        seen["a"] = c
        entered.set()
        release.wait(5)
        return "a"

    t = threading.Thread(target=lambda: reader.run(config.db_path, slow))
    t.start()
    assert entered.wait(5)
    try:
        assert reader.run(config.db_path, lambda c: seen.setdefault("b", c) or "b") == "b"
    finally:
        release.set()
        t.join(5)
    assert seen["a"] is not None and seen["b"] is None


def test_the_connection_is_usable_from_another_thread(config, reader):
    store(config.db_path, "alphamarker one", type="fact")
    assert _count(config) == 1
    seen = []
    t = threading.Thread(target=lambda: seen.append(_count(config)))
    t.start()
    t.join(10)
    assert seen == [1] and reader.opens == 1 and reader.fallbacks == 0


# --- shutdown ---------------------------------------------------------------


def test_main_closes_the_reader_on_exit(config, monkeypatch):
    store(config.db_path, "alphamarker one", type="fact")
    made = []

    class Recording(ReadConnection):
        def __init__(self):
            super().__init__()
            made.append(self)

    monkeypatch.setattr(read_conn_mod, "ReadConnection", Recording)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "techne_search", "arguments": {"query": "alphamarker"}},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    mcp_server.main()

    body = json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])
    assert body["count"] == 1
    (reader,) = made
    assert reader.opens == 1  # the call went through it
    assert reader._conn is None  # and main closed it on EOF
    assert mcp_server._READER is None
