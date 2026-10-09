"""Tests for techne.core.embeddings — the swappable model2vec/fastembed
embedding backend (techne#601/#602).

Wrapper-interface tests run against whatever backend is actually importable in
this environment — on the py3.14 dev venv that's model2vec (fastembed has no
onnxruntime wheel there), on <3.14 CI it'll typically be fastembed. Tests that
need a SPECIFIC backend `pytest.importorskip` that backend's real package so
they skip cleanly rather than fail when it's genuinely absent.
"""

import os
import sys
from unittest.mock import patch

import pytest

# numpy ships only with the [vectors] extra; a base install (as CI runs on
# <3.14) has none. Skip the whole module cleanly there instead of crashing
# collection — the wrapper tests need a real backend (hence numpy) anyway, and
# they still run on any env with [vectors] installed (e.g. the 3.14 dev venv).
np = pytest.importorskip("numpy")

from techne.core import embeddings


@pytest.fixture(autouse=True)
def reset_singleton():
    """Each test gets a fresh process-singleton so backend-selection tests
    (which patch importability / config) aren't polluted by whatever an
    earlier test already built and cached."""
    embeddings._reset_embedding_model()
    yield
    embeddings._reset_embedding_model()


# ---------------------------------------------------------------------------
# Wrapper interface — exercised against whatever real backend is installed.
# ---------------------------------------------------------------------------


class TestWrapperInterface:
    def test_embed_returns_1d_float32_rows_matching_input_count(self):
        if not embeddings.backend_available():
            pytest.skip("no embedding backend installed")
        model = embeddings._get_embedding_model()
        texts = ["hello world", "a second, different sentence", "third one"]
        vecs = list(model.embed(texts))
        assert len(vecs) == len(texts)
        for v in vecs:
            assert v.ndim == 1
            assert v.dtype == np.float32
            assert v.shape[0] == model.dim

    def test_embed_stack_matches_existing_call_site_pattern(self):
        """Callers everywhere do `np.stack(list(model.embed(texts)))` — verify
        that shape contract holds for whichever backend is active."""
        if not embeddings.backend_available():
            pytest.skip("no embedding backend installed")
        model = embeddings._get_embedding_model()
        texts = ["foo", "bar"]
        stacked = np.stack(list(model.embed(texts)))
        assert stacked.shape == (2, model.dim)
        assert stacked.dtype == np.float32

    def test_dim_is_positive_int(self):
        if not embeddings.backend_available():
            pytest.skip("no embedding backend installed")
        model = embeddings._get_embedding_model()
        assert isinstance(model.dim, int)
        assert model.dim > 0

    def test_name_is_backend_colon_model_string(self):
        if not embeddings.backend_available():
            pytest.skip("no embedding backend installed")
        model = embeddings._get_embedding_model()
        assert ":" in model.name
        backend, _, model_id = model.name.partition(":")
        assert backend in ("model2vec", "fastembed")
        assert model_id  # non-empty

    def test_singleton_returns_same_instance(self):
        if not embeddings.backend_available():
            pytest.skip("no embedding backend installed")
        m1 = embeddings._get_embedding_model()
        m2 = embeddings._get_embedding_model()
        assert m1 is m2


# ---------------------------------------------------------------------------
# model2vec-specific: normalization (techne#601/#602 — sqlite-vec's vec0 uses
# L2 distance, and model2vec's StaticModel.encode() does not universally
# guarantee normalized output the way fastembed's TextEmbedding does).
# ---------------------------------------------------------------------------


