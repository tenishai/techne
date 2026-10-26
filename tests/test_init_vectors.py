"""techne#1021: `init` downloads the embedding model once and embeds what is stored.

Without it a fresh install never gets semantic search: the SessionStart backfill
passes `--no-download` (#815), and nothing else fetched the model. The model loader
is stubbed in every test here; conftest stubs `_warm_vectors` for all other tests.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
    return tmp_path


pytestmark = pytest.mark.real_warm_vectors  # conftest leaves _warm_vectors real here


@pytest.fixture
def real_warm(monkeypatch):
    """Record model-loader calls; everything that would touch a real model is stubbed."""
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
    monkeypatch.setattr(
        "techne.core.embeddings._select_backend", lambda c: ("model2vec", "stub/model")
    )
    monkeypatch.setattr(
        "techne.core.search.backfill_vectors",
        lambda db, **kw: {
            "orphans_swept": 2,
            "sweep_errors": 0,
            "threads": 0,
            "observations": 0,
            "knowledge": 3,
        },
    )
    calls: list[bool] = []
    return calls


def _init(*args):
    result = CliRunner().invoke(main, ["init", "--platform", "standalone", *args])
    assert result.exit_code == 0, result.output
    return result.output


def _loader(calls, cached, download_ok: bool = True, monkeypatch=None):
    """`cached` is what model_cached() reports: True, False, or None (cannot tell)."""
    monkeypatch.setattr("techne.core.embeddings.model_cached", lambda config=None: cached)

    def load(config=None, *, allow_download=True):
        calls.append(allow_download)
        if allow_download and not download_ok:
            raise OSError("network unreachable")
        return object()

    return load


def test_a_cached_model_is_not_downloaded_again(isolated, real_warm, monkeypatch):
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=True, monkeypatch=monkeypatch),
    )
    out = _init()
    assert real_warm == [False], "a cached model must be loaded without a download"
    assert "Downloading" not in out
    assert "Semantic recall ready: embedded 3" in out


def test_a_cold_cache_downloads_once_then_embeds(isolated, real_warm, monkeypatch):
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=False, monkeypatch=monkeypatch),
    )
    out = _init()
    # ONE load, downloading. Never an offline probe first: on model2vec that probe
    # left huggingface_hub offline for the process and the download then failed
    # (#1029 r1, Critical).
    assert real_warm == [True]
    assert "Downloading the embedding model (stub/model)" in out
    assert "Semantic recall ready" in out
    assert "nothing embedded yet" not in out, "the old hint contradicts a warmed model"


def test_a_failed_download_does_not_fail_init(isolated, real_warm, monkeypatch):
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=False, download_ok=False, monkeypatch=monkeypatch),
    )
    out = _init()  # exit code 0 asserted inside
    assert "could not be loaded (network unreachable)" in out
    assert "keyword-only" in out and "techne vectors backfill" in out


def test_no_backend_falls_back_to_the_install_hint(isolated, real_warm, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
    out = _init()
    assert real_warm == []
    assert "tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@" in out


def test_no_vectors_skips_the_download(isolated, real_warm, monkeypatch):
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=False, monkeypatch=monkeypatch),
    )
    out = _init("--no-vectors")
    assert real_warm == [], "--no-vectors must not touch the model"
    assert "Downloading" not in out


def test_an_unknown_cache_state_downloads_rather_than_guessing(isolated, real_warm, monkeypatch):
    """model_cached() returns None when it cannot tell; treat that as not cached."""
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=None, monkeypatch=monkeypatch),
    )
    _init()
    assert real_warm == [True]


def test_a_failed_backfill_does_not_fail_init(isolated, real_warm, monkeypatch):
    """#1029 r1 nit 3: the backfill except-branch was untested."""
    monkeypatch.setattr(
        "techne.core.embeddings._get_embedding_model",
        _loader(real_warm, cached=True, monkeypatch=monkeypatch),
    )

    def boom(db, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("techne.core.search.backfill_vectors", boom)
    out = _init()  # exit code 0 asserted inside
    assert "embedding failed (disk full)" in out
