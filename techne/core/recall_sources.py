"""Recall sources: the ladder's rungs as named, ordered, pluggable things (techne#1170).

Recall has always read four built-in sources: this instance's memory, the local and
remote archivists (one combined search plus federation), and the librarian. This
module adds two things the operator decides:

* PLUGGED-IN SOURCES from installed packages, through the `techne.recall_sources`
  entry-point group, refused by default. Recall output goes straight into the
  model's context, so admitting a source is an ingress trust decision, gated before
  import exactly as `sync.sinks.discover_sinks` gates sinks (techne#1172).
* THE ORDER, `recall.order`: which sources are consulted, in what sequence, which
  are fallbacks (consulted only when the sources before them returned fewer than
  `recall.fill` rows), and therefore whose rows come first in the result.

With no `recall.order` and no allowed source loaded, recall does not come here at
all and stays byte-identical to the pre-#1170 path. That is checked by
`ordering_active`, which `mcp_server` asks before taking this route.
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


def discover_sources(config: Config) -> SourceDiscovery:
    """Load the allowed `techne.recall_sources` entry points. Never raises."""
    allowed = set(config.recall.allowed_sources)
    allow_all = bool(config.recall.allow_unlisted_sources)
    found = SourceDiscovery()
    for ep in entry_points(group=SOURCE_ENTRY_POINT_GROUP):
        if not (allow_all or ep.name in allowed):
            found.refused.append(ep.name)
            continue
        made = _LOADED.get(ep.name)
        if made is None:
            try:
                out = ep.load()()
                made = list(out) if isinstance(out, (list, tuple)) else [out]
                bad = [m for m in made if not isinstance(m, RecallSource)]
                if bad:
                    raise TypeError(f"returned {type(bad[0]).__name__}, not a RecallSource")
                clash = [m.name for m in made if m.name in BUILTIN_SOURCES]
                if clash:
                    raise ValueError(f"source name {clash[0]!r} is a built-in rung")
            except Exception as exc:  # noqa: BLE001 — recorded and reported, never fatal
                found.failed[ep.name] = f"{type(exc).__name__}: {exc}"
                log.warning("recall source entry point %r failed to load: %s", ep.name, exc)
                continue
            _LOADED[ep.name] = made
        for src in made:
            found.loaded[src.name] = src
    return found


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


#: One shared pool, so a recall does not pay thread start-up per source. A call that
#: overruns its timeout keeps its worker until it returns; the pool bounds how many
#: such calls can pile up.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="recall-src")


def query_source(src: RecallSource, topic: str, limit: int, timeout_s: float) -> tuple[list, str]:
    """Call one plugged-in source, bounded. Returns (rows, detail); detail is empty
    on success and says why otherwise, in which case rows is []."""

    def _call() -> list[dict]:
        if not src.health():
            raise ConnectionError("health() returned False")
        return list(src.search(topic, limit) or [])[:limit]

    try:
        rows = _POOL.submit(_call).result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return [], f"timed out after {timeout_s:g}s"
    except Exception as exc:  # noqa: BLE001 — a source must never break recall
        return [], f"{type(exc).__name__}: {exc}"
    return [_normalise(src, r) for r in rows if isinstance(r, dict)], ""


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
    core_rows: list[dict],
    rung_of: Callable[[dict], str],
    librarian: Callable[[], list[dict]],
) -> tuple[list[dict], dict[str, SourceOutcome]]:
    """Walk the effective order; return the rows in source order, and per-source
    outcomes for every source the order names.

    ``core_rows`` are this recall's memory and archivist rows, already searched and
    federated together; ``rung_of`` says which built-in rung each belongs to. They
    are placed by source, in order, keeping their order within a source. A fallback
    entry is consulted only when the rows gathered BEFORE it number fewer than
    ``recall.fill``. For the experience rungs that decides inclusion (they were
    searched together); the librarian and plugged-in sources are not called at all.
    """
    grouped: dict[str, list[dict]] = {n: [] for n in (MEMORY, ARCHIVIST_LOCAL, ARCHIVIST_REMOTE)}
    for row in core_rows:
        row.setdefault("origin", "memory")
        grouped.setdefault(rung_of(row), []).append(row)

    rows: list[dict] = []
    outcomes: dict[str, SourceOutcome] = {}
    for entry in effective_order(config, found):
        name = entry.name
        if name in outcomes:
            continue  # named twice: the first position wins
        if entry.fallback and len(rows) >= config.recall.fill:
            outcomes[name] = SourceOutcome(
                STATE_SKIPPED,
                detail=f"fallback: the sources before it returned {len(rows)} row(s), "
                f"fill is {config.recall.fill}",
            )
            continue
        detail = ""
        if name in grouped:
            got = grouped[name]
        elif name == LIBRARIAN:
            got = librarian()
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
            outcomes[name] = SourceOutcome("unavailable", detail=_why_missing(name, found))
            continue
        rows.extend(got)
        outcomes[name] = SourceOutcome("hit" if got else "empty", len(got), detail)

    for name in BUILTIN_SOURCES:
        if name not in outcomes:
            outcomes[name] = SourceOutcome(STATE_EXCLUDED, detail="not in recall.order")
    return rows, outcomes


def _why_missing(name: str, found: SourceDiscovery) -> str:
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
