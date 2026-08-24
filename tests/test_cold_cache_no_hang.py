"""A cold HF cache must degrade to FTS, never block the turn (techne#708).

hooks/user_prompt_submit.py -> search() (use_vectors=True) -> search_vec()
reaches the embedding backend on EVERY prompt submission. With the default
allow_download=True and no cached model that is a Hub fetch, and a blocking
HTTP download is signal-deaf, so bounded_hook's SIGALRM cannot preempt it —
the turn hangs instead of degrading.
"""

from __future__ import annotations

from pathlib import Path

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

    # ONCE PER PROCESS. NOT once per turn on the hook path, which is what the comment
    # here used to imply (Prudence, #728 r3 nit 5): user_prompt_submit runs as a FRESH
    # SUBPROCESS per prompt, so the flag starts False every turn. The dedupe earns its
    # keep in a long-lived process — a CLI backfill loop, the MCP server — and this
    # asserts it there. The hook's signal is the MARKER, not this line; see
    # `test_a_dead_vector_channel_records_a_failed_marker`.
    search_mod.search_vec(tmp_path / "x.db", "q")
    assert capsys.readouterr().err == "", "the warning repeated within one process"


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


# ---------------------------------------------------------------------------
# #728 r2 Important 1 — every "cannot tell" was returning False
# ---------------------------------------------------------------------------