class TestModel2VecNormalization:
    def test_embed_output_is_unit_normalized(self):
        pytest.importorskip("model2vec")
        # Force the model2vec backend explicitly so this test doesn't depend
        # on AUTO-selection picking it (it does on py3.14, may not on <3.14
        # if fastembed is also installed there).
        with patch.object(embeddings, "_select_backend", return_value=("model2vec", "minishlab/potion-base-8M")):
            model = embeddings._get_embedding_model()
        vecs = list(model.embed(["short", "a considerably longer sentence with many more words in it"]))
        for v in vecs:
            norm = float(np.linalg.norm(v))
            assert norm == pytest.approx(1.0, abs=1e-5)

    def test_l2_normalize_rows_handles_zero_vector(self):
        """Defensive: a zero vector must not raise (division by zero) or come
        back as NaN — it's left as the zero vector."""
        raw = np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]], dtype="float32")
        normed = embeddings._l2_normalize_rows(raw, np)
        assert np.allclose(normed[0], [0.0, 0.0, 0.0])
        assert np.allclose(normed[1], [0.6, 0.8, 0.0])

    def test_l2_normalize_rows_is_idempotent_on_already_unit_vectors(self):
        raw = np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
        once = embeddings._l2_normalize_rows(raw, np)
        twice = embeddings._l2_normalize_rows(once, np)
        assert np.allclose(once, twice)


class TestModel2VecAllowDownloadFalse:
    """techne#601/#602 regression coverage: _get_embedding_model(...,
    allow_download=False) must be a fast, network-free local-cache read when
    the model IS cached (the common warm-cache case for hook invocations),
    and must raise quickly rather than block when it is NOT cached — this is
    the fix for a real bug found while implementing this backend: the hook
    timeout suite (tests/test_hook_timeout.py) started failing because a
    cold-cache model2vec build inside the post_tool_use hook subprocess
    blocked past the hook's SIGALRM-based timeout budget (see
    embeddings.py's "Cold-cache downloads and hooks" docstring section)."""

    def test_cached_model_builds_fast_with_allow_download_false(self):
        """Requires the default model already warm in the local HF cache —
        which it will be after any earlier test/call in this suite has built
        the AUTO-selected backend at least once. If genuinely cold, skip
        rather than let this test itself perform a real network fetch."""
        pytest.importorskip("model2vec")
        from model2vec.persistence.hf import maybe_get_cached_model_path

        if maybe_get_cached_model_path(embeddings._DEFAULT_MODEL2VEC_MODEL) is None:
            pytest.skip("default model2vec model not present in local HF cache")

        import time

        with patch.object(
            embeddings, "_select_backend", return_value=("model2vec", embeddings._DEFAULT_MODEL2VEC_MODEL)
        ):
            t0 = time.monotonic()
            model = embeddings._get_embedding_model(allow_download=False)
            elapsed = time.monotonic() - t0
        assert model.dim > 0
        # Generous ceiling: a local safetensors+tokenizer load should be well
        # under a second; 3s leaves headroom for slow CI disks without
        # letting a real network fetch (multiple seconds, observed ~8s cold)
        # sneak through undetected.
        assert elapsed < 3.0, f"allow_download=False took {elapsed:.2f}s — looks like it hit the network"

    def test_uncached_model_raises_fast_instead_of_downloading(self):
        """Run in a FRESH subprocess with HOME pointed at an empty temp dir,
        so huggingface_hub's cache-dir resolution (frozen as a module-level
        constant at its own first import — same-process monkeypatching can't
        reach it, since huggingface_hub is already imported by the time any
        test runs) genuinely sees no cache. Confirms allow_download=False
        raises quickly rather than downloading — this is the exact scenario
        that broke tests/test_hook_timeout.py before the allow_download fix
        (a cold-cache post_tool_use hook invocation blocking past its SIGALRM
        budget); reproduced directly here against embeddings.py rather than
        through the hook subprocess.
        """
        pytest.importorskip("model2vec")
        import subprocess
        import tempfile
        import time

        script = (
            "from techne.core import embeddings\n"
            "embeddings._get_embedding_model(allow_download=False)\n"
        )
        with tempfile.TemporaryDirectory() as fake_home:
            t0 = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-c", script],
                env={"HOME": fake_home, "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                timeout=10,
            )
            elapsed = time.monotonic() - t0
        # Must fail fast (local cache miss under a genuinely empty HOME), not
        # hang trying to download. Cold-download was observed at ~8s; this
        # ceiling is well below that while leaving CI headroom.
        assert elapsed < 4.0, f"allow_download=False took {elapsed:.2f}s — looks like it hit the network"
        assert result.returncode != 0, "expected the uncached model2vec build to raise, not succeed"


