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


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------

#: Hooks that must report liveness, with how long between runs is acceptable.
#: Intervals differ per hook by design — a global staleness constant is
#: meaningless across a per-tool-call rail and a per-compaction one.
_HOOK_EXPECTATIONS = {
    "session_start": (timedelta(days=3), "fires once per session"),
    "session_end": (timedelta(days=3), "fires at session teardown"),
    "codex_grep_assist": (timedelta(days=7), "fires on Grep, when enabled"),
    "post_compact": (timedelta(days=7), "fires after a compaction"),
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
    # an alert. pre_compact stays generous: a compaction is an event an operator
    # may legitimately go a week without triggering. (#479 review 1.)
    "post_tool_use": (timedelta(hours=18), "captures observations — techne is dead without it"),
    "user_prompt_submit": (timedelta(hours=18), "per_turn_recall / per_turn_seed"),
    "pre_compact": (timedelta(days=7), "checkpoints before compaction"),
}

#: Hooks that fire once per SESSION. Judged against session activity rather than
#: wall-clock — for these, "days since last fire" measures how recently the
#: operator worked, not whether the hook works.
_SESSION_SCOPED = {"session_start", "session_end", "post_tool_use", "user_prompt_submit"}

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
    * the ``sink:`` lanes are no-ops when ``standalone`` (the default), so on a
      default install all three declared lanes were structurally unreachable.

    ``lifecycle._thread_summary_target`` is the single source of truth for which
    egress lane is live, so this defers to it rather than restating the rule.
    """
    from techne.lifecycle import _thread_summary_target

    targets = {_thread_summary_target(config)}
    if not config.standalone:
        # Team topology also drains the knowledge sink. Only meaningful when the
        # sink lane is the live one — standalone never registers these.
        targets.add("sink:archivist_knowledge")
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
                # recent-looking marker and reads healthy forever.
                results.append(
                    CheckResult(
                        name=f"hook:{hook}",
                        subsystem="hooks",
                        kind=Kind.TEMPORAL,
                        verdict=Verdict.DORMANT,
                        last_fired=last,
                        evidence=f"config {gate[0]} = false",
                        detail=f"disabled by config: {gate[0]}",
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
_RAIL_EXPECTATIONS = {
    "reflect": (timedelta(days=3), "distils sessions into knowledge candidates"),
    "consolidate": (timedelta(days=14), "dedups/merges knowledge"),
    "dream": (timedelta(days=14), "recombines knowledge into new connections"),
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
def check_runtime_revision(config) -> CheckResult | None:
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
    """
    import subprocess
    from pathlib import Path

    import techne

    runtime = Path(techne.__file__).resolve().parent.parent

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
            name="runtime:revision",
            subsystem="runtime",
            kind=Kind.FRESHNESS,
            verdict=Verdict.DORMANT,
            detail=f"not a git checkout — packaged install at {runtime}",
            evidence=f"git rev-parse in {runtime} did not resolve",
        )

    upstream = _git("rev-parse", "--short", "origin/main")
    if upstream is None:
        return CheckResult(
            name="runtime:revision",
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
            name="runtime:revision",
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
            name="runtime:revision",
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
            name="runtime:revision",
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
            name="runtime:revision",
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
        name="runtime:revision",
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

        for rail, (interval, note) in _RAIL_EXPECTATIONS.items():
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
                remedy = f"techne {rail} --help" if verdict is Verdict.STALE else None

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
        srow = conn.execute(
            "SELECT COUNT(*), MAX(ended_at) FROM sessions WHERE ended_at IS NOT NULL"
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
    # FALSE inside `techne doctor`, which never registers anything (only
    # `outbox flush-sinks` calls ensure_registered_for).
    #
    # Reusing it therefore reported a TEAM install's never-enqueued lane as
    # "correctly idle" — masking the eight-week-dead-lane case this check exists
    # to catch, which is a worse bug than the one the no-consumer branch fixes.
    # Caught by a mutation sweep: UNOBSERVABLE->DORMANT survived, and writing
    # the test to kill it exposed this.
    #
    # The question here is topological, and answerable from config alone:
    #   standalone -> the sync adapter drains; "noop" means nothing does.
    #   team       -> the archivist sink ships with the product; the lane HAS a
    #                 consumer whether or not this particular process wired it.
    has_consumer = (config.sync.adapter != "noop") if config.standalone else True
    for missing in sorted(_declared_outbox_targets(config) - seen):
        if not has_consumer:
            verdict = Verdict.DORMANT
            detail = "no live consumer (sync.adapter=noop / no registered sink) — correctly idle"
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

    from techne.sync.outbox import SINK_PREFIX, SYNC_PREFIX

    for target, total, last_synced, pending, last_enqueued in rows:
        pending = pending or 0

        # RETIRED LANES ARE DORMANT, NOT STALE. `enqueue()` rejects
        # un-namespaced targets, so a bare name is pre-#51 residue that
        # migration v10 deliberately left in place — its producer was retired
        # ten schema versions ago, and no operator action can ever clear it.
        # Reporting it as "producer may be dead" is technically true and
        # permanently un-actionable, which is the nag that teaches an operator
        # to skip the whole report (#469 review 3).
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
_PRINTABLE_DESTINATIONS = frozenset({"model", "reflect.model", "conflict.model", "standalone"})


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
        # version read four fields and SyncConfig has five: `extra`, which
        # `Config._from_dict` fills with EVERY unrecognised key under `sync:` —
        # i.e. precisely the catch-all for things this code does not know about.
        # So `sync: {token: ghp_LIVE_SECRET}` departed from default and rendered
        # a bare `sync.adapter=noop`, the same "true and useless" string this
        # renderer was just rewritten to kill, at the one field that catches
        # everything. Not hypothetical — executed. (Prudence, PR #508 r4.)
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
            return CheckResult(
                name="vectors:coverage",
                subsystem="search",
                kind=Kind.COVERAGE,
                verdict=Verdict.DORMANT,
                evidence="knowledge_vec_rowids absent",
                detail="vector channel not initialised (FTS-only search)",
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
    pct = (covered / total * 100) if total else 100.0
    # "and current" is load-bearing: this numerator is `covered` (embedded AND not
    # vec_stale), while `evidence` reports the raw `embedded`. Without the phrase a
    # reader comparing the two lines sees 1/6 against embedded=3 and reads a
    # contradiction — and evidence exists precisely so a finding can be re-derived
    # (#497 review, nit 1).
    detail = f"{covered}/{total} live knowledge embedded and current ({pct:.0f}%)"
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
    healthy = missing == 0 and stale == 0 and orphans == 0
    return CheckResult(
        name="vectors:coverage",
        subsystem="search",
        kind=Kind.COVERAGE,
        verdict=Verdict.LIVE if healthy else Verdict.STALE,
        evidence=(
            f"live knowledge={total}, embedded={embedded}, stale={stale}, "
            f"superseded={superseded}, superseded-but-embedded={orphans}, "
            f"dangling={dangling}"
        ),
        detail=detail,
        remedy=None if healthy else "techne vectors backfill",
    )
