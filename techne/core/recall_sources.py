"""Recall sources: the ladder's rungs as named, ordered, pluggable things (techne#1170).

Recall has always read four built-in sources: this instance's memory, the local and
remote archivists (one combined search plus federation), and the librarian. This
module adds two things the operator decides:

* PLUGGED-IN SOURCES from installed packages, through the `techne.recall_sources`
  entry-point group, refused by default. Recall output goes straight into the
  model's context, so admitting a source is an ingress trust decision, gated before
  import as the sync adapters are (techne#389).
* THE ORDER, `recall.order`: which sources are consulted, in what sequence, which
  are fallbacks (consulted only when the sources before them returned fewer than
  `recall.fill` rows), and therefore whose rows come first in the result. Every
  source is a LAZY provider here, the built-in ones included, so a source that is
  excluded or a fallback that is not reached is never searched (Prudence, PR #1174:
  an earlier version searched everything first and only then filtered, so a
  skipped remote archivist still cost a fan-out and took memory's seats).

`mcp_server._techne_recall` asks `ordering_active` first; with no `recall.order`
and no source loaded it takes the pre-#1170 path, whose response is unchanged.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from techne.config import Config, RecallOrderEntry

log = logging.getLogger(__name__)

SOURCE_ENTRY_POINT_GROUP = "techne.recall_sources"

#: The built-in rungs, by the names `recall.order` uses for them.
MEMORY = "memory"
ARCHIVIST_LOCAL = "archivist:local"
ARCHIVIST_REMOTE = "archivist:remote"
LIBRARIAN = "librarian"
BUILTIN_SOURCES: tuple[str, ...] = (MEMORY, ARCHIVIST_LOCAL, ARCHIVIST_REMOTE, LIBRARIAN)
#: The sync adapter's read-back, when the adapter can pull (techne#1171). Not a
#: built-in RUNG (it numbers from 5 like a plugged-in source) but a reserved name.
TEAM = "team"
_RESERVED = (*BUILTIN_SOURCES, TEAM)

#: Source states beyond tiers' hit/empty/unconfigured/unavailable.
STATE_SKIPPED = "skipped"
STATE_EXCLUDED = "excluded"


@runtime_checkable
class RecallSource(Protocol):
    """A read-only source recall can consult. Rows are plain dicts; `content` is
    what the model reads, and `source_url` is the provenance a reader can follow.

    Set ``escalation = True`` on a source of REFERENCE material (documents, a docs
    index): its rows then count as "documented", not as experience, in the recall
    outcome, exactly as the built-in librarian's do.
    """

    name: str

    def search(self, topic: str, limit: int) -> list[dict]: ...

    def health(self) -> bool: ...


@dataclass
class SourceDiscovery:
    """One `discover_sources` pass. `loaded` is keyed by SOURCE name; `refused` and
    `failed` by entry-point name, since a refused one was never imported."""

    loaded: dict[str, RecallSource] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


#: Entry points this process already loaded. A source object is reusable, so
#: loading once per process is enough; the GATE still runs on every call, so a
#: source removed from the allow-list stops being used at the next recall.
_LOADED: dict[str, list[RecallSource]] = {}
#: Entry points whose factory failed in this process: reported, not re-run on
#: every recall (a raising factory would otherwise run per call). A restart retries.
_FAILED: dict[str, str] = {}


def discover_sources(config: Config, *, scan_refused: bool = True) -> SourceDiscovery:
    """Load the allowed `techne.recall_sources` entry points. Never raises.

    FACTORIES MUST BE CHEAP AND SIDE-EFFECT FREE (no network): loading runs in the
    caller's thread, outside `source_timeout_s`, on the first recall of each process,
    in `techne doctor` and in the SessionStart banner. Only `search` and `health`
    are bounded.

    ``scan_refused=False`` (the recall path) skips the entry-point scan entirely when
    nothing can be allowed, since everything found would be refused anyway: the scan
    walks every installed distribution, 9-27 ms measured (Prudence, PR #1174).
    Doctor keeps the default and lists what it refused.
    """
    allowed = set(config.recall.allowed_sources)
    allow_all = bool(config.recall.allow_unlisted_sources)
    found = SourceDiscovery()
    eps = (
        entry_points(group=SOURCE_ENTRY_POINT_GROUP) if scan_refused or allowed or allow_all else []
    )
    for ep in eps:
        if not (allow_all or ep.name in allowed):
            found.refused.append(ep.name)
            continue
        if ep.name in _FAILED:
            found.failed[ep.name] = _FAILED[ep.name]
            continue
        made = _LOADED.get(ep.name)
        if made is None:
            try:
                out = ep.load()()
                made = list(out) if isinstance(out, (list, tuple)) else [out]
                bad = [m for m in made if not isinstance(m, RecallSource)]
                if bad:
                    raise TypeError(f"returned {type(bad[0]).__name__}, not a RecallSource")
                clash = [m.name for m in made if m.name in _RESERVED]
                if clash:
                    raise ValueError(f"source name {clash[0]!r} is reserved by techne")
            except Exception as exc:  # noqa: BLE001 — recorded and reported, never fatal
                found.failed[ep.name] = _FAILED[ep.name] = f"{type(exc).__name__}: {exc}"
                log.warning("recall source entry point %r failed to load: %s", ep.name, exc)
                continue
            _LOADED[ep.name] = made
        for src in made:
            found.loaded[src.name] = src
    team = team_source(config)
    if team is not None:
        found.loaded[TEAM] = team
    return found


class AdapterSource:
    """The configured sync adapter's `pull`, as the recall source `team` (techne#1171).

    `SyncAdapter.pull` was defined and never called, so an adapter could push to a
    team backend while recall never read back from it. Wrapping it as a source puts
    that read-back under #1170's order, fallback and timeout rules rather than
    adding a second recall mechanism beside them.

    THE ADAPTER IS BUILT LAZILY, inside the first `health()`, which `query_source`
    runs in its bounded call. A sync adapter's constructor never had to be cheap (it
    ran in hooks and the CLI), so constructing it on the recall thread let a slow
    one block the MCP server; a constructor that raises now reaches the caller as
    an ordinary `unavailable` with its real text, logged once, and is retried on the
    next recall (Prudence, PR #1175).

    Rows carry `source` (the `KnowledgeEntry.source` label) and `target`, not a
    `source_url`: a team backend's entry names who wrote it, not a page to follow.
    """

    name = TEAM
    escalation = False

    def __init__(self, cls: type, kwargs: dict) -> None:
        self._cls = cls
        self._kwargs = kwargs
        self._adapter = None

    def _get(self):
        if self._adapter is None:
            self._adapter = self._cls(**self._kwargs)
        return self._adapter

    def search(self, topic: str, limit: int) -> list[dict]:
        from techne.sync.adapter import SyncQuery

        rows = []
        for e in self._get().pull(SyncQuery(query=topic, limit=limit)) or []:
            rows.append(
                {
                    "type": "knowledge",
                    "content": e.content,
                    "knowledge_type": e.type,
                    "tags": e.tags,
                    "confidence": e.confidence,
                    "source": e.source,
                    "target": e.target,
                }
            )
        return rows

    def health(self) -> bool:
        return bool(self._get().health())


#: (adapter, url, path, allow_unlisted) -> the `team` source, or None when the
#: adapter's class does not declare `supports_pull`. Process-lived: the MCP server
#: loads its Config once, so a config change needs a restart either way.
_TEAM: dict[tuple, AdapterSource | None] = {}
#: Same key -> why the adapter CLASS could not be loaded (refused by the #389 gate,
#: unknown name, import error). Kept for the process, like `_FAILED`; a restart retries.
_TEAM_ERROR: dict[tuple, str] = {}


def team_source(config: Config) -> AdapterSource | None:
    """The `team` source, if the configured sync adapter's CLASS declares
    ``supports_pull = True``. The class comes from `adapter_class`, the same #389
    gate `load_adapter` applies, checked before import; the instance is built later,
    inside the bounded call. techne's own `noop` and `filesystem` do not declare it,
    so a default install gains no source."""
    key = (
        config.sync.adapter,
        config.sync.url,
        config.sync.path,
        bool(config.sync.allow_unlisted_adapter),
    )
    if key in _TEAM:
        return _TEAM[key]
    src: AdapterSource | None = None
    try:
        from techne.sync.adapter import adapter_class

        cls = adapter_class(config.sync.adapter, allow_unlisted=key[3])
        if getattr(cls, "supports_pull", False) is True:
            kwargs = {k: v for k, v in {"url": key[1], "path": key[2]}.items() if v}
            src = AdapterSource(cls, kwargs)
    except Exception as exc:  # noqa: BLE001 — reported through the tier report and doctor
        _TEAM_ERROR[key] = f"{type(exc).__name__}: {exc}"
        _degraded(TEAM, f"the sync adapter could not be loaded: {_TEAM_ERROR[key]}")
    _TEAM[key] = src
    return src


def ordering_active(config: Config, found: SourceDiscovery) -> bool:
    """Does this recall need the ordered path at all? No order and no loaded source
    means the pre-#1170 path, untouched."""
    return bool(config.recall.order) or bool(found.loaded)


def effective_order(config: Config, found: SourceDiscovery) -> list[RecallOrderEntry]:
    """`recall.order`, or the built-in ladder followed by every loaded source."""
    if config.recall.order:
        return list(config.recall.order)
    return [RecallOrderEntry(n) for n in BUILTIN_SOURCES] + [
        RecallOrderEntry(n) for n in sorted(found.loaded)
    ]


#: A shared pool, so a recall does not pay thread start-up per source. A call that
#: overruns its timeout keeps its worker until it returns, so each source may have
#: AT MOST ONE call in flight (`_IN_FLIGHT`): without that, one source that hangs on
#: every call took a new worker per recall and, after four recalls, every other
#: source queued behind it and timed out too, blamed for the hang (Prudence, PR
#: #1174). A timed-out call that never started is cancelled.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="recall-src")
_IN_FLIGHT: dict[str, concurrent.futures.Future] = {}


def query_source(src: RecallSource, topic: str, limit: int, timeout_s: float) -> tuple[list, str]:
    """Call one plugged-in source, bounded. Returns (rows, detail); detail is empty
    on success and says why otherwise, in which case rows is []."""

    def _call() -> list[dict]:
        if not src.health():
            raise ConnectionError("health() returned False")
        return list(src.search(topic, limit) or [])[:limit]

    prev = _IN_FLIGHT.get(src.name)
    if prev is not None and not prev.done():
        return [], _degraded(src.name, "its previous call is still running")
    future = _POOL.submit(_call)
    _IN_FLIGHT[src.name] = future
    try:
        rows = future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return [], _degraded(src.name, f"timed out after {timeout_s:g}s")
    except Exception as exc:  # noqa: BLE001 — a source must never break recall
        return [], _degraded(src.name, f"{type(exc).__name__}: {exc}")
    return [_normalise(src, r) for r in rows if isinstance(r, dict)], ""


#: Sources already logged as unavailable in this process: once, not on every
#: recall. The tier report still says so on every call.
_WARNED: set[str] = set()


def _degraded(name: str, why: str) -> str:
    if name not in _WARNED:
        _WARNED.add(name)
        log.warning("recall source %r unavailable, continuing without it: %s", name, why)
    return why


def _normalise(src: RecallSource, row: dict) -> dict:
    """Stamp provenance and redact. Rows from an installed package are data from
    outside this store, so they say so (`external`), name their source, and pass
    the same credential redaction techne applies to what it captures."""
    from techne.core.redaction import redact_credentials

    out = dict(row)
    for key in ("content", "summary", "title"):
        if isinstance(out.get(key), str):
            out[key] = redact_credentials(out[key])
    out["origin"] = src.name
    out["recall_source"] = src.name
    out["external"] = True
    return out


@dataclass
class SourceOutcome:
    """What one source contributed to one recall, for the ladder report."""

    state: str
    results: int = 0
    detail: str = ""


def assemble(
    config: Config,
    topic: str,
    found: SourceDiscovery,
    providers: dict[str, Callable[[], list[dict]]],
    settle: Callable[[list[dict]], list[dict]] | None = None,
) -> tuple[list[dict], dict[str, SourceOutcome]]:
    """Walk the effective order; return the rows in source order, and per-source
    outcomes for every source the order names.

    ``providers`` maps each built-in rung to a callable that searches it. EVERY
    source is called only when the walk reaches it and it is not skipped: a
    fallback entry runs only when the rows gathered BEFORE it, as ``settle`` counts
    them when given, number fewer than ``recall.fill``, and a source the order
    leaves out never runs at all.

    ``settle`` turns the rows gathered so far into what the caller would receive
    (cross-store dedup). A fallback is decided on THAT count, not the raw one:
    remote copies of local rows are exactly what federation sync produces, and
    counting them skipped a fallback while returning fewer than `fill` rows
    (techne#1177, Prudence PR #1174 r3).
    """
    rows: list[dict] = []
    outcomes: dict[str, SourceOutcome] = {}
    for entry in effective_order(config, found):
        name = entry.name
        if name in outcomes:
            continue  # named twice: the first position wins
        if entry.fallback:
            have = len(settle(rows)) if settle else len(rows)
            if have >= config.recall.fill:
                outcomes[name] = SourceOutcome(
                    STATE_SKIPPED,
                    detail=f"fallback, not searched: the sources before it returned "
                    f"{have} row(s), fill is {config.recall.fill}",
                )
                continue
        if name in providers:
            got = providers[name]()
            if name != LIBRARIAN:
                for row in got:
                    row.setdefault("origin", "memory")
        elif name in found.loaded:
            got, detail = query_source(
                found.loaded[name],
                topic,
                config.recall.source_limit,
                config.recall.source_timeout_s,
            )
            if detail:
                outcomes[name] = SourceOutcome("unavailable", detail=detail)
                continue
        else:
            outcomes[name] = SourceOutcome("unavailable", detail=_why_missing(name, found, config))
            continue
        rows.extend(got)
        outcomes[name] = SourceOutcome("hit" if got else "empty", len(got))

    for name in BUILTIN_SOURCES:
        if name not in outcomes:
            outcomes[name] = SourceOutcome(
                STATE_EXCLUDED, detail="not in recall.order, not searched"
            )
    return rows, outcomes


def _why_missing(name: str, found: SourceDiscovery, config: Config) -> str:
    if name == TEAM:
        err = next(iter(_TEAM_ERROR.values()), None) if _TEAM_ERROR else None
        if err:
            return f"the sync adapter could not be loaded ({err}); a restart retries"
        return "the configured sync adapter does not declare supports_pull = True"
    if not config.recall.allowed_sources and not config.recall.allow_unlisted_sources:
        return "not allowed: recall.allowed_sources is empty"
    if name in found.refused:
        return "refused: not in recall.allowed_sources"
    if name in found.failed:
        return f"failed to load: {found.failed[name]}"
    return "no installed source by this name (refused entry points are listed by entry-point name)"


def describe(found: SourceDiscovery) -> dict[str, Any]:
    """For doctor: what loaded, what was refused, what failed."""
    return {
        "loaded": sorted(found.loaded),
        "refused": sorted(found.refused),
        "failed": dict(sorted(found.failed.items())),
    }
