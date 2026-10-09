"""techne#1049 — a backend swap empties the vec tables; say so, and never rank in the wrong space.

`_ensure_vec_backend_identity` drops the vec tables on a swap and `backfill_vectors`
refills them. Until it finishes, vector recall is FTS-only. These tests pin the three
pieces that make that window visible: the `vec_reembed` marker, `vectors:coverage`
naming it, and the query path refusing an index another backend built.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sqlite_vec")

from techne.config import Config
from techne.core import embeddings as emb_mod
from techne.core import search as search_mod
from techne.core.checks import check_vector_coverage
from techne.core.database import connect, init_db
from techne.core.extension_state import get_state
from techne.core.state import add_observation, start_session


class FakeBackend:
    def __init__(self, name, dim, fail_after=None):
        self.name, self.dim = name, dim
        self.fail_after = fail_after
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("interrupted")
        return [np.full(self.dim, 0.5, dtype="float32") for _ in texts]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "techne.db"
    init_db(db).close()
    sid = start_session(db, project_path="/tmp/p")
    for i in range(5):
        add_observation(db, sid, "Edit", f"edit {i}")
    conn = connect(db)
    conn.execute("UPDATE observations SET timestamp = datetime('now', '-60 minutes')")
    conn.commit()
    conn.close()
    cfg = Config()
    cfg.db_path, cfg.data_dir = db, tmp_path

    def use(backend):
        monkeypatch.setattr(search_mod, "_get_embedding_model", lambda **kw: backend)
        monkeypatch.setattr(emb_mod, "_embedding_model", backend)
        monkeypatch.setattr(emb_mod, "backend_available", lambda: True)

    yield db, cfg, use
    monkeypatch.setattr(emb_mod, "_embedding_model", None)


def _marker(db):
    conn = connect(db)
    try:
        return get_state(conn, "vec_reembed")
    finally:
        conn.close()


def test_swap_completes_and_clears_marker(env):
    db, cfg, use = env
    use(FakeBackend("A", 4))
    search_mod.backfill_vectors(db, batch_size=2)
    assert _marker(db) is None
    use(FakeBackend("B", 6))
    search_mod.backfill_vectors(db, batch_size=2)
    assert _marker(db) is None
    assert check_vector_coverage(cfg).verdict.name == "LIVE"


def test_interrupted_swap_keeps_marker_and_coverage_names_it(env):
    db, cfg, use = env
    use(FakeBackend("A", 4))
    search_mod.backfill_vectors(db, batch_size=2)
    use(FakeBackend("B", 6, fail_after=1))
    with pytest.raises(RuntimeError):
        search_mod.backfill_vectors(db, batch_size=2)
    m = _marker(db)
    assert m["from"] == {"name": "A", "dim": 4} and m["to"] == {"name": "B", "dim": 6}
    assert m["totals"]["observations_vec"] == 5
    res = check_vector_coverage(cfg)
    assert res.verdict.name == "STALE"
    assert "re-embedding after backend swap A/4 -> B/6" in res.detail
    assert "2 of 5 rows embedded" in res.detail
    assert "reembed_in_progress=True" in res.evidence

    use(FakeBackend("B", 6))  # resume
    search_mod.backfill_vectors(db, batch_size=2)
    assert _marker(db) is None
    assert "re-embedding" not in check_vector_coverage(cfg).detail


@pytest.mark.parametrize("new", [("B", 4), ("B", 6)], ids=["same-dim", "dim-change"])
def test_query_refuses_index_from_another_backend(env, new):
    db, _cfg, use = env
    use(FakeBackend("A", 4))
    search_mod.backfill_vectors(db)
    use(FakeBackend(*new))  # swapped, backfill not yet run
    search_mod.take_vec_channel_error()
    out = search_mod.search_vec(db, "edit", query_vec=np.zeros(new[1], dtype="float32"))
    assert out == []
    err = search_mod.take_vec_channel_error()
    assert err and "A/4" in err and f"{new[0]}/{new[1]}" in err


def test_query_unchanged_when_stamp_matches(env):
    db, _cfg, use = env
    use(FakeBackend("A", 4))
    search_mod.backfill_vectors(db)
    search_mod.take_vec_channel_error()
    out = search_mod.search_vec(db, "edit", query_vec=np.full(4, 0.5, dtype="float32"))
    assert out and search_mod.take_vec_channel_error() is None


def test_no_stamp_skips_guard(env):
    db, _cfg, use = env
    use(FakeBackend("A", 4))
    search_mod.backfill_vectors(db)
    conn = connect(db)
    conn.execute("DELETE FROM extension_state WHERE name = 'embedding_backend'")
    conn.commit()
    conn.close()
    search_mod.take_vec_channel_error()
    out = search_mod.search_vec(db, "edit", query_vec=np.full(4, 0.5, dtype="float32"))
    assert out and search_mod.take_vec_channel_error() is None
