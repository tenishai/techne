"""SyncAdapter protocol and registry for team backends."""

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


def load_adapter(name: str, **kwargs) -> SyncAdapter:
    """Load a sync adapter by name from entry points or builtins.

    Unset kwargs (``None`` or empty string — `SyncConfig` defaults `url` and
    `path` to ``""``) are dropped so adapters that don't take optional config
    (e.g. ``NoopAdapter``) aren't forced to accept ``url``/``path``.
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

    # Check entry points
    eps = entry_points(group="techne.sync_adapters")
    for ep in eps:
        if ep.name == name:
            cls = ep.load()
            return cls(**kwargs)

    raise ValueError(f"Unknown sync adapter: '{name}'. Available: {list(builtins.keys())}")
