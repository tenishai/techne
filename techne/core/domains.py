"""Address the four knowledge domains by name (techne#552, the connection seam).

WHY THIS EXISTS, IN THE OPERATOR'S WORDS: "I want a user that starts using techne to
refer to the archivist, librarian, codex, and architect, so that they start to
understand these functions — then when they want to start using liminal and cabal,
they can continue to refer to things in that manner, but the infrastructure gets
larger. A local librarian might become a persona in cabal, and that might become a
deployed service later, but their interface to it looks the same."

So the four names are the DURABLE VOCABULARY. techne is where they are learned.

THE LADDER ALREADY DID THE RETRIEVAL; WHAT WAS MISSING WAS ADDRESS. `techne_recall`
escalates across all four rungs and reports each one's binding — the seam has worked
for months. But a caller could only ask the LADDER a question, never a named domain:
there was no librarian tool at all (`test_librarian_flagged_not_mcp_exposed` asserted
its absence), and the architect answered to `pattern_search`, the mechanism rather
than the function. Users learned `techne_*`, which is the one vocabulary that does
NOT survive the move to cabal.

RECALL IS UNCHANGED AND REMAINS THE DEFAULT. Escalating across four rungs is the
right answer to "what do we know about X"; these calls are for when you already know
WHICH function you want. `test_the_recall_ladder_is_unchanged` guards that.

EVERY ANSWER CARRIES ITS BINDING, which is the whole point rather than a nicety. The
same call says `local` today, and would say `store` — or, once cabal owns the domain,
whatever that resolves to — without the caller changing. The binding is the part that
grows; the call is the part that does not.
"""

from __future__ import annotations

from typing import Any

#: The four domains, in the recall ladder's order so both surfaces read the same way
#: round. Names are the vocabulary; do not rename without reading the module docstring.
DOMAIN_NAMES = ("archivist", "librarian", "architect", "codex")


def _binding_of(config, domain: str):
    """Resolve the binding BEFORE the read, never after.

    `architect_db()` and its siblings CREATE their store on access, so computing the
    binding afterwards reported a missing named store as `store`/`readable` — the read
    had brought it into existence. The answer would then claim a store the operator
    never had. Caught by a test asserting an unreadable domain gives a REASON.
    """
    from techne.core.bindings import domain_binding

    return domain_binding(config, domain)


def _envelope(domain: str, config, rows: list[dict], report=None, **extra: Any) -> dict:
    """One shape for all four, including the binding.

    Shared deliberately: four domains answering in four shapes would put the burden of
    knowing which one you got back on the caller — the exact thing the seam removes.
    """
    if report is None:
        report = _binding_of(config, domain)
    out = {
        "domain": domain,
        "binding": report.binding,
        "state": report.state,
        "target": str(report.target) if report.target else None,
        "count": len(rows),
        "results": rows,
    }
    if not report.ok:
        # A domain that could not be read answers with its REASON, not an empty list.
        # An empty result and an unreadable store are different facts, and collapsing
        # them is what the ladder's `unconfigured`/`empty` split exists to prevent.
        out["notice"] = report.detail
    out.update(extra)
    return out


def librarian_search(config, query: str, limit: int = 20) -> dict:
    """What is written down — documents, transcripts and cards in librarian.db.

    THE ROLE IS "WHAT IS WRITTEN DOWN", NOT "WHICH FILES". That phrasing is the
    ladder's own (rung 4) and it is chosen to survive growth: today the corpus is the
    operator's ingested documents; a cabal Librarian with a fetcher would add external
    docs to the same question without changing it.
    """
    from techne.core.documents import search_documents
    from techne.core.librarian import librarian_db

    report = _binding_of(config, "librarian")
    if not report.ok:
        # DO NOT READ AN UNREADABLE DOMAIN. Beyond being pointless, the accessor would
        # CREATE the store and the next call would report it healthy.
        return _envelope("librarian", config, [], report=report)
    rows = search_documents(librarian_db(config), query, limit=limit)
    return _envelope("librarian", config, rows, report=report)


def architect_search(config, query: str, limit: int = 20) -> dict:
    """Architectural patterns — the "what is worth stealing" corpus in architect.db."""
    from techne.core.architect import architect_db
    from techne.core.patterns import search_patterns

    report = _binding_of(config, "architect")
    if not report.ok:
        # DO NOT READ AN UNREADABLE DOMAIN. Beyond being pointless, the accessor would
        # CREATE the store and the next call would report it healthy.
        return _envelope("architect", config, [], report=report)
    rows = search_patterns(architect_db(config), query, limit=limit)
    return _envelope("architect", config, rows, report=report)


def codex_search(config, query: str, limit: int = 20, repo: str | None = None) -> dict:
    """Symbols in the active repo's code graph. db-per-repo, so the binding is a
    property of WHERE YOU ARE — which is why the envelope's target matters here."""
    import os

    from techne.core.codegraph import search_symbols
    from techne.core.codex import resolve_repo_db_for_query

    db = resolve_repo_db_for_query(config, repo or os.getcwd())
    rows = [] if db is None else search_symbols(db, query, limit=limit)
    return _envelope("codex", config, rows)


def archivist_search(config, query: str, limit: int = 20) -> dict:
    """What the team knows.

    ASKS THE RESOLVER, NEVER RE-DERIVES (techne#552). `archivist_binding` is the single
    answer to what the archivist is bound to, and this is not allowed to become the
    eighth place with an opinion. `local` means it resolves to this instance's own
    knowledge — not a copy, not a second read — which is why the local branch searches
    `config.db_path` rather than opening anything else.
    """
    from techne.core.archivist import _resolve_archivist_path, archivist_binding
    from techne.core.search import search
    from techne.core.tiers import ARCHIVIST_ABSENT, ARCHIVIST_LOCAL

    binding = archivist_binding(config)
    if binding == ARCHIVIST_ABSENT:
        return _envelope("archivist", config, [])
    if binding == ARCHIVIST_LOCAL:
        rows = search(config.db_path, query, table="knowledge", limit=limit)
        return _envelope("archivist", config, rows)
    path = _resolve_archivist_path(config)
    rows = search(path, query, table="knowledge", limit=limit) if path.is_file() else []
    return _envelope("archivist", config, rows)


#: Name -> callable, so a caller can dispatch on the vocabulary itself.
SEARCH = {
    "archivist": archivist_search,
    "librarian": librarian_search,
    "architect": architect_search,
    "codex": codex_search,
}
