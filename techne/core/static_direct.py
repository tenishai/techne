"""Load a model2vec static model WITHOUT importing model2vec (techne#1096).

Every per-turn hook is a fresh Python process, and `import model2vec` costs
~130 ms on top of numpy: it pulls in huggingface_hub (repocard, hf_api), joblib
and tqdm, none of which inference needs. Measured end to end, building the
backend through `StaticModel.from_pretrained` was 401-406 ms of the per-turn
hook's ~0.65 s wall time; the embedding itself is under a millisecond.

A potion model is a token-embedding table plus a tokenizer, so this module
reads the same three files model2vec reads (`config.json`, `model.safetensors`,
`tokenizer.json`) with `tokenizers` and `safetensors` directly, and `encode()`
repeats `StaticModel.encode`'s steps (model2vec 0.9.0, model.py):

  1. cut each sentence to `max_length * median_token_length` characters;
  2. tokenize with `add_special_tokens=False`;
  3. drop the tokenizer's unk id, then keep the first `max_length` ids;
  4. mean the rows (a float64 zero vector for a sentence with no ids);
  5. stack per batch of 1024 and, when `config["normalize"]`, divide by
     `norm + 1e-32`.

Same steps, same dtypes, so the vectors match model2vec's bit for bit, and the
caller stamps the SAME backend identity (`model2vec:<model>`), so a DB embedded
through model2vec is not re-embedded. tests/test_static_direct.py pins that
against the installed model2vec; if a model2vec release changes `encode`, that
test is what fails.

ANYTHING THIS DOES NOT FULLY UNDERSTAND RAISES `Unsupported` (or the parser's
own error on a corrupt file; `_build_backend`'s broad except handles both),
and the caller falls back to model2vec: a vocabulary-quantized model (`weights`/`mapping`
tensors), a sentence-transformers layout, a `model_type` other than model2vec,
a vocab/table size mismatch, a missing file. Falling back costs ~350 ms; a
wrong vector costs silent recall damage, so the bar for the fast path is "the
exact case model2vec's first layout loads", nothing looser.

NO NETWORK, BY CONSTRUCTION. Resolution reads the local HF cache the way
model2vec's `maybe_get_cached_model_path` does (newest snapshot by mtime) and
never imports huggingface_hub; a cache miss raises `Unsupported`, and whether
the fallback may download is decided by the caller's `allow_download`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# StaticModel.encode's defaults (model2vec 0.9.0). The backend calls encode(texts)
# with no kwargs, so these are the values the stored vectors were built with.
_MAX_LENGTH = 512
_BATCH_SIZE = 1024


class Unsupported(Exception):
    """The files are not a plain model2vec model this loader reproduces exactly."""


class DirectStaticModel:
    """The subset of `model2vec.StaticModel` that `EmbeddingBackend` uses: `.dim`, `.encode`."""

    def __init__(self, embedding: "np.ndarray", tokenizer, normalize: bool):
        import numpy as np

        vocab = tokenizer.get_vocab()
        if len(vocab) != len(embedding):
            raise Unsupported(f"vocab size {len(vocab)} != embedding rows {len(embedding)}")
        self.embedding = embedding
        self.tokenizer = tokenizer
        self.normalize = normalize
        unk = getattr(tokenizer.model, "unk_token", None)
        if unk is not None and unk not in vocab:
            raise Unsupported(f"unk token {unk!r} is not in the vocab")
        self.unk_token_id = vocab[unk] if unk is not None else None
        self.median_token_length = int(np.median([len(token) for token in vocab]))
        self._encode_fast = hasattr(tokenizer, "encode_batch_fast")

    @property
    def dim(self) -> int:
        return self.embedding.shape[1]

    def _tokenize(self, sentences: Sequence[str]) -> list[list[int]]:
        m = _MAX_LENGTH * self.median_token_length
        sentences = [s[:m] for s in sentences]
        if self._encode_fast:
            encodings = self.tokenizer.encode_batch_fast(sentences, add_special_tokens=False)
        else:
            encodings = self.tokenizer.encode_batch(sentences, add_special_tokens=False)
        ids = [e.ids for e in encodings]
        if self.unk_token_id is not None:
            ids = [[i for i in row if i != self.unk_token_id] for row in ids]
        return [row[:_MAX_LENGTH] for row in ids]

    def _encode_batch(self, sentences: Sequence[str]) -> "np.ndarray":
        import numpy as np

        out = []
        for row in self._tokenize(sentences):
            if row:
                out.append(self.embedding[row].mean(axis=0))
            else:
                out.append(np.zeros(self.dim))
        arr = np.stack(out)
        if self.normalize:
            arr = arr / (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-32)
        return arr

    def encode(self, sentences: Sequence[str] | str) -> "np.ndarray":
        import numpy as np

        single = isinstance(sentences, str)
        if single:
            sentences = [sentences]
        batches = [sentences[i : i + _BATCH_SIZE] for i in range(0, len(sentences), _BATCH_SIZE)]
        arr = np.concatenate([self._encode_batch(b) for b in batches], axis=0)
        return arr[0] if single else arr


def _hub_cache() -> Path:
    # Same chain as embeddings._hf_hub_cache, plus the ~ / $VAR expansion
    # huggingface_hub.constants applies, so both loaders land on one directory.
    from techne.core.embeddings import _hf_hub_cache

    return Path(os.path.expandvars(os.path.expanduser(str(_hf_hub_cache()))))


def resolve_local(model_name: str) -> Path:
    """The folder model2vec would load `model_name` from, from disk only.

    Mirrors model2vec's `_resolve_folder` with `force_download=False`: an existing
    local path wins, else the newest snapshot (by mtime) in the HF hub cache.
    """
    local = Path(model_name)
    if local.exists():
        return local
    if model_name.count("/") != 1:
        raise Unsupported(f"{model_name!r} is not a local path or an org/name id")
    snapshots = _hub_cache() / f"models--{model_name.replace('/', '--')}" / "snapshots"
    if not snapshots.is_dir():
        raise Unsupported(f"{model_name!r} is not in the local HF cache ({snapshots})")
    revs = [p for p in snapshots.iterdir() if p.is_dir()]
    if not revs:
        raise Unsupported(f"{snapshots} holds no snapshot")
    return max(revs, key=lambda p: p.stat().st_mtime)


def load(model_name: str) -> DirectStaticModel:
    """Build a DirectStaticModel from local files. Raises on anything unsupported."""
    folder = resolve_local(model_name)
    config_path = folder / "config.json"
    weights_path = folder / "model.safetensors"
    tokenizer_path = folder / "tokenizer.json"
    missing = [p.name for p in (config_path, weights_path, tokenizer_path) if not p.exists()]
    if missing:
        # model2vec would try its sentence-transformers layouts next; leave that to it.
        raise Unsupported(f"{folder} lacks {', '.join(missing)}")

    with open(config_path) as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        raise Unsupported("config.json is not an object")
    if config.get("model_type", "model2vec") != "model2vec":
        raise Unsupported(f"model_type {config.get('model_type')!r}")

    import numpy as np
    from safetensors import safe_open
    from tokenizers import Tokenizer

    with safe_open(str(weights_path), framework="numpy") as tensors:
        keys = set(tensors.keys())
        if keys != {"embeddings"}:
            # "weights"/"mapping" mean vocabulary quantization; anything else is a
            # layout this loader has not been checked against.
            raise Unsupported(f"tensors {sorted(keys)} (expected only 'embeddings')")
        embedding = tensors.get_tensor("embeddings")
    if embedding.ndim != 2 or not np.issubdtype(embedding.dtype, np.floating):
        raise Unsupported(f"embeddings {embedding.dtype} {embedding.shape}")

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    model = DirectStaticModel(embedding, tokenizer, bool(config.get("normalize", False)))
    logger.debug("static_direct: loaded %s from %s (dim=%d)", model_name, folder, model.dim)
    return model
