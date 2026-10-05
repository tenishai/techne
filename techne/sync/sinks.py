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
from dataclasses import dataclass, field
from importlib.metadata import entry_points
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


#: Entry-point group an installed package uses to contribute sinks (techne#1172).
#: Each entry point resolves to a zero-arg callable returning an `OutboxSink` or a
#: list of them.
SINK_ENTRY_POINT_GROUP = "techne.outbox_sinks"


@dataclass
class SinkDiscovery:
    """What one `discover_sinks` pass did, by ENTRY-POINT name.

    loaded  — allowed and registered (with the sink names each one registered)
    refused — present but not allowed; never imported
    failed  — allowed, but loading or calling it raised; reason kept for doctor
    """

    loaded: dict[str, list[str]] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


#: Entry points this process already loaded, with the sink names each registered.
#: Calling a factory twice builds new handler objects, which `register_sink` treats
#: as a conflict, so a loaded entry point is not re-run while its sinks are still in
#: the registry.
_LOADED_ENTRY_POINTS: dict[str, list[str]] = {}


def discover_sinks(config) -> SinkDiscovery:
    """Register sinks contributed by installed packages, gated like adapters.

    REFUSED BY DEFAULT (techne#1172, mirroring techne#389 in
    `sync.adapter.load_adapter`). A sink receives outbox payloads, so loading one is
    an egress decision. An entry point is loaded only when its name is in
    ``sync.allowed_sinks`` or ``sync.allow_unlisted_sinks`` is set, and the check
    sits BEFORE ``ep.load()``, because importing the module is already code
    execution. The allow-list is keyed on entry-point names for that reason: a
    sink's own target name is only known after it is loaded.

    Never raises: a factory that fails is recorded in ``failed`` and the rest still
    load, so one broken package cannot stop the built-in lanes from draining.

    FACTORIES MUST BE CHEAP AND SIDE-EFFECT FREE (no network): `techne doctor`, the
    SessionStart banner and `techne_status` all run this, so a slow factory sits in
    those hot paths. And a sink already registered in a long-lived process (the MCP
    server) stays deliverable there after its name leaves `sync.allowed_sinks`, until
    restart; the next process honours the change.
    """
    allowed = set(config.sync.allowed_sinks)
    allow_all = bool(config.sync.allow_unlisted_sinks)
    result = SinkDiscovery()
    for ep in entry_points(group=SINK_ENTRY_POINT_GROUP):
        if not (allow_all or ep.name in allowed):
            result.refused.append(ep.name)
            continue
        names = _LOADED_ENTRY_POINTS.get(ep.name)
        if names is not None and all(get_sink(n) is not None for n in names):
            result.loaded[ep.name] = names
            continue
        try:
            made = ep.load()()
            made_sinks = list(made) if isinstance(made, (list, tuple)) else [made]
            bad = [m for m in made_sinks if not isinstance(m, OutboxSink)]
            if bad:
                raise TypeError(f"returned {type(bad[0]).__name__}, not OutboxSink")
            for sink in made_sinks:
                register_sink(sink)
        except Exception as exc:  # noqa: BLE001 — recorded and reported, never fatal
            result.failed[ep.name] = f"{type(exc).__name__}: {exc}"
            logger.warning("outbox sink entry point %r failed to load: %s", ep.name, exc)
            continue
        names = [m.name for m in made_sinks]
        _LOADED_ENTRY_POINTS[ep.name] = names
        result.loaded[ep.name] = names
    if result.refused:
        logger.debug(
            "refused outbox sink entry points (not in sync.allowed_sinks): %s", result.refused
        )
    return result


def ensure_sinks_registered(config) -> SinkDiscovery:
    """Register every sink a delivery or prune may need: the built-in Archivist
    lanes, then installed packages' sinks (techne#1172).

    THE ONE CALL every site that drains, prunes or lists sinks makes. Before this,
    each site called `archivist.ensure_registered_for` alone, so a package's sink
    existed only in a process that happened to import that package, and its rows
    stayed pending or, worse, were marked dead by `prune-orphans`.
    """
    from techne.core.archivist import ensure_registered_for

    ensure_registered_for(config)
    return discover_sinks(config)
