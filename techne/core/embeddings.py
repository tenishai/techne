"""Shared embedding model singleton (techne#56-18).

`_get_embedding_model` previously lived in search.py, which created a
backwards dependency: inference.py (the call-site) had to import from search.py
(a caller of inference). Moving the singleton here breaks the cycle — both
search.py and inference.py import from this neutral module.
"""

from __future__ import annotations

from pathlib import Path

_embedding_model = None


def _get_embedding_model():
    """Return the process-singleton fastembed TextEmbedding model.

    Lazy-initialises on first call; subsequent calls return the cached instance.
    Callers should guard with a try/except ImportError around `fastembed` before
    calling, or let the ImportError propagate if fastembed is required.
    """
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding

        cache_dir = Path.home() / ".cache" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _embedding_model = TextEmbedding(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            cache_dir=str(cache_dir),
        )
    return _embedding_model
