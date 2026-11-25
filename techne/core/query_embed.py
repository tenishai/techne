"""Embed one search query without numpy (issue 1217).

The per-turn recall hook embeds a single prompt in a fresh process. Through the
normal backend that meant importing numpy (~55 ms) and reading the whole 30 MB
potion table (``static_direct.load``, ~90 ms) to look up a few dozen rows. A
potion query vector is the mean of the rows for the query's token ids, so this
module memory-maps ``model.safetensors``, reads only those rows with
``struct.unpack_from``, and does the arithmetic in Python.

SAME BYTES AS THE NUMPY PATH, NOT JUST CLOSE. Every operation is rounded to
float32 in the order numpy performs it, so the result equals
``EmbeddingBackend.embed`` over ``static_direct.DirectStaticModel.encode`` bit
for bit. That order (numpy 2.x, float32):

  1. ``embedding[ids].mean(axis=0)``: an axis-0 reduction, so a running sum row
     by row, starting from 0, then one division by the count.
  2. ``np.linalg.norm(arr, axis=1)`` is ``sqrt(add.reduce(x * x, axis=1))``, and
     an add-reduce along the contiguous axis is numpy's pairwise summation
     (``_pairwise_sum`` below, from ``loops_utils.h.src``), starting from 0.
  3. ``arr / (norm + 1e-32)`` when the model config says ``normalize``, then
     ``EmbeddingBackend``'s own L2 normalisation (a zero norm divides by 1).

Python floats are doubles, and a double result rounded to float32 is the
correctly rounded float32 result for +, *, / and sqrt (53 >= 2*24 + 2), so
rounding after each step reproduces float32 arithmetic exactly.
``tests/test_query_embed.py`` pins the equality against the installed numpy.
CI runs it on the model2vec leg, where a skip fails the step, so a numpy
version or build that rounds differently (another summation order, contracted
multiply-adds) fails there; the vectors would then differ in the last bit, which
moves distances by ~1e-7.

ANYTHING ``static_direct.load`` WOULD REFUSE, THIS REFUSES (returns None), plus
a table that is not float32, and the caller takes the numpy path. That keeps
the set of models this handles inside the set ``static_direct`` handles, so it
never answers where model2vec itself would have been asked.

The median token length (``static_direct`` computes it with ``np.median`` over
the whole vocabulary, ~35 ms) is cached in ``$TECHNE_HOME/.query_embed.json``,
keyed by snapshot folder and ``tokenizer.json`` mtime and size. NO NETWORK: the
model folder comes from ``static_direct.resolve_local``, which reads the local
HF cache only.
"""

from __future__ import annotations

import json
import math
import os
import struct
from array import array
from operator import add
from pathlib import Path

from techne._lazylog import get_logger

log = get_logger(__name__)

SIDECAR_NAME = ".query_embed.json"

#: numpy's PW_BLOCKSIZE: pairwise summation splits blocks larger than this.
_PW_BLOCKSIZE = 128

_F32 = struct.Struct("<f")

#: Loaded models, by snapshot folder. One process (the MCP server) may embed many
#: queries; the hook embeds one.
_MODELS: dict[str, "_QueryModel"] = {}

#: ``(backend name, dim)`` of the model the last fast-path embed used, in the form
#: ``EmbeddingBackend.name`` takes ("model2vec:<model>"). The fast path never builds
#: the embeddings singleton, so ``search._index_backend_mismatch`` reads this to
#: check the index stamp against the model that actually embedded the query.
_SERVED: tuple[str, int] | None = None


def served_identity() -> tuple[str, int] | None:
    """``(name, dim)`` of the model the last successful ``embed_query_bytes`` used, or None."""
    return _SERVED


class Unsupported(Exception):
    """A model or file layout this module does not reproduce exactly."""


def _f32(x: float) -> float:
    return _F32.unpack(_F32.pack(x))[0]


