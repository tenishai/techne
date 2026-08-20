"""The recall ladder, stated (techne#552, spec knowledge#1085 S1-S5).

WHY THIS MODULE EXISTS, AND WHY IT IS INTERFACE RATHER THAN MACHINERY.

All four rungs already existed and were already attributed before this file did:
local memory, the local archivist, remote archivists, and the librarian. What was
never done is ASSEMBLING them into a stated ladder — so the design taught nobody.
An agent calling `techne_recall` got one flat list and no way to learn that a team
tier exists, let alone that it was empty.

The comparative read on #552 found both sibling repos had already solved the same
problem the same way. liminal's `Target` protocol: "an in-process handler becomes
ONE KIND of target alongside HttpTarget" — transport is a field, not a call shape.
cabal's `DispatchRouter`: the caller never chooses local vs remote. Neither makes
the caller name the binding. techne did, four different ways. This module is the
missing protocol: a named, ordered ladder whose rungs stay visible whether or not
the binding behind any one of them is interesting.

THE TEACHING SURFACE IS THE POINT (S1). The rungs are reported on EVERY call, even
when two of them resolve to the same table and even when three are empty, because
the model is learned by seeing the shape on every call — not by reading docs. This
is the same move as liminal prefixing tool names with `{target}.`.

TIMING. Open-sourcing freezes a response shape in a way it never freezes a storage
binding: strangers write against the shape. Two measured facts about what stating it
costs, rather than a claim that it is free (an earlier draft said "costs nothing" and
"the same stores are read the same number of times"; Prudence counted `sqlite3.connect`
per db across one recall and falsified BOTH, in opposite directions):

  * ONE MORE READ PER RECALL, in every arm — `has_direct_match` in `_with_ladder` is a
    new MATCH that did not exist before. Measured local.db 4->5 (ABSENT), 6->7 (STORE).
  * ONE FEWER STORE, in the LOCAL arm — S2 stops opening archivist.db at all, so the
    set of stores read is strictly SMALLER, not the same. Measured 6 + archivist.db x1
    -> 5, the pop moving that read off the federation path entirely.

No rows are duplicated and no existing row moves, which is what "additive" claims and
all this shape freezing is about. The cost is one index probe.
See knowledge#1267 (go-public paused) and #692.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The librarian rung is an ESCALATION, not a fifth store (S4). Tiers 1-3 answer
#: "what have we learned"; tier 4 answers "what is written down". Keeping it in
#: the same ladder but marked `escalation` is what lets S5 tell "we have never
#: worked on this, but here is the reference" apart from "we know nothing".
ESCALATION_TIER = 4


@dataclass(frozen=True, slots=True)
class Rung:
    """One tier of the ladder, as a stated thing rather than an implicit one."""

    number: int
    name: str
    #: What a caller learns from THIS rung, in the caller's terms. Not what the
    #: code does — the question the rung answers.
    answers: str
    #: True for the librarian rung (S4): reference material, not experience.
    escalation: bool = False


#: The ladder, in order. Order is from knowledge#1085 and is not arbitrary:
#: 1-3 are the same question against widening scopes of experience, which is what
#: makes cross-tier contradiction detection meaningful (S3). 4 changes the question.
LADDER: tuple[Rung, ...] = (
    Rung(1, "memory", "what this agent has learned here"),
    Rung(2, "archivist:local", "what the team knows, on this machine"),
    Rung(3, "archivist:remote", "what the team knows, elsewhere"),
    Rung(4, "librarian", "what is written down", escalation=True),
)

#: Rung states. `unconfigured` and `empty` are DELIBERATELY DISTINCT: "there is no
#: remote archivist" and "the remote archivist has nothing on this" are different
#: facts about the world, and flattening them is precisely the S5 failure.
STATE_HIT = "hit"
STATE_EMPTY = "empty"
STATE_UNCONFIGURED = "unconfigured"
STATE_UNAVAILABLE = "unavailable"

#: How tier 2 is bound, as a THREE-valued thing. It was a bool, and the missing
#: third value was the default install: `standalone: True` means no archivist exists
#: at all, which is neither "local mirror" nor "some other store".
ARCHIVIST_ABSENT = "absent"
ARCHIVIST_LOCAL = "local"
ARCHIVIST_STORE = "store"

#: S5's three distinguishable outcomes, plus the ordinary one.
OUTCOME_FOUND = "found"
OUTCOME_DOCUMENTED_ONLY = "documented-not-experienced"
OUTCOME_NOTHING = "nothing-known"
#: A FOURTH outcome the spec did not anticipate, added because MEASUREMENT forced it.
#: knowledge#1085 assumed "nothing in tiers 1-3" would present as zero rows. It never
#: does. `search()` is hybrid (techne#401) and a nearest-neighbour query returns k rows
#: however distant, so a topic nobody has touched still fills the page. Two candidate
#: signals were tried and both were measured DEAD before this one was written:
#:
#:   * DISTANCE cannot separate them. The nonsense query "zzzqqxx" produced a NEARER
#:     top neighbour (0.961) than a real query about work done that same day (0.974).
#:     Any threshold would have been a number invented to make the feature look right.
#:   * `match_source` cannot either. `vec_first` fusion fills the page from the vector
#:     channel before appending FTS hits: "tier ladder recall archivist" returns 10 FTS
#:     rows on its own yet 10/10 `vec` through the hybrid path. It measures fusion.
#:
#: What works is asking directly — see `search.has_direct_match`. Note it is asked of
#: the KNOWLEDGE layer, not `all`: tiers 1-3 answer "what have we LEARNED", and with
#: capture_bash on, `all` matches the operator's own probe commands within seconds of
#: typing them. That is not a hypothetical — it silently defeated this very check
#: during development ("zzzqqxx" matched 6 observations, all of them the test itself).
OUTCOME_LOOSE_ONLY = "loose-match-only"

_OUTCOME_SUMMARY = {
    OUTCOME_FOUND: "Prior experience found.",
    OUTCOME_LOOSE_ONLY: (
        "No exact match on this topic — nothing in memory contains all of these terms. "
        "The rows below matched on individual words only. Treat them as leads, not as "
        "prior work on this subject, and prefer saying so over narrating them as recall."
    ),
    OUTCOME_DOCUMENTED_ONLY: (
        "No prior experience on this — nobody has worked on it. There IS reference "
        "material in the library; treat it as documentation, not as something we did."
    ),
    OUTCOME_NOTHING: (
        "No prior experience and no documentation. This is new ground — say so rather "
        "than reconstructing an answer that will read as recalled."
    ),
}


@dataclass(slots=True)
class RungReport:
    """A rung's outcome for one recall. Rendered into the response verbatim."""

    rung: Rung
    state: str
    binding: str
    results: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tier": self.rung.number,
            "name": self.rung.name,
            "answers": self.rung.answers,
            "state": self.state,
            "binding": self.binding,
            "results": self.results,
        }
        if self.rung.escalation:
            out["escalation"] = True
        if self.detail:
            out["detail"] = self.detail
        return out


