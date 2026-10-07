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

The model2vec side is usually NOT a `StaticModel` (techne#1096): `_build_backend`
first builds a `static_direct.DirectStaticModel` from the cached files, which
produces the same vectors without the ~350 ms model2vec import, and falls back to
`StaticModel` for anything it cannot reproduce exactly. See core/static_direct.py.

Callers are unchanged: `np.stack(list(model.embed(texts)))` works identically
against either backend. `.name` is what search.py's vec-table migration stamps
to detect a backend/model swap (see search.py `_ensure_vec_table`).

Cold-cache downloads and hooks (techne#601/#602)
--------------------------------------------------
Building either backend for the FIRST time on a machine with no local HF cache
triggers a real network download (fastembed: onnx model + tokenizer; model2vec:
safetensors + tokenizer — both from the Hugging Face Hub). That is fine for an
explicit, operator-initiated action (CLI `backfill_vectors`) — BUT SEE THE
CAVEAT AT THE END OF THIS SECTION: a deployment can wire that CLI into a hook,
at which point "operator-initiated" stops being true and this dichotomy is the
thing that breaks.

Separately, `_get_embedding_model()` is also reached from inference.py's
`infer()`, which runs inside techne's `post_tool_use` hook — and that is NOT
the only such path, see the second caveat below. It is wrapped in a
SIGALRM-based `bounded_hook` (hooks/_timeout.py) with a tight 5s default. A blocking
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

RESOLVED, AND THE OPT-IN SURFACE NOW EXISTS (techne#814). This section used to
say the `allow_download` default was "a deployment-shape decision, not a settled
invariant" — because one observed deployment hand-wired `techne vectors backfill`
into its SessionStart hook, and settings.json wiring is invisible to the Python
call graph. Two things changed.

First, techne itself now writes that chain: `platforms/claude_code/hooks.py`
puts the backfill segment in `register_hooks`' SessionStart template, because
the hand edit it replaced was silently deleted by every `techne init`. So the
hook-invoked path is no longer some deployment's private choice — it is the
shipped default, on every install.

Second, that is exactly what made the fix below actionable. The paragraph that
used to sit here said "make the auto-invoked path pass `allow_download=False`"
was impossible, since operator shell and hook chain invoked the identical string
with no in-process signal to branch on. Owning the chain CREATES the branch
point: the hook passes `--no-download`, which reaches `backfill_vectors`'
`allow_download` parameter. A cold cache degrades in ~0.2s instead of blocking
session start on a Hub fetch.

THE DEFAULT IS STILL PERMISSIVE, and must stay that way: the bare CLI is
README's documented cold-cache recovery, so guarding it too would leave nothing
able to fetch the weights — techne#496's never-green shape from the other side.
Both directions are pinned by test in tests/test_cold_cache_no_hang.py; read the
two as a pair, since each looks wrong alone.

DO NOT REASON ABOUT THESE PATHS VIA THE SIGALRM BUDGET (PR #705 review r3, which
caught an earlier draft of this paragraph doing exactly that). It is tempting to
rank the shell chain as worse because it carries no `bounded_hook`, but that
ranking contradicts the finding above: a Hub fetch is signal-deaf, so the budget
does not bound it on ANY path. That is precisely why inference.py passes
`allow_download=False` even though it already runs under `bounded_hook`. Having
a budget and lacking one are equally exposed; only the kwarg helps.

THE OPT-IN SURFACE THIS PARAGRAPH USED TO ASK FOR NOW EXISTS — scope it
carefully, it is `backfill_vectors` ONLY (PR #705 review r1). The obstacle was
real: operator shell and hook chain invoked the identical string `techne vectors
backfill`, landing on the same `cli.py` command, so in-process there was NO
signal to branch on. Flipping the default instead would break README.md's
documented cold-cache recovery, which REQUIRES the download — rebuilding the
never-green #496 defect from the other side.

What removed the obstacle was techne#814 putting the segment in
`register_hooks`' template: the branch point that does not exist in-process
exists in the shell string this package now writes. The flag is `--no-download`
(cli.py's `vectors backfill`), reaching `backfill_vectors(allow_download=...)`.
The hook sets it; the bare CLI does not.

SECOND HOOK-REACHABLE PATH, AND THIS ONE *DOES* HAVE A BRANCH POINT (PR #705
review r2). Do not read the paragraph above as covering the whole module. The
per-turn recall rail reaches an UNGUARDED `_get_embedding_model()`:

    hooks/user_prompt_submit.py -> search() [use_vectors=True by default]
      -> search_vec() -> `_get_embedding_model()`   <- no kwarg

ONE live site, not two. `_mmr_reorder` also calls it unguarded, but that call
sits below a `mmr_lambda == 0.0` short-circuit that NO live caller lifts (the
config knob was removed) — see docs/arch/search-embeddings.md, "MMR — CODE
EXISTS, NO CONFIG SURFACE". An earlier draft of this note counted it; #705
review r3 caught that, and the card had already said so.

The live site takes the permissive `allow_download=True` default, and
`backend_available()` does not save it: that is a PACKAGE probe, so it passes
on a cold HF cache and the download is attempted anyway.

Unlike backfill this is an in-repo call site with no CLI in between, so there
IS an in-process branch point. It is NOT a free one-liner, though — see #708:
every other in-process site already passes `allow_download=False`, so guarding
this one too leaves the CLI as the ONLY thing that can ever warm the cache, and
an operator who installs `[vectors]` and never runs backfill gets permanently
FTS-only recall with `backend_available()` still reporting True. That is #496's
never-green shape on the query side, and it is why the fix needs its own change
rather than riding along here.
"""

from __future__ import annotations

import logging
import os
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

_log = logging.getLogger(__name__)


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
    """Is model2vec INSTALLED? Answered with find_spec, not an import (techne#1096).

    `import model2vec` costs ~130 ms (huggingface_hub, joblib, tqdm) and this probe
    runs on every per-turn hook via `backend_available()`, which would spend the
    time `static_direct` exists to save. A module already in sys.modules (or set to
    None there, the usual way to simulate "not installed") answers from that entry.
    """
    import importlib.util

    if "model2vec" in sys.modules:
        return sys.modules["model2vec"] is not None
    try:
        return importlib.util.find_spec("model2vec") is not None
    except (ImportError, ValueError):
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
            _DEFAULT_MODEL2VEC_MODEL
            if backend_override == "model2vec"
            else _DEFAULT_FASTEMBED_MODEL
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
    from techne import install_spec

    raise ImportError(
        f"no embedding backend available — run `pip install '{install_spec('vectors')}'` "
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
        # FAST PATH FIRST (techne#1096): read the cached files with tokenizers +
        # safetensors and skip the ~350 ms model2vec import chain. Same vectors,
        # same identity string, local files only; see core/static_direct.py.
        # Anything it cannot reproduce exactly (or a cache miss) falls through to
        # model2vec below, which is also the only place a download can happen.
        from techne.core import static_direct

        try:
            impl = static_direct.load(model_name)
        except Exception as exc:  # noqa: BLE001 — any failure means "let model2vec do it"
            _log.debug("static_direct unavailable for %s, using model2vec: %r", model_name, exc)
        else:
            return EmbeddingBackend("model2vec", model_name, impl, impl.dim)

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


def _flatten(name: str) -> str:
    """Lower-case *name* with every separator that differs between a model id and its
    on-disk directory removed. BOTH SIDES OF A COMPARISON MUST USE THIS — they used to
    strip different characters (`/` and `-` vs `-` and `_`), so a model name carrying
    `_` missed on the asymmetry alone (Prudence, #728 r2 nit)."""
    return name.replace("/", "").replace("-", "").replace("_", "").lower()


def _hf_hub_cache() -> Path:
    """The huggingface_hub cache directory, honouring its FULL resolution chain.

    `huggingface_hub/constants.py` resolves HF_HUB_CACHE -> HUGGINGFACE_HUB_CACHE ->
    $HF_HOME/hub -> $XDG_CACHE_HOME/huggingface/hub -> ~/.cache/huggingface/hub. We
    honoured the first and third only, so with weights genuinely on disk under either
    of the other two, `model_cached()` reported False — measured (Prudence, #728 r2):

        XDG_CACHE_HOME                        -> False
        HUGGINGFACE_HUB_CACHE (legacy alias)  -> False
        HF_HUB_CACHE (the one we honoured)    -> True
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return Path(os.environ[var])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_cached(config: "Config | None" = None) -> bool | None:
    """Are the embedding WEIGHTS already on disk — WITHOUT building the backend?

    THIS IS A FILESYSTEM CHECK, DELIBERATELY, AND THE DISTINCTION IS THE WHOLE POINT.
    `techne#361` pins that `collect_health` must not build the backend on an install
    with no vec tables (`test_collect_health_skips_backend_build_when_no_vectors` spies
    on `_get_embedding_model` and asserts it is never called) — the load plus dim probe
    is 85-290 MB of work on the SessionStart path. A previous attempt to answer this
    question in that branch called the resolver and broke exactly that invariant.

    So it answers by LOOKING, not by loading: the HF hub cache layout for model2vec,
    fastembed's own cache dir. No import of the backend, no construction, no network.

    Returns True / False, or **None when it cannot tell** — an unknown backend, or a
    cache convention this does not know. None is not False: reporting "not cached" for
    a state we did not measure is the guess-dressed-as-a-fact this codebase keeps
    finding, and the caller must be able to stay quiet instead.
    """
    try:
        backend, model_name = _select_backend(config)
    except Exception:  # noqa: BLE001 — no backend installed is not "not cached"
        return None

    if backend == "fastembed":
        cache = Path.home() / ".cache" / "fastembed"
        if not cache.is_dir():
            return False
        # FASTEMBED NAMES THE DIRECTORY AFTER THE SOURCE REPO, NOT THE LOGICAL MODEL,
        # and matching the logical name reported a fully-cached model as NOT cached
        # (Prudence, #728 r2). Measured on the author's own cache:
        #     logical:  sentence-transformers/all-MiniLM-L6-v2
        #     on disk:  models--qdrant--all-MiniLM-L6-v2-onnx
        # `sentencetransformersallminilml6v2` cannot match `modelsqdrantallminilml6v2onnx`
        # and could not for the rest of the Qdrant-hosted catalogue either. The LAST path
        # segment is what the two spellings share: `allminilml6v2` IS a substring of the
        # directory, while an uncached `BAAI/bge-small-en-v1.5` still finds nothing.
        #
        # BOTH SIDES NORMALISE IDENTICALLY via `_flatten`. They did not: the needle
        # stripped `/` and `-` while the directory stripped `-` and `_`, so a model name
        # carrying `_` missed on that asymmetry alone.
        needle = _flatten(model_name.rsplit("/", 1)[-1])
        for entry in cache.iterdir():
            if not entry.is_dir() or needle not in _flatten(entry.name):
                continue
            # KEEP LOOKING AFTER A NAME MATCH THAT HAS NO WEIGHTS (Prudence, #728 r3
            # nit 1). This used to `return any(...)` on the FIRST directory whose name
            # matched, so an interrupted `models--baai--bge-small-en` sorting ahead of
            # a complete `models--qdrant--bge-small-en-onnx` answered False with the
            # weights on disk — the r2 false negative again, in a narrower form and
            # DEPENDENT ON READDIR ORDER, which is worse than simply wrong because it
            # will not reproduce. Measured with the order forced:
            #     ['models--baai--bge-small-en', 'models--qdrant--bge-small-en-onnx']
            #     weights in the second; probe -> False
            # AN EMPTY REPO DIR IS AN INTERRUPTED FETCH, NOT A CACHE HIT. The model2vec
            # branch below already knew this; fastembed had no equivalent, and
            # `snapshot_download` creates `<repo>/refs/` BEFORE any weight lands, so a
            # Ctrl-C'd fetch read as cached — the very state that hangs the turn.
            # The artifact fastembed loads is an ONNX graph; looking for it directly is
            # layout-agnostic (flat or `snapshots/<rev>/`) and is the honest expression
            # of "the weights are here", which `refs/` alone is not.
            if any(entry.rglob("*.onnx")):
                return True
        return False

    if backend == "model2vec":
        # Standard HF hub layout: <hub>/models--<org>--<name>/snapshots/<rev>/...
        hub = _hf_hub_cache()
        repo = hub / f"models--{model_name.replace('/', '--')}"
        snapshots = repo / "snapshots"
        if not snapshots.is_dir():
            return False
        # A snapshot dir that exists but holds nothing is an interrupted fetch, which
        # is NOT cached — the state that produced techne#708's hang in the first place.
        return any(any(rev.iterdir()) for rev in snapshots.iterdir() if rev.is_dir())

    return None


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