def _pairwise_sum(a: list[float], lo: int, n: int) -> float:
    """numpy's pairwise summation of ``a[lo:lo+n]``, each add rounded to float32."""
    if n < 8:
        res = 0.0
        for i in range(lo, lo + n):
            res = _f32(res + a[i])
        return res
    if n <= _PW_BLOCKSIZE:
        r = a[lo : lo + 8]
        i = 8
        stop = n - n % 8
        while i < stop:
            r = array("f", map(add, r, a[lo + i : lo + i + 8])).tolist()
            i += 8
        res = _f32(
            _f32(_f32(r[0] + r[1]) + _f32(r[2] + r[3]))
            + _f32(_f32(r[4] + r[5]) + _f32(r[6] + r[7]))
        )
        for k in range(lo + i, lo + n):
            res = _f32(res + a[k])
        return res
    n2 = n // 2
    n2 -= n2 % 8
    return _f32(_pairwise_sum(a, lo, n2) + _pairwise_sum(a, lo + n2, n - n2))


def _norm(vec: list[float]) -> float:
    """``np.linalg.norm`` of one float32 row, as numpy computes it."""
    squares = array("f", [v * v for v in vec]).tolist()
    return _f32(math.sqrt(_f32(0.0 + _pairwise_sum(squares, 0, len(squares)))))


def _sidecar_path() -> Path:
    from techne.config import techne_home

    return techne_home() / SIDECAR_NAME


