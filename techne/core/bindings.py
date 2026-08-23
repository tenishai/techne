"""What each knowledge domain is bound to — one question, four domains (techne#552 part 4).

Richard, scoping the connection seam:

    I want to review techne and make sure connecting to the librarian, archivist,
    architect, and codex in techne had the same feel as connecting to liminal with a
    persona, and cabal with a local agent and with a remote agent.

That is a question about CONNECTING, and this module answers it. `techne_recall` already
fans out over memory / archivist / librarian and reports the ladder; what had no uniform
answer was "what is domain X bound to, and can I reach it" — asked identically of all
four.

=== WHY BINDING AND NOT THE RECALL LADDER, WHICH IS WHAT #552 PART 4 LITERALLY SAYS ===

#552 part 4 reads "bring patterns and codex through the same call", i.e. add them as
rungs 5 and 6. TWO THINGS ARGUE AGAINST THAT, one from the siblings and one measured.

THE SIBLINGS ANSWER "SAME FEEL" ON THE BINDING AXIS, NOT THE QUERY AXIS. Read rather
than recalled, 2026-08-22:

  * liminal `src/liminal/target/base.py` — a `Target` is "a named backend with a
    transport". `name` is the namespace, `list_tools` returns tools UNPREFIXED and the
    Multiplexer adds `{name}.`, and in-process vs HTTP vs stdio are KINDS OF TARGET.
    The call shape never changes; the transport is a field.
  * cabal `src/cabal/handlers/` already ships `patterns.py`, `code.py`, `documents.py`
    and `archivist.py` SIDE BY SIDE — these exact domains, as peers, each exposing
    `{domain}.{verb}`.

Neither fuses its domains into one query. Both make the CONNECTION uniform and let the
caller name the domain. Fusing all four into `recall` would be a third answer, not the
same one.

AND THE MEASURED HALF — retrieval quality today would make those rungs noise:

  * PATTERNS. `search_patterns` is FTS over name+body with an OR fallback, and its own
    docstring says the corpus is queried in problem language while patterns are titled
    metaphorically. Measured: "archivist binding" -> "Late-binding task definitions";
    "guarded read-only SQL over a shared store" -> "Dialect-agnostic filter-DSL
    compiler". Plausible-looking, mostly irrelevant. techne#550 (a one-line `problem`
    field) is the fix and is not landed.
  * CODEX. The DURABLE finding is dilution: across six realistic topics, 45% of hits
    were TEST symbols, because test names are long English sentences and FTS matches
    them better than implementation identifiers. That is structural — it follows from
    what FTS ranks, not from when the index last ran.

    Staleness was also measured (2026-08-21: 166 files fresh / 83 stale, and
    `retired_producer_targets`, merged the same day, returned zero hits) but is a
    SNAPSHOT and was already out of date within a day; a reindex moves it. Cited here
    as the state on that date, not as a standing property. (Prudence, #717.)

A rung that returns noise on EVERY recall is worse than no rung, and the response shape
is the thing open-sourcing freezes (knowledge#1085). So: bind uniformly now, and revisit
the rungs when #550 and codex freshness make them answerable. Recorded as a decision
rather than an omission.

=== THE SHAPE, AND ITS PRECEDENT IN THIS FILE'S NEIGHBOUR ===

`checks._VERSION_REGISTRY` already enumerates these same domains and already carries the
lesson this module needs: A UNIFORM QUESTION MAY NOT HAVE A UNIFORM MECHANISM. Reading
`PRAGMA user_version` off every store would report three false problems on a healthy
machine, because core uses a `schema_version` TABLE while domain stores use the pragma
and librarian has no stamp at all.

Binding is the same: "what is this bound to" is one question, but the archivist answers
it from `archivist_binding`, architect and librarian from the `stores` registry, and
codex from a per-repo index that may simply not exist. So the registry below declares
the MECHANISM per domain and the CALLER never has to know which one it got.

THE VOCABULARY IS REUSED, NOT INVENTED. `absent` / `local` / `store` come from
`core.tiers`, where they already describe the archivist rung and are already tested. A
second vocabulary for the same three states is how two subsystems come to disagree about
what "local" means.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from techne.core.tiers import (
    ARCHIVIST_ABSENT,
    ARCHIVIST_LOCAL,
    ARCHIVIST_STORE,
    STATE_UNAVAILABLE,
)

log = logging.getLogger(__name__)

#: The three states, re-exported under domain-neutral names so callers outside the
#: archivist do not import an `ARCHIVIST_*` constant to describe the codex.
BOUND_ABSENT = ARCHIVIST_ABSENT
BOUND_LOCAL = ARCHIVIST_LOCAL
BOUND_STORE = ARCHIVIST_STORE
#: FOUR states, not three-plus-a-bool. `reachable: bool` collapsed three different
#: facts — "absent by design", "bound and readable", "bound and unreadable" — and the
#: CLI then reported `3 domain(s) not reachable` on a HEALTHY fresh install, because
#: `absent` is unreachable by definition and librarian/architect dbs are created lazily
#: on first write. An operator would have chased four non-problems.
#:
#: The module claimed to reuse the ladder's vocabulary, borrowed three constants, then
#: RE-INVENTED the fourth case the ladder already names — `tiers.STATE_UNAVAILABLE`,
#: which exists there for precisely this and whose comment says flattening these states
#: "is precisely the S5 failure". Borrowed now, so the claim is true rather than
#: three-quarters true. (Prudence, #717.)
BOUND_UNAVAILABLE = STATE_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class BindingReport:
    """What one domain is bound to, in the caller's terms."""

    domain: str
    #: absent | local | store | unavailable — ONE axis, four states.
    #: `absent` is HEALTHY and expected for codex (unindexed repo) and for
    #: `archivist: none`; `unavailable` is the fault state: configured and unreadable.
    binding: str
    #: Where it resolves, when that is meaningful. None when absent or unresolvable.
    target: Path | None
    #: One operator-facing line. For `unavailable` it carries the REASON VERBATIM —
    #: see `all_bindings`, where discarding it hid the techne#346 guard's message.
    detail: str

    @property
    def ok(self) -> bool:
        """True unless this domain is configured and unreadable.

        `absent` counts as ok: an unindexed repo has no codex and `archivist: none` is
        a deliberate choice, and flagging either trains an operator to ignore the
        report — the same reasoning the liveness rail applies to DORMANT.
        """
        return self.binding != BOUND_UNAVAILABLE

    def as_dict(self) -> dict:
        return {
            "domain": self.domain,
            "binding": self.binding,
            "target": str(self.target) if self.target else None,
            "ok": self.ok,
            "detail": self.detail,
        }


