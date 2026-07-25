"""Shared fixtures for the vector/hybrid eval_vec suite (techne#399).

Both test_retrieval_gate_vec.py and test_ranking_ablation_vec.py need the
exact same pinned-singleton-or-skip dance, so it lives here once rather than
being duplicated per-file.
"""

import pytest


@pytest.fixture
def pinned_model2vec():
    """Pin the embeddings singleton to model2vec/potion-base-8M, or skip.

    Determinism (techne#399): model2vec/potion-base-8M is a numpy-only static
    embedding backend (no onnxruntime, no torch) — byte-identical run-to-run,
    dim 256, loads on all Python versions. Pinning it (rather than AUTO, which
    can select fastembed on <3.14) is what makes the eval_vec suite reproducible
    across environments/backends the way eval_gate is via use_vectors=False.

    ORDER MATTERS: this fixture resets the process-singleton and rebuilds it
    pinned to model2vec BEFORE the test does anything else. `_get_embedding_model`
    only reads its `config` argument on the FIRST build in a process — if
    `search()`/`search_vec()`/`backfill_vectors()` (all of which call
    `_get_embedding_model()` with NO config) run first, or if an earlier test in
    the same pytest session already built the singleton under AUTO, the pin
    would silently no-op. Every eval_vec test must request this fixture before
    touching seed()/search()/backfill_vectors().

    Skip-when-absent (CI-safe, mirrors eval_llm's resolver-returns-None skip):
    skips unless `sqlite_vec` imports AND at least one embedding backend is
    importable AND the pinned model is already in the local HF cache. The cache
    probe goes through `_get_embedding_model(cfg, allow_download=False)`, which
    sets HF_HUB_OFFLINE=1 for the call and raises (LocalEntryNotFoundError,
    backend-dependent) instead of hitting the network on a cold cache — so this
    fixture can never trigger a blocking Hub download from inside a test.
    """
    from techne.config import Config, SearchConfig
    from techne.core import embeddings

    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        pytest.skip("sqlite_vec not installed — eval_vec is opt-in (techne#399)")

    if not embeddings.backend_available():
        pytest.skip(
            "no embedding backend (model2vec/fastembed) installed — eval_vec is opt-in (techne#399)"
        )

    embeddings._reset_embedding_model()
    cfg = Config()
    cfg.search = SearchConfig(embedding_backend="model2vec")
    try:
        model = embeddings._get_embedding_model(cfg, allow_download=False)
    except Exception as exc:
        pytest.skip(
            f"model2vec/{embeddings._DEFAULT_MODEL2VEC_MODEL} unavailable or not "
            f"locally cached (offline probe raised {type(exc).__name__}) — "
            f"eval_vec is opt-in (techne#399)"
        )

    yield model

    # Don't let a pinned model2vec singleton leak into tests that run after
    # this one in the same pytest session (e.g. `pytest -m "eval_vec or eval"`).
    embeddings._reset_embedding_model()
