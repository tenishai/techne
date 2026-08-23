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


def test_model_cached_is_a_filesystem_probe_not_a_backend_build(monkeypatch, tmp_path):
    """THE #361 INVARIANT IS THE CONSTRAINT, and it is why this is a `Path.is_dir()`
    question rather than a resolver call.

    `test_collect_health_skips_backend_build_when_no_vectors` spies on
    `_get_embedding_model` and asserts `collect_health` never calls it — the load plus
    dim probe is 85-290 MB on the SessionStart path. An earlier attempt to answer
    "are the weights here?" in that branch called the resolver and broke exactly that.

    So: `model_cached` must answer without touching it.
    """
    from unittest.mock import MagicMock

    from techne.core import embeddings

    spy = MagicMock()
    monkeypatch.setattr(embeddings, "_get_embedding_model", spy)
    monkeypatch.setattr(embeddings, "_select_backend", lambda cfg: ("model2vec", "org/mdl"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))

    assert embeddings.model_cached(None) is False  # nothing on disk
    spy.assert_not_called()

    snap = tmp_path / "hub" / "models--org--mdl" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").write_text("weights")
    assert embeddings.model_cached(None) is True
    spy.assert_not_called()


def test_model_cached_says_None_when_it_cannot_tell(monkeypatch):
    """NONE IS NOT FALSE. Reporting "not cached" for a state we did not measure is the
    guess-dressed-as-a-fact this codebase keeps finding — and the caller (the doctor)
    must be able to stay quiet rather than tell an operator to run a backfill that
    would not help."""
    from techne.core import embeddings

    def no_backend(_cfg):
        raise ImportError("neither model2vec nor fastembed")

    monkeypatch.setattr(embeddings, "_select_backend", no_backend)
    assert embeddings.model_cached(None) is None

    monkeypatch.setattr(embeddings, "_select_backend", lambda cfg: ("unknown-backend", "m"))
    assert embeddings.model_cached(None) is None


def test_an_empty_snapshot_dir_is_not_cached(monkeypatch, tmp_path):
    """An interrupted fetch leaves the snapshot directory present and EMPTY — which is
    the state that produced techne#708's hang. Treating "directory exists" as cached
    would report a broken install as healthy."""
    from techne.core import embeddings

    monkeypatch.setattr(embeddings, "_select_backend", lambda cfg: ("model2vec", "org/mdl"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    (tmp_path / "hub" / "models--org--mdl" / "snapshots" / "abc").mkdir(parents=True)

    assert embeddings.model_cached(None) is False


def test_the_doctor_names_the_uncached_state_with_a_remedy(monkeypatch, tmp_path):
    """PRUDENCE, #728. `DORMANT` is documented as "off by configuration" and must name
    the key that disabled it. On an install where vectors were deliberately turned ON
    and the weights are simply absent, nothing disabled anything — the channel is
    BROKEN, not dormant, and after #708 it degrades silently forever because
    `backfill_vectors` is the only call left that may download.
    """
    from techne.config import Config
    from techne.core import checks, embeddings
    from techne.core.database import init_db

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    config = Config.load()
    init_db(config.db_path)  # fresh db: no vec tables, so the DORMANT branch is taken

    monkeypatch.setattr(embeddings, "backend_available", lambda: True)
    monkeypatch.setattr(embeddings, "model_cached", lambda cfg=None: False)

    res = checks.check_vector_coverage(config)

    assert res.verdict == checks.Verdict.DORMANT, "the verdict itself should not change"
    assert res.remedy == "techne vectors backfill", "no remedy — the operator is stuck"
    assert "not cached" in res.detail