def _archivist(config) -> BindingReport:
    """Delegates to the archivist's own resolver — it is the one domain whose binding
    is a config DECISION rather than a path lookup, and techne#552 spent a PR making
    that single-sourced. Re-deriving it here would be the seventh private opinion."""
    from techne.core.archivist import _resolve_archivist_path, archivist_binding

    binding = archivist_binding(config)
    if binding == BOUND_ABSENT:
        return BindingReport("archivist", binding, None, "no archivist (archivist: none)")
    if binding == BOUND_LOCAL:
        return BindingReport(
            "archivist",
            binding,
            Path(config.db_path),
            "resolves to this instance's own knowledge — no second store",
        )
    # `_resolve_archivist_path` RAISES on the techne#346 misconfiguration
    # (archivist_db_path pointing at the personal db). Left to propagate to
    # `all_bindings`, which turns it into an `unavailable` row CARRYING THE MESSAGE —
    # this is the one tool an operator would run to diagnose exactly that.
    path = _resolve_archivist_path(config)
    if not path.exists():
        return BindingReport(
            "archivist",
            BOUND_UNAVAILABLE,
            path,
            f"team store named but not present at {path}",
        )
    return BindingReport("archivist", binding, path, f"separate team store at {path}")


def _registry_domain(domain: str, label: str) -> Callable[[object], BindingReport]:
    """A domain whose binding is the `stores` registry: architect and librarian.

    `store` when an operator named a dsn, `local` at the default
    `<data_dir>/<domain>.db`. Routed through `store_path`, never path-derived —
    techne#409 makes that a standing rule after path-derivation broke domain routing.
    """

    def resolve(config) -> BindingReport:
        from techne.core.stores import store_path

        spec = config.stores.get(domain)
        path = store_path(config, domain)
        named = bool(spec and spec.dsn)
        if named and not path.exists():
            # An operator NAMED a store and it is not there. That is the fault case.
            return BindingReport(
                domain, BOUND_UNAVAILABLE, path, f"{label} store named but missing: {path}"
            )
        if named:
            return BindingReport(
                domain, BOUND_STORE, path, f"{label} at an operator-named store ({path})"
            )
        # A DEFAULT db that does not exist yet is NOT a fault: librarian and architect
        # create theirs lazily on first write, so a fresh install has neither file and
        # nothing is wrong. Reporting that as unreachable is what made the CLI say
        # "3 domain(s) not reachable" on a healthy box. (Prudence, #717.)
        state = "" if path.exists() else " (created on first write)"
        return BindingReport(
            domain, BOUND_LOCAL, path, f"{label} at the default {path.name}{state}"
        )

    return resolve