# ---------------------------------------------------------------------------
# fastembed path — real package required; skip cleanly when absent (e.g. on
# this py3.14 venv, where fastembed has no onnxruntime wheel).
# ---------------------------------------------------------------------------


class TestFastembedBackend:
    def test_fastembed_backend_builds_and_embeds(self):
        pytest.importorskip("fastembed")
        with patch.object(
            embeddings,
            "_select_backend",
            return_value=("fastembed", "sentence-transformers/all-MiniLM-L6-v2"),
        ):
            model = embeddings._get_embedding_model()
        assert model.name == "fastembed:sentence-transformers/all-MiniLM-L6-v2"
        assert model.dim == 384
        vecs = list(model.embed(["hello world"]))
        assert len(vecs) == 1
        assert vecs[0].shape == (384,)

    def test_fastembed_rows_are_cast_to_float32(self):
        """Issue 536. fastembed 0.8.1 yields float64 rows and 0.9.0 float32, so
        the real-model tests above only catch a missing cast on an old
        fastembed. This stub pins the cast whatever version CI installs."""

        class _Float64Impl:
            def embed(self, texts):
                for _ in texts:
                    yield np.ones(4, dtype=np.float64)

        backend = embeddings.EmbeddingBackend("fastembed", "stub", _Float64Impl(), 4)
        vecs = list(backend.embed(["a", "b"]))
        assert [v.dtype for v in vecs] == [np.float32, np.float32]
        assert np.stack(vecs).shape == (2, 4)


# ---------------------------------------------------------------------------
# backend_available()
# ---------------------------------------------------------------------------


class TestBackendAvailable:
    def test_true_when_at_least_one_importable(self):
        # On any dev/CI box with `techne[vectors]` installed, at least one of
        # model2vec/fastembed is present.
        assert embeddings.backend_available() == (
            embeddings._model2vec_importable() or embeddings._fastembed_importable()
        )

    def test_false_when_neither_importable(self):
        with (
            patch.object(embeddings, "_model2vec_importable", return_value=False),
            patch.object(embeddings, "_fastembed_importable", return_value=False),
        ):
            assert embeddings.backend_available() is False

    def test_true_when_only_model2vec_importable(self):
        with (
            patch.object(embeddings, "_model2vec_importable", return_value=True),
            patch.object(embeddings, "_fastembed_importable", return_value=False),
        ):
            assert embeddings.backend_available() is True


# ---------------------------------------------------------------------------
# Backend selection logic (_select_backend) — mocked availability/py-version,
# no real imports or network/model downloads needed here.
# ---------------------------------------------------------------------------