class TestModelCachedReadsTheDiskCorrectly:
    """The probe reported a fully-cached fastembed model as NOT cached, so
    `check_vector_coverage` printed "model weights not cached" with the weights on
    disk. A measurement claim, and it was wrong."""

    def _fastembed(self, monkeypatch, tmp_path, model):
        from techne.core import embeddings as E

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(E, "_select_backend", lambda cfg: ("fastembed", model))

    def test_a_cached_fastembed_model_reads_as_cached(self, monkeypatch, tmp_path):
        """fastembed names the directory after the SOURCE REPO, not the logical model:
        `sentence-transformers/all-MiniLM-L6-v2` lands in `models--qdrant--
        all-MiniLM-L6-v2-onnx`. Matching the logical name could never match, and could
        not for the rest of the Qdrant-hosted catalogue either."""
        from techne.core.embeddings import model_cached

        repo = tmp_path / ".cache" / "fastembed" / "models--qdrant--all-MiniLM-L6-v2-onnx"
        (repo / "snapshots" / "abc").mkdir(parents=True)
        (repo / "snapshots" / "abc" / "model.onnx").write_bytes(b"weights")
        self._fastembed(monkeypatch, tmp_path, "sentence-transformers/all-MiniLM-L6-v2")

        assert model_cached() is True

    def test_an_uncached_fastembed_model_still_reads_as_uncached(self, monkeypatch, tmp_path):
        """The looser match must not trade a false negative for a false positive."""
        from techne.core.embeddings import model_cached

        repo = tmp_path / ".cache" / "fastembed" / "models--qdrant--all-MiniLM-L6-v2-onnx"
        (repo / "snapshots" / "abc").mkdir(parents=True)
        (repo / "snapshots" / "abc" / "model.onnx").write_bytes(b"weights")
        self._fastembed(monkeypatch, tmp_path, "BAAI/bge-small-en-v1.5")

        assert model_cached() is False

    def test_an_interrupted_fastembed_fetch_is_not_cached(self, monkeypatch, tmp_path):
        """`snapshot_download` creates `<repo>/refs/` BEFORE any weight lands, so a
        Ctrl-C'd fetch read as cached — which is the state that hangs the turn. The
        model2vec branch already knew this; fastembed had no equivalent."""
        from techne.core.embeddings import model_cached

        repo = tmp_path / ".cache" / "fastembed" / "models--qdrant--all-MiniLM-L6-v2-onnx"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("deadbeef")
        self._fastembed(monkeypatch, tmp_path, "sentence-transformers/all-MiniLM-L6-v2")

        assert model_cached() is False

    def test_a_name_match_without_weights_does_not_stop_the_search(self, monkeypatch, tmp_path):
        """ORDER-DEPENDENT, WHICH IS WORSE THAN WRONG (Prudence, #728 r3 nit 1). The loop
        used to `return` on the FIRST directory whose flattened name matched, so an
        interrupted repo sorting ahead of a complete one answered False with the weights
        on disk — the r2 false negative again, and it would not reproduce.

        Both directories here match the needle `bgesmallen`; only the second has weights.
        """
        from techne.core.embeddings import model_cached

        cache = tmp_path / ".cache" / "fastembed"
        (cache / "models--baai--bge-small-en" / "refs").mkdir(parents=True)
        complete = cache / "models--qdrant--bge-small-en-onnx" / "snapshots" / "abc"
        complete.mkdir(parents=True)
        (complete / "model.onnx").write_bytes(b"weights")
        self._fastembed(monkeypatch, tmp_path, "BAAI/bge-small-en")

        # THE ORDER MUST BE FORCED, or this test asserts nothing. `iterdir()` yields in
        # filesystem order, so the complete repo may come first by luck and the reverted
        # code passes — measured: with a bare `sorted(...)` precondition instead of this
        # patch, reverting the fix left all 22 green. A test of order-dependence that
        # does not control the order is the defect it is testing for.
        _iterdir = Path.iterdir
        monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(_iterdir(self))))
        assert next(iter(cache.iterdir())).name == "models--baai--bge-small-en", (
            "precondition: the WEIGHTLESS repo must be visited first"
        )

        assert model_cached() is True

    def test_an_underscore_in_the_model_name_still_matches(self, monkeypatch, tmp_path):
        """`_flatten` normalises BOTH sides (Prudence, #728 r2 nit). They used to strip
        different characters — the needle `/` and `-`, the directory `-` and `_` — so a
        model id carrying `_` missed on that asymmetry alone. Nothing in the suite carried
        one, so reverting the fix left every test green (#728 r3 nit 3)."""
        from techne.core.embeddings import model_cached

        repo = tmp_path / ".cache" / "fastembed" / "models--acme--my-embed-v1-onnx"
        (repo / "snapshots" / "abc").mkdir(parents=True)
        (repo / "snapshots" / "abc" / "model.onnx").write_bytes(b"weights")
        self._fastembed(monkeypatch, tmp_path, "acme/my_embed_v1")

        assert model_cached() is True

    @pytest.mark.parametrize("var", ["HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"])
    def test_the_hf_cache_chain_is_honoured_end_to_end(self, monkeypatch, tmp_path, var):
        """huggingface_hub resolves HF_HUB_CACHE -> HUGGINGFACE_HUB_CACHE -> $HF_HOME/hub
        -> $XDG_CACHE_HOME/huggingface/hub -> ~/.cache/... We honoured the first and
        third only, so weights genuinely on disk under either of the others read False."""
        from techne.core import embeddings as E
        from techne.core.embeddings import model_cached

        for v in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
            monkeypatch.delenv(v, raising=False)
        # ISOLATE THE FALLBACK. Without this the chain's last link resolves to the REAL
        # ~/.cache/huggingface/hub, which on a developer machine genuinely holds this
        # model — so the test passed even with the env var removed from the chain, i.e.
        # it asserted nothing. Found by mutating the chain and watching it stay green.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nowhere"))
        hub = tmp_path / ("huggingface/hub" if var == "XDG_CACHE_HOME" else "hub")
        monkeypatch.setenv(var, str(tmp_path if var == "XDG_CACHE_HOME" else hub))
        snap = hub / "models--minishlab--potion-base-8M" / "snapshots" / "r1"
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_bytes(b"weights")
        monkeypatch.setattr(
            E, "_select_backend", lambda cfg: ("model2vec", "minishlab/potion-base-8M")
        )

        assert model_cached() is True, f"{var} is in the documented chain and must be read"