def classify_outcome(reports: list[RungReport], *, direct: bool) -> dict[str, str]:
    """S5 — make the three outcomes distinguishable.

    Richard's "institutional knowledge we may or may not have", made concrete:

      * something in 1-3          -> we have experience
      * nothing in 1-3, hit in 4  -> we have NEVER worked on this, here is the reference
      * nothing in 1-4            -> we know nothing about this

    The middle case is the one today's flat list destroys, and it is the one most
    worth telling apart: an agent that cannot distinguish "documented" from "done"
    will narrate a library snippet as if it were our own prior work.
    """
    experience = sum(r.results for r in reports if not r.rung.escalation)
    documented = sum(r.results for r in reports if r.rung.escalation)
    if experience and direct:
        code = OUTCOME_FOUND
    elif experience:
        code = OUTCOME_LOOSE_ONLY
    elif documented:
        code = OUTCOME_DOCUMENTED_ONLY
    else:
        code = OUTCOME_NOTHING
    return {"code": code, "summary": _OUTCOME_SUMMARY[code]}


def tier_of(row: dict, archivist_name: str) -> int:
    """Which rung produced this row.

    Reads the tags the existing read paths ALREADY write — `origin` from
    `_fold_in_documents`, `_store` from the federation fan-out. That is the whole
    reason S1 is interface rather than machinery: the attribution was there, it
    was simply never assembled into a ladder.

    The `_store`-absent case is the unfederated path, whose rows are local by
    construction and carry no tag at all.
    """
    if row.get("origin") == "library":
        return 4
    store = row.get("_store")
    if store is None or store == "local":
        return 1
    if store == archivist_name:
        return 2
    return 3