def _median_token_length(tokenizer, folder: Path) -> tuple[int, int]:
    """(median token length, vocab size), from the sidecar or computed and cached.

    ``int(statistics.median(...))`` equals ``static_direct``'s
    ``int(np.median(...))``: both average the two middle values of an even-sized
    list, and the lengths are small integers, so the mean is exact.
    """
    st = (folder / "tokenizer.json").stat()
    key = {"folder": str(folder), "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    path = _sidecar_path()
    try:
        cached = json.loads(path.read_text())
        if {k: cached.get(k) for k in key} == key:
            median, vocab_size = cached["median"], cached["vocab_size"]
            if isinstance(median, int) and isinstance(vocab_size, int):
                return median, vocab_size
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # absent, corrupt or stale: derive it again

    import statistics

    vocab = tokenizer.get_vocab()
    median = int(statistics.median(len(token) for token in vocab))
    vocab_size = len(vocab)
    # PID-unique tmp then rename, as fastconfig.write_cache does: concurrent hooks
    # must never rename a half-written file into place. Best-effort.
    tmp = path.with_name(f"{SIDECAR_NAME}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({**key, "median": median, "vocab_size": vocab_size}))
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
    return median, vocab_size


class _QueryModel:
    """The pieces of a potion model needed to embed a query, table memory-mapped."""

    def __init__(self, folder: Path):
        import mmap

        from techne.core import static_direct

        config_path = folder / "config.json"
        weights_path = folder / "model.safetensors"
        tokenizer_path = folder / "tokenizer.json"
        missing = [p.name for p in (config_path, weights_path, tokenizer_path) if not p.exists()]
        if missing:
            raise Unsupported(f"{folder} lacks {', '.join(missing)}")
        with open(config_path) as fh:
            config = json.load(fh)
        if not isinstance(config, dict):
            raise Unsupported("config.json is not an object")
        if config.get("model_type", "model2vec") != "model2vec":
            raise Unsupported(f"model_type {config.get('model_type')!r}")
        self.normalize = bool(config.get("normalize", False))

        with open(weights_path, "rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            (header_len,) = struct.unpack_from("<Q", mm, 0)
            header = json.loads(mm[8 : 8 + header_len])
            tensors = {k: v for k, v in header.items() if k != "__metadata__"}
            if set(tensors) != {"embeddings"}:
                raise Unsupported(f"tensors {sorted(tensors)} (expected only 'embeddings')")
            meta = tensors["embeddings"]
            if meta.get("dtype") != "F32":
                # static_direct keeps F16/F64 tables in their dtype; leave those to it.
                raise Unsupported(f"embeddings dtype {meta.get('dtype')!r}")
            shape = meta.get("shape")
            if not (isinstance(shape, list) and len(shape) == 2):
                raise Unsupported(f"embeddings shape {shape!r}")
            rows, dim = shape
            start, end = meta["data_offsets"]
            if end - start != rows * dim * 4 or 8 + header_len + end > len(mm):
                raise Unsupported("embeddings data_offsets do not match the shape")
        except Exception:
            mm.close()
            raise
        self._mm = mm
        self._base = 8 + header_len + start
        self.rows, self.dim = rows, dim
        self._row = struct.Struct(f"<{dim}f")

        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.median_token_length, vocab_size = _median_token_length(tokenizer, folder)
        if vocab_size != rows:
            raise Unsupported(f"vocab size {vocab_size} != embedding rows {rows}")
        unk = getattr(tokenizer.model, "unk_token", None)
        self.unk_id = None
        if unk is not None:
            self.unk_id = tokenizer.token_to_id(unk)
            if self.unk_id is None:
                raise Unsupported(f"unk token {unk!r} is not in the vocab")
        self.tokenizer = tokenizer
        self._encode_fast = hasattr(tokenizer, "encode_batch_fast")
        self._max_length = static_direct._MAX_LENGTH

    def _ids(self, text: str) -> list[int]:
        """``DirectStaticModel._tokenize`` for one sentence."""
        text = text[: self._max_length * self.median_token_length]
        if self._encode_fast:
            enc = self.tokenizer.encode_batch_fast([text], add_special_tokens=False)[0]
        else:
            enc = self.tokenizer.encode_batch([text], add_special_tokens=False)[0]
        ids = enc.ids
        if self.unk_id is not None:
            ids = [i for i in ids if i != self.unk_id]
        return ids[: self._max_length]

    def embed(self, text: str) -> bytes:
        """The query's float32 vector, as the bytes sqlite-vec takes."""
        ids = self._ids(text)
        if not ids:
            # static_direct yields a zero row; both normalisations leave it zero.
            return array("f", bytes(4 * self.dim)).tobytes()
        if max(ids) >= self.rows or min(ids) < 0:
            raise Unsupported("token id outside the embedding table")
        acc = array("f", bytes(4 * self.dim))
        for i in ids:
            acc = array(
                "f", map(add, acc, self._row.unpack_from(self._mm, self._base + i * self.dim * 4))
            )
        n = len(ids)
        vec = array("f", [v / n for v in acc]).tolist()
        if self.normalize:
            denom = _f32(_norm(vec) + _f32(1e-32))
            vec = array("f", [v / denom for v in vec]).tolist()
        norm = _norm(vec)
        if norm != 0.0:
            vec = array("f", [v / norm for v in vec]).tolist()
        return array("f", vec).tobytes()


def _model_name(config) -> str | None:
    """The model2vec model ``_get_embedding_model`` would serve, or None for any other backend.

    A singleton that is already built is what the process embeds with, so its
    identity wins; otherwise this is the same choice ``_get_embedding_model``
    would make on its first build.
    """
    from techne.core import embeddings

    current = embeddings._embedding_model
    if current is not None:
        backend, _, name = str(getattr(current, "name", "")).partition(":")
        return name if backend == "model2vec" and name else None
    # The ambient Config, like the resolver (techne#1260): without it an operator's
    # fastembed or custom-model override was ignored here, and the query embedded
    # with the AUTO potion model whatever the index was built with.
    backend, name = embeddings._select_backend(embeddings._ambient_config(config))
    return name if backend == "model2vec" else None


def embed_query_bytes(text: str, config=None) -> bytes | None:
    """``text``'s embedding as float32 bytes, or None to send the caller to the numpy path.

    None covers every case this does not handle exactly: another backend, a model
    not in the local cache, a layout ``static_direct`` would refuse, a non-float32
    table, a missing ``tokenizers``. Never raises and never downloads.
    """
    try:
        name = _model_name(config)
        if name is None:
            return None
        from techne.core import static_direct

        folder = static_direct.resolve_local(name)
        model = _MODELS.get(str(folder))
        if model is None:
            model = _MODELS[str(folder)] = _QueryModel(folder)
        out = model.embed(text)
        global _SERVED
        _SERVED = (f"model2vec:{name}", model.dim)
        return out
    except Exception as exc:  # noqa: BLE001 — any failure means "use the numpy path"
        log.debug("query_embed: using the numpy path: %r", exc)
        return None
