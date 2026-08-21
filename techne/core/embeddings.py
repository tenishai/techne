"""Shared embedding backend singleton (techne#56-18, techne#601/#602).

`_get_embedding_model` previously lived in search.py, which created a
backwards dependency: inference.py (the call-site) had to import from search.py
(a caller of inference). Moving the singleton here breaks the cycle — both
search.py and inference.py import from this neutral module.

Swappable backend (techne#601/#602)
------------------------------------
onnxruntime (a fastembed dependency) ships no Python 3.14 wheel, so fastembed
vector search is unusable there. model2vec/potion is a numpy-only static
embedding backend (no onnxruntime, no torch) that DOES install on 3.14. Rather
than forking every call site on backend, `EmbeddingBackend` wraps either
fastembed's `TextEmbedding` or model2vec's `StaticModel` behind one interface:

    .embed(texts: list[str]) -> Iterable[np.ndarray]   # 1-D float32 rows
    .dim: int                                            # output width
    .name: str                                           # stable identity string

Callers are unchanged: `np.stack(list(model.embed(texts)))` works identically
against either backend. `.name` is what search.py's vec-table migration stamps
to detect a backend/model swap (see search.py `_ensure_vec_table`).

Cold-cache downloads and hooks (techne#601/#602)
--------------------------------------------------
Building either backend for the FIRST time on a machine with no local HF cache
triggers a real network download (fastembed: onnx model + tokenizer; model2vec:
safetensors + tokenizer — both from the Hugging Face Hub). That's fine for an
explicit, operator-initiated action (CLI `backfill_vectors`), but
`_get_embedding_model()` is also reached from inference.py's `infer()`, which
runs inside techne's `post_tool_use` hook — wrapped in a SIGALRM-based
`bounded_hook` (hooks/_timeout.py) with a tight default 5s budget. A blocking
HTTP download is EXACTLY the case that guard's own docstring flags as
unpreemptable ("blocked inside a single long, signal-deaf C call isn't
interrupted until that call returns") — confirmed empirically: with a cold
cache, the hook subprocess was killed by the OS-level test-harness timeout,
never reaching bounded_hook's graceful exit-0 path at all.

`_get_embedding_model(..., allow_download=False)` is the fix: it passes
`local_files_only=True` (fastembed) / raises before attempting a Hub fetch
(model2vec — see `_build_backend`) so an uncached model degrades to
ImportError-like unavailability (caller's existing try/except treats it the
same as "not installed") instead of blocking on a network call. inference.py
passes `allow_download=False` since it's the hook-reachable path; search.py's
`backfill_vectors` keeps the default `allow_download=True` so an operator's
first explicit backfill can still fetch the model.

CAVEAT ON THAT DEFAULT (techne#704). This docstring used to justify
`allow_download=True` on the grounds that `backfill_vectors` is "never
hook-invoked". That premise is only safe for the code in THIS repo, where the
`techne vectors backfill` CLI command is the sole non-test caller. A deployment
is free to chain that CLI into a hook as a shell string — one observed
SessionStart hook does exactly that:

    preflight_guard.py ; techne vectors backfill ; -m techne.hooks.session_start

and settings.json wiring is invisible to the Python call graph. In that shape the
cold-cache download this section exists to prevent is reachable from session
startup, and WORSE than the inference.py case it was written about: a bare shell
command gets no `bounded_hook` SIGALRM budget at all, so there is nothing to
degrade gracefully — startup simply blocks on the Hub fetch. The `allow_download`
default is therefore a deployment-shape decision, not a settled invariant; if
this becomes a real hang, the fix is to make the auto-invoked path pass
`allow_download=False` rather than to reword this paragraph.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import numpy as np

    from techne.config import Config

# Default models per backend. Model choice went through three A/B runs on real
# techne data (techne#602/#603): with doc-to-doc TAG-similarity labels
# potion-retrieval-32M (512-d) won, but with natural-language paraphrase queries
# — which is how techne_recall is actually used — potion-base-8M (256-d) beat
# retrieval-32M (static embeddings trail transformers on abstractive matching,
# and the retrieval-tuned variant fared *worst* on NL queries). base-8M is also
# the smaller/faster model, so it's the default; retrieval-32M and base-32M stay
# available via config override. PROVISIONAL: the NL run used silver (LLM-judge)
# labels — revisit once the human gold set (scratchpad/gold_eval_labeling.jsonl)
# is scored, and ideally after measuring combined FTS+vector recall.
_DEFAULT_MODEL2VEC_MODEL = "minishlab/potion-base-8M"
_DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_embedding_model: "EmbeddingBackend | None" = None


class EmbeddingBackend:
    """Uniform wrapper around a fastembed `TextEmbedding` or model2vec `StaticModel`.

    Constructed via `_build_backend()` below — never instantiate directly with
    a bare model object from outside this module, since normalization behavior
    depends on which underlying library produced the model.
    """

    def __init__(self, backend: str, model_name: str, impl, dim: int):
        self._backend = backend  # "fastembed" | "model2vec"
        self._impl = impl
        self.name = f"{backend}:{model_name}"
        self.dim = dim

    def embed(self, texts: list[str]) -> Iterable["np.ndarray"]:
        """Yield one 1-D float32 embedding per input text, same order as input.

        fastembed's `TextEmbedding.embed()` already returns an iterable of 1-D
        arrays, normalized by default — passed through unchanged.

        model2vec's `StaticModel.encode()` returns a single 2-D (N, dim) array
        and does NOT guarantee normalization for every model/config combo (see
        `_l2_normalize_rows` docstring) — rows are L2-normalized here before
        being yielded so distance semantics match the fastembed path.
        """
        if self._backend == "fastembed":
            yield from self._impl.embed(texts)
            return

        # model2vec path.
        import numpy as np

        vecs = self._impl.encode(texts).astype("float32")
        vecs = _l2_normalize_rows(vecs, np)
        yield from vecs


def _l2_normalize_rows(vecs: "np.ndarray", np) -> "np.ndarray":
    """L2-normalize each row of a 2-D array to unit length.

    Why this matters: sqlite-vec's vec0 virtual table (search.py's threads_vec/
    observations_vec/knowledge_vec) uses L2 (Euclidean) distance, not cosine.
    On unit-normalized vectors, L2 distance and cosine distance are monotonic
    in each other (||a-b||^2 = 2 - 2*cos(a,b) when ||a||=||b||=1), so ranking
    is identical to cosine similarity — which is what these embeddings are
    actually trained/evaluated for. fastembed's TextEmbedding normalizes its
    outputs by default, so the existing vec tables (and search_vec's distance
    ordering) implicitly assume unit vectors.
    model2vec's `StaticModel.encode()` bakes normalization into the model's
    saved config (`model.normalize`) rather than guaranteeing it universally —
    both bundled potion models default to normalize=True, but a future model
    (or an explicit `normalize=False` override) could yield raw, unnormalized
    vectors whose norms vary with sentence length (observed 19-48 for an
    unnormalized potion-8M encode vs. an expected 1.0). Normalizing here,
    unconditionally, is defense-in-depth: harmless (idempotent) when the
    model already normalized, and required when it didn't — either way the
    L2 distances written into vec0 stay comparable to the fastembed baseline.
    Zero vectors (pathological/empty input) are left as-is rather than
    dividing by zero.
    """
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0.0, 1.0, norms)
    return vecs / safe_norms


def _model2vec_importable() -> bool:
    try:
        import model2vec  # noqa: F401

        return True
    except ImportError:
        return False


def _fastembed_importable() -> bool:
    try:
        import fastembed  # noqa: F401

        return True
    except ImportError:
        return False


def backend_available() -> bool:
    """True iff at least one embedding backend (model2vec or fastembed) is importable.

    The single availability probe for callers (search.py, inference.py) that
    previously did `from fastembed import TextEmbedding` inside a try/except
    ImportError just to decide whether to degrade to []/disabled. Those probes
    missed model2vec entirely — they'd wrongly report "unavailable" on a 3.14
    install that has model2vec but not fastembed. This is the fix.
    """
    return _model2vec_importable() or _fastembed_importable()


def _select_backend(config: "Config | None") -> tuple[str, str]:
    """Return (backend, model_name) to construct, honoring config override else AUTO.

    Explicit config (`config.search.embedding_backend`) always wins when set.
    AUTO (no override, the common case):
      - model2vec, IF it's importable AND (we're on py>=3.14 OR fastembed isn't
        importable). This is the 3.14 case (fastembed/onnxruntime literally
        can't install there) AND the "operator chose to only install model2vec"
        case on older pythons.
      - else fastembed, if importable (pre-3.14 default, unchanged behavior).
      - else raise ImportError — callers already guard this with try/except
        (search.py/inference.py degrade to []/disabled).
    """
    backend_override = ""
    model_override = ""
    if config is not None:
        backend_override = (config.search.embedding_backend or "").strip().lower()
        model_override = (config.search.embedding_model or "").strip()

    if backend_override in ("model2vec", "fastembed"):
        default_for_override = (
            _DEFAULT_MODEL2VEC_MODEL if backend_override == "model2vec" else _DEFAULT_FASTEMBED_MODEL
        )
        return backend_override, model_override or default_for_override

    # AUTO selection.
    model2vec_ok = _model2vec_importable()
    fastembed_ok = _fastembed_importable()
    py314_plus = sys.version_info >= (3, 14)

    if model2vec_ok and (py314_plus or not fastembed_ok):
        return "model2vec", model_override or _DEFAULT_MODEL2VEC_MODEL
    if fastembed_ok:
        return "fastembed", model_override or _DEFAULT_FASTEMBED_MODEL
    # model2vec_ok here implies py314_plus was False AND fastembed_ok was True
    # (else the first branch above would have matched) — but fastembed_ok=True
    # would have hit the second branch. So reaching here with model2vec_ok=True
    # is impossible; both `if`s above are exhaustive for any True flag. Only
    # the neither-importable case falls through.
    raise ImportError(
        "no embedding backend available — install techne[vectors] "
        "(model2vec on py3.14+, fastembed on <3.14)"
    )


def _build_backend(
    backend: str, model_name: str, *, allow_download: bool = True
) -> EmbeddingBackend:
    """Construct an EmbeddingBackend. Raises (ImportError/OSError-family) if the
    backend package is missing, OR — when `allow_download=False` — if the model
    isn't already present in the local HF cache. See the module docstring
    ("Cold-cache downloads and hooks") for why `allow_download=False` exists:
    callers reachable from techne's hook subprocesses must never risk a
    blocking network fetch that SIGALRM-based bounded_hook can't reliably
    preempt.
    """
    if backend == "fastembed":
        from fastembed import TextEmbedding

        cache_dir = Path.home() / ".cache" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # local_files_only=True makes fastembed raise instead of hitting the
        # Hub when the model isn't already cached — the offline-safe knob for
        # hook-reachable callers (see module docstring).
        impl = TextEmbedding(
            model_name=model_name, cache_dir=str(cache_dir), local_files_only=not allow_download
        )
        # fastembed doesn't expose dim as a plain attribute uniformly across
        # versions — the reliable way is to probe with a throwaway encode.
        dim = len(next(iter(impl.embed(["dim probe"]))))
        return EmbeddingBackend("fastembed", model_name, impl, dim)

    if backend == "model2vec":
        # No cache_dir plumbing here — model2vec uses the standard HF cache
        # (~/.cache/huggingface) by default, same convention operators already
        # have configured for other HF downloads. Nothing to mkdir.
        #
        # force_download=False is NOT optional here regardless of allow_download:
        # model2vec's own default is force_download=True (verified against
        # installed 0.8.2 — StaticModel.from_pretrained's signature literally
        # defaults to True), which re-hits the Hub to re-validate/re-fetch every
        # file on EVERY call, cache or no cache.
        #
        # allow_download=False additionally sets HF_HUB_OFFLINE=1 for the
        # duration of the call: model2vec has no first-class local_files_only
        # kwarg of its own (unlike fastembed's TextEmbedding), so the HF-Hub-
        # standard env var is the mechanism — it makes an uncached model raise
        # LocalEntryNotFoundError immediately (~0.2s, verified) instead of
        # blocking on a real download. Scoped to just this call (restored in
        # `finally`) so it can't leak into an unrelated concurrent download
        # elsewhere in the process.
        import os

        from model2vec import StaticModel

        prior_offline = os.environ.get("HF_HUB_OFFLINE")
        if not allow_download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            impl = StaticModel.from_pretrained(model_name, force_download=False)
        finally:
            if not allow_download:
                if prior_offline is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = prior_offline
        dim = impl.dim if hasattr(impl, "dim") else impl.encode(["dim probe"]).shape[1]
        return EmbeddingBackend("model2vec", model_name, impl, dim)

    raise ValueError(f"unknown embedding backend {backend!r}")  # unreachable via _select_backend


def _get_embedding_model(
    config: "Config | None" = None, *, allow_download: bool = True
) -> EmbeddingBackend:
    """Return the process-singleton `EmbeddingBackend`.

    Lazy-initialises on first call; subsequent calls return the cached instance
    regardless of `config`/`allow_download` (the singleton is built once per
    process — a config change mid-process requires a restart, same as the
    original fastembed-only singleton). Callers should guard with
    try/except ImportError (or check `backend_available()` first) before
    calling.

    `config` is optional and only consulted on the FIRST call (to build the
    singleton); passing it is how callers opt into an explicit backend/model
    override via `config.search.embedding_backend` / `.embedding_model`.

    `allow_download=False` (pass this from any hook-reachable call site — see
    inference.py) refuses a cold-cache network fetch and raises instead,
    rather than risking a blocking download that a SIGALRM-based hook timeout
    can't reliably preempt. On a FAILED allow_download=False attempt, the
    singleton slot is deliberately left unset (not cached as broken) so a
    later call in the same process — e.g. a different code path willing to
    download — can still succeed.
    """
    global _embedding_model
    if _embedding_model is None:
        backend, model_name = _select_backend(config)
        _embedding_model = _build_backend(backend, model_name, allow_download=allow_download)
    return _embedding_model


def _reset_embedding_model() -> None:
    """Test-only: clear the process singleton so the next call rebuilds it."""
    global _embedding_model
    _embedding_model = None
