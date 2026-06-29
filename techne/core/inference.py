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

# Module-level caches: prototypes + their embeddings. Two-slot, keyed on whether
# operator overrides were merged (techne#168): a first call with config=None (e.g. a
# test path) must NOT cache a bundled-only result that later poisons a config-bearing
# call — and the embed cache is keyed identically so the two never diverge.
# Reset by calling _reset_cache() (used in tests).
_proto_cache: dict[bool, dict] = {}
_embed_cache: dict[bool, dict] = {}  # override_active -> {label: np.ndarray mean}

# Sentinel for the fastembed+numpy availability check in infer() (techne#56-14).
# None = not yet attempted; True = deps present; False = ImportError on first attempt.
# Short-circuits the failing import on every subsequent call when deps are absent,
# instead of re-attempting a known-failing import each time.
_INFER_DEPS: bool | None = None


@dataclass
class InferenceResult:
    tags: list[str] = field(default_factory=list)
    kind: str = "observation-shaped"
    tag_scores: dict[str, float] = field(default_factory=dict)
    kind_scores: dict[str, float] = field(default_factory=dict)


def _reset_cache():
    global _INFER_DEPS
    _proto_cache.clear()
    _embed_cache.clear()
    _INFER_DEPS = None


def _override_active(config: "Config | None") -> bool:
    """True iff an operator prototypes.yaml exists to merge for this config.

    The cache key for both prototype and embedding caches, so a config=None call
    (key False) and a config-with-overrides call (key True) never share a slot.
    """
    if config is None:
        return False
    return (config.data_dir / "prototypes.yaml").exists()


def load_prototypes(config: "Config | None" = None) -> dict[str, list[str]]:
    """Load bundled prototypes, optionally merged with operator overrides."""
    key = _override_active(config)
    cached = _proto_cache.get(key)
    if cached is not None:
        return cached

    with open(_BUNDLED_PROTOTYPES) as f:
        merged: dict[str, list[str]] = yaml.safe_load(f) or {}

    if key:  # config is not None and the override file exists
        user_path = config.data_dir / "prototypes.yaml"
        with open(user_path) as f:
            user = yaml.safe_load(f) or {}
        for label, sentences in user.items():
            if isinstance(sentences, list):
                merged[label] = sentences  # user wins on conflict

    _proto_cache[key] = merged
    return merged


def _get_prototype_embeddings(
    prototypes: dict[str, list[str]],
    *,
    key: bool = False,
) -> dict:
    """Embed each prototype label's sentences and cache mean vectors.

    *key* selects the cache slot (same key as load_prototypes — whether operator
    overrides are active) so the embeddings always match the prototype set they
    were built from. (techne#168)
    """
    cached = _embed_cache.get(key)
    if cached is not None:
        return cached

    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        log.debug("fastembed not available; tag inference disabled")
        _embed_cache[key] = {}
        return _embed_cache[key]

    # Reuse the already-loaded singleton from search.py if available.
    # Import here to avoid circular imports.
    from techne.core.embeddings import _get_embedding_model

    model: TextEmbedding = _get_embedding_model()

    result: dict[str, np.ndarray] = {}
    for label, sentences in prototypes.items():
        if not sentences:
            continue
        vecs = np.stack(list(model.embed(sentences)))  # (N, 384)
        result[label] = vecs.mean(axis=0)  # centroid

    _embed_cache[key] = result
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
    global _INFER_DEPS
    if _INFER_DEPS is False:
        return InferenceResult()
    try:
        import numpy as np
        from fastembed import TextEmbedding  # noqa: F401 — import guard

        _INFER_DEPS = True
    except ImportError:
        _INFER_DEPS = False
        return InferenceResult()

    from techne.core.embeddings import _get_embedding_model

    key = _override_active(config)
    prototypes = load_prototypes(config)
    proto_embs = _get_prototype_embeddings(prototypes, key=key)
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