def build_reports(
    *,
    rows: list[dict],
    connections: dict,
    archivist_name: str,
    archivist_binding: str,
    librarian_available: bool,
) -> list[RungReport]:
    """Report every rung, hit or not — the S1 requirement in one function.

    AN EMPTY TIER IS REPORTED AS AN EMPTY TIER, never as an absent one. That
    distinction is load-bearing for S5 and is the single easiest thing to lose:
    the natural implementation filters to rungs that produced rows, and a caller
    then cannot tell "the team knows nothing about this" from "there is no team".
    """
    counts: dict[int, int] = {r.number: 0 for r in LADDER}
    for row in rows:
        n = tier_of(row, archivist_name)
        counts[n] += 1

    # Which remotes are real remotes? The local archivist auto-registers itself as
    # a connection (mcp_server._effective_connections), so its presence says
    # nothing about whether a REMOTE archivist exists.
    remotes = [name for name in connections if name != archivist_name]

    reports: list[RungReport] = []
    for rung in LADDER:
        n = counts[rung.number]
        if rung.number == 1:
            reports.append(RungReport(rung, STATE_HIT if n else STATE_EMPTY, "local:knowledge", n))
        elif rung.number == 2:
            # S2 — at n=1 this rung RESOLVES TO THE SAME TABLE as tier 1 and reads
            # no second file. It stays visible anyway: the interface is the
            # deliverable, and an agent that only ever reads team knowledge through
            # an archivist call keeps that habit when a real team store appears
            # behind it. Naming the sharing is honest; hiding the rung is not.
            if archivist_binding == ARCHIVIST_ABSENT:
                # THE DEFAULT INSTALL LANDS HERE (`standalone: True`), and a boolean
                # could not say so. Reporting `empty` against `store:archivist` was a
                # POSITIVE claim that a team store was consulted and had nothing —
                # strictly worse than the flat list this replaces, because an omission
                # is silent while a false `empty` recruits the reader. Tier 3 always
                # got this right two branches down; tier 2 could not, because the
                # parameter had no way to express "there is no archivist".
                reports.append(
                    RungReport(
                        rung,
                        STATE_UNCONFIGURED,
                        "none",
                        0,
                        detail="no archivist is configured (standalone)",
                    )
                )
            elif archivist_binding == ARCHIVIST_LOCAL:
                reports.append(
                    RungReport(
                        rung,
                        STATE_HIT if counts[1] else STATE_EMPTY,
                        "local:knowledge",
                        counts[1],
                        detail=(
                            "bound to the same local knowledge as tier 1 (n=1) — one store "
                            "read once, reported as two rungs; the rows below are tier 1's"
                        ),
                    )
                )
            else:
                reports.append(
                    RungReport(rung, STATE_HIT if n else STATE_EMPTY, f"store:{archivist_name}", n)
                )
        elif rung.number == 3:
            if not remotes:
                reports.append(
                    RungReport(
                        rung,
                        STATE_UNCONFIGURED,
                        "none",
                        0,
                        detail="no remote archivist is configured",
                    )
                )
            else:
                reports.append(
                    RungReport(rung, STATE_HIT if n else STATE_EMPTY, ",".join(sorted(remotes)), n)
                )
        else:
            reports.append(
                RungReport(
                    rung,
                    STATE_HIT if n else (STATE_EMPTY if librarian_available else STATE_UNAVAILABLE),
                    "local:librarian.db" if librarian_available else "none",
                    n,
                )
            )
    return reports


def annotate_rows(rows: list[dict], archivist_name: str) -> list[dict]:
    """Stamp each row with the rung it came from (S1, per-row half).

    The ladder block says which rungs exist; this says which rung each ROW is
    from, so a caller reading a single result still knows whether it is our
    experience or somebody's document.
    """
    by_number = {r.number: r for r in LADDER}
    for row in rows:
        n = tier_of(row, archivist_name)
        # `recall_tier`, NOT `tier`: observation rows already carry a `tier` column
        # (hot|warm|cold, database.py) which search surfaces verbatim, so writing an
        # int 1-4 there replaced an existing key AND changed its meaning. That is the
        # #579 one-key-two-meanings shape — documented at length in the very card this
        # change re-blesses, where the chosen fix was to REMOVE the ambiguous name so a
        # stale consumer fails loudly rather than compare against a plausible wrong
        # value. Colliding with it again, silently, in a shape about to be frozen, is
        # the one thing that card asks the next person not to do.
        row["recall_tier"] = n
        row["recall_tier_name"] = by_number[n].name
    return rows
