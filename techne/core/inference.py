"""Embedding-based tag + kind inference for observation ingest.

Uses the fastembed model already loaded for vector search. Zero new deps.
Prototypes ship in techne/inference/prototypes.yaml; operator can override
or extend via ~/.techne/prototypes.yaml (merged, user entries win on conflict).

Public API
----------
infer(content, config) -> InferenceResult
    Embed content, cosine-sim against prototypes, return top-K tags + kind.

load_prototypes(config) -> dict[str, list[str]]
    Load + merge prototype sentences. Cached in-process after first load.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from techne.config import Config

log = logging.getLogger(__name__)

_BUNDLED_PROTOTYPES = Path(__file__).parent.parent / "inference" / "prototypes.yaml"

# Module-level cache: prototypes + their embeddings.
# Reset by calling _reset_cache() (used in tests).
_proto_cache: dict | None = None
_embed_cache: dict | None = None  # label -> np.ndarray (mean of prototype embeds)


@dataclass
class InferenceResult:
    tags: list[str] = field(default_factory=list)
    kind: str = "observation-shaped"
    tag_scores: dict[str, float] = field(default_factory=dict)
    kind_scores: dict[str, float] = field(default_factory=dict)


def _reset_cache():
    global _proto_cache, _embed_cache
    _proto_cache = None
    _embed_cache = None


def load_prototypes(config: "Config | None" = None) -> dict[str, list[str]]:
    """Load bundled prototypes, optionally merged with operator overrides."""
    global _proto_cache
    if _proto_cache is not None:
        return _proto_cache

    with open(_BUNDLED_PROTOTYPES) as f:
        merged: dict[str, list[str]] = yaml.safe_load(f) or {}

    if config is not None:
        user_path = config.data_dir / "prototypes.yaml"
        if user_path.exists():
            with open(user_path) as f:
                user = yaml.safe_load(f) or {}
            for label, sentences in user.items():
                if isinstance(sentences, list):
                    merged[label] = sentences  # user wins on conflict

    _proto_cache = merged
    return merged


def _get_prototype_embeddings(
    prototypes: dict[str, list[str]],
) -> dict:
    """Embed each prototype label's sentences and cache mean vectors."""
    global _embed_cache
    if _embed_cache is not None:
        return _embed_cache

    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        log.debug("fastembed not available; tag inference disabled")
        _embed_cache = {}
        return _embed_cache

    # Reuse the already-loaded singleton from search.py if available.
    # Import here to avoid circular imports.
    from techne.core.search import _get_embedding_model

    model: TextEmbedding = _get_embedding_model()

    result: dict[str, np.ndarray] = {}
    for label, sentences in prototypes.items():
        if not sentences:
            continue
        vecs = np.stack(list(model.embed(sentences)))  # (N, 384)
        result[label] = vecs.mean(axis=0)  # centroid

    _embed_cache = result
    return result


def _cosine(a, b) -> float:
    na, nb = float((a @ a) ** 0.5), float((b @ b) ** 0.5)
    if na == 0 or nb == 0:
        return 0.0
    return float(a @ b) / (na * nb)


def infer(
    content: str,
    config: "Config | None" = None,
    *,
    tag_threshold: float = 0.6,
    top_k: int = 5,
) -> InferenceResult:
    """Embed *content*, cosine-sim against prototypes, return tags + kind.

    Returns a default InferenceResult (empty tags, kind="observation-shaped")
    if fastembed is unavailable — callers must not fail on that path.

    Args:
        content: Raw observation text.
        config: Config object (used to find operator prototype overrides).
        tag_threshold: Minimum cosine similarity to attach a tag (default 0.6).
        top_k: Max number of tags to attach (by score).
    """
    try:
        import numpy as np
        from fastembed import TextEmbedding  # noqa: F401 — import guard
    except ImportError:
        return InferenceResult()

    from techne.core.search import _get_embedding_model

    prototypes = load_prototypes(config)
    proto_embs = _get_prototype_embeddings(prototypes)
    if not proto_embs:
        return InferenceResult()

    model = _get_embedding_model()
    content_vec: np.ndarray = np.stack(list(model.embed([content])))[0]

    tag_scores: dict[str, float] = {}
    kind_scores: dict[str, float] = {}

    for label, proto_vec in proto_embs.items():
        score = _cosine(content_vec, proto_vec)
        if label.startswith("kind:"):
            kind_scores[label[5:]] = score  # strip "kind:" prefix
        else:
            tag_scores[label] = score

    # Top-K tags above threshold
    qualifying = [(lbl, s) for lbl, s in tag_scores.items() if s >= tag_threshold]
    qualifying.sort(key=lambda x: x[1], reverse=True)
    tags = [lbl for lbl, _ in qualifying[:top_k]]

    # Highest-scoring kind
    kind = max(kind_scores, key=kind_scores.get) if kind_scores else "observation-shaped"

    return InferenceResult(
        tags=tags,
        kind=kind,
        tag_scores=tag_scores,
        kind_scores=kind_scores,
    )