class TestTheRemedyIsGatedOnTheBackendNotTheProbe:
    """#728 r2 suggestion. Gating the remedy on `model_cached() is False` made the
    probe load-bearing for whether the operator is told anything at all — so a warm
    cache with no vec tables, and a probe that could not tell, both got silence."""

    def _dormant_check(self, monkeypatch, tmp_path, *, available, cached):
        from techne.config import Config
        from techne.core import embeddings as E
        from techne.core.checks import check_vector_coverage
        from techne.core.database import init_db

        cfg = Config()
        cfg.db_path = tmp_path / "t.db"
        init_db(cfg.db_path)
        conn = __import__("sqlite3").connect(cfg.db_path)
        conn.execute("DROP TABLE IF EXISTS knowledge_vec_rowids")
        conn.commit()
        conn.close()
        monkeypatch.setattr(E, "backend_available", lambda: available)
        monkeypatch.setattr(E, "model_cached", lambda c=None: cached)
        results = check_vector_coverage(cfg)
        return results if isinstance(results, list) else [results]

    @pytest.mark.parametrize("cached", [True, None])
    def test_a_warm_or_unmeasurable_cache_still_gets_a_remedy(self, monkeypatch, tmp_path, cached):
        """Both are permanently FTS-only until a backfill runs, and neither licenses
        claiming the weights are missing."""
        (res,) = self._dormant_check(monkeypatch, tmp_path, available=True, cached=cached)
        assert res.remedy == "techne vectors backfill", (
            "the backend is on and there are no vectors — that is actionable regardless "
            "of what the cache probe could determine"
        )
        assert "not cached" not in (res.detail or ""), (
            "with cached=True/None the doctor must not assert the weights are missing"
        )

    def test_an_uncached_backend_keeps_its_specific_sentence(self, monkeypatch, tmp_path):
        (res,) = self._dormant_check(monkeypatch, tmp_path, available=True, cached=False)
        assert "not cached" in (res.detail or "")
        assert res.remedy == "techne vectors backfill"

    def test_no_backend_means_no_remedy(self, monkeypatch, tmp_path):
        """The operator never turned this on; there is nothing to tell them to do."""
        (res,) = self._dormant_check(monkeypatch, tmp_path, available=False, cached=None)
        assert res.remedy is None


