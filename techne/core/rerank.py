"""Opt-in cross-encoder reranking for architect search (numpy only).

Architect search ranks by BM25. A cross-encoder reads the query and each candidate
together and scores how well the candidate answers it. Measured on the pooled
architect eval (78 judged queries, 2026-11-22): reranking BM25's top 20 with
cross-encoder/ms-marco-MiniLM-L-6-v2 raised nDCG@10 from 0.550 to 0.613
(+0.063, 95% CI +0.040 to +0.088). A 568M reranker (bge-reranker-v2-m3) did no
better, and reading 256 tokens instead of 512 cut the gain to +0.046.

NUMPY, NOT TORCH OR ONNXRUNTIME. The forward pass below is a plain BERT encoder
over the model's safetensors weights, tokenized by `tokenizers`; it matches the
sentence-transformers logits to ~1e-5. onnxruntime is about twice as fast but has
no Python 3.14 wheel for Intel Macs, and torch is far larger than the 91 MB
model. Cost on one CPU core: about 5.6 s to rerank 20 patterns at 512 tokens.
That is why it is off by default (`architect.rerank`).

THREADS. In a CPU-limited container, OpenBLAS starts one thread per host core and
the quota throttles them: measured 46 s per search instead of 5.6 s. Set
`OPENBLAS_NUM_THREADS=1` (or the container's CPU count) for the process.

FAILS SOFT. Any failure to load or score (a missing extra, an uncached model with
downloads off, a corrupt file) keeps the BM25 order and says why; search never
breaks because the reranker did.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Config names for supported models. Only BERT-style sequence classifiers with
#: one output logit fit the forward pass below.
MODELS = {"minilm": "cross-encoder/ms-marco-MiniLM-L-6-v2"}

#: The eval scored a pattern as its name and body joined by ". ", cut to this many
#: characters. Keep the two the same so the measured gain is the shipped one.
TEXT_CHARS = 3000


@dataclass
class RerankResult:
    hits: list[dict]
    applied: bool
    model: str
    reason: str | None = None
    seconds: float = 0.0

    def info(self) -> dict:
        out = {"model": self.model, "applied": self.applied, "seconds": round(self.seconds, 2)}
        if self.reason:
            out["reason"] = self.reason
        return out


def _erf(x, np):
    # Abramowitz & Stegun 7.1.26, |error| < 1.5e-7. numpy has no erf, and BERT's
    # GELU is the exact (erf) form, not the tanh approximation.
    s = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = (
        (((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592
    ) * t
    return s * (1 - poly * np.exp(-x * x))


def _layer_norm(x, w, b, np, eps=1e-12):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * w + b


class CrossEncoder:
    """A BERT sequence classifier read from a directory or the HF cache."""

    def __init__(self, weights: dict, config: dict, tokenizer, max_tokens: int):
        import numpy as np

        self.np = np
        self.W = {k.removeprefix("bert."): v.astype(np.float32) for k, v in weights.items()}
        self.heads = int(config["num_attention_heads"])
        self.layers = int(config["num_hidden_layers"])
        self.tok = tokenizer
        self.tok.enable_truncation(max_tokens, strategy="longest_first")
        self.tok.no_padding()

    @classmethod
    def from_files(
        cls, config_path, weights_path, tokenizer_path, max_tokens: int
    ) -> "CrossEncoder":
        from safetensors.numpy import load_file
        from tokenizers import Tokenizer

        with open(config_path) as f:
            config = json.load(f)
        return cls(
            load_file(str(weights_path)),
            config,
            Tokenizer.from_file(str(tokenizer_path)),
            max_tokens,
        )

    def score(self, query: str, docs: list[str], batch: int = 16) -> list[float]:
        np = self.np
        out: list[float] = []
        for i in range(0, len(docs), batch):
            enc = self.tok.encode_batch([(query, d) for d in docs[i : i + batch]])
            n = max(len(e.ids) for e in enc)
            ids = np.zeros((len(enc), n), np.int64)
            types = np.zeros_like(ids)
            mask = np.zeros((len(enc), n), np.float32)
            for j, e in enumerate(enc):
                k = len(e.ids)
                ids[j, :k] = e.ids
                types[j, :k] = e.type_ids
                mask[j, :k] = 1
            out.extend(self._forward(ids, types, mask))
        return out

    def _forward(self, ids, types, mask) -> list[float]:
        np, W = self.np, self.W
        B, T = ids.shape

        def lin(t, p):
            return t @ W[p + ".weight"].T + W[p + ".bias"]

        x = (
            W["embeddings.word_embeddings.weight"][ids]
            + W["embeddings.position_embeddings.weight"][:T]
            + W["embeddings.token_type_embeddings.weight"][types]
        )
        x = _layer_norm(x, W["embeddings.LayerNorm.weight"], W["embeddings.LayerNorm.bias"], np)
        D = x.shape[-1]
        hd = D // self.heads
        bias = ((1.0 - mask) * -1e9)[:, None, None, :]
        for i in range(self.layers):
            p = f"encoder.layer.{i}."
            q, k, v = (
                lin(x, p + "attention.self." + n)
                .reshape(B, T, self.heads, hd)
                .transpose(0, 2, 1, 3)
                for n in ("query", "key", "value")
            )
            a = q @ k.transpose(0, 1, 3, 2) / math.sqrt(hd) + bias
            a = np.exp(a - a.max(-1, keepdims=True))
            a /= a.sum(-1, keepdims=True)
            ctx = (a @ v).transpose(0, 2, 1, 3).reshape(B, T, D)
            x = _layer_norm(
                x + lin(ctx, p + "attention.output.dense"),
                W[p + "attention.output.LayerNorm.weight"],
                W[p + "attention.output.LayerNorm.bias"],
                np,
            )
            h = lin(x, p + "intermediate.dense")
            h = 0.5 * h * (1 + _erf(h / math.sqrt(2), np))
            x = _layer_norm(
                x + lin(h, p + "output.dense"),
                W[p + "output.LayerNorm.weight"],
                W[p + "output.LayerNorm.bias"],
                np,
            )
        pooled = np.tanh(lin(x[:, 0], "pooler.dense"))
        return (pooled @ W["classifier.weight"].T + W["classifier.bias"])[:, 0].tolist()


_loaded: dict[tuple[str, int], CrossEncoder] = {}


def load(name: str, max_tokens: int, *, allow_download: bool = True) -> CrossEncoder:
    """Load a configured model once per process. Raises on any failure."""
    key = (name, max_tokens)
    if key in _loaded:
        return _loaded[key]
    if name not in MODELS:
        raise ValueError(f"unknown reranker {name!r}; known: {', '.join(sorted(MODELS))}")
    from huggingface_hub import hf_hub_download

    repo = MODELS[name]
    t = time.time()
    paths = [
        hf_hub_download(repo, f, local_files_only=not allow_download)
        for f in ("config.json", "model.safetensors", "tokenizer.json")
    ]
    model = CrossEncoder.from_files(*paths, max_tokens=max_tokens)
    logger.debug("rerank: loaded %s (max_tokens=%d) in %.2fs", repo, max_tokens, time.time() - t)
    _loaded[key] = model
    return model


def pattern_text(hit: dict) -> str:
    return f"{hit.get('name') or ''}. {hit.get('body') or ''}"[:TEXT_CHARS]


def rerank_hits(
    query: str,
    hits: list[dict],
    *,
    name: str,
    top_k: int,
    max_tokens: int,
    allow_download: bool = True,
) -> RerankResult:
    """Reorder the first `top_k` hits by cross-encoder score; the rest keep their place.

    Never raises: on failure the hits come back in their original order with the
    reason, so the caller can say the reranker was configured but not applied.
    """
    t = time.time()
    head, tail = hits[:top_k], hits[top_k:]
    if len(head) < 2:
        return RerankResult(hits, False, name, "fewer than two candidates")
    try:
        model = load(name, max_tokens, allow_download=allow_download)
        scores = model.score(query, [pattern_text(h) for h in head])
    except ImportError as exc:
        reason = f"missing dependency ({exc.name}); install tenish-techne[rerank]"
        logger.warning("rerank: %s", reason)
        return RerankResult(hits, False, name, reason, time.time() - t)
    except Exception as exc:  # noqa: BLE001 — search must not break because the reranker did
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("rerank: not applied: %s", reason)
        return RerankResult(hits, False, name, reason, time.time() - t)
    order = sorted(range(len(head)), key=lambda i: -scores[i])
    ranked = [dict(head[i], rerank_score=round(scores[i], 4)) for i in order] + tail
    secs = time.time() - t
    logger.debug("rerank: %d candidates in %.2fs", len(head), secs)
    return RerankResult(ranked, True, name, None, secs)


def search_architect(config, db_path, query: str, *, limit: int = 20, allow_download: bool = True):
    """`search_patterns`, reranked when `architect.rerank` is set.

    Returns (hits, info). `info` is None when reranking is off, so callers that
    report it add nothing to their output for operators who never turned it on.
    BM25 fetches max(limit, top_k) candidates so a small `limit` still gets the
    full candidate set the gain was measured on.
    """
    from techne.core.patterns import search_patterns

    a = config.architect
    if not a.rerank:
        return search_patterns(db_path, query, limit=limit), None
    hits = search_patterns(db_path, query, limit=max(limit, a.rerank_top_k))
    res = rerank_hits(
        query,
        hits,
        name=a.rerank,
        top_k=a.rerank_top_k,
        max_tokens=a.rerank_max_tokens,
        allow_download=allow_download,
    )
    return res.hits[:limit], res.info()
