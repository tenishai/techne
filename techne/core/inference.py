"""Embedding-based tag + kind inference for observation ingest.

Uses whichever embedding backend is already loaded for vector search — model2vec
or fastembed (techne#601/#602, see core/embeddings.py). Zero new deps beyond
what core/search.py already pulls in. Prototypes ship in
techne/inference/prototypes.yaml; operator can override or extend via
~/.techne/prototypes.yaml (merged, user entries win on conflict).

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

# Sentinel for the embedding-backend+numpy availability check in infer()
# (techne#56-14). None = not yet attempted; True = deps present; False =
# ImportError/no-backend on first attempt. Short-circuits the failing check on
# every subsequent call when deps are absent, instead of re-attempting a
# known-failing import each time.
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
    config: "Config | None" = None,
) -> dict:
    """Embed each prototype label's sentences and cache mean vectors.

    *key* selects the cache slot (same key as load_prototypes — whether operator
    overrides are active) so the embeddings always match the prototype set they
    were built from. (techne#168)

    *config* is only consulted to build the embedding-backend singleton on the
    first call in-process (see embeddings._get_embedding_model) — it's not
    part of the cache key, since the singleton itself is process-wide.
    """
    cached = _embed_cache.get(key)
    if cached is not None:
        return cached

    # techne#601/#602: was a bare `from fastembed import TextEmbedding` probe —
    # that's wrong on py3.14 (fastembed/onnxruntime can't install there) even
    # when model2vec IS available. backend_available() checks both.
    from techne.core.embeddings import backend_available

    if not backend_available():
        log.debug("no embedding backend available; tag inference disabled")
        _embed_cache[key] = {}
        return _embed_cache[key]

    import numpy as np

    # Reuse the already-loaded singleton from search.py if available.
    # Import here to avoid circular imports.
    from techne.core.embeddings import _get_embedding_model

    # allow_download=False (techne#601/#602): infer() runs inside the
    # post_tool_use hook subprocess, wrapped in a SIGALRM-bounded timeout that
    # can't reliably preempt a blocking cold-cache HF download (see
    # embeddings.py's "Cold-cache downloads and hooks" module docstring
    # section — this was verified empirically to hang the hook past its
    # budget). An uncached model raises here (backend-dependent exception,
    # e.g. LocalEntryNotFoundError) — caught below and treated exactly like
    # "package not installed": disabled, not a crash.
    try:
        model = _get_embedding_model(config, allow_download=False)
    except Exception:
        log.debug("embedding backend unavailable (not installed/cached); disabling tag inference")
        _embed_cache[key] = {}
        return _embed_cache[key]

    result: dict[str, np.ndarray] = {}
    for label, sentences in prototypes.items():
        if not sentences:
            continue
        vecs = np.stack(list(model.embed(sentences)))  # (N, model.dim)
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
    if no embedding backend (model2vec or fastembed) is available — callers
    must not fail on that path.

    Args:
        content: Raw observation text.
        config: Config object (used to find operator prototype overrides, and
            to resolve an explicit embedding_backend/embedding_model override).
        tag_threshold: Minimum cosine similarity to attach a tag (default 0.6).
        top_k: Max number of tags to attach (by score).
    """
    global _INFER_DEPS
    if _INFER_DEPS is False:
        return InferenceResult()

    # techne#601/#602: was a bare `from fastembed import TextEmbedding` probe —
    # that's wrong on py3.14 (fastembed/onnxruntime can't install there) even
    # when model2vec IS available. backend_available() checks both.
    from techne.core.embeddings import _get_embedding_model, backend_available

    try:
        import numpy as np

        if not backend_available():
            raise ImportError("no embedding backend available")

        _INFER_DEPS = True
    except ImportError:
        _INFER_DEPS = False
        return InferenceResult()

    key = _override_active(config)
    prototypes = load_prototypes(config)
    proto_embs = _get_prototype_embeddings(prototypes, key=key, config=config)
    if not proto_embs:
        return InferenceResult()

    # allow_download=False (techne#601/#602) — see the matching call+comment in
    # _get_prototype_embeddings just above: infer() is hook-reachable and must
    # never risk blocking on a cold-cache network fetch. An uncached model
    # raises here (LocalEntryNotFoundError/OSError-family, backend-dependent)
    # rather than downloading — caught below and degraded, same as "no backend
    # installed" from the caller's point of view.
    try:
        model = _get_embedding_model(config, allow_download=False)
        content_vec: np.ndarray = np.stack(list(model.embed([content])))[0]
    except Exception:
        return InferenceResult()

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
