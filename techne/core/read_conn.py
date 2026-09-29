"""The MCP server's long-lived READ connection (techne#1095).

The MCP server is one long-lived process, but ``search()`` opened a fresh
connection per call, so every call started with a cold page cache, reloaded
sqlite-vec and read the store with ``mmap_size`` 0. ``ReadConnection`` keeps one
connection open for the server's search reads and hands it to ``search(conn=...)``.
Writes never go through it: it is opened ``query_only``, and every write path
keeps its own connections. Hooks and the CLI never build one; they are a new
process per call, so there is no cache to keep warm.

What keeps it correct:

* NO STALENESS. Python's sqlite3 runs a SELECT in autocommit mode, so each
  statement's read transaction ends when the statement is reset (its cursor is
  exhausted or freed), and the next call's first read takes a fresh WAL
  snapshot. Nothing here opens a transaction; ``run`` rolls back if one is
  somehow open when a call ends. ``tests/test_read_conn.py`` checks both halves:
  a write committed between two calls is seen by the second, and a
  ``wal_checkpoint(TRUNCATE)`` after a real search is not blocked by a reader.
  SQLite itself drops cached pages another connection has changed, so no
  ``data_version`` poll is needed for row freshness.
* INVALIDATION. At the start of every call: ``os.stat`` the path and compare
  ``(st_dev, st_ino)`` with the file the connection was opened on (a restore
  ``os.replace``-s a new file into place, and an open connection would keep
  reading the old, unlinked one), then compare ``PRAGMA schema_version`` and
  ``user_version`` with the values at open (a migration by another process). A
  mismatch closes the connection and opens a new one. The stat is taken BEFORE
  the open, so a swap racing the open costs one spare reconnect, never a missed one.
  Closing the connection to a replaced file leaves the new file's ``-wal`` and
  its committed rows alone; a test pins that with a writer holding the new WAL.
* FAILURE. Any ``sqlite3.Error`` or ``OSError`` from the cached connection drops
  it and runs the call again on a fresh connection (``fn(None)``). The cache can
  cost a retry, never a tool call. An error on the fresh connection propagates,
  exactly as it did before this module existed.
* THREADING. The server dispatches tool calls one at a time from its stdin loop
  (``mcp_server.main``): no threads, no asyncio. The lock is still taken around
  every use, non-blocking: if a second thread ever arrives while the connection
  is busy, that call uses a fresh connection instead of waiting or sharing.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from techne.core.database import connect

log = logging.getLogger(__name__)

T = TypeVar("T")

#: 512 MB of address space, not memory: SQLite maps the file and the kernel pages
#: in only what queries touch. Measured 2026-09-29 on a 259 MB copy of the
#: operator store (knowledge #3995): warm vec KNN 7.0 / 0.6 / 0.4 ms
#: (observations / knowledge / threads) against 21.4 / 1.7 / 1.2 ms on a fresh
#: connection, search(all,10) p50/p95 76/217 -> 54/188 ms. Larger than the store,
#: so the whole file can be mapped; SQLite maps no more than the file's size.
MMAP_SIZE = 512 * 1024 * 1024


class ReadConnection:
    """One long-lived, mmap'd, query-only connection, re-validated on every use."""

    def __init__(self, mmap_size: int = MMAP_SIZE) -> None:
        self._mmap_size = int(mmap_size)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._file_id: tuple[int, int] | None = None
        self._versions: tuple[int, int] | None = None
        #: Counters for tests and debugging: opens, and calls that fell back.
        self.opens = 0
        self.fallbacks = 0

    def run(self, db_path: Path, fn: Callable[[sqlite3.Connection | None], T]) -> T:
        """Return ``fn(conn)`` on the cached connection, or ``fn(None)`` without it.

        ``fn(None)`` must open its own connection, as ``search(conn=None)`` does.
        """
        if not self._lock.acquire(blocking=False):
            log.debug("read_conn: busy on another thread; this call opens its own")
            return fn(None)
        try:
            try:
                conn = self._validated(Path(db_path))
            except (sqlite3.Error, OSError):
                log.warning(
                    "read_conn: could not open or validate; using a fresh connection", exc_info=True
                )
                self._drop()
                conn = None
            if conn is None:
                return fn(None)
            try:
                return fn(conn)
            except (sqlite3.Error, OSError):
                log.warning(
                    "read_conn: long-lived connection failed; retrying on a fresh connection",
                    exc_info=True,
                )
                self._drop()
                self.fallbacks += 1
                return fn(None)
            finally:
                if self._conn is conn:
                    self._end_read(conn)
        finally:
            self._lock.release()

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock:
            self._drop()

    # --- internals (called with the lock held) ---

    def _validated(self, db_path: Path) -> sqlite3.Connection | None:
        """The cached connection if it still points at ``db_path``'s current file and schema."""
        try:
            st = os.stat(db_path)
        except FileNotFoundError:
            # No store yet: the fresh path creates it, exactly as before.
            self._drop()
            return None
        file_id = (st.st_dev, st.st_ino)
        if self._conn is not None:
            if self._path != db_path or self._file_id != file_id:
                log.debug("read_conn: %s replaced or path changed; reconnecting", db_path)
                self._drop()
            else:
                try:
                    changed = _versions(self._conn) != self._versions
                except sqlite3.Error:
                    log.debug("read_conn: cached connection unusable; reconnecting", exc_info=True)
                    changed = True
                if changed:
                    log.debug("read_conn: schema of %s changed; reconnecting", db_path)
                    self._drop()
        if self._conn is None:
            self._open(db_path, file_id)
        return self._conn

    def _open(self, db_path: Path, file_id: tuple[int, int]) -> None:
        conn = connect(db_path, check_same_thread=False)
        try:
            conn.execute(f"PRAGMA mmap_size={self._mmap_size}")
            # Search never writes; this makes a write through this connection an
            # error (and so a fallback) instead of a silent second write path.
            conn.execute("PRAGMA query_only=ON")
            versions = _versions(conn)
            if conn.in_transaction:
                conn.rollback()
        except BaseException:
            conn.close()
            raise
        self._conn, self._path, self._file_id, self._versions = conn, db_path, file_id, versions
        self.opens += 1
        log.debug("read_conn: opened %s (mmap_size=%d)", db_path, self._mmap_size)

    def _end_read(self, conn: sqlite3.Connection) -> None:
        # Autocommit reads leave no transaction; this is the backstop that keeps
        # a future explicit BEGIN from pinning the WAL snapshot into the next call.
        if conn.in_transaction:
            log.warning("read_conn: a call left a transaction open; rolling back")
            try:
                conn.rollback()
            except sqlite3.Error:
                self._drop()

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        self._path = self._file_id = self._versions = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                log.debug("read_conn: close failed", exc_info=True)


def _versions(conn: sqlite3.Connection) -> tuple[int, int]:
    """SQLite's schema cookie and the user_version header field."""
    schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    user = conn.execute("PRAGMA user_version").fetchone()[0]
    return int(schema), int(user)
