"""A cold HF cache must degrade to FTS, never block the turn (techne#708).

hooks/user_prompt_submit.py -> search() (use_vectors=True) -> search_vec()
reaches the embedding backend on EVERY prompt submission. With the default
allow_download=True and no cached model that is a Hub fetch, and a blocking
HTTP download is signal-deaf, so bounded_hook's SIGALRM cannot preempt it —
the turn hangs instead of degrading.
"""

from __future__ import annotations

import pytest

from techne.core import embeddings as embeddings_mod
from techne.core import search as search_mod


class _Uncached(Exception):
    """Stands in for LocalEntryNotFoundError / fastembed's offline raise."""


@pytest.fixture(autouse=True)
def _clear_singleton():
    embeddings_mod._reset_embedding_model()
    yield
    embeddings_mod._reset_embedding_model()


def test_search_vec_never_asks_to_download(monkeypatch, tmp_path):
    """THE TEETH: pin the kwarg, not just the outcome.

    Asserting only 'returns []' would keep passing if the guard were deleted,
    because an uncached model raises either way — it would just hang first in
    the real world, which a unit test cannot observe. So assert on what was
    ASKED of the resolver.
    """
    seen: list[dict] = []

    def _spy(config=None, **kwargs):
        seen.append(kwargs)
        raise _Uncached("model not in local cache")

    import sys

    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(search_mod, "_get_embedding_model", _spy)
    # sqlite_vec and numpy are both imported before the resolver is reached.
    # Real installs always have numpy when backend_available() is True (both
    # backends depend on it), so stubbing it here restores a reachable state
    # rather than inventing one.
    monkeypatch.setitem(
        sys.modules, "sqlite_vec", type("m", (), {"load": staticmethod(lambda c: None)})()
    )
    monkeypatch.setitem(sys.modules, "numpy", type("m", (), {})())

    out = search_mod.search_vec(tmp_path / "x.db", "a query")

    assert out == [], "an uncached model must degrade to FTS, not raise"
    assert seen, "search_vec did not reach the embedding resolver at all"
    assert seen[0].get("allow_download") is False, (
        "search_vec asked for a downloadable model on a hook-reachable path — "
        "that is the techne#708 hang"
    )


def test_backfill_may_still_download():
    """The counterpart: the CLI is the one path that MUST be able to fetch.

    README documents `techne vectors backfill` as the cold-cache recovery, so
    guarding it too would leave no way to ever warm the cache — the never-green
    shape of techne#496, on the other side.
    """
    import inspect

    src = inspect.getsource(search_mod.backfill_vectors)
    assert "_get_embedding_model()" in src, (
        "backfill_vectors must keep the permissive default; it is the only "
        "documented way to fetch the weights"
    )


def test_model_cached_is_false_when_backend_absent(monkeypatch):
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)
    assert embeddings_mod.model_cached() is False


def test_model_cached_is_false_when_weights_absent(monkeypatch):
    """The third state: package present, weights absent.

    backend_available() cannot see this — it is an import probe — which is why
    a fresh install reports a healthy backend and silently searches FTS-only.
    """
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(
        embeddings_mod,
        "_get_embedding_model",
        lambda *a, **k: (_ for _ in ()).throw(_Uncached("not cached")),
    )
    assert embeddings_mod.model_cached() is False


def test_model_cached_is_true_when_loadable(monkeypatch):
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(embeddings_mod, "_get_embedding_model", lambda *a, **k: object())
    assert embeddings_mod.model_cached() is True
