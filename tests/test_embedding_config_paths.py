"""techne#1260: the embedding backend/model override reaches the query and backfill paths.

`init` built the index with `search.embedding_backend` / `.embedding_model`, but the
query path (`search._query_embedding`) and `backfill_vectors` called the resolver with
no config, so they embedded with the AUTO default: the vector channel then saw a
mismatched index stamp, and a later backfill re-embedded everything with the default.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import techne.core.embeddings as emb
import techne.core.search as search_mod

CUSTOM = ("fastembed", "example/custom-embedder")


class _Fake:
    name = f"{CUSTOM[0]}:{CUSTOM[1]}"
    dim = 4

    def embed(self, texts):
        np = pytest.importorskip("numpy")
        return [np.ones(4, dtype="float32") for _ in texts]


@pytest.fixture
def built(monkeypatch):
    """Write an override into the (test-isolated) config.yaml and record what the
    resolver builds."""
    home = Path(os.environ["TECHNE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"search:\n  embedding_backend: {CUSTOM[0]}\n  embedding_model: {CUSTOM[1]}\n"
    )
    calls: list[tuple[str, str]] = []

    def _build(backend, model_name, *, allow_download=True):
        calls.append((backend, model_name))
        return _Fake()

    emb._reset_embedding_model()
    monkeypatch.setattr(emb, "_build_backend", _build)
    yield calls
    emb._reset_embedding_model()


def test_query_path_uses_configured_backend(built, monkeypatch):
    from techne.core import vec_ext

    pytest.importorskip("numpy")
    monkeypatch.setattr(vec_ext, "available", lambda: True)
    monkeypatch.setattr(emb, "backend_available", lambda: True)
    search_mod._query_embedding("which deploy method")
    assert built == [CUSTOM]


def test_numpy_free_query_path_defers_to_the_configured_backend(built):
    """The potion fast path must not answer for an operator who chose fastembed."""
    from techne.core import query_embed

    assert query_embed._model_name(None) is None


def test_backfill_uses_configured_backend(built, tmp_path):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("numpy")
    if not emb.backend_available():
        pytest.skip("backfill embeds only with a backend installed")
    from techne.core.database import init_db

    db = tmp_path / "t.db"
    init_db(db).close()
    search_mod.backfill_vectors(db, allow_download=False)
    assert built == [CUSTOM]


def test_explicit_config_still_wins(built):
    from techne.config import Config

    cfg = Config()
    cfg.search.embedding_backend = "model2vec"
    cfg.search.embedding_model = "example/explicit"
    emb._get_embedding_model(cfg)
    assert built == [("model2vec", "example/explicit")]
