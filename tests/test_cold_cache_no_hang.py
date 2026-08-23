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


def test_backfill_may_still_download(monkeypatch, tmp_path):
    """The counterpart, pinned BEHAVIOURALLY (review of #728, Important 3).

    An earlier version of this test grepped `inspect.getsource` for the literal
    `_get_embedding_model()`. That failed in BOTH directions, measured: guarding
    the real call while leaving the bare form in an adjacent comment passed
    silently, and the benign refactor `_get_embedding_model(None)` false-alarmed.
    A source-text assertion pins the spelling, not the behaviour.

    Why it must stay permissive: README documents `techne vectors backfill` as
    the cold-cache recovery, and after #708 this is the ONLY call in the package
    that may download. Guard it too and a fresh install can never acquire its
    weights — techne#496's never-green shape from the other side.
    """
    import sys

    seen: list[dict] = []

    class _StubVec:
        @staticmethod
        def load(conn):  # pragma: no cover - trivial
            return None

    def _spy(config=None, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop here — we only care what was asked")

    monkeypatch.setitem(sys.modules, "numpy", type("m", (), {})())
    monkeypatch.setitem(sys.modules, "sqlite_vec", _StubVec())
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(search_mod, "_get_embedding_model", _spy)

    with pytest.raises(Exception):
        search_mod.backfill_vectors(tmp_path / "x.db")

    assert seen, (
        "backfill_vectors never reached the embedding resolver — the test no "
        "longer exercises the thing it pins"
    )
    assert seen[0].get("allow_download") is not False, (
        "backfill_vectors was guarded with allow_download=False; it is the only "
        "path that may fetch the weights, so guarding it strands every fresh "
        "install on FTS-only forever"
    )


def test_the_degrade_is_not_silent(monkeypatch, tmp_path, capsys):
    """Restores a signal this fix would otherwise DELETE (review of #728).

    Pre-#708 there was no try/except here, so a construction failure propagated
    to user_prompt_submit's handler, which printed to stderr and made _record
    write outcome="failed" — the very thing _record's docstring exists to
    guarantee. Returning [] silently would make a rail whose every search fails
    report LIVE.
    """
    import sys

    monkeypatch.setattr(search_mod, "_EMBED_UNAVAILABLE_WARNED", False)
    monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
    monkeypatch.setattr(
        search_mod,
        "_get_embedding_model",
        lambda *a, **k: (_ for _ in ()).throw(_Uncached("weights absent")),
    )
    monkeypatch.setitem(sys.modules, "sqlite_vec", type("m", (), {})())
    monkeypatch.setitem(sys.modules, "numpy", type("m", (), {})())

    search_mod.search_vec(tmp_path / "x.db", "q")
    first = capsys.readouterr().err
    assert "vector recall unavailable" in first
    assert "techne vectors backfill" in first, "the warning must name the remedy"

    # ONCE PER PROCESS, not once per turn — this is a per-prompt hook path.
    search_mod.search_vec(tmp_path / "x.db", "q")
    assert capsys.readouterr().err == "", "the warning repeated on a later turn"