class TestADeadVectorChannelIsNotRecordedAsHealthy:
    """techne#728 r3 Important 1. `allow_download=False` is what fixes the hang;
    swallowing the exception into `outcome="ok"` was a SEPARATE choice made in the same
    `except`, and only the first was necessary. Post-#708 `search_vec` returns [] instead
    of raising, so `search()` succeeds, user_prompt_submit's handler never fires,
    `degraded` stays None, and `_record` writes `ok` over a backend that answered
    nothing — the exact shape `_record`'s own docstring names as the bug it exists to
    surface. The guard was intact; the failure walked around it."""

    @staticmethod
    def _config(tmp_path):
        from techne.config import Config
        from techne.core.database import init_db

        cfg = Config()
        cfg.db_path = tmp_path / "test.db"
        init_db(cfg.db_path).close()
        cfg.hooks.per_turn_recall = True
        cfg.hooks.user_prompt_submit = True
        return cfg

    @staticmethod
    def _marker(config):
        from techne.core.database import connect
        from techne.core.extension_state import get_state

        conn = connect(config.db_path)
        try:
            return get_state(conn, "hook:user_prompt_submit:last_run") or {}
        finally:
            conn.close()

    @staticmethod
    def _dead_backend(monkeypatch):
        import sys

        monkeypatch.setattr(search_mod, "_EMBED_UNAVAILABLE_WARNED", False)
        monkeypatch.setattr(search_mod, "_VEC_CHANNEL_ERROR", None)
        monkeypatch.setattr(embeddings_mod, "backend_available", lambda: True)
        monkeypatch.setattr(
            search_mod,
            "_get_embedding_model",
            lambda *a, **k: (_ for _ in ()).throw(_Uncached("weights corrupt")),
        )
        # sqlite_vec and numpy are reached BEFORE the resolver. A real install with
        # backend_available() True always has both; without the stubs this venv fails
        # earlier, for the wrong reason, and the test passes on a false premise.
        monkeypatch.setitem(sys.modules, "sqlite_vec", type("m", (), {})())
        monkeypatch.setitem(sys.modules, "numpy", type("m", (), {})())

    @staticmethod
    def _run(config, monkeypatch, prompt="what did we decide about markers"):
        import io
        import json

        from techne.config import Config
        from techne.hooks import user_prompt_submit

        monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": prompt}))
        )
        try:
            user_prompt_submit.main()
        except SystemExit as exc:
            assert exc.code in (0, None), f"a hook must never exit non-zero: {exc.code}"

    @staticmethod
    def _seed(config):
        """FTS must return a row, or the hook takes the noop branch and the test would
        be measuring the wrong exit."""
        from techne.core.knowledge import store

        store(
            config.db_path,
            "we decided markers are read at binding time",
            type="decision",
        )

    def test_a_dead_vector_channel_records_a_failed_marker(self, monkeypatch, tmp_path):
        config = self._config(tmp_path)
        self._seed(config)
        self._dead_backend(monkeypatch)

        self._run(config, monkeypatch)

        marker = self._marker(config)
        assert marker.get("outcome") == "failed", (
            f"a rail whose vector channel is dead must not report ok: {marker}"
        )

    def test_a_healthy_turn_still_records_ok(self, monkeypatch, tmp_path):
        """THE OTHER HALF, and the one that makes the pair mean something. Asserting
        only `failed` would keep passing if the signal fired unconditionally — which is
        a worse defect than the one being fixed, because it marks every healthy turn.

        THE STUBS ARE NOT DECORATION (Prudence, #728 r4 nit 2). Without them this test
        was green for a reason its own docstring did not describe: `sqlite_vec` is not
        installed in this venv, so `search_vec` returned at the `import sqlite_vec`
        ImportError — ONE EXIT EARLIER than the `backend_available()` gate the patch
        below was meant to exercise, making that patch inert. Measured with a spy:
        `backend_available() reached? False`. `_dead_backend` guards against exactly
        this and names it; this test omitted the same two lines. It would also have
        taken a different path on a box WITH sqlite-vec installed.

        NOT COVERED HERE, deliberately: a LIVE vector channel recording `ok`. Both
        halves of this pair are "the channel did not answer, quietly" — driving a
        working channel needs the real sqlite-vec extension, which this venv does not
        have. Recorded rather than faked, since a stubbed "live" channel would be the
        same false premise one layer up.
        """
        import sys

        config = self._config(tmp_path)
        self._seed(config)
        monkeypatch.setattr(search_mod, "_VEC_CHANNEL_ERROR", None)
        monkeypatch.setitem(sys.modules, "sqlite_vec", type("m", (), {})())
        monkeypatch.setitem(sys.modules, "numpy", type("m", (), {})())
        monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)

        self._run(config, monkeypatch)

        marker = self._marker(config)
        assert marker.get("outcome") == "ok", marker

    def test_the_channel_error_is_consumed_not_left_standing(self, monkeypatch, tmp_path):
        """CONSUME-ONCE (see `_VEC_CHANNEL_ERROR`). In a long-lived process a failure
        left standing would mark a LATER, healthy turn as degraded — a stale-derived-fact
        defect, which is the class this whole change is about."""
        self._dead_backend(monkeypatch)

        search_mod.search_vec(tmp_path / "x.db", "q")

        assert search_mod.take_vec_channel_error() is not None, "the failure must be recorded"
        assert search_mod.take_vec_channel_error() is None, "and must not outlive one read"