def _codex(config) -> BindingReport:
    """Codex is db-per-repo (techne#209), so its binding is a property of WHERE YOU ARE.

    `absent` is the common, healthy state here in a way it is not for the other three:
    an unindexed repo has no codex, which is a fact about that repo rather than a fault.
    Reported rather than resolved to a default, because a codex bound to some other
    repo's index would answer confidently about the wrong code.
    """
    import os

    from techne.core.codex import resolve_repo_db_for_query

    try:
        path = resolve_repo_db_for_query(config, os.getcwd())
    except Exception:  # noqa: BLE001 — an unresolvable repo is a report, not a crash
        log.warning("codex: could not resolve a repo db for the cwd", exc_info=True)
        path = None

    if path is None:
        return BindingReport(
            "codex",
            BOUND_ABSENT,
            None,
            "no index for the active repo — `techne codex index <repo>`",
        )
    # ALWAYS `local` — there is no store binding for a per-repo index, and reporting one
    # was a fabricated distinction. `config.stores["codex"].dsn` feeds only
    # `store_path(config, "codex")`, the LEGACY GLOBAL db that `migrate_legacy_codex_db`
    # moves away from; the live read path — `resolve_repo_db_for_query` -> `repo_db_path`
    # — derives from `data_dir` plus a sha of the repo root and NEVER consults
    # `config.stores`. So the old code reported `store` while pointing at a path the
    # operator's dsn had no bearing on. (Prudence, #717.)
    #
    # `resolve_repo_db_for_query` returns a path only if it EXISTS, so there is no
    # unavailable case here: either an index is there, or the branch above says absent.
    return BindingReport("codex", BOUND_LOCAL, Path(path), f"index at {path}")


#: The four knowledge domains, and how each answers the one question. Order matches the
#: recall ladder's, so the two surfaces read the same way round.
DOMAINS: tuple[tuple[str, Callable], ...] = (
    ("archivist", _archivist),
    ("librarian", _registry_domain("librarian", "documents")),
    ("architect", _registry_domain("architect", "patterns")),
    ("codex", _codex),
)


def domain_binding(config, domain: str) -> BindingReport:
    """What ONE domain is bound to. Raises KeyError for an unknown domain."""
    for name, resolver in DOMAINS:
        if name == domain:
            return resolver(config)
    known = ", ".join(n for n, _ in DOMAINS)
    raise KeyError(f"unknown domain: {domain!r} (known: {known})")


def all_bindings(config) -> list[BindingReport]:
    """Every domain, same shape, in ladder order — the "same feel" surface.

    NEVER RAISES for one bad domain. A resolver that throws would otherwise take the
    whole report down, and the report's value is that it is complete: an operator
    comparing four domains needs four rows, one of which may say it could not be read.
    """
    out: list[BindingReport] = []
    for name, resolver in DOMAINS:
        try:
            out.append(resolver(config))
        except Exception as e:  # noqa: BLE001
            # UNAVAILABLE, NOT ABSENT — a resolver that failed and a domain that is
            # deliberately unconfigured are different facts, and reporting both as
            # `absent` left a --json consumer unable to tell them apart at all.
            #
            # AND THE MESSAGE VERBATIM. Discarding `str(e)` for the exception TYPE meant
            # the techne#346 guard's deliberately loud refusal — "archivist_db_path must
            # not be the personal techne db" — arrived as "could not resolve: ValueError"
            # in the one tool an operator would run to diagnose that misconfiguration.
            # (Prudence, #717.)
            # DEBUG, NOT WARNING-WITH-TRACEBACK. `techne bindings` is a diagnostic
            # whose product IS the four rows; at warning level a config error printed a
            # full stack trace to stderr directly above a row already carrying the same
            # message verbatim, burying the readable answer under the raw one. The
            # traceback still exists for a genuine resolver bug, one level down.
            log.debug("bindings: %s resolver failed", name, exc_info=True)
            out.append(BindingReport(name, BOUND_UNAVAILABLE, None, f"{type(e).__name__}: {e}"))
    return out
