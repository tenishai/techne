"""Subsystem liveness checks — "is techne actually working?", answerable (techne#468).

WHY THIS EXISTS. On 2026-08-02 six mechanisms were found built, proven by hand,
and never wired — reflect disabled, federation never engaged, an architecture-card
gate invoked by nothing for 13 days, a card mirror that was hand-fed while
"mirrored on merge" was aspirational, two injection rails dead for three days, and
a review agent that exited 0 around a review that never happened (techne#942).

The common shape: **a mechanism proven manually at time T is indistinguishable
from an automated one on the day you demonstrate it.** The difference only appears
at T+n, and nothing announces it. Silence is what failure looks like, and silence
is also what success looks like.

So this module does not ask "is it configured?" — configuration was correct in
every one of those six cases. It asks **when did it last actually run, and what
did it do.**

THE VERDICT TAXONOMY IS THE PRODUCT. A boolean cannot distinguish "ran and found
nothing" from "never ran" from "cannot be observed", and those need different
responses (nothing / fix the wiring / add telemetry). In particular
:attr:`Verdict.UNOBSERVABLE` is a FAILING state, per Richard's rule: *"if we built
a rail, shouldn't it have something to tell us?"* — telemetry is a build
requirement, so a rail that cannot report is unfinished, not merely quiet.

TWO CONSTRAINTS THIS MODULE HOLDS TO:

1. **Checks report; they never repair.** Auto-heal is how the interpreter got
   poisoned three times (techne#458) — ``register_hooks`` re-derived and baked a
   broken value unasked. A checker that mutates state can break what it checks.
   Each result instead carries a ``remedy`` string: the command to run.
2. **Checks must be cheap.** :func:`run_checks` is called from ``collect_health``,
   which runs inside the SessionStart hook under a wall-clock budget. Checks are
   queries against tables that already exist — never a probe that executes the
   rail it is checking.

   A check that genuinely cannot be cheap declares it with :func:`expensive` and
   is skipped when ``run_checks(cheap_only=True)``. That marker exists because
   the constraint was previously held by nothing but this paragraph: #487 added a
   check spawning six git subprocesses, and its docstring asserted twice that it
   stayed out of the banner while ``collect_health`` ran the whole registry
   unfiltered. Prose cannot enforce a budget; the parameter can.

A check NEVER raises: a probe that fails reports :attr:`Verdict.UNKNOWN` with the
error as evidence. Silently dropping a failed check would reproduce the exact bug
this module exists to catch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from techne.config import CLAUDE_CODE


class Verdict(str, Enum):
    """Outcome of a single check.

    DECLARATION ORDER IS NOT SEVERITY ORDER — ``worst()`` ranks by ``_SEVERITY``
    below, and UNKNOWN deliberately sits between DORMANT and STALE rather than
    last. This docstring previously said "worst-last so ``max()`` finds the
    headline", which described the declaration and was read as describing the
    ranking; the architecture card then repeated it. Two documents agreeing does
    not make either true. (#469 review 9.)
    """

    LIVE = "live"
    #: Off by configuration. Most default-off flags are correctly off — per
    #: techne#941 the signal is not "off", it is "off despite being recorded as
    #: proven/intended", so a DORMANT result must name the key that disables it.
    DORMANT = "dormant"
    #: Enabled and wired, but has not run within its declared interval.
    STALE = "stale"
    #: Enabled and wired, but emits no signal — liveness is UNDECIDABLE. Failing
    #: by decision (techne#468): a rail that cannot report is not finished.
    UNOBSERVABLE = "unobservable"
    #: Enabled but structurally cannot fire (e.g. a hook matched to a tool that
    #: does not exist in this client).
    BROKEN = "broken"
    #: The probe itself failed. Never silently green.
    UNKNOWN = "unknown"


#: Severity order for picking the banner headline. DORMANT sorts with LIVE
#: because deliberate dormancy is a healthy state; it is surfaced in the full
#: table, not escalated to the banner.
#:
#: UNKNOWN MUST OUTRANK LIVE/DORMANT — this is the module's founding invariant
#: expressed as a number, and it was held by an untested constant. Set it to 0
#: and a probe that RAISED ties with a healthy rail: the banner renders ✅ and
#: the failure is never named, which is precisely the "confirming the absence of
#: evidence" bug this subsystem exists to remove. It sits BELOW stale/
#: unobservable/broken on purpose: a check that could not run is less
#: informative than one that ran and reported trouble, so a real finding should
#: win the headline over a broken probe. Pinned in test_checks.py. (#469 review 9.)
_SEVERITY = {
    Verdict.LIVE: 0,
    Verdict.DORMANT: 0,
    Verdict.UNKNOWN: 1,
    Verdict.STALE: 2,
    Verdict.UNOBSERVABLE: 3,
    Verdict.BROKEN: 4,
}


class Kind(str, Enum):
    """How a subsystem fails — the probe shape and remedy differ per kind.

    Derived from the 2026-08-03 audit, where every finding fell into one of four
    shapes rather than a single "broken" bucket:

    * ``FRESHNESS`` — a source moved and its mirror did not (arch cards, the
      librarian card mirror, git-anchored knowledge, the pattern graph).
    * ``TEMPORAL`` — should have run by now and did not (consolidate, dream, the
      daily snapshot).
    * ``FAIL_OPEN`` — degrades silently to a no-op when a dependency is missing
      (redaction, shell_guard, codex_grep_assist before techne#468).
    * ``COVERAGE`` — processed N of M and nothing flagged the shortfall (581 of
      758 knowledge rows embedded, while every row read as fresh).
    """

    FRESHNESS = "freshness"
    TEMPORAL = "temporal"
    FAIL_OPEN = "fail-open"
    COVERAGE = "coverage"


@dataclass(frozen=True)
class CheckResult:
    """One subsystem's answer to "are you working?"."""

    name: str
    subsystem: str
    kind: Kind
    verdict: Verdict
    #: When the subsystem last did its job. None when never, or unknowable.
    last_fired: str | None = None
    #: How long between runs before this is STALE. None = not time-based.
    expected_interval: timedelta | None = None
    #: The query/probe that produced this verdict — quotable, so a reader can
    #: re-derive the finding instead of trusting the checker (techne#941's
    #: "demand proof of absence" rule).
    evidence: str = ""
    #: One operator-facing line.
    detail: str = ""
    #: The command that fixes it. Checks never run this themselves.
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.LIVE, Verdict.DORMANT)


CheckFn = Callable[..., "CheckResult | list[CheckResult]"]
_REGISTRY: list[CheckFn] = []


def register_check(fn: CheckFn) -> CheckFn:
    """Register a subsystem check.

    Intended to be applied next to the subsystem it checks, so a new rail arrives
    with its liveness check or does not arrive. Built-ins live in this module
    because they are plain queries and the SessionStart hot path must not import
    every subsystem to discover them; a subsystem with a check that genuinely
    needs its own module can import and use this decorator.
    """
    _REGISTRY.append(fn)
    return fn


def registered_checks() -> list[CheckFn]:
    return list(_REGISTRY)


def deferred_check_names() -> list[str]:
    """Checks that ``run_checks(cheap_only=True)`` will SKIP (techne#488).

    The banner omits these, and this module's founding rule is that an absent
    check reads as "nothing wrong". The omission is bounded and deliberate (see
    :func:`run_checks`); what is removable is the SILENCE, so a caller can say
    how many checks it did not run instead of leaving the reader to infer a
    total from a number that never mentions one.

    Costs nothing: reads the registry's own markers, spawns no subprocess, and
    is the reason this is a separate function rather than a synthetic result.
    A placeholder ``UNKNOWN`` CheckResult was the obvious alternative and is
    WRONG — ``health.py`` ranks verdicts through :func:`worst`, so an UNKNOWN
    would pin a permanent ⚠️ on every banner for checks that were never run.
    Deferred-ness is not a verdict and must not be ranked as one.

    NAMES ARE FUNCTION NAMES (``check_runtime_loaded``), not the
    ``subsystem:name`` labels doctor prints (``runtime:loaded``). Those labels
    live on a ``CheckResult``, and a deferred check has produced none — that is
    what deferred means. Deriving one from the other would be a guess about a
    naming convention nothing enforces, and declaring it on the decorator would
    be a second copy free to drift from the check's own return value.
    """
    return [fn.__name__ for fn in _REGISTRY if getattr(fn, "expensive", False)]


def expensive(fn: CheckFn) -> CheckFn:
    """Mark a check as too costly for the SessionStart hot path.

    Applied UNDER ``@register_check`` so the two read together — the check is
    registered, and it declares its own cost. Marked checks still run under
    ``techne doctor``, which is an explicit invocation that can afford them.

    The bar is deliberately high: subprocesses, filesystem walks, or network. A
    slightly slower SQL query is not expensive, and marking one to be safe would
    quietly empty the banner of the checks it exists to show.
    """
    fn.expensive = True  # type: ignore[attr-defined]
    return fn


def run_checks(config, *, cheap_only: bool = False) -> list[CheckResult]:
    """Run every registered check. Never raises.

    A check that raises becomes an UNKNOWN result carrying the exception — never
    an absent one. An absent check reads as "nothing wrong", which is the failure
    mode this whole module exists to prevent.

    ``cheap_only=True`` skips checks marked :func:`expensive`, for the
    SessionStart path. THIS IS THE ONE PLACE THE MODULE DELIBERATELY OMITS A
    CHECK, so it is worth naming the tension: an absent check does read as
    "nothing wrong", and that is the very bug this module exists to catch. It is
    acceptable here only because the omission is *bounded and known* — the banner
    is a summary surface, ``techne doctor`` remains the complete one, and the
    alternative is worse. An expensive check in the hot path can blow the hook's
    wall-clock budget, and that path is fail-open: one timeout drops the ENTIRE
    banner, silently, taking every cheap check with it. Omitting one check beats
    a tail risk of omitting all of them without saying so.
    """
    results: list[CheckResult] = []
    for fn in _REGISTRY:
        if cheap_only and getattr(fn, "expensive", False):
            continue
        try:
            out = fn(config)
        except Exception as exc:  # noqa: BLE001 — a failed probe is a result
            results.append(
                CheckResult(
                    name=getattr(fn, "__name__", "unknown"),
                    subsystem="?",
                    kind=Kind.TEMPORAL,
                    verdict=Verdict.UNKNOWN,
                    evidence=f"{type(exc).__name__}: {exc}",
                    detail="check raised",
                )
            )
            continue
        if out is None:
            continue
        results.extend(out if isinstance(out, list) else [out])
    return results


def worst(results: list[CheckResult]) -> Verdict | None:
    """The headline verdict — what the banner shows. None when nothing ran."""
    if not results:
        return None
    return max((r.verdict for r in results), key=lambda v: _SEVERITY[v])


def summarize(results: list[CheckResult]) -> dict[str, int]:
    """Counts per verdict, for a one-line banner summary."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Shared probe helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts: str | None):
    """Parse the timestamp shapes SQLite/ISO produce here, or None."""
    if not ts:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _age(ts: str | None) -> timedelta | None:
    dt = _parse_ts(ts)
    return None if dt is None else datetime.now(tz=timezone.utc) - dt


def temporal_verdict(last: str | None, interval: timedelta) -> Verdict:
    """LIVE inside the interval, STALE outside it, UNOBSERVABLE when never seen.

    "Never fired" is deliberately UNOBSERVABLE rather than STALE: with no
    timestamp at all we cannot distinguish a rail that has never run from one
    whose telemetry does not exist — and conflating those is precisely the
    ambiguity techne#468 was raised to remove.
    """
    age = _age(last)
    if age is None:
        return Verdict.UNOBSERVABLE
    return Verdict.LIVE if age <= interval else Verdict.STALE


def init_remedy(config) -> str | None:
    """The ``techne init`` command line to hand this operator, or None.

    ONE BUILDER, so a new surface cannot be added without inheriting both guards.
    They were learned at THREE different sites inside a single PR, and every time
    the defect had the same shape: a remedy that is precise, confident and wrong.

    PLATFORM — settings.json registration is claude-code's mechanism. Off that
    platform "reinstall onto claude-code" is advice for a machine the operator
    does not have. It was being printed six times, one screen above a drift block
    that had just been guarded against printing it once.

    POSTURE — never a literal. A bare ``techne init`` defaults ``--posture`` to
    ``auto``, which RE-RESOLVES and can change posture as a side effect of fixing
    hook registration. A hardcoded ``--posture observe`` is worse: an operator in
    ``spool-primary`` who follows doctor's own advice is DEMOTED globally (init
    resolves the flag, rebuilds PostureConfig and SAVES it), with reflect flipped
    out of advisory — because the health check told them to.

    NOTHING TO PRESERVE IS NOT THE SAME AS A PRESERVED DEFAULT (techne#480). The
    posture guard protects a choice the operator already made. On a machine that
    has used Claude Code but has NEVER run ``techne init`` there is no config
    file, so ``config.posture.mode`` is the dataclass default ``observe`` — not a
    choice. Pinning it there does the very thing the guard exists to prevent, one
    case over: it suppresses the auto-detection that would have found
    ``spool-primary`` from the CC memory sitting on disk, and freezes a first
    install into the weakest posture. With no file, ``--posture auto`` IS the
    honest value; auto-detection is the correct behaviour on a first install and
    the only reason to pin is to protect an existing answer.

    Still ONE BUILDER: this is a condition inside it, not a second remedy string
    at a call site, so every surface keeps inheriting both guards.
    """
    if config.platform != CLAUDE_CODE:
        return None
    if config.loaded_from is None:
        # No config.yaml — posture.mode is a default, not an answer.
        #
        # RESIDUAL, deliberately not chased: this asks "does a config file
        # exist", which is a proxy for "did the operator choose a posture".
        # A hand-written config.yaml with no `posture:` stanza sets
        # loaded_from while posture.mode is still the dataclass default, so
        # the original defect survives one level in. Narrow, because save()
        # always writes a full posture stanza — any machine `techne init`
        # created is safe, and reaching it needs a hand-rolled config or one
        # predating the field. Recorded rather than fixed: the honest
        # discriminator would be "was a posture persisted", which the Config
        # does not carry today. (PR #525 review, nit 2.)
        return "techne init --platform claude-code --posture auto"
    return f"techne init --platform claude-code --posture {config.posture.mode}"


def posture_switch_command(mode: str) -> str:
    """The ``techne init`` line that CHANGES posture to ``mode``, on purpose.

    init_remedy is for repairs and must preserve posture. This is the other case:
    text telling the operator how to move posture (e.g. spool-primary to techne-only,
    techne#1023). It lives here so the hint is still spelled in one module
    (test_init_remedy_is_the_only_place_the_init_hint_is_spelled). Use it only where
    the surrounding text says the posture will change.
    """
    return f"techne init --platform claude-code --posture {mode}"


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------

#: Hooks that must report liveness, with how long between runs is acceptable.
#: Intervals differ per hook by design — a global staleness constant is
#: meaningless across a per-tool-call rail and a per-compaction one.
_HOOK_EXPECTATIONS = {
    "session_start": (timedelta(days=3), "fires once per session"),
    "session_end": (timedelta(days=3), "fires at session teardown"),
    "codex_grep_assist": (
        timedelta(days=7),
        "fires on a symbol-shaped Grep or Bash search, when enabled",
    ),
    "post_compact": (timedelta(days=7), "fires after a compaction"),
    # techne#1018. Written by post_tool_use, only on a hand-back, fetched page or
    # coord message, so it is judged on the clock like codex_grep_assist rather
    # than against sessions: plenty of sessions have none of those.
    "inbound_recall": (
        timedelta(days=7),
        "recall on hand-backs, fetched pages and coord messages, when armed",
    ),
    # INSTRUMENTED as of techne#479. These were listed here while carrying no
    # record_hook_run call at all, deliberately — so they read UNOBSERVABLE
    # rather than being absent, because leaving them out made a DEAD CAPTURE
    # RAIL invisible: deleting a month of observations produced byte-identical
    # verdicts (#469 review 3).
    #
    # INTERVALS ARE A FALLBACK HERE, NOT THE MECHANISM — and the first draft of
    # this comment claimed otherwise. post_tool_use and user_prompt_submit are
    # both in _SESSION_SCOPED, and that branch is tested BEFORE
    # temporal_verdict(last, interval), so what actually catches a dead rail is
    # the marker-vs-last-settled-session comparison, not the window below.
    # Measured: a FIVE-DAY-OLD marker on a 3-session store still reads LIVE under
    # an 18h window, because the interval is only reachable on a store with fewer
    # than two settled sessions. 18h is chosen for that thin case — tight enough
    # to be useful on a near-new store, loose enough that a quiet evening is not
    # an alert. The 7-day figure below for pre_compact/post_compact is a LIVE
    # window only, not a staleness deadline — see _EVENT_DRIVEN_NO_SIGNAL
    # (#479 review 1, revisited techne#1075).
    "post_tool_use": (timedelta(hours=18), "captures observations — techne is dead without it"),
    "user_prompt_submit": (timedelta(hours=18), "per_turn_recall / per_turn_seed"),
    "pre_compact": (timedelta(days=7), "checkpoints before compaction"),
}

#: Hooks that fire once per SESSION. Judged against session activity rather than
#: wall-clock — for these, "days since last fire" measures how recently the
#: operator worked, not whether the hook works.
_SESSION_SCOPED = {"session_start", "session_end", "post_tool_use", "user_prompt_submit"}

#: Hooks whose trigger is an EXTERNAL EVENT (a compaction) this store cannot
#: independently confirm happened — SessionStart's `source="compact"` (#906) is
#: read but never persisted, and nothing here parses transcript compaction
#: markers either. So an aged or never-set marker is idle, not stale
#: (techne#1075); see docs/arch/liveness.md for the full reasoning, and
#: `check_hook_drift` for the mis-registered case this set does not cover.
_EVENT_DRIVEN_NO_SIGNAL = {"pre_compact", "post_compact"}

#: How long a store may exist without any session before "nothing has run yet"
#: stops being a plausible explanation. Below this the store is genuinely new;
#: above it, the absence of sessions is itself the finding.
_NEW_STORE_GRACE = timedelta(days=7)


def _declared_outbox_targets(config) -> set[str]:
    """The outbox lanes this install SHOULD be using, resolved from topology.

    Declared rather than discovered, so a lane that has never enqueued a row
    still produces a result instead of vanishing from the report.

    RESOLVED FROM THE CODE THAT WRITES, NOT FROM LITERALS (#469 review 3,
    finding 2). An earlier version pinned three names grepped off a live store
    and got all three wrong:

    * bare ``"thread_summary"`` is a PRE-#51 legacy name. ``enqueue()`` now
      rejects un-namespaced targets outright, so it was a phantom reporting
      UNOBSERVABLE forever with no operator action able to clear it — and the
      140 drained rows that made it look real are terminal legacy rows migration
      v10 deliberately left alone.
    * the lane that actually exists standalone, ``sync:thread_summary``, was NOT
      declared — so when it had never enqueued it still vanished, which is the
      absence-reads-as-healthy bug the declaration was added to close, left open
      on the very lane the incident was about.
    * the ``sink:`` lanes are fed only under a STORE binding (techne#552), so on a
      default install all three declared lanes were structurally unreachable.

    ``lifecycle._thread_summary_target`` is the single source of truth for which
    egress lane is live, so this defers to it rather than restating the rule.
    """
    from techne.lifecycle import _thread_summary_target

    targets = {_thread_summary_target(config)}
    # DECLARE THE KNOWLEDGE LANE IFF SOMETHING FEEDS IT (techne#552). This asked
    # `not config.standalone`, which mirrored the OLD fork; what decides whether the
    # lane is fed is now `push_knowledge_to_archivist`, which egresses only for a STORE
    # binding. The two came apart in BOTH directions — a team box with no store named
    # declared a lane nothing writes (false red), and a lone box WITH a store named
    # fed a lane that was never declared, so it produced no result at all. That second
    # one is the absence-reads-as-healthy bug the docstring above says this declaration
    # was added to close, reopened on the very binding #552 introduces.
    from techne.core.archivist import (
        ARCHIVIST_KNOWLEDGE_TARGET,
        ARCHIVIST_STORE,
        archivist_binding,
    )

    if archivist_binding(config) == ARCHIVIST_STORE:
        targets.add(ARCHIVIST_KNOWLEDGE_TARGET)
    return targets


#: How far the newest SESSION may run past a lane's newest enqueue before the
#: producer is suspect. Measured session-to-enqueue, not wall-clock: these lanes
#: are fed at session end, so an idle operator must never look like a dead
#: producer. Generous, because not every session produces a thread summary.
_PRODUCER_SILENT_AFTER = timedelta(days=7)

#: Hooks whose rail is behind a config flag, and the flag that gates it.
#: CONFIG IS READ FIRST, MARKER SECOND — deliberately (#469 review). Because a
#: disabled rail records nothing, a rail switched OFF after use keeps its last
#: marker forever; reading the marker alone would report it healthy indefinitely.
#: Config is the durable record of "should this run at all", so the marker must
#: never be interpreted without its gate.
_HOOK_CONFIG_GATE = {
    "codex_grep_assist": ("hooks.codex_grep_assist", lambda c: c.hooks.codex_grep_assist),
    # post_tool_use is deliberately NOT gated. `hooks.post_tool_use` disables
    # CAPTURE, not the hook: main() carries on into the reminder (#207), coord
    # (#376) and codex edit (#335) rails unconditionally. Gating here would mean
    # the marker is never read, so if those rails died nothing would say so —
    # config-first would give a WRONG verdict rather than a cheap one. The hook
    # records `noop:capture-disabled` on that path instead. (#479 review 1.)
    # Two flags, either of which makes the rail live — the hook emits when
    # EITHER is on, so gating on one alone would report a working rail as
    # disabled (techne#479).
    "user_prompt_submit": (
        "hooks.per_turn_recall / hooks.per_turn_seed",
        lambda c: c.hooks.per_turn_recall or c.hooks.per_turn_seed,
    ),
    # Armed only with BOTH set (techne#1018 part 4): the flag alone leaves the
    # rail dormant for want of a floor, and that is what this reports.
    "inbound_recall": (
        "hooks.inbound_recall + hooks.inbound_recall_floor_vec_distance",
        lambda c: c.hooks.inbound_recall and c.hooks.inbound_recall_floor_vec_distance is not None,
        lambda c: (
            "disabled by config: hooks.inbound_recall is on but "
            "hooks.inbound_recall_floor_vec_distance is unset, and the rail will not run "
            "without a floor"
            if c.hooks.inbound_recall
            else "disabled by config: hooks.inbound_recall"
        ),
    ),
}


@register_check
def check_hooks(config) -> list[CheckResult]:
    """Per-hook liveness from the markers techne#468 step 1 introduced.

    Reads `hook:<name>:last_run` / `:last_error` rather than asking whether the
    hook is registered — registration was CORRECT in the 2026-07-31 incident and
    the rails were still dead (techne#942). Registration drift is a separate,
    already-existing check (`check_hook_drift`, surfaced in the banner by #457).
    """
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    results: list[CheckResult] = []
    conn = connect(config.db_path)
    try:
        # A store with no sessions may legitimately never have run a hook — but
        # ONLY IF IT IS NEW. The unbounded version of this test (#469 review,
        # finding 1) was the worst bug in this module: if session_start never
        # fires — mis-registered, wrong interpreter (techne#458, three times),
        # or the techne#942 shape — no session row is EVER written, so every
        # hook stayed DORMANT and `techne doctor` printed "All subsystems
        # confirmed live" forever. It confirmed the ABSENCE of evidence and
        # reported it as confirmation, which is the precise inversion this
        # module exists to prevent.
        #
        # Bounding it by the store's own age keeps the quiet fresh install
        # (correct, and worth keeping — a red first run teaches the operator to
        # ignore the signal) while closing the indefinite window.
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        # THE SECOND-NEWEST SESSION, not the newest (#469 review 3, finding 1).
        # session_start's ordering is: Phase 3 inserts the session row → Phase 5
        # runs collect_health → Phase 8 writes the marker. So at check time the
        # NEWEST session is the in-flight one whose marker does not exist yet,
        # and comparing against it measured the INTER-SESSION GAP rather than
        # marker lag: a 12h slack meant a once-a-day operator (finish 6pm, start
        # 9am = 15h) went STALE every single day. That is the bank-holiday alarm
        # I set out to remove, firing six times more often.
        #
        # The second-newest session is the newest one that HAS HAD ITS CHANCE to
        # write a marker, which is exactly the predicate wanted: did a session
        # that should have recorded fail to?
        srow = conn.execute(
            "SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1 OFFSET 1"
        ).fetchone()
        last_settled_session = srow[0] if srow else None
        row = conn.execute("SELECT MIN(applied_at) FROM schema_version").fetchone()
        store_age = _age(row[0] if row else None)
        store_is_new = store_age is not None and store_age < _NEW_STORE_GRACE

        for hook, (interval, note) in _HOOK_EXPECTATIONS.items():
            run = get_state(conn, f"hook:{hook}:last_run") or {}
            err = get_state(conn, f"hook:{hook}:last_error") or {}
            last = run.get("ts")
            outcome = run.get("outcome")

            gate = _HOOK_CONFIG_GATE.get(hook)
            if gate and not gate[1](config):
                # CONFIG FIRST. Without this a rail turned off after use keeps a
                # recent-looking marker and reads healthy forever. A gate with a
                # third element names its own reason (a rail that is on but
                # unarmed is not "= false").
                why = gate[2](config) if len(gate) > 2 else None
                results.append(
                    CheckResult(
                        name=f"hook:{hook}",
                        subsystem="hooks",
                        kind=Kind.TEMPORAL,
                        verdict=Verdict.DORMANT,
                        last_fired=last,
                        evidence=f"config {gate[0]}: {why}" if why else f"config {gate[0]} = false",
                        detail=why or f"disabled by config: {gate[0]}",
                    )
                )
                continue

            if err:
                verdict = Verdict.BROKEN
                detail = f"last run FAILED: {err.get('stage', '?')} — {err.get('msg', '')}"[:160]
            elif hook in _SESSION_SCOPED and last_settled_session:
                # SESSION-SCOPED HOOKS ARE JUDGED AGAINST THE LAST SESSION, NOT
                # THE CLOCK (#469 review, finding 5). For a hook that only fires
                # when a session starts, "days since last fire" measures how
                # recently the OPERATOR WORKED, not whether the hook works — a
                # long weekend turned session_start/session_end STALE and made
                # `techne doctor` exit 1 every Tuesday after a bank holiday,
                # which is the teach-them-to-ignore-it failure arriving through
                # the other door.
                #
                # The real question is: did a session that HAS HAD ITS CHANCE to
                # record fail to? `last_settled_session` is the second-newest
                # session precisely so the in-flight one — whose marker is
                # written three phases after this check runs — is excluded
                # rather than absorbed by a slack window wide enough to hide it.
                # No wall-clock term at all: an operator's absence must never
                # read as a broken hook.
                # KNOWN LIMIT, stated rather than implied (#469 review 4,
                # critical 3). For `session_start` this predicate detects a
                # PARTIAL death only — one where the session row was written
                # (Phase 3) and the marker was not. It CANNOT detect a total
                # death, because session_start is the sole creator of session
                # rows on the Claude Code path: if it never runs, there is no
                # row to compare against, no observations either (add_observation
                # does not create sessions and the FK is enforced), and the store
                # simply goes quiet.
                #
                # A quiet store is indistinguishable from an absent operator
                # using only this hook's own writes — which is precisely why the
                # wall-clock term was removed. Do not "fix" this by
                # reintroducing a clock: that trades a real gap for a false
                # positive every long weekend, which is the failure this module
                # exists to prevent.
                #
                # PARTIAL COVER ONLY, and be precise about how partial (#469
                # review 5). `check_hook_drift` catches the UNREGISTERED flavour
                # of total death — a `hooks.*` flag enabled in config with no
                # matching entry in settings.json. It does NOT catch a hook that
                # is registered but points at a dead interpreter, because the
                # comparison is deliberately COARSE: by (event, matcher)
                # presence, explicitly ignoring interpreter and launcher-path
                # differences so a re-pinned shim cannot false-positive
                # (hooks.py:867-871). That is the right trade for drift, but it
                # means the wrong-interpreter death this module's own docstring
                # cites is caught by NEITHER check. It is a known, open gap.
                marker = _parse_ts(last)
                sess = _parse_ts(last_settled_session)
                if marker is None:
                    verdict = Verdict.UNOBSERVABLE
                    detail = f"{note}; sessions exist but no marker was ever written"
                elif sess is not None and marker < sess:
                    verdict = Verdict.STALE
                    detail = (
                        f"{note}; a session started at {last_settled_session} and left no marker"
                    )
                else:
                    verdict = Verdict.LIVE
                    detail = f"{note}; last outcome: {outcome or 'unknown'}"
            elif not last and sessions == 0 and store_is_new:
                verdict = Verdict.DORMANT
                detail = "store created <7d ago, no sessions yet — nothing has run"
            elif not last and sessions == 0:
                # Old store, still no sessions: the absence IS the finding.
                verdict = Verdict.UNOBSERVABLE
                age = f"{int(store_age.days)}d" if store_age else "unknown age"
                detail = f"store is {age} old and has NEVER recorded a session"
            elif hook in _EVENT_DRIVEN_NO_SIGNAL:
                # techne#1075: no independent signal confirms whether the
                # triggering event (a compaction) happened, so an aged-out
                # marker is idle, not stale — see _EVENT_DRIVEN_NO_SIGNAL.
                marker_age = _age(last)
                if marker_age is not None and marker_age <= interval:
                    verdict = Verdict.LIVE
                    detail = f"{note}; last outcome: {outcome or 'unknown'}"
                elif last:
                    verdict = Verdict.DORMANT
                    detail = f"{note}; idle: no compaction since {last}"
                else:
                    verdict = Verdict.DORMANT
                    detail = f"{note}; idle — no compaction recorded yet"
            else:
                verdict = temporal_verdict(last, interval)
                detail = f"{note}; last outcome: {outcome or 'never recorded'}"

            results.append(
                CheckResult(
                    name=f"hook:{hook}",
                    subsystem="hooks",
                    kind=Kind.TEMPORAL,
                    verdict=verdict,
                    last_fired=last,
                    expected_interval=interval,
                    evidence=f"extension_state hook:{hook}:last_run = {run or '(absent)'}",
                    detail=detail,
                    remedy=(
                        init_remedy(config)
                        if verdict in (Verdict.UNOBSERVABLE, Verdict.BROKEN)
                        else None
                    ),
                )
            )
    finally:
        conn.close()
    return results


#: Self-maintenance rails, and how often each should produce something. These are
#: the ones found four weeks cold on 2026-08-03 with nothing reporting it.
#: (interval, note, remedy). THE REMEDY IS PER-RAIL AND NOT DERIVED FROM THE NAME
#: (techne#936). It used to be built as f"techne {rail} --help", which is a real
#: command for exactly one of these three: `techne consolidate` and `techne dream`
#: do not exist and error with "No such command". The remedy line is the one part
#: of a check an operator acts on without reading the code, so two of three rails
#: handed them a dead end at the moment the check fired -- and the working path is
#: not guessable from that error, because consolidate and dream have no CLI verb of
#: their own. They run headless through `custodian run` (needs a bound model) or
#: through the MCP emit/ingest pair, which is what a resident agent uses.
_RAIL_EXPECTATIONS = {
    "reflect": (
        timedelta(days=3),
        "distils sessions into knowledge candidates",
        "techne reflect --help",
    ),
    "consolidate": (
        timedelta(days=14),
        "dedups/merges knowledge",
        "techne custodian run --rail consolidate (bound model), or techne_consolidate_emit via MCP",
    ),
    "dream": (
        timedelta(days=14),
        "recombines knowledge into new connections",
        "techne custodian run --rail dream (bound model), or techne_dream_emit via MCP",
    ),
}


@register_check
def check_mcp_registration(config) -> CheckResult | None:
    """Is the MCP surface LAUNCHABLE? (techne#370)

    Not about whether something RAN: the MCP server is launched by the HOST, not
    by us, so there is no marker to read. What can be checked is whether the
    recorded launch command still points at something that exists.

    Kind.FRESHNESS, which is the taxonomy's "a source moved and its mirror did
    not" — the interpreter moved and the registration did not. I nearly added a
    fifth Kind before reading that docstring; the bucket was already there.

    THE FAILURE THIS EXISTS FOR IS SPLIT-BRAIN AND SILENT. The registration held a
    raw interpreter path; when that path went (a reboot clearing a /tmp venv, a
    worktree cleanup, a pipx reinstall landing elsewhere) the hooks self-healed
    and rendered `techne ✅ live` while `claude mcp list` said `✘ Failed to
    connect` and no mcp__techne__* tool existed for the entire session. The CLI,
    hooks and DB all worked. Only the surface the agent talks through was dead,
    and nothing said so.

    Registration now routes through the self-healing launcher, but an install
    created before that keeps its raw path — so this is what tells that operator,
    rather than leaving it to be discovered mid-session.
    """
    if config.platform != CLAUDE_CODE:
        return None
    try:
        from techne.platforms.claude_code.hooks import mcp_registration_problem
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="mcp:registration",
            subsystem="mcp",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNKNOWN,
            detail=f"could not check MCP registration: {type(exc).__name__}: {exc}",
            evidence="import of mcp_registration_problem failed",
        )

    # NOT OUR BUSINESS ON AN UNINITIALISED MACHINE. techne-hook.sh is written by
    # register_hooks and deleted by unregister, so its absence means techne has
    # not installed here — and telling that operator their MCP is broken is a red
    # they cannot act on, the same mistake #469 review 6 caught in the drift
    # block. Once installed, a missing or dead registration is a real failure.
    from techne.platforms.claude_code.hooks import _launcher_path

    if not _launcher_path().exists():
        return None

    problem = mcp_registration_problem(config)
    if problem is None:
        return CheckResult(
            name="mcp:registration",
            subsystem="mcp",
            kind=Kind.FRESHNESS,
            verdict=Verdict.LIVE,
            detail="launch command resolves",
            evidence="mcpServers.techne command/args target exists",
        )
    return CheckResult(
        name="mcp:registration",
        subsystem="mcp",
        kind=Kind.FRESHNESS,
        verdict=Verdict.BROKEN,
        detail=problem,
        evidence="mcpServers.techne in the claude MCP config",
        remedy=init_remedy(config),
    )


@register_check
@expensive
def check_runtime_revision(config) -> CheckResult | list[CheckResult]:
    """WHAT CODE IS ACTUALLY RUNNING? (techne#473)

    Every other check here asks whether a subsystem ran. This one asks which
    REVISION did the running, because a merged fix is not a deployed fix and
    nothing said so.

    Demonstrated live on 2026-08-03: PR #472 fixed a production crash and merged
    to main, while the interpreter every hook on the machine runs was an EDITABLE
    install whose `.pth` was a bare path to a shared working tree — a pointer to a
    CHECKOUT, not to a revision. That checkout sat on a peer session's branch
    predating the merge, so the machine kept running the bug. Merging changed
    nothing about what executed, and no surface reported it.

    The structural half is done (the runtime is now a dedicated clone that only
    tracks main), but it is still a git checkout somebody can leave behind, so
    this is the half that makes it legible.

    NO NETWORK, DELIBERATELY. The comparison is against the LOCAL `origin/main`
    ref, so it is only as fresh as the last fetch — which EVERY detail says
    outright rather than implying a live comparison. Fetching from a health check
    would make `techne doctor` fail on a plane and turn a diagnostic into an
    action.

    Marked `@expensive`, so `collect_health` (the SessionStart banner) skips it
    and only `doctor` pays. An earlier revision of this docstring ASSERTED that
    exemption while nothing implemented it: `collect_health` ran the whole
    registry, so this check's six git subprocesses — ceiling 6 x timeout=5 = 30s
    against a 5s hook budget, on a fail-open path that drops the whole banner on
    overrun — ran in the hot path the comment said they avoided. Caught in
    #487 review. The marker is now the mechanism; this paragraph only describes it.

    IT ALSO REPORTS THE DEPLOYMENT WHEN THAT IS A DIFFERENT CHECKOUT (techne#690).
    Everything above answers "what am I running?" about the checkout `techne` was
    imported from. `techne doctor` is almost always run from a repo working copy,
    where that answer is a DORMANT "working checkout, not a deployment" — so on
    2026-08-19 the deployed runtime sat 20 commits behind main, re-deriving rows a
    migration had just removed, while every doctor run anyone did stayed quiet.
    The one place the check would have fired, `cd ~/.techne/runtime && techne
    doctor`, is the one place nobody runs it. So when `<techne_home>/runtime` is a
    checkout and is not the one we imported, it is evaluated too, under its own
    name (`runtime:deployed`), with the same rules — two questions, two rows. The
    same deployment `_deployment_checkout` already prefers for `runtime:loaded`.
    """
    from pathlib import Path

    import techne
    from techne.config import techne_home

    runtime = Path(techne.__file__).resolve().parent.parent
    own = _runtime_revision_result(runtime, "runtime:revision")

    deployed = techne_home() / "runtime"
    try:
        separate = (deployed / ".git").exists() and deployed.resolve() != runtime
    except OSError:
        # An unreadable techne_home is not a deployment we can describe; the
        # imported checkout's own answer still stands.
        separate = False
    if not separate:
        return own

    from dataclasses import replace

    dep = _runtime_revision_result(deployed, "runtime:deployed")
    return [own, replace(dep, detail=f"deployment at {deployed}: {dep.detail}")]


def _runtime_revision_result(runtime: Path, name: str) -> CheckResult:
    """The body of `check_runtime_revision`, for ONE checkout (see its docstring)."""
    import subprocess

    def _git(*args) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(runtime), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    head = _git("rev-parse", "--short", "HEAD")
    if head is None:
        # A real (non-editable) install has no revision to compare, and that is a
        # legitimate, healthy state — not something to report red.
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=f"not a git checkout — packaged install at {runtime}",
            evidence=f"git rev-parse in {runtime} did not resolve",
        )

    upstream = _git("rev-parse", "--short", "origin/main")
    if upstream is None:
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNKNOWN,
            detail=f"running {head}, but no local origin/main to compare against",
            evidence=f"git rev-parse origin/main failed in {runtime}",
        )

    # A WORKING CHECKOUT IS NOT A DEPLOYMENT, and conflating them makes this
    # check a red nobody can clear. CI checks out the PR branch and every dev
    # worktree sits on a feature branch — both are legitimately behind main, and
    # reporting that as "a merged fix is not deployed" is noise on the exact
    # surface that must stay trustworthy. Caught by CI going red on this PR's own
    # branch; it passed locally only because my branch happened to be AHEAD of
    # main, which is luck, not coverage.
    #
    # The question this check exists to answer is about the DEPLOYED runtime, and
    # a deployment tracks main. Anything else reports what it is and stays quiet.
    # ONLY `main` COUNTS AS A DEPLOYMENT. I first allowed a detached HEAD too,
    # reasoning "detached at a released tag" — but CI checks out detached as well,
    # so that exemption would have kept the false red on every CI run while
    # looking like it fixed it. The deployment this check exists for
    # (~/.techne/runtime) tracks the main branch, so gate on exactly that; a
    # detached tag-deploy loses the staleness signal, which is the cheaper miss.
    def _last_fetch() -> str | None:
        """When the local origin/main ref last MOVED — the real freshness bound.

        NOT the tip commit's committer date, which is what this reported before
        #487 review: a backdated commit fetched seconds earlier rendered "last
        known origin/main commit 2 years, 7 months ago", describing the comparison
        as ancient when it was current. The ref's own reflog answers the question
        actually being asked. It is empty in a clone that has never re-fetched
        (verified), hence the None the callers fall back from.
        """
        sel = _git("log", "-g", "-1", "--date=relative", "--format=%gd", "origin/main")
        if sel and "@{" in sel and sel.endswith("}"):
            return sel.split("@{", 1)[1][:-1]
        return None

    # THE FRESHNESS CAVEAT GOES ON EVERY BRANCH, not just STALE. It was on STALE
    # alone, which is backwards: "level with origin/main" is the FALSE-GREEN case,
    # the one where a ref last fetched three weeks ago reads as up to date. The
    # branch that most needs the caveat was the branch that omitted it.
    fetch_age = _last_fetch()
    if fetch_age:
        freshness = f"compared against the local ref, last fetched {fetch_age}"
    else:
        tip = _git("log", "-1", "--format=%cr", "origin/main") or "unknown"
        freshness = (
            "compared against the local ref; last fetch time unknown "
            f"(origin/main tip committed {tip})"
        )

    # One call for both directions: "<behind>\t<ahead>". Needed on the working-
    # checkout path too — see the DORMANT block below.
    counts = _git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    parts = (counts or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        # A PROBE THAT FAILED IS NOT A HEALTHY RUNTIME. Previously this fell
        # through to LIVE "level with the local origin/main" — the check asserting
        # exactly what it had just failed to determine, thirty lines below the
        # `upstream is None` branch that handles the identical situation
        # correctly. Reached in practice via the 5s timeout (#487 review).
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNKNOWN,
            detail=f"running {head}, but could not count commits against origin/main",
            evidence=f"git rev-list --left-right --count origin/main...HEAD = {counts!r}",
        )
    behind, ahead = int(parts[0]), int(parts[1])

    # `_git` returns None on failure and "" on a clean tree — two different
    # answers that both read as falsy. Collapsing them made a FAILED `git status`
    # indistinguishable from a clean one, i.e. the same don't-vouch-for-what-you-
    # cannot-see shape as the rev-list branch above (#487 review round 2, nit 1).
    # Not fatal to the verdict — how far behind you are is the headline and it is
    # already known here — so this degrades the suffix rather than the result.
    dirty = _git("status", "--porcelain")
    if dirty is None:
        dirty_suffix = " — LOCAL EDITS UNKNOWN (git status failed)"
    else:
        dirty_suffix = " — WORKING TREE DIRTY" if dirty else ""

    # A WORKING CHECKOUT IS NOT A DEPLOYMENT, and conflating them makes this
    # check a red nobody can clear. CI checks out the PR branch and every dev
    # worktree sits on a feature branch — both are legitimately behind main, and
    # reporting that as "a merged fix is not deployed" is noise on the exact
    # surface that must stay trustworthy. Caught by CI going red on this PR's own
    # branch; it passed locally only because my branch happened to be AHEAD of
    # main, which is luck, not coverage.
    #
    # The question this check exists to answer is about the DEPLOYED runtime, and
    # a deployment tracks main. Anything else reports what it is and stays quiet.
    # ONLY `main` COUNTS AS A DEPLOYMENT. I first allowed a detached HEAD too,
    # reasoning "detached at a released tag" — but CI checks out detached as well,
    # so that exemption would have kept the false red on every CI run while
    # looking like it fixed it. The deployment this check exists for
    # (~/.techne/runtime) tracks the main branch, so gate on exactly that; a
    # detached tag-deploy loses the staleness signal, which is the cheaper miss.
    #
    # BUT THE VERDICT STAYING QUIET DOES NOT LICENSE THE DETAIL SAYING NOTHING.
    # This DORMANT branch IS the #473 incident state — an editable install
    # pointing at a shared tree parked on a peer's branch, behind the merge — and
    # it reported green with no distance and no remedy, rendering identically to a
    # healthy runtime. That is the #486 shape (an un-healed thing indistinguishable
    # from a healed one) inside the check written to make #473 legible. The fix is
    # NOT a red that CI can never clear: keep DORMANT and `ok`, and put the number
    # in front of the operator reading `doctor`. (#487 review finding 2.)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        # Same rule again: without the branch this cannot tell a deployment from
        # a working checkout, so it must not assert either. It previously rendered
        # the failure into the sentence — "running 9494351 on None — a working
        # checkout" — which at least looked malformed rather than authoritative,
        # but "looks obviously broken" is not the standard this module holds.
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNKNOWN,
            detail=f"running {head}, but could not determine which branch is checked out",
            evidence=f"git rev-parse --abbrev-ref HEAD failed in {runtime}",
        )
    if branch != "main":
        where = f"on {branch!r}" if branch != "HEAD" else "at a detached HEAD"
        if behind:
            distance = f"{behind} commit(s) behind origin/main ({upstream})"
        else:
            distance = f"level with origin/main ({upstream})"
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=(
                f"running {head} {where} — a working checkout, not a deployment; "
                f"{distance}{dirty_suffix}. {freshness}"
            ),
            evidence=(
                f"HEAD is on {branch!r} in {runtime}; behind={behind} ahead={ahead} vs origin/main"
            ),
        )

    if behind:
        return CheckResult(
            name=name,
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.STALE,
            detail=(
                f"running {head}, {behind} commit(s) BEHIND origin/main ({upstream}) — "
                f"a merged fix is not deployed{dirty_suffix}. {freshness}"
            ),
            evidence=f"git rev-list behind={behind} vs origin/main in {runtime}",
            remedy=f"git -C {runtime} pull",
        )

    # Ahead-of-main is still LIVE — nothing is undeployed — but on a deployment a
    # commit nobody pushed is worth naming rather than papering over as "level
    # with". It is also the state that let this PR pass locally while CI went red.
    position = (
        f"{ahead} commit(s) AHEAD of origin/main ({upstream}) — unpushed local commits"
        if ahead
        else f"level with the local origin/main ({upstream})"
    )
    return CheckResult(
        name=name,
        subsystem="runtime",
        kind=Kind.FRESHNESS,
        verdict=Verdict.LIVE,
        detail=f"running {head}, {position}{dirty_suffix}. {freshness}",
        evidence=f"HEAD={head} origin/main={upstream} behind=0 ahead={ahead} in {runtime}",
    )


#: Long-lived techne consumers worth asking "what did you LOAD?", matched against
#: the process command line. A short-lived process (the CLI, a hook) forks a fresh
#: interpreter per invocation and cannot hold stale modules, so it is not a
#: candidate — the question only exists for a process that outlives a deploy.
_LONG_LIVED_PROCESSES = ("techne.mcp_server", "techne.watch", "techne watch")


def _running_techne_processes() -> list[tuple[int, float, str]] | None:
    """(pid, start_epoch, command) per long-lived techne process.

    Returns None when the table could not be READ **or when every matching row
    failed to parse** — both are failed probes (UNKNOWN). An empty list means
    "read it, understood it, nothing long-lived is running" (DORMANT). Collapsing
    the two would let a broken parser emit an all-clear.

    `lstart` is a local-time string ("Thu Aug  6 15:41:42 2026") and is converted
    through `time.mktime`, which interprets a naive struct_time as LOCAL time —
    matching how `ps` emitted it. Do not "fix" this to `calendar.timegm`: that
    would read a local timestamp as UTC and shift every process start by the
    UTC offset, which on this timezone would silently mark a fresh process stale
    for four hours after every deploy.
    """
    import subprocess
    import time

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,lstart=,args="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None

    found: list[tuple[int, float, str]] = []
    matched = 0
    for line in out.stdout.splitlines():
        if not any(pat in line for pat in _LONG_LIVED_PROCESSES):
            continue
        matched += 1
        parts = line.split()
        # pid + the five lstart tokens + at least one command token
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
            start = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
        except (ValueError, OverflowError):
            continue
        found.append((pid, start, " ".join(parts[6:])))

    # SOME ROWS FAILED AND EVERY ROW FAILED ARE DIFFERENT FACTS. Skipping a bad
    # row keeps one oddity from taking down `techne doctor`; skipping ALL of them
    # means the `lstart` shape is not what this parser assumes, and returning []
    # then renders as "no long-lived techne process running (nothing can hold
    # stale modules)" — an affirmative ok=True all-clear, emitted precisely when
    # the probe has stopped working. That is a silent green in the one check whose
    # job is to stop silent greens. A parser that matched processes and understood
    # none of them has FAILED, not observed an empty machine.
    # (Prudence, PR #510 — demonstrated, and the fix prototyped, before I took it.)
    if matched and not found:
        return None
    return found


def _deployment_checkout():
    """The checkout the LONG-LIVED PROCESSES run from — not necessarily ours.

    `runtime:revision` may reasonably ask about the checkout it was imported
    from, because it only ever talks about itself. This check compares a
    timestamp against OTHER processes, and those two come apart hard: running
    `techne doctor` from a review worktree made `deployed_at` the worktree's own
    HEAD — a commit that exists nowhere else on the machine — and a server that
    started after it was reported healthy against a revision it could not
    possibly have loaded. That is the #473 configuration turning this check
    green. Measured on a real box (Prudence, PR #510).

    So prefer the deployed clone at `<techne_home>/runtime`, which is what
    techne#473 established as THE deployment, and fall back to our own checkout
    only when it is absent. Either way the caller names the path it measured, so
    the number is falsifiable by whoever reads it.
    """
    from pathlib import Path

    import techne
    from techne.config import techne_home

    deployed = techne_home() / "runtime"
    if (deployed / ".git").exists():
        return deployed
    return Path(techne.__file__).resolve().parent.parent


def _runtime_deployed_at(runtime) -> tuple[int, str] | None:
    """(epoch, how) for when `runtime` last LANDED at its current revision.

    COMMITTER TIME IS NOT DEPLOY TIME, and the gap is a false green: a commit
    lands upstream at 22:02, the operator pulls at 23:00, a server starts at
    22:30. It loaded pre-pull code, yet it started after the commit — reported
    LIVE. Local landing time closes that, and it also shrinks the check's duty
    cycle, because pulls are rarer than commits on an actively developed repo —
    which is the difference between a useful warning and a permanently-amber one.

    `.git/logs/HEAD`'s mtime is when the ref last MOVED here: one stat, no
    subprocess. NOT `.git/HEAD`, whose mtime read three days stale on the live
    runtime — a fast-forward rewrites `refs/heads/main` and the reflog, never the
    symref. Falls back to the committer date when there is no reflog (a fresh
    clone with logging off), and says which it used.
    """
    import subprocess
    from pathlib import Path

    runtime = Path(runtime)
    reflog = runtime / ".git" / "logs" / "HEAD"
    try:
        if reflog.is_file():
            return int(reflog.stat().st_mtime), "landed here"
    except OSError:
        pass

    try:
        out = subprocess.run(
            ["git", "-C", str(runtime), "log", "-1", "--format=%ct", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return int(out.stdout.strip()), "committed upstream"
    except ValueError:
        return None


def _real_or(part: str, fallback: str) -> str:
    """The operator's own value when it is usable, else a concrete word.

    Never echoes a placeholder back: a remedy the guard rejects is a dead loop, and
    `agent or '<agent>'` produced exactly that for four forms (techne#553, Prudence r3).
    """
    return part if part and not part.startswith("<") else fallback


@register_check
def check_instance_identity(config) -> CheckResult | None:
    """Is this instance's `written_by` name unambiguous in a shared store? (techne#553)

    Cheap: reads config, touches no db. Fires only when this instance actually writes
    to a shared archivist — a standalone install attributes nothing and has nothing to
    collide with.

    FAIL_OPEN, not a fifth kind: the write SUCCEEDS and the row lands — it is the
    ATTRIBUTION that silently degrades to something that cannot discriminate, which is
    this taxonomy's "degrades silently to a no-op when a dependency is missing" with
    the dependency being a unique name. (An earlier draft invented `Kind.CONFIG`; the
    four kinds came out of the 2026-08-03 audit and adding a fifth to fit one check is
    how a taxonomy stops meaning anything.)

    THE COLLISION IS GUARANTEED, NOT LIKELY. cabal's `identity.toml.example` — the file
    every new cabal starts from — ships `archivist`, `architect`, `librarian`, `mason`
    and `wright`. Three operators federating into one store contribute three different
    agents all named `archivist`, which is precisely the ambiguity `written_by` exists
    to remove. `<agent>@<cabal>` qualifies it; the machine is the wrong scope, since two
    cabals can share a host and one cabal can span several.
    """
    # KEYED ON THE BINDING, NOT THE LEGACY FLAG (techne#552). This asked
    # `config.standalone`, which DEFAULTS TO TRUE — so an operator who adopted
    # `archivist: store` (and had no reason to touch `standalone`) got DORMANT
    # "attributes nothing to a shared store" while writing distilled knowledge into a
    # shared store under an empty instance_name. That is a false GREEN on precisely the
    # configuration this guard exists for, and liveness.md records the collision it
    # prevents as GUARANTEED rather than likely — cabal ships `archivist` as a persona
    # name. The inverse fired too: a local-bound box reported STALE for writing nowhere.
    from techne.core.archivist import ARCHIVIST_STORE, archivist_binding

    if archivist_binding(config) != ARCHIVIST_STORE:
        return CheckResult(
            name="identity:instance",
            subsystem="archivist",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.DORMANT,
            detail="not bound to a shared store — this instance attributes nothing",
            # Report the thing actually consulted. This said `standalone=true`, which on
            # a `standalone: false` + local box states a config value that is FALSE and
            # points the operator at a flag saying the opposite. (Prudence, #714.)
            evidence=f"archivist_binding={archivist_binding(config)}",
        )
    name = (getattr(config, "instance_name", "") or "").strip()
    if not name:
        return CheckResult(
            name="identity:instance",
            subsystem="archivist",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.STALE,
            detail=(
                "writing to a shared archivist with no instance_name — every row lands "
                "unattributed and cannot be attributed later"
            ),
            evidence="instance_name is empty",
            remedy="set instance_name: your-agent@your-cabal in config.yaml",
        )
    agent, sep, cabal = name.partition("@")
    # BOTH SIDES MUST BE REAL. `"@" in name` blessed `archivist@`, `@alice` and `@`
    # — an empty qualifier qualifies nothing, so `archivist@` collides with every other
    # cabal's `archivist@` exactly as `archivist` does. It also blessed
    # `archivist@<cabal>`: an operator following this check's own remedy literally lands
    # on a value containing '@', is told LIVE, and writes a string byte-identical to
    # what every other operator following the same instruction writes.
    #
    # This does NOT constrain the operator's scheme, which was the reason for keeping
    # the test loose — it only requires that the qualifier is actually there. The
    # placeholder test is the second guard; the two-sides test is the load-bearing one.
    placeholder = agent.startswith("<") or cabal.startswith("<")
    if not (sep and agent and cabal) or placeholder:
        return CheckResult(
            name="identity:instance",
            subsystem="archivist",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.STALE,
            detail=(
                f"instance_name {name!r} does not identify a writer — cabal's persona "
                "roster is shared, so a name without a real qualifier on BOTH sides of "
                "'@' collides with the same persona in every other cabal writing here"
            ),
            # NOT "has no '@'" — this branch fires for six names that HAVE one
            # (`archivist@`, `@alice`, `@`, and the placeholder forms). That string
            # was left over from the presence-only test and was the last place the
            # check contradicted its own rule.
            evidence=f"instance_name={name!r} lacks a real value on both sides of '@'",
            # NEVER SUBSTITUTE A PLACEHOLDER BACK IN. `agent or '<agent>'` printed
            # `<agent>@your-cabal` whenever the agent side was empty or itself a
            # placeholder — a remedy the guard three lines above rejects, so
            # `@alice`, `@`, `<agent>@<cabal>` and `<agent>@alice` dead-looped.
            # Closing "the check blesses its own remedy" opened its mirror image.
            # Reuse the operator's own agent side only when it is real.
            remedy=(
                f"set instance_name: {_real_or(agent, 'your-agent')}@your-cabal"
                " in config.yaml (both sides real, no placeholders)"
            ),
        )
    return CheckResult(
        name="identity:instance",
        subsystem="archivist",
        kind=Kind.FAIL_OPEN,
        verdict=Verdict.LIVE,
        detail=f"attributing federated writes as {name!r}",
        evidence=f"instance_name={name!r}",
    )


@register_check
def check_codex_freshness(config) -> CheckResult | None:
    """Was this repo's code index built at the commit HEAD is on now? (techne#937)

    THAT IS NARROWER THAN "does the index still describe the repo", which an
    earlier headline claimed. HEAD not having moved does not mean the working
    tree is unchanged: uncommitted edits, a stash, or an index built from a dirty
    tree all read LIVE. The predicate is deliberately the cheap, honest one --
    proving the index matches the tree means re-hashing every file, which is
    exactly the `@expensive` walk that would banish this from the banner.


    THE INCIDENT. A session started on 30984e0 with cabal's index 5 days old and
    60 source files changed since. Three symbols that exist in cabal source
    returned ZERO hits from `codex search` -- `_delegation_budget_note`,
    `_budget_at_least_one`, `_doom_loop_guard`. kobo was 31 files behind,
    daedalus 22, liminal 18. All silent, because no check looked at codex at all.
    A stale index does not degrade gracefully: it answers "we don't have that"
    with the same confidence as a correct miss, which is the one question codex
    exists to answer.

    NOT `@expensive`, AND THAT IS THE WHOLE DESIGN CONSTRAINT. The issue flagged
    up front that the obvious implementations -- re-hashing every file, or
    shelling out to git -- are exactly what `@expensive` excludes, so the obvious
    version lands in `doctor` only and the banner stays silent, which is the
    thing being asked for rather than delivered. This reads two rows from a KV
    table and one small file, then compares two strings.

    INDEXED-HEAD, NOT AGE, IS THE PREDICATE. A fixed age threshold is crude: a
    repo nobody has touched for a month is not stale, and one touched twice this
    morning may be. "Indexed before the current HEAD" is the honest question, and
    `indexed_head` is recorded at index time so asking it costs one ref read. The
    age is reported alongside because that is what an operator reads, but it does
    not decide the verdict.

    PER-REPO, NOT GLOBAL. There are 19 codex stores on this machine; the banner
    knows the cwd, and the current repo's index is the useful one.
    """
    from techne.core.codex import get_meta, resolve_repo_db_for_query
    from techne.core.repo import head_sha, primary_repo_root

    try:
        cwd = Path.cwd()
    except OSError:
        return None

    root = primary_repo_root(cwd)
    if root is None:
        return CheckResult(
            name="codex:freshness",
            subsystem="codex",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail="not inside a repo — nothing to index",
            evidence=f"no repo root above {cwd}",
        )

    db = resolve_repo_db_for_query(config, root)
    if db is None or not db.exists():
        return CheckResult(
            name="codex:freshness",
            subsystem="codex",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=f"{root.name} has no codex index yet",
            evidence=f"no per-repo db for {root}",
            remedy=f"techne codex index {root}",
        )

    indexed_head = get_meta(db, "indexed_head")
    indexed_at = get_meta(db, "indexed_at")
    current = head_sha(root)

    # UNOBSERVABLE, NOT LIVE, WHEN THE COMPARISON CANNOT BE MADE. An index written
    # before techne#937 has no indexed_head, and a repo whose HEAD is unreadable
    # gives nothing to compare against. Reporting either as healthy would be the
    # false green this check exists to remove; the banner's own convention is that
    # a probe which could not run outranks a healthy one.
    if not indexed_head or not current:
        why = "index predates head recording" if not indexed_head else "HEAD unreadable"
        return CheckResult(
            name="codex:freshness",
            subsystem="codex",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNOBSERVABLE,
            detail=f"{root.name}: cannot tell whether the index is current ({why})",
            evidence=f"indexed_head={indexed_head!r} current_head={current!r}",
            remedy=f"techne codex index {root}",
        )

    # A TIMESTAMP, AND THE TEXT NOW SAYS SO. An earlier version called this "the
    # age", which it is not -- `indexed_at` is a stored UTC instant and nothing
    # here subtracts it from now. Rendering it as an age would need a clock read
    # and a format decision; printing the instant is honest and costs nothing.
    stamp = f", indexed at {indexed_at}Z" if indexed_at else ""
    if indexed_head == current:
        return CheckResult(
            name="codex:freshness",
            subsystem="codex",
            kind=Kind.FRESHNESS,
            verdict=Verdict.LIVE,
            detail=f"{root.name} indexed at the current HEAD{stamp}",
            evidence=f"indexed_head == HEAD == {current[:7]}",
        )
    return CheckResult(
        name="codex:freshness",
        subsystem="codex",
        kind=Kind.FRESHNESS,
        verdict=Verdict.STALE,
        detail=(
            f"{root.name} was indexed at {indexed_head[:7]}, HEAD is now "
            f"{current[:7]} — codex may answer 'not found' for code that exists{stamp}"
        ),
        evidence=f"indexed_head={indexed_head[:7]} != HEAD={current[:7]}",
        remedy=f"techne codex index {root}",
    )


@register_check
def check_arch_card_freshness(config) -> CheckResult | None:
    """Is any architecture card out of date with the file it vouches for? (techne#937)

    THE ANSWER IS AUTHORITATIVE AND STILL CHEAP, WHICH THE ISSUE ASSUMED WAS NOT
    AVAILABLE. It proposed comparing stat times, reasoning that the real
    comparison needs `git hash-object` -- 21 subprocesses, 0.50s measured -- and
    that `@expensive` would therefore banish it from the banner, which is the
    thing being asked for rather than delivered.

    But a git blob sha is not a git feature, it is a formula:

        sha1(b"blob %d\0" % len(data) + data)

    Verified byte-identical to `git hash-object` on this repo -- 19 anchors hash
    in 0.006s with ZERO subprocesses, roughly 80x cheaper than shelling out and
    comfortably inside the 5s hook budget the banner is fail-open against.

    THAT IDENTITY IS CONDITIONAL, AND THE UNQUALIFIED CLAIM WAS WRONG. `git
    hash-object <path>` runs CLEAN FILTERS; the formula hashes raw bytes. They
    agree only where no eol filter applies. Measured: with `core.autocrlf=true`
    (the Windows default) or `* text=auto` in `.gitattributes`, a CRLF working
    file hashes differently and every anchor would read STALE. This repo sets
    neither -- `git config --get core.autocrlf` is empty -- so the equivalence
    holds here, but it is a property of this checkout, not of the formula. The
    exact claim is: identical to `git hash-object --no-filters` always, and to
    plain `git hash-object` wherever no eol filter applies.

    WHY NOT MTIME, WHICH IS WHAT THE ISSUE PROPOSED. Measured first: it reported
    12 of 19 anchors stale while the sha gate said all 19 were fresh. `git
    checkout` rewrites mtimes, so every branch switch manufactures a full set of
    false positives. The issue argued mtime "over-warns, never under-warns" and
    that this is the right trade for a banner hint -- but 12 false positives out
    of 19 is the "trains you to ignore it" failure this module keeps naming, and
    a check nobody reads is worth less than no check. Over-warning is the right
    direction only when the rate is low enough to stay readable.

    Scales with anchor count, not repo size, so it stays cheap as techne#910
    takes coverage from 20 anchors toward 120 files. `scripts/check_arch_cards.py`
    remains the pre-push gate and keeps its own stricter contract: a MALFORMED
    anchor is a hard failure there, while here it is skipped, because a banner
    that cannot render is worse than a banner missing one line.
    """
    import hashlib
    import re as _re

    from techne.core.repo import find_repo_root

    # find_repo_root, NOT primary_repo_root, AND THE DIFFERENCE IS A REAL BUG THIS
    # CHECK SHIPPED WITH. primary_repo_root deliberately maps a linked worktree to
    # its parent, which is right for CODEX (indexed against the primary by design)
    # and wrong here: cards live in the checkout being EDITED. Worktree-per-session
    # is the standing dev topology, and with the primary resolver a session that
    # edited a seam without bumping its card read LIVE in the banner, because the
    # PRIMARY's cards were being compared against the PRIMARY's seams. Measured
    # from a worktree at 542211d: resolved to the primary on main, verdict LIVE.
    try:
        root = find_repo_root(Path.cwd())
    except OSError:
        return None
    if root is None:
        return None

    # THE SAME GLOBS THE PRE-PUSH GATE USES (techne#937, Prudence). The check
    # globbed only docs/arch/*.md while scripts/check_arch_cards.py globs
    # ("ARCHITECTURE.md", "docs/arch/*.md"), so ARCHITECTURE.md's one anchor --
    # pyproject.toml -- was unguarded in the banner. Measured by drifting it: gate
    # RED and banner LIVE at the same instant, on the same drift. That is verbatim
    # the failure this check already fixes for an unreadable seam, left standing
    # one directory up.
    #
    # NOT IMPORTED FROM THE SCRIPT, deliberately: scripts/ is not a package and is
    # absent from an installed build. test_banner_and_gate_cover_the_same_anchors
    # asserts the two lists agree, which closes the CLASS -- a third card location
    # fails that test rather than silently going unguarded.
    card_globs = ("ARCHITECTURE.md", "docs/arch/*.md")
    cards_dir = root / "docs" / "arch"
    if not cards_dir.is_dir() and not (root / "ARCHITECTURE.md").is_file():
        return CheckResult(
            name="cards:freshness",
            subsystem="architecture",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=f"{root.name} keeps no architecture cards",
            evidence=f"no {cards_dir}",
        )

    anchor_re = _re.compile(r"^anchor:\s*(\S+)\s+sha=([0-9a-f]{40})\s*$", _re.M)
    drifted: list[str] = []
    unreadable: list[str] = []
    checked = 0
    cards: list[Path] = []
    for pattern in card_globs:
        cards.extend(sorted(root.glob(pattern)))
    for card in cards:
        try:
            text = card.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rel, recorded in anchor_re.findall(text):
            seam = root / rel
            try:
                data = seam.read_bytes()
            except OSError:
                # UNREADABLE IS A FINDING, NOT A SKIP. The pre-push gate treats a
                # missing seam as a HARD failure ("anchor seam file missing"), and
                # silently continuing here let the banner read LIVE while the gate
                # was red -- a card vouching for a file that has been renamed or
                # deleted is exactly the drift this check exists to surface.
                checked += 1
                unreadable.append(f"{card.name} <- {rel}")
                continue
            checked += 1
            current = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324
            if current != recorded:
                drifted.append(f"{card.name} <- {rel}")

    if not checked:
        return CheckResult(
            name="cards:freshness",
            subsystem="architecture",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail="no readable anchors to compare",
            evidence=f"{cards_dir} has no parsable anchor lines",
        )
    _EVIDENCE = (
        "in-process git blob sha — identical to `git hash-object --no-filters`, and "
        "to plain `git hash-object` wherever no eol filter applies"
    )
    problems = drifted + [f"{u} (unreadable)" for u in unreadable]
    if not problems:
        return CheckResult(
            name="cards:freshness",
            subsystem="architecture",
            kind=Kind.FRESHNESS,
            verdict=Verdict.LIVE,
            detail=f"{checked} anchor(s) match the card vouching for them",
            evidence=_EVIDENCE,
        )
    shown = ", ".join(problems[:3]) + (f", +{len(problems) - 3} more" if len(problems) > 3 else "")
    return CheckResult(
        name="cards:freshness",
        subsystem="architecture",
        kind=Kind.FRESHNESS,
        verdict=Verdict.STALE,
        detail=(f"{len(problems)} of {checked} seam(s) have drifted from their card ({shown})"),
        evidence=_EVIDENCE,
        remedy="update the card body and bump its anchor sha in the same change",
    )


@register_check
def check_runtime_torn(config) -> CheckResult | None:
    """A LIVE server has told us it is serving stale code (techne#696 part A).

    NOT `@expensive`, and that is the entire point of the shape. `check_runtime_loaded`
    below has to ask the process table who is running and ask git when the deployment
    landed, because it interrogates OTHER processes from outside — which is why it is
    deferred out of the SessionStart budget, and why PR #697 was withdrawn for calling
    it from there anyway while the banner four lines above said it was deferred.

    Here the servers do the knowing. Each one compares its own `sys.modules` against a
    baseline it took at startup and publishes the verdict; this reads it. A directory
    listing, one `open()` each, and `os.kill(pid, 0)` to drop the dead. No subprocess,
    so it goes through `run_checks` like everything else instead of around it.

    It is also STRICTLY BETTER than the ordering signal it sits beside: `runtime:loaded`
    says "a process started before the deployment landed", and says of itself that this
    is "ordering only… not a tear detector". This one names the modules.
    """
    from techne.core.runtime_drift import live_reports

    reports = live_reports()
    if not reports:
        return CheckResult(
            name="runtime:torn",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.LIVE,
            detail="no live server reports serving stale modules",
            evidence="no runtime-state file from a running pid carries torn modules",
        )
    torn = sorted({m for r in reports for m in r.get("torn", [])})
    pids = ", ".join(str(r.get("pid")) for r in reports)
    shown = ", ".join(torn[:3]) + (f", +{len(torn) - 3} more" if len(torn) > 3 else "")
    return CheckResult(
        name="runtime:torn",
        subsystem="runtime",
        kind=Kind.FRESHNESS,
        verdict=Verdict.STALE,
        detail=(
            f"{len(reports)} live server(s) are serving {len(torn)} module(s) that have "
            f"changed on disk since they were loaded ({shown})"
        ),
        evidence=f"published by pid(s) {pids}",
        remedy="restart the techne MCP server (or the session that owns it)",
    )


@register_check
@expensive
def check_runtime_loaded(config) -> CheckResult | None:
    """DEPLOYED IS NOT LOADED — the corollary of #473 (techne#491).

    `runtime:revision` asks which revision is on DISK. It reads git HEAD, so a
    long-running process holding modules imported BEFORE the last deploy renders
    as perfectly healthy. That is the silhouette `docs/arch/liveness.md` names —
    a state the check cannot vouch for, rendered as one it can — and it produced
    a wrong diagnosis when it happened: `techne_reflect_emit` died with
    `ImportError: cannot import name 'extract_json_array_or_none'` while the
    symbol sat on disk in both the repo and the runtime, and `rail:reflect` was
    read as STALE-because-unreflected when it was BROKEN in the only process that
    could run it.

    THIS IS A CONSERVATIVE STALENESS WARNING, NOT A TEAR DETECTOR, and the
    difference is measured rather than hedged. On 2026-08-06 the runtime was
    pulled under a live server — the same operation that tore it originally — and
    it did NOT tear: reflect_emit then ran on that long-running process, all 5
    threads, 15 candidates, 0 parse failures. Tearing depends on WHICH modules the
    process has yet to import and whether the deploy changed them; the unlucky
    pairing was `reflect.py` importing a symbol newly added to `_resident.py`, not
    a general property of pulling. So this check REPORTS WHAT IT OBSERVED — this
    process started before the deployed revision was committed — and never claims
    the process is torn. Overstating it would cry wolf on every routine deploy,
    and a warning that cries wolf stops being read, which is how the subsystem
    this lives in fails.

    SEPARATE CHECK RATHER THAN AN EXTENSION of `runtime:revision`, because the
    remedy differs: that one says `git -C ~/.techne/runtime pull`, this one says
    restart the process. A single result cannot carry two remedies, and a remedy
    that cannot clear its own finding is the never-green defect this module has
    already paid for twice.

    `@expensive` for the same reason as its sibling: it shells out to `ps`, and
    `collect_health` runs inside SessionStart on a fail-open path where one
    overrun drops the entire banner.

    A SHARPER SIGNAL EXISTS AND IS DELIBERATELY NOT BUILT: comparing the mtimes of
    the files a process has actually loaded (`sys.modules[...].__file__`) against
    disk measures the thing the failure really depends on. It needs the process to
    introspect ITSELF, which a check running in a different process cannot do
    without a new reporting rail on every long-lived consumer. Recorded on #491 as
    the follow-up if the conservative version proves too noisy in practice.
    """
    from datetime import datetime, timezone

    runtime = _deployment_checkout()
    landed = _runtime_deployed_at(runtime)
    if landed is None:
        # Same call as the sibling makes: a packaged install has no revision to
        # compare and that is healthy, not red.
        return CheckResult(
            name="runtime:loaded",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=f"not a git checkout — packaged install at {runtime}",
            evidence=f"no reflog and no HEAD commit date in {runtime}",
        )
    deployed_at, how = landed

    procs = _running_techne_processes()
    if procs is None:
        return CheckResult(
            name="runtime:loaded",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.UNKNOWN,
            detail="could not read or could not parse the process table",
            evidence=(
                "ps -eo pid=,lstart=,args= failed, or matched processes whose "
                "lstart shape this parser does not understand"
            ),
        )
    if not procs:
        return CheckResult(
            name="runtime:loaded",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail="no long-lived techne process running (nothing can hold stale modules)",
            evidence=(
                f"no process matching {', '.join(_LONG_LIVED_PROCESSES)}; "
                f"measured against {runtime} ({how} {deployed_at})"
            ),
        )

    stale = [(pid, start) for pid, start, _cmd in procs if start < deployed_at]
    when = datetime.fromtimestamp(deployed_at, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    if not stale:
        return CheckResult(
            name="runtime:loaded",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.LIVE,
            detail=(
                f"{len(procs)} long-lived process(es), all started after the runtime {how} ({when})"
            ),
            evidence=f"{runtime} {how} {deployed_at}; "
            + "; ".join(f"pid {p} start={int(s)}" for p, s, _ in procs),
        )

    ages = ", ".join(
        f"pid {pid} ({int((deployed_at - start) / 60)} min before)" for pid, start in stale
    )
    return CheckResult(
        name="runtime:loaded",
        subsystem="runtime",
        kind=Kind.FRESHNESS,
        verdict=Verdict.STALE,
        detail=(
            # LEAD WITH THE QUALIFICATION. The row renders at ~250 chars behind a
            # "!" and a capitalised verdict, so a skimmer meets the alarm two
            # wrapped lines before the hedge. Six words in is where it has to be.
            f"ordering only: {len(stale)} of {len(procs)} long-lived process(es) started before "
            f"the runtime {how} ({when}), so they run code that predates it. Whether any is "
            "actually torn depends on which modules it has yet to import — this is not a "
            "tear detector."
        ),
        evidence=f"{runtime} {how} {deployed_at}; {ages}",
        remedy="restart the techne MCP server (or the session that owns it) to reload the runtime",
    )


@register_check
def check_maintenance_rails(config) -> list[CheckResult]:
    """reflect / consolidate / dream liveness, from what they actually produced.

    Keyed on `knowledge_candidates.origin` — the rail's OUTPUT — not on a config
    flag. `reflect.enabled: true` was true while the pass had never run once
    (techne#942 instance 1), so the flag proves nothing.
    """
    from techne.core.database import connect

    results: list[CheckResult] = []
    conn = connect(config.db_path)
    try:
        # Same reasoning as the hook cohort: a store with nothing in it has
        # nothing for these rails to chew on, so their silence is explained
        # rather than undecidable.
        has_material = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] > 0

        for rail, (interval, note, rail_remedy) in _RAIL_EXPECTATIONS.items():
            row = conn.execute(
                "SELECT MAX(created_at), COUNT(*) FROM knowledge_candidates WHERE origin = ?",
                (rail,),
            ).fetchone()
            last, count = (row[0], row[1]) if row else (None, 0)

            if rail == "reflect" and not config.reflect.enabled:
                verdict, remedy = Verdict.DORMANT, None
                detail = "disabled by config: reflect.enabled = false"
            elif not last and not has_material:
                verdict, remedy = Verdict.DORMANT, None
                detail = "no knowledge stored yet — nothing to process"
            else:
                verdict = temporal_verdict(last, interval)
                detail = f"{note}; {count} candidates all-time"
                remedy = rail_remedy if verdict is Verdict.STALE else None
                # AND SAY WHAT HAPPENED TO THE OUTPUT (techne#636). This check reads
                # MAX(created_at) — pure EMISSION — so it goes green the moment a rail
                # produces a candidate and stays green no matter what becomes of it.
                # Measured when #636 was filed: rail:reflect green with 206 candidates
                # PENDING and the last review decision on 2026-07-27. A rail that emits
                # into a queue nobody drains is not working; it is accumulating.
                #
                # VERDICT DELIBERATELY STILL KEYED ON EMISSION. This check is named for
                # liveness and that is what it answers; making an unreviewed backlog
                # turn doctor STALE is a policy change about the OPERATOR's review
                # cadence, not about whether the rail ran, and it belongs in its own
                # decision rather than smuggled into the fix that made it visible.
                # Reported, not enforced — the same split #497 landed for dangling.
                pending, last_decision = _rail_consumption(conn, rail)
                if pending:
                    detail += f"; {pending} pending review"
                    detail += (
                        f", last decision {last_decision}"
                        if last_decision
                        else ", NO decision ever recorded"
                    )

            results.append(
                CheckResult(
                    name=f"rail:{rail}",
                    subsystem="self-maintenance",
                    kind=Kind.TEMPORAL,
                    verdict=verdict,
                    last_fired=last,
                    expected_interval=interval,
                    evidence=(
                        f"SELECT MAX(created_at) FROM knowledge_candidates "
                        f"WHERE origin='{rail}' → {last or '(none)'}"
                    ),
                    detail=detail,
                    remedy=remedy,
                )
            )
    finally:
        conn.close()
    return results


def _rail_consumption(conn, rail: str) -> tuple[int, str | None]:
    """(candidates still pending, date of the last promote/discard) for one rail.

    The half `MAX(created_at)` structurally cannot see (techne#636): emission is
    the rail doing its job, consumption is anyone acting on it, and a check that
    measures only the first reports a rail as healthy while its output piles up
    unread. Best-effort — a missing column must never take the whole check down.
    """
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE origin = ? AND status = 'pending'",
            (rail,),
        ).fetchone()[0]
        last_decision = conn.execute(
            "SELECT MAX(reviewed_at) FROM knowledge_candidates "
            "WHERE origin = ? AND status IN ('promoted', 'discarded')",
            (rail,),
        ).fetchone()[0]
    except Exception:  # noqa: BLE001 — schema drift must not break liveness reporting
        return 0, None
    return pending or 0, (last_decision or None)


def _rail_binding(config, rail: str) -> tuple[bool, str]:
    """Is *rail* bound to a model RIGHT NOW? Returns (bound, why_not).

    THE QUESTION A MARKER CANNOT ANSWER (techne#735). A marker is a record of a run
    that happened under a configuration that may no longer exist. #732 fixed half of
    this at WRITE time — the unbound path retracts its marker — but a write-time
    retraction only fires when a session actually ends, and it cannot fire at all on
    the paths that never reach the retracting code:

      * `reflect.enabled = false` — `lifecycle.py` gates the call site, so
        `reflect_on_session` is never invoked and nothing can ever clear the marker.
      * the five custodian rails — `clear_rail_marker` exists but reflect is its only
        caller, so an unbound custodian rail keeps its last marker permanently.
      * even the fixed unbound-reflect path lies in the window between an operator
        editing the config and the next session end.

    Measured before the fix, one marker `outcome="ok"` and three configurations:

        reflect, enabled + bound   -> LIVE      (correct)
        reflect, enabled = false   -> LIVE      (the rail is OFF)
        reflect, enabled, no model -> LIVE      (the rail cannot run)
        dream,   custodian unbound -> LIVE      (the rail cannot run)

    So the rule is Prudence's generalisation rather than three patches: no verdict is
    derived from a marker without first asking whether the rail is currently bound.

    DELIBERATE BOUNDARY: `custodian.rails` is NOT consulted. It selects what the
    SCHEDULED runner drives (`custodian.effective_rails` — NAMED rather than cited by
    line, because a line number is the one citation form that goes stale silently, in
    a change whose whole subject is prose that stopped being true; techne#747 is open
    on that class), and an excluded rail can still be run
    explicitly with `--rail`, so exclusion is a scheduling choice rather than an absent
    binding. Widening this to scheduling would need its own decision.
    """
    if rail == "reflect":
        if not config.reflect.enabled:
            return False, "reflect.enabled = false"
        if not config.reflect.model:
            return False, "reflect.model unset"
        return True, ""

    from techne.core.custodian import effective_model

    if not effective_model(config, rail=rail):
        return False, "custodian.model unset"
    return True, ""


@register_check
def check_model_engine(config) -> CheckResult:
    """Can the configured model engine send a call at all? (techne#1189)

    ``litellm`` is the default and reads DORMANT: whether a LiteLLM call can succeed is
    the model-bound rail checks' question, answered from their markers. ``claude-code``
    depends on one thing this check can see cheaply, the ``claude`` CLI on PATH. Without
    it every model call raises, so it reads BROKEN with the remedy. Whether the CLI is
    signed in is not probed: that would mean a model call from the SessionStart hot path.
    """
    from techne.core.model_engine import CLAUDE_CODE, claude_binary

    engine = getattr(config, "model_engine", "litellm")
    if engine != CLAUDE_CODE:
        return CheckResult(
            name="model:engine",
            subsystem="model",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.DORMANT,
            detail=f"{engine} (default): model calls go through LiteLLM",
            evidence="config.model_engine",
        )
    binary = claude_binary()
    if binary is None:
        return CheckResult(
            name="model:engine",
            subsystem="model",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.BROKEN,
            detail="claude-code: the `claude` CLI is not on PATH, so every model call fails",
            evidence="shutil.which('claude') is None",
            remedy="Install Claude Code, or set model_engine: litellm",
        )
    return CheckResult(
        name="model:engine",
        subsystem="model",
        kind=Kind.FAIL_OPEN,
        verdict=Verdict.LIVE,
        detail=f"claude-code: model calls run `claude -p` ({binary}) on the user's own login",
        evidence="shutil.which('claude')",
    )


@register_check
def check_model_bound_rails(config) -> list[CheckResult]:
    """Did a MODEL-BOUND rail actually produce, or did it fail where nobody was looking?

    THE BLIND SPOT THIS CLOSES (techne#555). `check_maintenance_rails` above keys on
    `knowledge_candidates.origin` — the rail's OUTPUT — which was the right call when it
    was written and is still right for what it answers. But `origin` does not record
    WHO produced the row, and two different producers write it: the resident
    emit→ingest route (`model_used='agent-assisted'`) and the model-bound automatic
    route (`model_used=<the model id>`).

    MEASURED 2026-08-21 on the operator's store: `rail:reflect` read LIVE off 420
    all-time candidates while the automatic rail had produced **zero, ever**, since
    2026-06-20. Every one of those 420 came from the resident route. The automatic pass
    was raising `AuthenticationError` on every session — `config.model` is an Anthropic
    id and the operator holds no Console API key — and `reflect_on_session` caught it,
    incremented a counter nobody reads, and let teardown proceed. Two months of a rail
    reporting healthy while doing nothing.

    So this check reads the MARKER — and, since techne#735, the CONFIG; see below —
    rather than the output: `rail:<name>:last_run`, written by
    `custodian.record_rail_marker` on every automatic or headless run, success and
    failure alike, with the reason.

    NO MARKER IS DORMANT, NOT BROKEN. An instance that only ever uses the resident
    route never writes one, and that is the default and correct posture — crying wolf
    at every install that has not opted in is how a check gets trained into noise. The
    finding is a marker that EXISTS and says the run failed: that is a rail someone
    switched on which is not working.

    AND SINCE techne#735 IT READS THE MARKER *AND THE CONFIG*, which the paragraph
    above no longer tells you on its own. `_rail_binding` is asked FIRST: a marker
    records a run under a configuration that may no longer exist, so an UNBOUND rail
    reads DORMANT for every outcome — the payload is kept and shown, but it stops
    driving the verdict. "The finding is a marker that says the run failed" therefore
    holds only while the rail is STILL BOUND; an install that opted OUT now reads the
    same as one that never opted in, which is the consistent reading rather than a new
    exception.

    THIS IS THE CHECK `liveness.md` MEANS when it justifies a verdict depending on
    `config` against the `config:trust` precedent. A maintainer auditing which checks
    read config lands HERE, so the contract has to say so — otherwise the card raises
    the question and this docstring answers it wrongly. (Prudence, #743 r3.)
    """
    from techne.core.custodian import MARKER_PREFIX, RAILS
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    results: list[CheckResult] = []
    conn = connect(config.db_path)
    try:
        # `reflect` is included alongside the five custodian rails because it is the
        # one that HAS an automatic path today, and it is the rail the finding came
        # from. It is not in the custodian registry (it has its own runner), so it is
        # named explicitly rather than inferred.
        for rail in [*sorted(RAILS), "reflect"]:
            marker = get_state(conn, f"{MARKER_PREFIX}:{rail}:last_run") or {}
            outcome = str(marker.get("outcome", ""))
            ts = marker.get("ts")
            detail = str(marker.get("detail", ""))

            if not marker:
                results.append(
                    CheckResult(
                        name=f"rail:{rail}:bound",
                        subsystem="custodian",
                        kind=Kind.FAIL_OPEN,
                        verdict=Verdict.DORMANT,
                        detail="no model-bound run recorded — answered by a resident agent",
                        evidence=f"extension_state has no {MARKER_PREFIX}:{rail}:last_run",
                    )
                )
                continue

            bound, why_unbound = _rail_binding(config, rail)
            if not bound:
                # A MARKER FROM A BINDING THAT NO LONGER EXISTS IS NOT CURRENT STATE
                # (techne#735). The payload is KEPT AND SHOWN — that is what #732 was
                # protecting, and an operator switching a rail off for an afternoon
                # should still be able to read what the last bound run did — but it
                # stops driving the verdict, because the rail cannot run today.
                #
                # THIS DOWNGRADES `error`/`partial` TO DORMANT TOO, NOT ONLY `ok`, and
                # that is deliberate rather than an oversight. Reading BROKEN off a
                # stale payload is wrong in the same way as reading LIVE — both derive
                # a claim about now from a configuration that is gone. It is wrong in
                # the SAFE direction, which is exactly the argument that would let it
                # survive unexamined. Nothing is hidden by the downgrade: the outcome
                # and its detail ride in `detail` below, so an operator reading the
                # doctor still sees "last bound run error: AuthenticationError".
                results.append(
                    CheckResult(
                        name=f"rail:{rail}:bound",
                        subsystem="custodian",
                        kind=Kind.FAIL_OPEN,
                        verdict=Verdict.DORMANT,
                        last_fired=ts,
                        # `outcome or "<no outcome recorded>"` because a GARBLED marker
                        # reaches here too: the fail-closed path accepts an empty or
                        # unrecognised outcome, and the naive form rendered "last bound
                        # run : garbled" or trailed off at "last bound run ". Cosmetic,
                        # but this string is operator-facing, and this card's own
                        # invariant is that a detail string is documentation that rots.
                        # (Prudence, #743 r1 nit 5.)
                        detail=(
                            f"not bound ({why_unbound}) — the rail cannot run; "
                            f"last bound run {outcome or '<no outcome recorded>'}"
                            + (f": {detail}" if detail else "")
                        ),
                        evidence=(
                            f"{MARKER_PREFIX}:{rail}:last_run outcome={outcome!r} "
                            "retained but not authoritative — see _rail_binding"
                        ),
                    )
                )
                continue

            if outcome in ("error", "partial"):
                # `partial` is BROKEN deliberately, and the noise hazard is real and
                # accepted: one refusal in twenty marks the rail until the next fully
                # clean run. Kept because "ran and reported trouble" has no other
                # verdict in this vocabulary — DORMANT ties with LIVE, UNKNOWN means a
                # probe that could not run, STALE means overdue. A rail answering some
                # of its prompts is not healthy, and the detail carries the ratio so an
                # operator can judge severity. Revisit with a policy about acceptable
                # failure fractions, not by quietly greening it. (Prudence, #709.)
                verdict = Verdict.BROKEN
                remedy = "techne custodian status"
            elif outcome == "ok" or outcome.startswith("noop:"):
                # `noop:` is a healthy steady state for consolidate/dream/conflict —
                # nothing to chew on is not a fault. The rail's resolved-inputs note
                # rides in `detail`, which is what makes "found nothing" separable from
                # "looked in the wrong place".
                verdict, remedy = Verdict.LIVE, None
            else:
                # FAIL CLOSED ON AN OUTCOME WE DO NOT RECOGNISE (Prudence, #709). The
                # previous shape mapped everything that was not literally "error" or
                # "partial" to LIVE — including a missing, empty or garbled outcome,
                # since `str(marker.get("outcome", ""))` yields "". That is fail-OPEN
                # in the one check whose entire job is to catch silent nothing, and it
                # is the same defect class as the rail this check exists to expose.
                verdict = Verdict.UNKNOWN
                remedy = "techne custodian status"

            results.append(
                CheckResult(
                    name=f"rail:{rail}:bound",
                    subsystem="custodian",
                    kind=Kind.FAIL_OPEN,
                    verdict=verdict,
                    last_fired=ts,
                    detail=detail or outcome,
                    evidence=f"{MARKER_PREFIX}:{rail}:last_run outcome={outcome!r}",
                    remedy=remedy,
                )
            )
    finally:
        conn.close()
    return results


@register_check
def check_outbox_targets(config) -> list[CheckResult]:
    """Per-target sync liveness — deliberately NOT aggregated.

    An aggregate hid the real state on 2026-08-03: `sink:archivist` was healthy
    with 54k rows synced that hour while `thread_summary` had been dead for eight
    weeks. Summing them reports "sync is fine".
    """
    from techne.core.database import connect

    results: list[CheckResult] = []
    # ONE connection for all three queries (#469 review 3, nit 6) — each
    # `connect()` re-runs the WAL pragmas and `_ensure_initialized`, and this is
    # the hot path the same PR is optimising.
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending'), MAX(created_at) "
            "FROM outbox GROUP BY target"
        ).fetchall()
        arow = conn.execute("SELECT MIN(applied_at) FROM schema_version").fetchone()
        # Sessions since the newest enqueue, per lane, is the activity clock for
        # the producer test below — these lanes are fed at SESSION END, so
        # wall-clock silence measures operator absence, not producer death.
        #
        # MAX(ended_at), NOT MAX(started_at) (#469 review 4, Important). These
        # lanes are fed at SESSION END, so the only session that constitutes
        # evidence is one that has actually ENDED without feeding the lane.
        # `MAX(started_at)` included the IN-FLIGHT session — and because
        # collect_health runs in Phase 5, after Phase 3 inserts that row, it was
        # always ≈ now. The predicate `newest > enq + 7d` therefore reduced to
        # `now > enq + 7d`: the wall-clock test this check was written to
        # replace, at 3× the sensitivity. A three-week holiday reproduced it.
        # CLEANLY-ENDED ONLY (#516). Since ended_at is now DERIVED for sessions
        # that died unclean, "has an ended_at" no longer means "reached
        # run_session_end". These lanes are fed AT SESSION END, so a crashed
        # session never had the OPPORTUNITY to feed one — counting it as evidence
        # inverts the question above. With 93% of August sessions never reaching
        # run_session_end, the unfiltered version reports STALE ("producer may be
        # dead") on a healthy store and points the operator at the sync producer
        # when the real cause is unclean exits.
        #
        # NULL source = a row predating the column. Read as 'clean', which is a
        # DELIBERATE UNDER-COUNT rather than a proof: transcript_import also wrote
        # ended_at then (a transcript's LAST MESSAGE, i.e. derived) and set no
        # source, so some legacy rows read clean when they are derived. Bounded
        # and historical; all THREE live writers stamp the column now —
        # end_session ('clean'), close_abandoned_sessions ('derived'), and
        # transcript_import ('derived'). "Both" was wrong twice over in a comment
        # whose own subject is a miscounted writer set (#533 r5). The count is
        # pinned by test_check_outbox_targets_ignores_derived_session_ends
        # rather than asserted here, so it cannot rot silently again.
        srow = conn.execute(
            "SELECT COUNT(*), MAX(ended_at) FROM sessions "
            "WHERE ended_at IS NOT NULL AND COALESCE(ended_at_source, 'clean') = 'clean'"
        ).fetchone()
    finally:
        conn.close()

    # DECLARED lanes, unioned with whatever the data shows. Deriving the list
    # from `GROUP BY target` alone (the previous version) meant a lane that has
    # NEVER enqueued a row produced NO RESULT AT ALL — reintroducing, one layer
    # up, exactly the absence-reads-as-healthy bug that `run_checks` goes to
    # trouble to prevent for raising probes.
    # Same store-age grace as the hook cohort: a store created minutes ago has
    # legitimately enqueued nothing, and reporting that as a failure would make
    # every fresh install red.
    age = _age(arow[0] if arow else None)
    store_is_new = age is not None and age < _NEW_STORE_GRACE
    newest_session = srow[1] if srow else None

    seen = {r[0] for r in rows}
    # A LANE WITH NO CONSUMER IS DORMANT, NOT UNOBSERVABLE (#469 review 4,
    # critical 2). `_thread_summary_target` answers WHICH lane; it does not
    # answer WHETHER anything drains it. On a DEFAULT install — standalone=True,
    # sync.adapter="noop" — `sync:thread_summary` is selected but has no live
    # consumer, so it correctly never enqueues. Judging that UNOBSERVABLE made
    # every fresh install fail on day 8, permanently, with remedy=None: a
    # verdict the operator cannot act on and cannot clear.
    #
    # That is the "teach them to ignore it" failure this module exists to
    # prevent, and it is worse than the gap it was covering. Under RULE 1
    # (techne#959) UNOBSERVABLE is a FAILING state — which is exactly why it
    # must be reserved for a rail that SHOULD be emitting and isn't, not spent
    # on one that is correctly idle.
    #
    # ASK THE CONFIG QUESTION, NOT THE PROCESS QUESTION. The obvious move is to
    # reuse `lifecycle._has_live_consumer` — the predicate `enqueue` gates on —
    # but it is subtly wrong HERE. On the team path it returns
    # `get_sink(...) is not None`, i.e. "is a sink REGISTERED IN THIS PROCESS".
    # That is true at enqueue time, because the writer registers first; it is
    # FALSE inside `techne doctor` AT THIS POINT: since techne#1172 doctor's
    # `check_sink_entry_points` registers every sink, but it is registered later in
    # this file and so runs after this check. Reordering them changes this answer.
    #
    # Reusing it therefore reported a TEAM install's never-enqueued lane as
    # "correctly idle" — masking the eight-week-dead-lane case this check exists
    # to catch, which is a worse bug than the one the no-consumer branch fixes.
    # Caught by a mutation sweep: UNOBSERVABLE->DORMANT survived, and writing
    # the test to kill it exposed this.
    #
    # The question here is topological, and answerable from config alone:
    #   Thread summaries always take the sync lane since techne#552, so the sync
    #   adapter is the only consumer that decides; "noop" means nothing drains.
    #
    # MIRRORS `lifecycle._has_live_consumer` AND MUST KEEP MIRRORING IT. This branched
    # on `config.standalone` and returned True for a team box — matching the OLD
    # lifecycle. After #552 that combination (standalone: false + sync.adapter: noop,
    # the typical team box, which routed via the sink and never needed a sync adapter)
    # produced a PERMANENT UNCLEARABLE FALSE FAILURE: lifecycle correctly enqueues
    # nothing while this believed a consumer existed, so the lane rendered
    # UNOBSERVABLE — a failing state under RULE 1 — with the operator's only escapes
    # being to configure a sync adapter they do not want or to set `standalone: true`.
    # The comment above exists to prevent exactly "a verdict the operator cannot act on
    # and cannot clear".
    # ONE import, above BOTH branches that state the prefix rule. The r3 nit was that
    # `missing.startswith("sink:")` re-spelled a constant imported 21 lines below it;
    # removing the duplicate in r4 deleted the hoisted copy rather than the lower one
    # and restored the literal. Hoisting the surviving import is the resolution that
    # satisfies both rounds — the literal is the copy that would go stale silently if
    # the constant ever moved, which is the failure mode this PR has spent four rounds
    # closing elsewhere. (Prudence, #714 r3 + r5.)
    from techne.lifecycle import retired_producer_targets
    from techne.sync.outbox import SINK_PREFIX, SYNC_PREFIX

    # Resolved ONCE, above BOTH loops, because they now share it — that sharing is the
    # fix: the declared-but-never-enqueued branch and the discovered-rows branch used to
    # answer "does this lane have a producer" independently, and disagreed. (#716)
    retired_here = retired_producer_targets(config)

    for missing in sorted(_declared_outbox_targets(config) - seen):
        # DERIVED FROM THE SAME PREDICATE AS THE DISCOVERED-ROWS LOOP BELOW, not
        # restated. This branch asked "does this lane have a producer" and the loop
        # below did not, so the SAME lane under the SAME config got opposite verdicts
        # decided only by whether it had history — dormant/ok with zero rows,
        # stale/ok=False with three drained ones. One predicate, one answer.
        #
        # The per-lane distinction that #714 r2 established is preserved inside
        # `retired_producer_targets`: a sink lane is declared only because something
        # feeds it, and `sync.adapter` does not govern `flush_sinks`. What changed is
        # where that reasoning lives. (Prudence, #714 r2, #716.)
        has_producer = missing not in retired_here
        if not has_producer:
            verdict = Verdict.DORMANT
            # HARDCODED, AND SAFE ONLY BY A COINCIDENCE WORTH WRITING DOWN. The
            # discovered-rows loop below reads `retired_here[target]`; this branch does
            # not, so the same lane under the same config yields two different DETAIL
            # strings depending on whether it has history. The verdicts agree — that was
            # the #716 finding — but the actionable payload still does not.
            #
            # It is accurate in every case it can currently be reached in. Verified by
            # sweeping the product of archivist x sync.adapter x standalone x
            # named-store and taking `_declared_outbox_targets & retired_producer_targets`
            # for each: EVERY overlap is `sync:thread_summary`, retired for the ADAPTER
            # reason, and there are none for any other lane or reason. (Prudence swept
            # the same property over a wider enumeration including loader-rejected
            # combinations and got the same answer; the overlap COUNT differs with the
            # enumeration, so the property is what is recorded here, not the number.)
            #
            # The structural reason: `sink:archivist_knowledge` is declared IFF the
            # binding is STORE and retired IFF it is not — the same call on the same
            # config — and `sink:archivist` is never declared at all.
            #
            # THAT IS THE SAME COINCIDENCE-OF-AGREEMENT THAT MAKES THE SINK MUTANT
            # UNKILLABLE, so it protects this line for the same reason and expires the
            # same way: THE MOMENT A FOURTH LANE IS BOTH DECLARED AND RETIRED FOR A
            # NON-ADAPTER REASON, THIS IS THE #716 r2 BUG AGAIN — a message re-derived
            # at the format site naming the wrong knob. Read `retired_here[missing]`
            # then. (Prudence, #716 r3.)
            detail = "no live consumer (sync.adapter=noop) — correctly idle"
            # "/ no registered sink" removed: after the per-lane fix this branch is
            # reachable only for `sync:` lanes, so that clause named a cause that
            # cannot produce this verdict. (Prudence, #714 r3.)
        elif store_is_new:
            verdict = Verdict.DORMANT
            detail = "store created <7d ago — nothing enqueued yet"
        else:
            verdict = Verdict.UNOBSERVABLE
            detail = "declared lane has NEVER enqueued anything"
        results.append(
            CheckResult(
                name=f"outbox:{missing}",
                subsystem="sync",
                kind=Kind.TEMPORAL,
                verdict=verdict,
                evidence=f"no outbox rows have ever existed for target={missing}",
                detail=detail,
            )
        )

    for target, total, last_synced, pending, last_enqueued in rows:
        pending = pending or 0

        # RETIRED LANES ARE DORMANT, NOT STALE. `enqueue()` rejects
        # un-namespaced targets, so a bare name is pre-#51 residue that
        # migration v10 deliberately left in place — its producer was retired
        # ten schema versions ago, and no operator action can ever clear it.
        # Reporting it as "producer may be dead" is technically true and
        # permanently un-actionable, which is the nag that teaches an operator
        # to skip the whole report (#469 review 3).
        # A LANE WITH NO PRODUCER UNDER THIS BINDING IS A RETIRED LANE (techne#552 part 3,
        # Prudence #714 r4). `sink:archivist` stopped having a producer when thread
        # summaries moved to the sync lane, but its delivered rows stay — deliberately,
        # so they remain drainable — so `GROUP BY target` keeps returning it forever
        # with MAX(created_at) frozen at the upgrade. Seven days later
        # _PRODUCER_SILENT_AFTER elapses and it takes the producer-silence branch
        # below: STALE, ok=False, remedy=None, feeding worst() and the banner headline.
        #
        # PERMANENT AND UNCLEARABLE, on exactly the cohort #552 exists to migrate. flush
        # and flush-sinks do not move MAX(created_at); prune-orphans cannot help since
        # the sink is now registered unconditionally; nothing deletes a synced row. The
        # only exit was hand-editing SQLite, destroying the rows this PR preserves.
        #
        # The rule and its reasoning were already twelve lines below, for un-namespaced
        # targets — "no operator action can ever clear it… the nag that teaches an
        # operator to skip the whole report". A lane retired by THIS release walked
        # straight past it because it is namespaced.
        # PENDING ROWS STILL COUNT. Retirement means "no new producer", NOT "ignore
        # what is owed" — a retired lane with an undrained backlog still needs
        # draining, and short-circuiting before the backlog branch would hide it. The
        # frozen-MAX(created_at) problem this fixes only arises once the lane is fully
        # drained, which is exactly when `pending == 0`.
        if target in retired_here and pending == 0:
            results.append(
                CheckResult(
                    name=f"outbox:{target}",
                    subsystem="sync",
                    kind=Kind.TEMPORAL,
                    verdict=Verdict.DORMANT,
                    last_fired=last_synced,
                    # THE REASON COMES FROM THE PREDICATE, not re-derived here. Each
                    # lane is retired by a DIFFERENT knob — release, archivist binding,
                    # sync adapter — and deriving the message at the format site named
                    # `archivist_binding` for all of them, so a store-bound box with a
                    # noop adapter was told its sync lane had "no producer under
                    # binding=store": the one knob it had set correctly, and not the one
                    # that retired the lane. `remedy` is None here, so these two strings
                    # are the whole actionable payload. (Prudence, #716 r2.)
                    evidence=f"target={target!r}: {retired_here[target]}",
                    detail=f"{retired_here[target]} ({total} delivered rows kept readable)",
                )
            )
            continue

        if not target.startswith((SINK_PREFIX, SYNC_PREFIX)):
            results.append(
                CheckResult(
                    name=f"outbox:{target}",
                    subsystem="sync",
                    kind=Kind.TEMPORAL,
                    verdict=Verdict.DORMANT,
                    last_fired=last_synced,
                    evidence=f"target={target!r} is not namespaced; enqueue() rejects it",
                    detail=f"retired lane ({total} legacy rows) — superseded, not broken",
                )
            )
            continue

        # TWO SIDES, NOT ONE. The backlog answers "is the DRAIN keeping up"; it
        # says nothing about whether the FILL still happens. `pending == 0` has
        # two opposite causes — sync is keeping up, or nothing is being produced
        # — and an earlier version short-circuited both to LIVE. That reported
        # `thread_summary` (140 rows, last enqueued 2026-06-10, 1321 threads
        # created since) as healthy: the very lane this check's docstring cites
        # as the motivating eight-week-dead incident (#469 review, finding 2).
        if pending == 0:
            # PRODUCER SILENCE IS MEASURED AGAINST SESSIONS, NOT THE CLOCK
            # (#469 review 3, finding 3). These lanes are fed at SESSION END, so
            # "nothing enqueued in 21 days" measured operator absence — a three
            # week holiday reported "producer may be dead" on a healthy lane,
            # the same wall-clock error as finding 1 at a larger scale.
            #
            # The honest signal is: sessions have happened SINCE the newest
            # enqueue. A session that ends without feeding a lane it should feed
            # is the actual evidence of a dead producer.
            enq = _parse_ts(last_enqueued)
            newest = _parse_ts(newest_session)
            if enq is not None and newest is not None and newest > enq + _PRODUCER_SILENT_AFTER:
                verdict = Verdict.STALE
                detail = (
                    f"{total} rows drained, but sessions have run since the last "
                    f"enqueue ({last_enqueued}) — producer may be dead"
                )
                remedy = None
            else:
                verdict = Verdict.LIVE
                detail = f"{total} rows, nothing owed"
                remedy = None
        elif target.startswith(SYNC_PREFIX) and config.sync.adapter == "noop":
            # noop PARKS rows deliberately rather than falsely marking them
            # synced, precisely so a real adapter can still take the backlog
            # (see sync/noop.py). Pending rows under noop are correct.
            #
            # ONLY FOR `sync:` LANES (#469 review 5, critical). `sync.adapter`
            # governs `flush(adapter)`, which touches `WHERE target LIKE
            # 'sync:%'` (outbox.py:112) and nothing else; `sink:` rows are
            # drained by `flush_sinks`. Excusing every lane on the adapter
            # setting therefore forgave a backlog the adapter was never
            # responsible for — and on the TEAM topology `sync.adapter` is
            # EXPECTED to stay "noop", so this was not a corner case but the
            # normal configuration of the very lane this check's docstring is
            # about. Probed: a 56-day-old pending `sink:archivist` row read
            # `dormant, ok=True :: 1 parked — sync.adapter = noop`.
            #
            # This is exactly the reasoning error recorded in knowledge#967 —
            # "pending + adapter=noop, therefore parked" — which was wrong about
            # three real rows in prose, and was wrong here in code for the same
            # reason: the noop explanation was applied to a lane it does not
            # govern.
            verdict = Verdict.DORMANT
            detail = f"{pending} parked — sync.adapter = noop"
            remedy = None
        else:
            verdict = temporal_verdict(last_synced, timedelta(days=14))
            detail = f"{pending} pending, {total} rows"
            remedy = "techne outbox flush" if verdict is not Verdict.LIVE else None

        results.append(
            CheckResult(
                name=f"outbox:{target}",
                subsystem="sync",
                kind=Kind.TEMPORAL,
                verdict=verdict,
                last_fired=last_synced,
                expected_interval=timedelta(days=14),
                evidence=(
                    f"outbox target={target}: {total} rows, {pending} pending, "
                    f"last_enqueued={last_enqueued or '(never)'}, "
                    f"last_synced={last_synced or '(never)'}"
                ),
                detail=detail,
                remedy=remedy,
            )
        )
    return results


def _dotted_attr(obj, dotted: str):
    """Read an EGRESS_KEYS path off a Config. Missing intermediates read as None."""
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


#: Governed keys whose VALUE is safe to print. Everything else reports presence
#: only. DEFAULT-DENY IS THE WHOLE POINT — see :func:`_describe_destination`.
#:
#: MEMBERSHIP IS A STRUCTURAL ARGUMENT, NOT A JUDGEMENT CALL, and that is the
#: property to preserve when adding to it. `standalone` is a bool. The three
#: model ids cannot carry a credential because the key that would — their
#: sibling `llm_api_base` — is denied, and a URL carries credentials in its
#: userinfo (``https://user:pass@host``).
#:
#: `archivist_db_path` WAS here and was removed. It is a local path today and a
#: DSN after techne#87, so it was kept printable behind a `("://", "@")` shape
#: test — a two-element hand-kept DENY-list living inside an allow-list, i.e.
#: the discipline of this very constant inverted one layer down. It failed both
#: ways under execution: libpq keyword/value form
#: (``host=db password=pa55 user=x``) contains neither substring and printed the
#: password, while ``model: vertex_ai/claude-3-5-sonnet@20240620`` tripped the
#: guard and rendered "model set" — the useless string, on the one key where
#: naming the destination is the entire job. The fix was to DELETE the
#: heuristic, not sharpen it. (Prudence, PR #508 r5, found while approving.)
_PRINTABLE_DESTINATIONS = frozenset(
    # `archivist` joins by the same structural argument that admits `standalone`,
    # its predecessor: its entire value space is four validated literals, and it is
    # the one key that NAMES the destination. Default-deny made omitting it safe but
    # uninformative — doctor rendered "archivist set" for a four-value enum.
    {"model", "reflect.model", "conflict.model", "standalone", "archivist"}
)


def _describe_destination(key: str, value, default=None) -> str:
    """One short operator-facing phrase per departed-from-default key.

    NEVER RENDERS A SECRET, AND THAT IS ENFORCED BY DEFAULT-DENY RATHER THAN BY
    THIS SENTENCE. An unrecognised key reports presence only; the informative
    renderings are the explicit exceptions.

    The first version inverted this — four hardcoded safe cases with
    ``f"{key}={value}"`` as the fallback — and it was a hazard of exactly the
    shape the derived key list had just been built to remove. The derivation
    GUARANTEES a new governed key is reported; the deny-list did NOT guarantee it
    is reported safely, so the next round to add a credential-bearing key (the
    way `stores` was added) would have printed it into `techne doctor` output
    with no diff to review. Demonstrated by Prudence on PR #508 r3, simulated
    against a future `sync.auth` entry: `sync.auth=sk-BRAND-NEW-SECRET`.
    A forgotten key is now merely uninformative instead of a leak.

    WHERE THIS LANDS — three surfaces, and the third is the one that makes
    default-deny load-bearing rather than tidy:
      1. `techne doctor`'s detail column (`cli.py` prints `r.detail` for every
         verdict), which gets pasted into issues.
      2. NOT the SessionStart banner — `format_banner` renders check NAMES only,
         and only for non-live/dormant verdicts, so `detail` reaches it on no
         path. An earlier revision claimed it did; the conclusion held but the
         claim was false, written in the round dedicated to removing false
         claims and contradicting the card in the same commit.
      3. THE CONSTRAINED AGENT'S OWN CONTEXT. `mcp_server.py` returns
         `collect_health(config)` verbatim, `health.py` copies `r.detail` into
         it, and this check carries no `@expensive` marker — so the string
         reaches the very agent this seam exists to constrain. Nothing leaks
         today, and that is default-deny earning its keep on a surface no
         enumeration here had noticed. (Prudence, PR #508 r4.)
    """
    if key == "sync":
        # DERIVED FROM THE DATACLASS, not from a hand-kept name list. The list
        # version read four fields and SyncConfig then had five: `extra`, a
        # catch-all for every unrecognised key under `sync:`, so
        # `sync: {token: ghp_LIVE_SECRET}` departed from default and rendered a
        # bare `sync.adapter=noop`. (Prudence, PR #508 r4.) techne#1074 removed
        # `extra`, `auth` and `audience` because no adapter call reads them; an
        # unrecognised `sync:` key now stays on disk and selects nothing.
        import dataclasses

        base = default if default is not None else type(value)()
        departed = [
            f.name
            for f in dataclasses.fields(value)
            if f.name != "adapter" and getattr(value, f.name) != getattr(base, f.name, None)
        ]
        suffix = f" (+{', '.join(departed)})" if departed else ""
        return f"sync.adapter={value.adapter}{suffix}"
    if key in ("connections", "stores"):
        return f"{len(value)} {key} entr{'y' if len(value) == 1 else 'ies'}"
    if key in _PRINTABLE_DESTINATIONS:
        return f"{key}={value}"
    return f"{key} set"


@register_check
def check_sink_entry_points(config) -> CheckResult:
    """Installed packages' outbox sinks: loaded, refused, failed, and stranded rows
    (techne#1172).

    Runs discovery the way a drain does, then names every pending ``sink:`` target
    that still has no registered sink. A refused entry point with nothing waiting
    is a deliberate policy and reads DORMANT; rows waiting on a sink nobody
    registered read BROKEN, because they will never drain on their own and
    ``prune-orphans`` will not touch them while an entry point is refused.
    """
    from techne.core.database import connect
    from techne.sync.sinks import ensure_sinks_registered, get_sink

    found = ensure_sinks_registered(config)
    conn = connect(config.db_path)
    try:
        targets = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT target FROM outbox WHERE status = 'pending' "
                "AND target LIKE 'sink:%' ORDER BY target"
            )
        ]
    finally:
        conn.close()
    stranded = [t for t in targets if get_sink(t) is None]
    parts = []
    if found.loaded:
        parts.append(f"loaded: {', '.join(sorted(found.loaded))}")
    if found.refused:
        parts.append(f"refused (not in sync.allowed_sinks): {', '.join(sorted(found.refused))}")
    if found.failed:
        parts.append("failed: " + "; ".join(f"{k} ({v})" for k, v in sorted(found.failed.items())))
    if stranded:
        parts.append(f"pending rows with no sink: {', '.join(stranded)}")
    if stranded:
        verdict = Verdict.BROKEN
        if found.refused or found.failed:
            remedy = (
                "Allow the package's entry point in `sync.allowed_sinks`, or fix the one "
                "that failed (`techne sinks list` shows each); rows for a target you know "
                "is dead can be pruned with `techne outbox prune-orphans --target <target>`."
            )
        else:
            # No package could own them: these are true orphans, and prune-orphans
            # is not blocked here (Prudence, PR #1173).
            remedy = "No installed package claims these targets: `techne outbox prune-orphans`."
    else:
        verdict = Verdict.LIVE if found.loaded else Verdict.DORMANT
        remedy = None
    return CheckResult(
        name="sinks:entry-points",
        subsystem="outbox",
        kind=Kind.FAIL_OPEN,
        verdict=verdict,
        evidence=(
            f"entry points: {len(found.loaded)} loaded, {len(found.refused)} refused, "
            f"{len(found.failed)} failed; pending sink targets: {len(targets)}, "
            f"with no sink: {len(stranded)}"
        ),
        detail="; ".join(parts) or "no sink entry points installed",
        remedy=remedy,
    )


@register_check
def check_recall_sources(config) -> CheckResult:
    """Installed packages' recall sources, and whether `recall.order` names any
    source recall cannot use (techne#1170).

    An ordered source that is unknown, refused or failed to load reads BROKEN: the
    operator asked recall to consult it and every recall reports it unavailable.
    A refused entry point the order does not name is a deliberate policy (DORMANT);
    loaded sources read LIVE.
    """
    from techne.core import recall_sources

    found = recall_sources.discover_sources(config)
    builtin = set(recall_sources.BUILTIN_SOURCES)
    ordered = [e.name for e in config.recall.order]
    unusable = [n for n in ordered if n not in builtin and n not in found.loaded]
    parts = []
    if found.loaded:
        parts.append(f"loaded: {', '.join(sorted(found.loaded))}")
    if found.refused:
        parts.append(f"refused (not in recall.allowed_sources): {', '.join(sorted(found.refused))}")
    if found.failed:
        parts.append("failed: " + "; ".join(f"{k} ({v})" for k, v in sorted(found.failed.items())))
    if unusable:
        parts.append(f"recall.order names unusable source(s): {', '.join(unusable)}")
    if unusable:
        verdict = Verdict.BROKEN
        if unusable == [recall_sources.TEAM]:
            # `team` is the sync adapter's read-back, not a plugged-in source: the
            # allow-list does not govern it (techne#1171).
            remedy = (
                "`team` comes from the sync adapter: it must load (`sync.adapter`, and "
                "`sync.allow_unlisted_adapter` for a non-built-in) and declare "
                "`supports_pull = True`; otherwise remove `team` from `recall.order`."
            )
        else:
            remedy = (
                "Allow the entry point in `recall.allowed_sources`, fix the one that "
                "failed, or remove the name from `recall.order`."
            )
    else:
        verdict = Verdict.LIVE if found.loaded else Verdict.DORMANT
        remedy = None
    return CheckResult(
        name="recall:sources",
        subsystem="recall",
        kind=Kind.FAIL_OPEN,
        verdict=verdict,
        evidence=(
            f"entry points: {len(found.loaded)} source(s) loaded, {len(found.refused)} "
            f"refused, {len(found.failed)} failed; recall.order: {len(ordered)} entr(y/ies), "
            f"{len(unusable)} unusable"
        ),
        detail="; ".join(parts) or "no recall source entry points installed",
        remedy=remedy,
    )


@register_check
def check_config_trust(config) -> CheckResult:
    """Is the file that SELECTS egress writable by the agent it constrains?

    A FAIL_OPEN check. techne's memory-egress surface is closed by default, but
    ``config.yaml`` chooses that default and lives in the agent's own writable
    tree — so the control it appears to provide is only as strong as the file
    permissions nobody was checking (techne#393).

    THE THREE VERDICTS ARE NOT A SEVERITY LADDER, they are three different
    facts, and conflating them is what made this worth a check rather than a
    boolean:

    * ``locked``   → LIVE. An operator layer exists and this process cannot
      rewrite it. The evidence names the owning uid and mode so the claim is
      re-derivable rather than trusted.
    * ``unlocked`` → BROKEN. A layer was DEPLOYED and is NOT HOLDING — someone
      intended a boundary and does not have one. This is the only state that
      should alarm, and it is the one a naive "is egress closed?" check would
      have rendered green — because the keys such a check reads (`sync.adapter`,
      `llm_api_base`) are sitting at their built-in values in exactly that state.
      Closed-by-accident and closed-by-enforcement are different states and only
      one of them survives an adversary. (The older wording here said "failing
      closed means egress IS closed at that moment", which this module's own
      EGRESS_KEYS note contradicts: `model` defaults to a live destination. The
      argument survives the rewording; that sentence did not.)
    * ``absent``   → DORMANT, with the reason named. A single-user install that
      never deployed a layer is not lying about anything, and DORMANT sorts with
      LIVE so it stays out of the banner headline. It is still reported, because
      "no boundary was ever deployed" is exactly the fact an operator planning a
      multi-agent rollout needs, and silence would read as safety.

    ABSENT IS REPORTED ALONGSIDE WHAT config.yaml ACTUALLY SELECTS. "No operator
    layer" is benign on an install whose destinations are all at their built-in
    values and is the whole vulnerability on one that has already set
    ``sync.adapter`` or ``llm_api_base`` — same verdict, opposite significance,
    so the detail must distinguish them or the check has not said anything.

    THE PROBE IS LIVE, and does not read ``config.egress_trust``. The first
    version of this check did, and it was wrong in a way worth recording: a
    ``Config`` built by direct construction carries no verdict, so the check
    answered UNKNOWN — a check whose result depends on how its input was
    CONSTRUCTED is not measuring the subsystem, it is measuring its caller. The
    question "is a boundary in place" is a property of the filesystem and is
    answerable directly, on one code path that always runs.

    The recorded verdict is still consulted for ONE thing: whether it disagrees
    with the live probe. A layer deployed after this process loaded its config
    is not yet in force here — the running process is still honoring the
    resolution it made at load time — so reporting the live LOCKED alone would
    be a false green of exactly the kind this module exists to prevent. The
    disagreement is its own verdict, with both facts named.
    """
    from techne.config import Config, techne_home
    from techne.security import config_trust

    # Resolved against techne_home() rather than config.data_dir SO THAT IT
    # MATCHES WHAT Config.load DID. A different home here would manufacture
    # disagreements out of a lookup difference and report them as drift.
    verdict = config_trust.evaluate_lock(config_trust.resolve_operator_config(techne_home()))
    recorded = getattr(config, "egress_trust", None)

    # WHICH TIER is in force is a security fact, not a detail: only
    # SYSTEM_OPERATOR_CONFIG is unreachable by the agent's environment. THREE-
    # VALUED, not two — the previous binary split labelled the <techne_home>
    # tier "env-selected" and then told the operator to defend against
    # `env -u TECHNE_OPERATOR_CONFIG`, which does nothing to that tier. Its
    # lever is TECHNE_HOME (config.py:techne_home), and handing someone the
    # wrong bypass is worse than handing them none. (Prudence, PR #508 r2.)
    if verdict.path == config_trust.SYSTEM_OPERATOR_CONFIG:
        tier, tier_lever = "system", None
    elif (
        verdict.path is not None
        and verdict.path
        == Path(os.environ.get(config_trust.OPERATOR_CONFIG_ENV, "\0")).expanduser()
    ):
        tier, tier_lever = "env-var", f"env -u {config_trust.OPERATOR_CONFIG_ENV}"
    else:
        tier, tier_lever = "techne-home", "TECHNE_HOME"

    # Which destination keys does this install actually depart from defaults on?
    # BOTH THE KEY LIST AND THE BASELINE ARE DERIVED — the list from EGRESS_KEYS,
    # the values from a fresh Config. The previous version derived only the
    # baseline while hardcoding five of the (then nine) keys, and its comment
    # claimed drift-proofing it did not have. It then drifted immediately: the
    # round that added `stores` to the governed set did not add it here, so an
    # install with a custom store DSN reported "all destinations at defaults" —
    # the benign string — which is the one thing this line exists not to do.
    d = Config()
    live_destinations = [
        _describe_destination(key, value, _dotted_attr(d, key))
        for key in config_trust.EGRESS_KEYS
        if (value := _dotted_attr(config, key)) != _dotted_attr(d, key)
    ]
    selected = (
        ", ".join(live_destinations) if live_destinations else "all destinations at built-in values"
    )

    path = verdict.path
    if recorded is not None and recorded.state != verdict.state:
        return CheckResult(
            name="config:trust",
            subsystem="security",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.STALE,
            evidence=f"loaded as '{recorded.state}', now '{verdict.state}' at {path}",
            detail=(
                f"operator layer changed since this config was loaded — this process is "
                f"still honoring the '{recorded.state}' resolution"
            ),
            remedy="restart the techne MCP server / re-run the command to reload config",
        )

    if verdict.state == config_trust.LOCKED:
        return CheckResult(
            name="config:trust",
            subsystem="security",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.LIVE,
            evidence=f"{path} [{tier} tier]: {verdict.reason}",
            detail=(
                f"egress pinned by operator layer ({selected})"
                + (
                    ""
                    if tier_lever is None
                    else f" — WARNING: {tier} tier, an agent can bypass it by changing "
                    f"`{tier_lever}` in its own environment; only "
                    f"{config_trust.SYSTEM_OPERATOR_CONFIG} is a real lock"
                )
            ),
        )

    if verdict.state == config_trust.UNLOCKED:
        return CheckResult(
            name="config:trust",
            subsystem="security",
            kind=Kind.FAIL_OPEN,
            verdict=Verdict.BROKEN,
            evidence=f"{path}: {verdict.reason}",
            detail=(
                "operator layer present but this process could rewrite it — "
                "egress forced to the BUILT-IN DEFAULT destinations (not silence: "
                "the default model still routes to its provider), config.yaml ignored "
                "for egress but preserved on disk"
            ),
            remedy=(
                # The generic chown advice CANNOT clear the euid-0 branch — root
                # can rewrite anything, so no ownership makes it lockable. A
                # remedy that cannot clear its own finding is the never-green
                # defect this module was fixed for once already, so this branches
                # on the reason. techne's own Dockerfile has no USER directive
                # and mounts /root/.techne, so the shipped image lands here.
                "run techne as a non-root user (the Dockerfile has no USER "
                "directive); a root process can never honor an operator layer"
                if "euid 0" in verdict.reason
                else f"chown to an operator uid and chmod 0644: {path} "
                "(and place it outside the agent's writable tree)"
            ),
        )

    return CheckResult(
        name="config:trust",
        subsystem="security",
        kind=Kind.FAIL_OPEN,
        verdict=Verdict.DORMANT,
        evidence=f"no operator layer at {config_trust.SYSTEM_OPERATOR_CONFIG}, "
        f"${config_trust.OPERATOR_CONFIG_ENV}, or <techne_home>/config.operator.yaml",
        detail=f"egress selected by agent-writable config.yaml ({selected})",
    )


#: How young an unembedded observation/thread row may be before it counts
#: against `vectors:coverage` (techne#704). The SessionStart chain runs the
#: backfill just before the banner, so anything older than this at banner time
#: is a row that backfill missed or never ran for; anything younger is the
#: capture tail of the sessions that are live right now.
_VECTOR_TAIL_GRACE = timedelta(minutes=15)


@register_check
def check_vector_coverage(config) -> CheckResult:
    """Are knowledge rows actually embedded?

    A COVERAGE check, not a staleness one. On 2026-08-03 the existing staleness
    predicate reported every row fresh (`vec_stale = 0`) while ~177 of 758 rows
    had no vector at all — "not stale" and "present" are different questions, and
    only the second one tells you hybrid search is whole.

    BOTH COUNTS ARE SCOPED TO LIVE ROWS, and that scoping is the whole point of
    techne#496. ``backfill_vectors`` embeds only ``superseded_by IS NULL`` and
    additionally PURGES the embeddings of superseded rows, because search_vec
    filters superseded results only AFTER the k-nearest cut, so a stale embedding
    occupies a top-k slot and starves a live one (techne#168). Taking the
    denominator from ALL knowledge therefore counted every superseded row as
    permanently "missing": on the live store, 16 of 786, verdict STALE forever,
    offering a remedy that provably embedded nothing (measured — a second
    consecutive backfill returned `knowledge: 0` and the warning was unchanged).
    It also grew monotonically, since superseding a row is NORMAL operation, so
    the warning would diverge from reality for as long as the store was used.

    A CHECK THAT CAN NEVER GO GREEN TRAINS THE OPERATOR TO IGNORE IT — which is
    precisely the failure this check was added to fix, arriving from the opposite
    direction. Prefer a narrower question you can answer honestly over a broader
    one you cannot.

    COUNT ALL THREE PARTS OF THAT POPULATION, NOT TWO. ``backfill_vectors``
    re-embeds ``(NOT in vec OR vec_stale = 1) AND superseded_by IS NULL``
    (search.py) and purges superseded embeddings. Scoping the denominator while
    counting only "is it present" left `vec_stale = 1` — set on ANY in-place
    content edit — covered by no check in this module at all, and it produced the
    mirror-image false green: 2/2, 100%, remedy None, while a backfill had real
    work to do (#497 review). Each condition is counted separately, named in the
    detail, and fails the check, because the SAME remedy genuinely fixes each.

    DANGLING VECTORS ARE REPORTED BUT DO NOT FAIL, AND THAT ASYMMETRY IS THE
    POINT. A vec rowid whose knowledge row was HARD-DELETED (``knowledge.delete``
    and the pattern cleanup both delete through a plain ``connect()``, which
    cannot touch a vec0 table) is a real defect — but the purge deletes
    ``WHERE rowid IN (SELECT id FROM knowledge WHERE superseded_by IS NOT NULL)``,
    which selects FROM knowledge, so a rowid with no knowledge row behind it is
    never matched. VERIFIED: purge removed 0 of 2 in that state.

    THAT REASONING EXPIRED WHEN #408 SHIPPED `_purge_orphaned_vec`. There IS a
    remedy now, so "failing would attach a remedy that cannot clear it" no longer
    holds. `dangling` is still excluded from `healthy`, but for a different and
    smaller reason: folding it in changes doctor output on live stores and
    belongs in its own change. It remains surfaced in the detail and evidence.

    AND THE BIGGER GAP: this counts dangling rows for `knowledge_vec` ONLY.
    #408's headline mechanism — threads rotating rowid on INSERT OR REPLACE, one
    stranded embedding per summary update, unbounded — is counted by no check at
    all. The leak is fixed; its observability is not.
    """
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        live = "superseded_by IS NULL"
        total = conn.execute(f"SELECT COUNT(*) FROM knowledge WHERE {live}").fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
        try:
            in_vec = "id IN (SELECT rowid FROM knowledge_vec_rowids)"
            embedded = conn.execute(
                f"SELECT COUNT(*) FROM knowledge WHERE {live} AND {in_vec}"
            ).fetchone()[0]
            stale = conn.execute(
                f"SELECT COUNT(*) FROM knowledge WHERE {live} AND {in_vec} AND vec_stale = 1"
            ).fetchone()[0]
            orphans = conn.execute(
                f"SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NOT NULL AND {in_vec}"
            ).fetchone()[0]
            dangling = conn.execute(
                "SELECT COUNT(*) FROM knowledge_vec_rowids"
                " WHERE rowid NOT IN (SELECT id FROM knowledge)"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 — vec tables absent = channel not enabled
            # DORMANT MEANS "OFF BY CONFIGURATION", AND IT MUST NAME THE KEY THAT
            # DISABLED IT (checks.py's own Verdict docs). On an install where vectors
            # were deliberately turned ON — the extra installed, 85-290 MB paid for —
            # and the weights simply are not on disk, nothing disabled anything: the
            # channel is broken, not dormant, and the operator is told neither.
            #
            # After techne#708 `search_vec` degrades silently to FTS instead of hanging,
            # and `backfill_vectors` is the ONLY call left in the package that may
            # download. So without this, a fresh install is permanently FTS-only and the
            # doctor reports a deliberately-off channel. (Prudence, #728 review.)
            #
            # A FILESYSTEM PROBE, NEVER A BACKEND BUILD. techne#361 pins that this path
            # must not construct the model; `model_cached` answers by looking at the
            # cache, and returns None when it cannot tell rather than guessing False.
            detail = "vector channel not initialised (FTS-only search)"
            remedy = None
            try:
                from techne.core import embeddings as _emb

                # THE REMEDY IS GATED ON `backend_available()`, AND `model_cached()`
                # ONLY PICKS THE SENTENCE (Prudence, #728 r2 suggestion). Gating the
                # remedy on `model_cached() is False` cost ONE thing, and this comment
                # claimed TWO until #728 r4. The real cost: an install with the extra
                # present, the weights cached and no vec tables — a fresh TECHNE_HOME on
                # a machine whose HF cache is already warm — is just as permanently
                # FTS-only and got no remedy at all.
                #
                # THE SECOND "COST" COULD NOT OCCUR. "A probe that cannot tell (`None`)
                # silenced the doctor entirely" describes an unreachable state:
                # `model_cached` returns None only when `_select_backend` raises, which
                # happens only when neither backend imports — and this branch has
                # already gated on `backend_available()`. Both cards retracted it and
                # this was the last copy, which is the shape the retraction is about.
                #
                # `backend_available()` is the honest expression of "the operator
                # turned this on"; `model_cached()` is only the REASON it is idle.
                if _emb.backend_available():
                    cached = _emb.model_cached(config)
                    if cached is False:
                        detail = (
                            "embedding backend installed, model weights not cached; "
                            "recall is FTS-only until they are fetched"
                        )
                    else:
                        # `True` (warm cache, no vec tables yet) and `None` (could not
                        # tell) share a sentence, because neither licenses claiming the
                        # weights are missing — the distinction the None-not-False rule
                        # exists to protect.
                        detail = (
                            "embedding backend installed but no vectors written yet; "
                            "recall is FTS-only until a backfill runs"
                        )
                    remedy = "techne vectors backfill"
            except Exception:  # noqa: BLE001 — a probe must never fail a health check
                pass
            return CheckResult(
                name="vectors:coverage",
                subsystem="search",
                kind=Kind.COVERAGE,
                verdict=Verdict.DORMANT,
                evidence="knowledge_vec_rowids absent",
                detail=detail,
                remedy=remedy,
            )
    finally:
        conn.close()

    # No clamp. Not because the old `max(..., 0)` was vestigial — it FIRED, in a
    # reachable state: hard-deleting an embedded row leaves the raw vec count
    # EXCEEDING the knowledge count (measured: total - embedded = -2), and the
    # clamp rendered those dangling vectors as a clean 0. It was guarding, badly.
    # The new code is immune for a better reason: `embedded` carries the same
    # `live` predicate as `total`, so it is a true subset by construction, and
    # dangling rows cannot enter it at all — they are counted explicitly instead.
    covered = embedded - stale
    missing = total - embedded
    # "and current" is load-bearing: this numerator is `covered` (embedded AND not
    # vec_stale), while `evidence` reports the raw `embedded`. Without the phrase a
    # reader comparing the two lines sees 1/6 against embedded=3 and reads a
    # contradiction — and evidence exists precisely so a finding can be re-derived
    # (#497 review, nit 1).
    # NO PERCENTAGE FOR AN EMPTY KNOWLEDGE TABLE (techne#942). This printed
    # "0/0 live knowledge embedded and current (100%)" on a store holding 35,096
    # fully embedded observations and no knowledge: true, and it told the operator
    # nothing about either. The empty case names what IS there instead, below,
    # once the sibling counts exist.
    if total:
        pct = covered / total * 100
        detail = f"{covered}/{total} live knowledge embedded and current ({pct:.0f}%)"
    else:
        detail = "no live knowledge yet"
    if missing:
        detail += f", {missing} missing"
    if stale:
        detail += f", {stale} stale (edited since embedding)"
    if orphans:
        detail += f", {orphans} superseded row(s) still embedded"
    if dangling:
        detail += f", {dangling} dangling vector(s) with no knowledge row (techne#408)"
    if not (missing or stale or orphans) and superseded:
        detail += f" ({superseded} superseded, correctly excluded)"
    # `dangling` still absent from `healthy`, but NOT for the old reason — #408
    # shipped the remedy, so "no remedy clears it" is false as of that PR. Left
    # non-failing deliberately: flipping it changes doctor output on live stores
    # and belongs in its own change, not smuggled into the PR that made it
    # possible. It IS surfaced in the detail, so the operator sees the symptom
    # and now has `techne vectors backfill` to act on.
    #
    # KNOWN GAP, and it is the bigger one: this counts dangling rows for
    # knowledge_vec ONLY. #408's headline mechanism — threads rotating rowid on
    # INSERT OR REPLACE, one stranded embedding per summary update, unbounded —
    # is counted by no check at all. The leak is fixed; its observability is not.
    # COUNT THE WHOLE POPULATION THE REMEDY ACTS ON (techne#635). This counted
    # `knowledge` alone while `backfill_vectors` embeds knowledge, OBSERVATIONS and
    # THREADS — so the card's own capitalised rule, "A CHECK MUST COUNT THE
    # POPULATION ITS REMEDY ACTS ON" (docs/arch/liveness.md), was violated by the
    # check that quotes it. Measured on the live store when filed: 450 observations
    # and 14 threads unembedded, invisible to the check that exists to catch this.
    #
    # THE SILENT-GREEN PATH IS SPECIFIC, not theoretical: work heavily for weeks
    # without promoting knowledge and knowledge coverage stays high, so doctor
    # stays green while OBSERVATION coverage rots. Observations are the population
    # `per_turn_recall` actually reads, so recall degrades to FTS-only for all
    # recent work with no signal anywhere. That is the 2026-07-16 incident (0 of
    # 11,000 observations embedded) waiting to recur.
    #
    # Simpler counts than knowledge deliberately: observations are append-only and
    # threads rotate their rowid, so neither carries `vec_stale` or supersession —
    # "present or not" IS the whole question for them, and inventing a richer
    # predicate here would be a second definition of coverage.
    #
    # A GRACE WINDOW FOR THE NEWEST ROWS (techne#704). `post_tool_use` captures
    # every Bash call as an observation and only `backfill_vectors` embeds one, so
    # the remedy's own invocation, the doctor run that re-checks it and the query
    # that inspects the result each land an unembedded row. The check could not be
    # relied on to read clean during a live session, and a check its own remedy
    # cannot clear trains the reader to skip the banner. Rows younger than
    # `_VECTOR_TAIL_GRACE` are therefore PENDING: counted and named in the detail,
    # left out of the verdict. Age, not a backfill watermark, because a watermark
    # freezes when the backfill stops running (the #814 incident: the SessionStart
    # segment was deleted by every `techne init`), and every row after it would
    # then read as pending forever — a silent green. Age keeps that backlog loud:
    # 883 observations behind is far outside any window.
    #
    # Threads too, keyed on `updated_at`: a summary re-save re-inserts the row, so
    # the threads lane leaves a seconds-old tail exactly as observations do. A
    # timestamp SQLite cannot parse yields NULL, which counts as a real gap.
    sibling_gaps: dict[str, int] = {}
    sibling_pending: dict[str, int] = {}
    sibling_counts: dict[str, tuple[int, int]] = {}
    grace = f"-{int(_VECTOR_TAIL_GRACE.total_seconds())} seconds"
    conn = connect(config.db_path)
    try:
        for tbl, id_col, ts_col in (
            ("observations", "id", "timestamp"),
            ("threads", "rowid", "updated_at"),
        ):
            try:
                tot = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                emb = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    f"WHERE {id_col} IN (SELECT rowid FROM {tbl}_vec_rowids)"
                ).fetchone()[0]
                pending = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    f"WHERE {id_col} NOT IN (SELECT rowid FROM {tbl}_vec_rowids) "
                    f"AND julianday({ts_col}) > julianday('now', ?)",
                    (grace,),
                ).fetchone()[0]
            except Exception:  # noqa: BLE001 — that table's vec channel is absent
                continue
            sibling_counts[tbl] = (emb, tot)
            if tot - emb - pending > 0:
                sibling_gaps[tbl] = tot - emb - pending
            if pending:
                sibling_pending[tbl] = pending
    finally:
        conn.close()
    if total:
        for tbl, gap in sibling_gaps.items():
            detail += f", {gap} {tbl} unembedded"
    else:
        # With no knowledge, the siblings ARE the vector channel, so report their
        # coverage outright rather than only their gaps (techne#942).
        present = [
            f"{tbl} {emb}/{tot} embedded" for tbl, (emb, tot) in sibling_counts.items() if tot
        ]
        detail += f"; {', '.join(present)}" if present else "; nothing to embed yet"
    if sibling_pending:
        minutes = int(_VECTOR_TAIL_GRACE.total_seconds() // 60)
        detail += (
            "; "
            + ", ".join(f"{n} {tbl}" for tbl, n in sibling_pending.items())
            + f" pending (under {minutes} min old — embedded at the next session start)"
        )

    healthy = missing == 0 and stale == 0 and orphans == 0 and not sibling_gaps
    return CheckResult(
        name="vectors:coverage",
        subsystem="search",
        kind=Kind.COVERAGE,
        verdict=Verdict.LIVE if healthy else Verdict.STALE,
        evidence=(
            f"live knowledge={total}, embedded={embedded}, stale={stale}, "
            f"superseded={superseded}, superseded-but-embedded={orphans}, "
            f"dangling={dangling}, unembedded-within-grace="
            f"{sum(sibling_pending.values())}"
        ),
        detail=detail,
        remedy=None if healthy else "techne vectors backfill",
    )


#: HOW EACH STORE RECORDS ITS SCHEMA VERSION (techne#676).
#:
#: This registry exists because the check below is impossible without it, and
#: writing it down uncovered that a naive version of this check would REPORT
#: THREE FALSE PROBLEMS on a healthy machine. `PRAGMA user_version` reads 0 on
#: techne.db, archivist.db and librarian.db — not because they are behind, but
#: because they do not use that mechanism at all. Their real stamps read 24, 24
#: and "none". A check comparing the pragma to a code constant across every db
#: would have flagged all three and been ignored within a week.
#:
#: `migrator.VersionIO` made the mechanism pluggable (techne#136) so core could
#: keep its audited `schema_version` table while domains use the zero-ceremony
#: pragma. That was the right call, and the cost is that "what version is this
#: store" has three different answers depending on the file. The knowledge was
#: spread across database.py, architect.py, codex.py and stores.py, and nothing
#: held it in one place — so nothing could ask the question uniformly.
#:
#: Entries are (label, filename-or-glob, mechanism, code-constant-getter).
#: mechanism: "table" = MAX(version) FROM schema_version | "pragma" =
#: PRAGMA user_version | "none" = purely additive, no ladder, nothing to compare.
_VERSION_REGISTRY: tuple[tuple[str, str, str, str | None], ...] = (
    ("core", "techne.db", "table", "techne.core.database:SCHEMA_VERSION"),
    ("archivist", "archivist.db", "table", "techne.core.database:SCHEMA_VERSION"),
    ("architect", "architect.db", "pragma", "techne.core.architect:ARCHITECT_VERSION"),
    ("codex", "codex/*.db", "pragma", "techne.core.codex:CODEX_VERSION"),
    # librarian.db is migrated by SqliteStore.migrate — executescript, additive
    # only, no ladder and no stamp. Nothing to compare, and saying so beats
    # reporting a 0 that means "unused field".
    ("librarian", "librarian.db", "none", None),
)


def _code_version(ref: str) -> int | None:
    """Resolve 'module:CONSTANT' at call time, so a renamed constant fails here
    rather than silently reading as 0."""
    import importlib

    mod_name, _, attr = ref.partition(":")
    try:
        return int(getattr(importlib.import_module(mod_name), attr))
    except Exception:  # noqa: BLE001 — a missing constant is a finding, not a crash
        return None


def _store_version(path: Path, mechanism: str) -> int | None:
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        if mechanism == "pragma":
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@register_check
def check_schema_versions(config) -> list[CheckResult]:
    """Does each store's version stamp match the code that migrates it? (techne#676)

    A STORE AHEAD OF ITS CODE IS SILENTLY UN-MIGRATABLE. `guarded_migrate` used to
    fold `current > target` into `current >= target` and return without a word;
    architect.db sat at user_version 4 against a code constant of 3 for at least
    six days, skipping every migration, and nothing could see it. The guard is now
    loud on stderr — this is the half a human reads on purpose.

    A store BEHIND its code is normal and reports LIVE: that is the ladder waiting
    to run, not a fault. Only AHEAD is a finding, because no step above the code's
    version has ever existed, so the stamp cannot describe the store's real shape.
    """
    results: list[CheckResult] = []
    root = Path(config.data_dir)
    for label, pattern, mechanism, const_ref in _VERSION_REGISTRY:
        paths = sorted(root.glob(pattern)) if "*" in pattern else [root / pattern]
        paths = [p for p in paths if p.exists()]
        if not paths:
            continue
        if mechanism == "none":
            results.append(
                CheckResult(
                    name=f"schema:{label}",
                    subsystem="migrator",
                    kind=Kind.FRESHNESS,
                    verdict=Verdict.DORMANT,
                    evidence=f"{pattern}: additive-only store (SqliteStore.migrate)",
                    detail="no version ladder — nothing to compare",
                )
            )
            continue

        expected = _code_version(const_ref) if const_ref else None
        if expected is None:
            results.append(
                CheckResult(
                    name=f"schema:{label}",
                    subsystem="migrator",
                    kind=Kind.FRESHNESS,
                    verdict=Verdict.UNKNOWN,
                    evidence=f"could not resolve {const_ref}",
                    detail=f"{label}: the code constant is unreadable — check renamed?",
                )
            )
            continue

        ahead, behind, unreadable = [], [], []
        for p in paths:
            got = _store_version(p, mechanism)
            if got is None:
                unreadable.append(p.name)
            elif got > expected:
                ahead.append(f"{p.name}=v{got}")
            elif got < expected:
                behind.append(f"{p.name}=v{got}")

        evidence = (
            f"{mechanism}, code=v{expected}, {len(paths)} file(s); "
            f"ahead={ahead or '-'} behind={behind or '-'}"
        )
        if ahead:
            results.append(
                CheckResult(
                    name=f"schema:{label}",
                    subsystem="migrator",
                    kind=Kind.FRESHNESS,
                    verdict=Verdict.BROKEN,
                    evidence=evidence,
                    detail=(
                        f"{len(ahead)} {label} store(s) are AHEAD of code v{expected} "
                        f"({', '.join(ahead)}) — migrations are being SKIPPED and the "
                        "stamp does not describe the real shape"
                    ),
                    remedy="techne doctor --explain schema (decide the stamp; see techne#676)",
                )
            )
        elif unreadable:
            results.append(
                CheckResult(
                    name=f"schema:{label}",
                    subsystem="migrator",
                    kind=Kind.FRESHNESS,
                    verdict=Verdict.UNKNOWN,
                    evidence=evidence,
                    detail=f"could not read the version of: {', '.join(unreadable)}",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"schema:{label}",
                    subsystem="migrator",
                    kind=Kind.FRESHNESS,
                    verdict=Verdict.LIVE,
                    evidence=evidence,
                    detail=(
                        f"{len(behind)} store(s) below code v{expected}, will migrate on open"
                        if behind
                        else f"all {len(paths)} store(s) at code v{expected}"
                    ),
                )
            )
    return results


#: How long a card may sit `pending` before it stops being "between indexing and
#: the fill pass" and becomes a backlog nobody is coming back for (techne#824).
#: One day, because the fill pass is a deliberate operator action taken in a
#: session — not a scheduled rail — so anything older has outlived an operator's
#: attention rather than merely queued behind one.
_PENDING_CARD_STALE_DAYS = 1


@register_check
def check_pending_cards(config) -> CheckResult:
    """Is any document card still carrying a stand-in gist? (techne#824)

    A COVERAGE check, and the same shape as `rail:reflect` before techne#636: the
    producing half was visible and the CONSUMING half was not. A card is created
    `pending`, holding the document's TITLE as a placeholder until the
    agent-assisted `librarian cards emit`/`ingest` pass writes a real gist. Nothing
    ever said one was waiting. On 2026-08-29, 28 cards had been pending since
    2026-08-17 — twelve days — and were found only because a consolidate cluster
    happened to surface some of them.

    A PENDING CARD IS NOT INERT, which is why this is a check and not a report. It
    rides recall like any other card, so for those twelve days recall could return
    a stand-in title where a gist was promised; nine of them returned the same
    80-character wrapper string. `techne librarian cards list` could always answer
    this, and was the ONLY thing that could — visible to someone who already
    suspected the backlog and knew the subcommand, which is the opposite of what a
    check is for.

    AGE, NOT COUNT, DECIDES THE VERDICT. A card pending for a minute is the normal
    state between indexing and the fill pass, so counting alone would fire on every
    healthy install that just indexed a document. What is a finding is a card that
    has been pending long enough that the fill pass plainly is not coming.
    """
    from techne.core.cards import CARD_TYPE
    from techne.core.database import connect

    db_path = Path(config.db_path)
    if not db_path.exists():
        return CheckResult(
            name="cards:pending",
            subsystem="librarian",
            kind=Kind.COVERAGE,
            verdict=Verdict.DORMANT,
            evidence=f"no store at {db_path}",
            detail="no techne store yet — nothing can be pending",
        )

    # OWN SQL RATHER THAN `pending_cards()`, for two reasons. That helper is the
    # emit WORK-LIST: it selects (id, content, source, tags) and carries no
    # `created_at`, so AGE — which is what decides this verdict — is not in its
    # shape at all. A first draft of this check read `r.get("created_at")` off its
    # rows, got None every time, and reported LIVE on a twelve-day backlog. And a
    # COUNT plus a MIN is one row rather than up to a thousand, on a check that
    # runs behind the SessionStart banner's budget.
    sql = (
        "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM knowledge "
        "WHERE type = ? AND superseded_by IS NULL AND tags LIKE '%pending%'"
    )
    try:
        conn = connect(db_path)
        try:
            row = conn.execute(sql, (CARD_TYPE,)).fetchone()
        finally:
            conn.close()
    except Exception as e:  # a probe that fails is never silently green
        return CheckResult(
            name="cards:pending",
            subsystem="librarian",
            kind=Kind.COVERAGE,
            verdict=Verdict.UNKNOWN,
            evidence=f"{type(e).__name__} reading the card backlog",
            detail="could not read the card backlog",
        )

    count = (row["n"] if row else 0) or 0
    if not count:
        return CheckResult(
            name="cards:pending",
            subsystem="librarian",
            kind=Kind.COVERAGE,
            verdict=Verdict.LIVE,
            evidence="COUNT(*) over pending document_cards → 0",
            detail="no card is waiting for a gist",
        )

    oldest = row["oldest"]

    # `temporal_verdict` + `_age`, NOT a local parser (#841 r1 Important 2). An
    # earlier version of this check carried its own `_age_in_days`, which covered
    # two of `_parse_ts`'s five formats and reached the rest by slicing `ts[:19]` —
    # so `"...T10:00:00+05:00"` was truncated and stamped UTC, silently absorbing a
    # five-hour error. A strictly weaker parser, 2,400 lines below a stronger one,
    # in the same file, for the same column family.
    #
    # It also buys the honest verdict for free. `age is None` used to short-circuit
    # `stale` to False and report LIVE — INDISTINGUISHABLE from age 0, so a store
    # with N pending cards whose age cannot be read reported `ok=True`, which is the
    # exact failure this check exists to end. `temporal_verdict` returns
    # UNOBSERVABLE there, and its docstring says why: conflating "never ran" with
    # "cannot be observed" is the ambiguity techne#468 was raised to remove.
    verdict = temporal_verdict(oldest, timedelta(days=_PENDING_CARD_STALE_DAYS))
    age = _age(oldest)

    detail = f"{count} card(s) pending"
    detail += f", oldest {age.days}d old" if age is not None else ", age UNREADABLE"
    return CheckResult(
        name="cards:pending",
        subsystem="librarian",
        kind=Kind.COVERAGE,
        verdict=verdict,
        evidence=f"oldest pending card created_at={oldest or 'unknown'}",
        detail=detail,
        # `--limit {count}`, because it defaults to 50 (cli.py:3542) and a bare
        # remedy tells an operator with a larger backlog that one emit drains it.
        # AGENTS.md already carries a trigger for that exact trap on the reflection
        # rail ("treats one emit call as having drained a session").
        remedy=(
            f"techne librarian cards emit -o /tmp/cards.json --limit {count}  # then ingest"
            if verdict is not Verdict.LIVE
            else None
        ),
    )
