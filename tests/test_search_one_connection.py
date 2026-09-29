"""techne#1094 — one connection, one sqlite-vec load and one query embed per search().

Before this, each channel opened its own connection: the FTS channel, the vector
channel, the #1078 identifier pool and the #946 curated pool. Measured 3.08
connections, 1.73 extension loads and 1.73 embeds per `search(table="all")`.

The counting test runs a REAL vec store with a stub embedding backend, and checks
that both extra paths fired (identifier pool, curated pool). Without that, a
count of one could mean the paths were skipped rather than shared.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core import embeddings as embeddings_mod
from techne.core import search as search_mod
from techne.core.database import connect, init_db
from techne.core.knowledge import store
from techne.core.state import ensure_session

sqlite_vec = pytest.importorskip("sqlite_vec")
np = pytest.importorskip("numpy")

DIM = 8
QUERY = "alpha zeta_key"


def _vec(text: str):
    """Knowledge rows sit far from the query and observations sit on it, so the
    vector head fills with observations and the #946 curated pool has a deficit."""
    v = np.zeros(DIM, dtype="float32")
    v[1 if "curated" in text else 0] = 1.0
    return v


class _StubBackend:
    name = "model2vec:stub-1094"
    dim = DIM

    def __init__(self):
        self.query_embeds = 0

    def embed(self, texts):
        texts = list(texts)
        if texts == [QUERY]:
            self.query_embeds += 1
        return [_vec(t) for t in texts]


@pytest.fixture
def vec_db(tmp_path, monkeypatch):
    db = tmp_path / "techne.db"
    init_db(db).close()
    ensure_session(db, "s-1094")
    kids = [store(db, f"alpha curated fact {i}", type="fact") for i in range(4)]
    conn = connect(db)
    try:
        obs = []
        for i in range(12):
            text = f"alpha observation {i}" + (" zeta_key" if i == 11 else "")
            cur = conn.execute(
                "INSERT INTO observations (session_id, content, tool_name) VALUES (?, ?, ?)",
                ("s-1094", text, "Bash"),
            )
            obs.append((cur.lastrowid, text))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        search_mod._ensure_vec_table(conn, "observations_vec", DIM)
        search_mod._ensure_vec_table(conn, "knowledge_vec", DIM)
        for oid, text in obs:
            conn.execute(
                "INSERT INTO observations_vec(rowid, embedding) VALUES (?, ?)",
                (oid, _vec(text).tobytes()),
            )
        for kid in kids:
            conn.execute(
                "INSERT INTO knowledge_vec(rowid, embedding) VALUES (?, ?)",
                (kid, _vec("curated").tobytes()),
            )
        conn.commit()
    finally:
        conn.close()

    backend = _StubBackend()
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(search_mod, "_get_embedding_model", lambda **_kw: backend)
    return db, backend


class _Counter:
    def __init__(self, monkeypatch):
        self.connects: list = []
        self.loads = 0
        real_connect, real_load = search_mod.connect, sqlite_vec.load

        def _connect(path):
            c = real_connect(path)
            self.connects.append(c)
            return c

        def _load(conn):
            self.loads += 1
            return real_load(conn)

        monkeypatch.setattr(search_mod, "connect", _connect)
        monkeypatch.setattr(sqlite_vec, "load", _load)


def _closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_search_opens_one_connection_loads_vec_once_and_embeds_once(vec_db, monkeypatch):
    db, backend = vec_db
    count = _Counter(monkeypatch)

    page = search_mod.search(db, QUERY, table="all", limit=10)

    # Both extra paths fired, so a count of one means they SHARED, not skipped.
    assert any(r.get("reserved_slot") == "curated_head" for r in page), (
        "fixture sanity: the #946 curated pool must run its own vector query"
    )
    assert search_mod._identifier_terms(QUERY), "fixture sanity: the #1078 pool must run"
    assert len(count.connects) == 1, f"{len(count.connects)} connections for one search()"
    assert count.loads == 1, f"sqlite-vec loaded {count.loads} times for one search()"
    assert backend.query_embeds == 1, f"query embedded {backend.query_embeds} times"
    assert _closed(count.connects[0]), "search() left its connection open"


def test_search_closes_its_connection_when_a_channel_raises(vec_db, monkeypatch):
    db, _backend = vec_db
    count = _Counter(monkeypatch)

    def _boom(*_a, **_kw):
        raise RuntimeError("channel failed")

    monkeypatch.setattr(search_mod, "_identifier_pool", _boom)

    with pytest.raises(RuntimeError, match="channel failed"):
        search_mod.search(db, QUERY, table="all", limit=10)
    assert len(count.connects) == 1
    assert _closed(count.connects[0]), "the connection leaked on the exception path"


def test_a_query_with_nothing_to_search_opens_no_connection(tmp_path, monkeypatch):
    """`search_fts` returns [] for an all-punctuation query without touching the
    store; with vectors off, search() as a whole must not open one either."""
    count = _Counter(monkeypatch)
    assert search_mod.search(tmp_path / "absent.db", "\\", use_vectors=False) == []
    assert count.connects == []
    assert not (tmp_path / "absent.db").exists()


# --- the standalone functions keep their contract --------------------------------


def test_standalone_search_fts_and_search_vec_open_and_close_their_own(vec_db, monkeypatch):
    db, backend = vec_db
    count = _Counter(monkeypatch)

    fts = search_mod.search_fts(db, QUERY, table="all", limit=10)
    vec = search_mod.search_vec(db, QUERY, table="all", limit=10)

    assert fts and vec
    assert len(count.connects) == 2, "each standalone call opens exactly one connection"
    assert all(_closed(c) for c in count.connects), "a standalone call leaked its connection"
    assert backend.query_embeds == 1, "search_vec without a vector embeds the query itself"


def test_standalone_functions_on_a_callers_connection_leave_it_open(vec_db, monkeypatch):
    db, backend = vec_db
    own_fts = search_mod.search_fts(db, QUERY, table="all", limit=10)
    own_vec = search_mod.search_vec(db, QUERY, table="all", limit=10)
    own_pool = search_mod._identifier_pool(db, QUERY, "all", 2, 0.5, own_fts)
    backend.query_embeds = 0

    conn = connect(db)
    try:
        count = _Counter(monkeypatch)
        qv = search_mod._query_embedding(QUERY)
        assert search_mod.search_fts(db, QUERY, table="all", limit=10, conn=conn) == own_fts
        assert search_mod.search_vec(db, QUERY, table="all", limit=10, conn=conn) == own_vec
        assert (
            search_mod.search_vec(db, QUERY, table="all", limit=10, conn=conn, query_vec=qv)
            == own_vec
        )
        assert search_mod._identifier_pool(db, QUERY, "all", 2, 0.5, own_fts, conn=conn) == (
            own_pool
        )
        assert own_pool, "fixture sanity: the identifier pool found the zeta_key row"
        assert count.connects == [], "a function given a connection opened another"
        assert count.loads == 1, "sqlite-vec must load once per connection, not per call"
        assert backend.query_embeds == 2, "query_vec must skip the embed; None must not"
        assert not _closed(conn), "a function closed the caller's connection"
    finally:
        conn.close()
