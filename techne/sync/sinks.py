"""OutboxSink registry — typed, in-process, per-target delivery handlers.

Sinks are a parallel concept to SyncAdapter: where SyncAdapter pushes the
entire outbox to a team backend, each OutboxSink owns one target string and
delivers only rows addressed to it.

Registry is module-global and process-scoped. Cabal startup wires sinks once;
everything else queries the registry. Tests use ``use_registry`` to isolate.
"""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

logger = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    """Returned by sink handlers; drives outbox status transition.

    ok=True  → row marked synced
    ok=False → row stays pending; error stored for diagnostics; retried on next flush
    Handler exceptions are caught by the caller and treated as ok=False with
    the exception repr as error — handlers don't need try/except wrappers.
    """

    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class OutboxSink:
    """A registered delivery target for outbox rows.

    name          — must match the outbox.target column value
    payload_schema — a dataclass type; used to validate payload at enqueue time
    handler       — called with the raw payload dict; must return DeliveryResult
    description   — human-readable; shown by ``techne sinks list``
    """

    name: str
    payload_schema: type
    handler: Callable[[dict], DeliveryResult]
    description: str = ""


class UnknownSinkError(Exception):
    """Raised by enqueue when no sink is registered for the requested target."""


class InvalidPayloadError(Exception):
    """Raised by enqueue when the payload fails the sink's schema validation."""

    def __init__(self, target: str, reason: str) -> None:
        self.target = target
        self.reason = reason
        super().__init__(f"Invalid payload for target={target!r}: {reason}")


_REGISTRY: dict[str, OutboxSink] = {}


def register_sink(sink: OutboxSink) -> None:
    """Register a sink.

    Idempotent on identical re-registration (same name, same schema, same
    handler object). Raises ValueError on conflict (same name, different
    schema or handler).
    """
    existing = _REGISTRY.get(sink.name)
    if existing is not None:
        if existing.payload_schema is sink.payload_schema and existing.handler is sink.handler:
            logger.debug("sink %r already registered (identical) — skipping", sink.name)
            return
        raise ValueError(
            f"Sink {sink.name!r} already registered with a different schema or handler. "
            f"Unregister first if you need to replace it."
        )
    _REGISTRY[sink.name] = sink
    logger.debug("registered sink %r", sink.name)


def unregister_sink(name: str) -> None:
    """Remove a sink by name. Pending rows for this target stay queued."""
    _REGISTRY.pop(name, None)
    logger.debug("unregistered sink %r", name)


def list_sinks() -> list[OutboxSink]:
    """Return all registered sinks, sorted by name."""
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def get_sink(name: str) -> OutboxSink | None:
    """Return the sink for *name*, or None if not registered."""
    return _REGISTRY.get(name)


@contextmanager
def use_registry(registry: dict[str, OutboxSink]) -> Generator[None, None, None]:
    """Context manager that swaps the global registry for test isolation.

    Usage::

        with use_registry({sink.name: sink}):
            enqueue(...)
    """
    global _REGISTRY
    saved = _REGISTRY
    _REGISTRY = registry
    try:
        yield
    finally:
        _REGISTRY = saved