class TestSelectBackend:
    def _select(self, *, model2vec_ok, fastembed_ok, py_version, config=None):
        with (
            patch.object(embeddings, "_model2vec_importable", return_value=model2vec_ok),
            patch.object(embeddings, "_fastembed_importable", return_value=fastembed_ok),
            patch.object(embeddings.sys, "version_info", py_version),
        ):
            return embeddings._select_backend(config)

    def test_auto_prefers_model2vec_on_py314(self):
        backend, model = self._select(
            model2vec_ok=True, fastembed_ok=True, py_version=(3, 14, 0, "final", 0)
        )
        assert backend == "model2vec"
        assert model == embeddings._DEFAULT_MODEL2VEC_MODEL

    def test_auto_prefers_model2vec_when_fastembed_absent_even_pre_314(self):
        backend, model = self._select(
            model2vec_ok=True, fastembed_ok=False, py_version=(3, 12, 0, "final", 0)
        )
        assert backend == "model2vec"

    def test_auto_prefers_fastembed_pre_314_when_both_available(self):
        backend, model = self._select(
            model2vec_ok=True, fastembed_ok=True, py_version=(3, 12, 0, "final", 0)
        )
        assert backend == "fastembed"
        assert model == embeddings._DEFAULT_FASTEMBED_MODEL

    def test_auto_falls_back_to_fastembed_on_314_if_model2vec_missing(self):
        """Pathological but must degrade sanely: model2vec absent, fastembed
        somehow present on 3.14 (e.g. an onnxruntime wheel eventually ships)."""
        backend, model = self._select(
            model2vec_ok=False, fastembed_ok=True, py_version=(3, 14, 0, "final", 0)
        )
        assert backend == "fastembed"

    def test_auto_raises_importerror_when_neither_available(self):
        with pytest.raises(ImportError):
            self._select(model2vec_ok=False, fastembed_ok=False, py_version=(3, 14, 0, "final", 0))

    def test_explicit_config_override_wins_over_auto(self):
        from techne.config import Config, SearchConfig

        cfg = Config()
        cfg.search = SearchConfig(embedding_backend="fastembed")
        # Even though AUTO would pick model2vec on 3.14, the explicit config wins.
        backend, model = self._select(
            model2vec_ok=True, fastembed_ok=True, py_version=(3, 14, 0, "final", 0), config=cfg
        )
        assert backend == "fastembed"
        assert model == embeddings._DEFAULT_FASTEMBED_MODEL

    def test_explicit_model_override_used_with_explicit_backend(self):
        from techne.config import Config, SearchConfig

        cfg = Config()
        cfg.search = SearchConfig(embedding_backend="model2vec", embedding_model="minishlab/potion-base-8M")
        backend, model = self._select(
            model2vec_ok=True, fastembed_ok=True, py_version=(3, 14, 0, "final", 0), config=cfg
        )
        assert backend == "model2vec"
        assert model == "minishlab/potion-base-8M"

    def test_unrecognized_backend_override_falls_back_to_auto(self):
        from techne.config import Config, SearchConfig

        cfg = Config()
        cfg.search = SearchConfig(embedding_backend="not-a-real-backend")
        backend, _ = self._select(
            model2vec_ok=True, fastembed_ok=True, py_version=(3, 14, 0, "final", 0), config=cfg
        )
        assert backend == "model2vec"  # AUTO kicked in, override was ignored


# ---------------------------------------------------------------------------
# _get_embedding_model() singleton construction (mocked _build_backend so no
# real model download happens in these unit tests).
# ---------------------------------------------------------------------------


class TestGetEmbeddingModelSingleton:
    def test_builds_once_via_select_and_build(self):
        sentinel = object()
        with (
            patch.object(embeddings, "_select_backend", return_value=("model2vec", "fake-model")) as sel,
            patch.object(embeddings, "_build_backend", return_value=sentinel) as build,
        ):
            m1 = embeddings._get_embedding_model()
            m2 = embeddings._get_embedding_model()
        assert m1 is sentinel
        assert m2 is sentinel
        sel.assert_called_once()
        build.assert_called_once_with("model2vec", "fake-model", allow_download=True)

    def test_allow_download_false_is_threaded_to_build_backend(self):
        """techne#601/#602: hook-reachable callers (inference.py) pass
        allow_download=False so a cold-cache model build raises instead of
        blocking on a network fetch — see embeddings.py's "Cold-cache
        downloads and hooks" module docstring section."""
        sentinel = object()
        with (
            patch.object(embeddings, "_select_backend", return_value=("model2vec", "fake-model")),
            patch.object(embeddings, "_build_backend", return_value=sentinel) as build,
        ):
            embeddings._get_embedding_model(allow_download=False)
        build.assert_called_once_with("model2vec", "fake-model", allow_download=False)

    def test_failed_download_refusal_does_not_poison_singleton_for_later_call(self):
        """A failed allow_download=False attempt must NOT cache a broken
        singleton — a later call (e.g. willing to download) should still be
        able to succeed."""
        sentinel = object()
        call_count = [0]

        def _build(backend, model_name, *, allow_download):
            call_count[0] += 1
            if not allow_download:
                raise OSError("simulated: model not cached, refusing to download")
            return sentinel

        with (
            patch.object(embeddings, "_select_backend", return_value=("model2vec", "fake-model")),
            patch.object(embeddings, "_build_backend", side_effect=_build),
        ):
            with pytest.raises(OSError):
                embeddings._get_embedding_model(allow_download=False)
            # Singleton slot still empty — next call is a fresh attempt, not a
            # cached failure.
            assert embeddings._embedding_model is None
            m = embeddings._get_embedding_model(allow_download=True)
            assert m is sentinel
