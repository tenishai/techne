"""SyncAdapter and SourceAdapter protocols and registry for team backends."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable


@dataclass
class OutboxEntry:
    id: int
    target: str
    data: dict
    content_hash: str | None


@dataclass
class PushResult:
    """Result of pushing a batch of outbox entries.

    Contract: `synced` is the count of entries successfully pushed,
    counted from the front of the input batch in order.

    Adapters MUST push entries in the order received and MUST NOT
    report `synced` greater than the number of consecutive successful
    pushes from the start. Partial success in the middle of a batch
    should be reported as the count of leading successes; the rest
    should be retried on the next flush.

    Example: batch of 5 entries; adapter pushes 0, 1, 2 successfully,
    then fails on 3; report synced=3 (NOT synced=4 even if entry 4
    would have succeeded).
    """

    synced: int = 0
    failed: int = 0
    errors: list[str] | None = None


@dataclass
class KnowledgeEntry:
    content: str
    type: str
    tags: str
    confidence: str
    source: str
    target: str = ""


@dataclass
class SyncQuery:
    query: str = ""
    tags: list[str] | None = None
    limit: int = 50


@runtime_checkable
class SyncAdapter(Protocol):
    def push(self, entries: list[OutboxEntry]) -> PushResult:
        """Push queued entries to the team backend."""
        ...

    def pull(self, query: SyncQuery) -> list[KnowledgeEntry]:
        """Pull relevant knowledge from the team backend."""
        ...

    def health(self) -> bool:
        """Fast probe — is the backend reachable?"""
        ...


@runtime_checkable
class SourceAdapter(Protocol):
    # ---------------------------------------------------------------------------
    # SourceAdapter Protocol — the ingest-side counterpart to SyncAdapter.
    # Unlike SyncAdapter (push/pull/health for team sync), a SourceAdapter emits
    # into the techne database (observations, or — for CCMemorySource — the
    # curated_memory drain).
    #
    # CONVENTION (not a structural member): adapters MAY declare a class attribute
    #   provides: ClassVar[frozenset[str]]   # {'curated'} | {'raw'} | both
    # intended to be read via getattr(a, 'provides', frozenset()) by a consumer
    # that needs the routing. NOTHING READS IT TODAY: it is declared once
    # (sync/cc_memory.py) and asserted in tests, and #107 — the reconciler this
    # comment was written for — is CLOSED. Stated in the present tense here until
    # 2026-08-29, which is why the arch card inherited the same false claim; the
    # CONVENTION above is the load-bearing half and it is unaffected.
    # Deliberately NOT annotated on this Protocol, because the
    # runtime_checkable non-method-member check enforces a declared data member in
    # isinstance() on EVERY Python this project supports (measured 2026-08-29 on
    # 3.9 through 3.14; this comment previously said "3.12+", which is wrong and
    # understates the blast radius — see techne#818), which would break adapters
    # that don't declare it (e.g. the isinstance check in test_jsonl_transcript).
    # ---------------------------------------------------------------------------

    def initial_walk(self) -> int:
        """Scan source; emit observations for everything not yet indexed.

        Returns count of observations emitted.
        """
        ...

    def start_watch(self) -> None:
        """Begin continuous watch. Non-blocking — returns immediately."""
        ...

    def stop_watch(self) -> None:
        """Stop watch and flush any pending debounced events."""
        ...

    def health(self) -> bool:
        """Fast probe — is the source reachable?"""
        ...


def load_adapter(name: str, *, allow_unlisted: bool = False, **kwargs) -> SyncAdapter:
    """Load a sync adapter by name from entry points or builtins.

    Unset kwargs (``None`` or empty string — `SyncConfig` defaults `url` and
    `path` to ``""``) are dropped so adapters that don't take optional config
    (e.g. ``NoopAdapter``) aren't forced to accept ``url``/``path``.

    DEFAULT-DENY ON UNLISTED ADAPTERS (techne#389), and the shape of that issue
    changed once the code was audited rather than assumed. #389 asked for a flag
    that refuses to POST to non-local destinations. THERE IS NO SUCH POST:
    measured 2026-08-17, the package contains ZERO network-capable imports — no
    ``requests``, ``httpx``, ``aiohttp``, ``urllib``, ``http``, ``socket``. Both
    built-in adapters (``noop``, ``filesystem``) are local. The one outbound path
    is ``litellm`` in ``core/thread_orchestrator.py``, already governed by
    ``EGRESS_KEYS`` (``model``, ``reflect.model``, ``conflict.model``,
    ``llm_api_base``, ``llm_api_key``), and ``github_issues`` is INGEST via the
    ``gh`` CLI.

    So the real hole is not a URL, it is THIS FUNCTION: it will import any class
    registered under the ``techne.sync_adapters`` entry-point group and hand it
    every outbox entry — full session and memory content — with no gate. A remote
    shipper does not have to be written by us to exist.

    WHAT THIS BUYS, STATED HONESTLY. Within a single OS user it is not a wall:
    an agent that can rewrite ``config.yaml`` can also rewrite the venv this code
    is imported from (``security/config_trust.py`` says the same about itself).
    What it buys is that an unlisted adapter can no longer load SILENTLY — it must
    be named twice, once as ``sync.adapter`` and once as
    ``sync.allow_unlisted_adapter``, and under a locked operator layer the
    operator controls both, since ``sync`` is a whole-block ``EGRESS_KEYS`` entry.
    A silent key-flip becomes a fail-closed and visible one. Say that, never "no
    traffic".

    Blast radius when this landed: ZERO. The only registered entry points on this
    machine were techne's own ``filesystem`` and ``noop``, both built-in.
    """
    # Drop unset config so minimal adapters can keep a no-arg __init__.
    kwargs = {k: v for k, v in kwargs.items() if v not in (None, "")}

    # Check built-in adapters first
    builtins = {
        "noop": "techne.sync.noop:NoopAdapter",
        "filesystem": "techne.sync.filesystem:FilesystemAdapter",
    }

    if name in builtins:
        module_path, class_name = builtins[name].rsplit(":", 1)
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls(**kwargs)

    # Check entry points. REFUSED BY DEFAULT — see the docstring. The check sits
    # BEFORE `ep.load()` on purpose: `load()` imports the module, and import-time
    # side effects are code execution. Deferring the gate until after the class is
    # in hand would make the refusal cosmetic.
    eps = entry_points(group="techne.sync_adapters")
    for ep in eps:
        if ep.name == name:
            if not allow_unlisted:
                raise ValueError(
                    f"Sync adapter '{name}' is not one of techne's built-ins "
                    f"({', '.join(sorted(builtins))}) and unlisted adapters are refused "
                    "by default (techne#389). A sync adapter receives every outbox "
                    "entry — full session and memory content — so loading arbitrary "
                    "registered code is an egress decision, not a plugin lookup.\n"
                    "To allow it deliberately, set `sync.allow_unlisted_adapter: true`. "
                    "Under a locked operator layer that key is the operator's, not the "
                    "agent's, because `sync` is governed as a whole block."
                )
            cls = ep.load()
            return cls(**kwargs)

    raise ValueError(f"Unknown sync adapter: '{name}'. Available: {list(builtins.keys())}")
