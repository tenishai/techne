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

A check NEVER raises: a probe that fails reports :attr:`Verdict.UNKNOWN` with the
error as evidence. Silently dropping a failed check would reproduce the exact bug
this module exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable


class Verdict(str, Enum):
    """Outcome of a single check, worst-last so ``max()`` finds the headline."""

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


def run_checks(config) -> list[CheckResult]:
    """Run every registered check. Never raises.

    A check that raises becomes an UNKNOWN result carrying the exception — never
    an absent one. An absent check reads as "nothing wrong", which is the failure
    mode this whole module exists to prevent.
    """
    results: list[CheckResult] = []
    for fn in _REGISTRY:
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
    # NOT YET INSTRUMENTED (#469 review finding 3). These three carry no
    # record_hook_run call, so they will report UNOBSERVABLE — which is exactly
    # right and exactly why they are listed here rather than omitted. Leaving
    # them out made a DEAD CAPTURE RAIL invisible: deleting a month of
    # observations produced byte-identical verdicts. post_tool_use IS the
    # observation capture path (techne is functionally dead without it), and
    # user_prompt_submit hosts per_turn_recall — half the founding incident
    # this module cites. An uninstrumented rail must read as "nothing to tell
    # us", never as absent.
    "post_tool_use": (timedelta(days=3), "captures observations — NOT YET INSTRUMENTED"),
    "user_prompt_submit": (timedelta(days=3), "per_turn_recall — NOT YET INSTRUMENTED"),
    "pre_compact": (timedelta(days=7), "checkpoints before compaction — NOT YET INSTRUMENTED"),
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
                marker = _parse_ts(last)
                sess = _parse_ts(last_settled_session)
                if marker is None:
                    verdict = Verdict.UNOBSERVABLE
                    detail = f"{note}; sessions exist but no marker was ever written"
                elif sess is not None and marker < sess:
                    verdict = Verdict.STALE
                    detail = (
                        f"{note}; a session started at {last_settled_session} "
                        f"and left no marker"
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
                        "techne init --platform claude-code --posture observe"
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
        srow = conn.execute("SELECT COUNT(*), MAX(started_at) FROM sessions").fetchone()
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
    for missing in sorted(_declared_outbox_targets(config) - seen):
        results.append(
            CheckResult(
                name=f"outbox:{missing}",
                subsystem="sync",
                kind=Kind.TEMPORAL,
                verdict=Verdict.DORMANT if store_is_new else Verdict.UNOBSERVABLE,
                evidence=f"no outbox rows have ever existed for target={missing}",
                detail=(
                    "store created <7d ago — nothing enqueued yet"
                    if store_is_new
                    else "declared lane has NEVER enqueued anything"
                ),
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
        elif config.sync.adapter == "noop":
            # noop PARKS rows deliberately rather than falsely marking them
            # synced, precisely so a real adapter can still take the backlog
            # (see sync/noop.py). Pending rows under noop are correct.
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


@register_check
def check_vector_coverage(config) -> CheckResult:
    """Are knowledge rows actually embedded?

    A COVERAGE check, not a staleness one. On 2026-08-03 the existing staleness
    predicate reported every row fresh (`vec_stale = 0`) while ~177 of 758 rows
    had no vector at all — "not stale" and "present" are different questions, and
    only the second one tells you hybrid search is whole.
    """
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        try:
            embedded = conn.execute("SELECT COUNT(*) FROM knowledge_vec_rowids").fetchone()[0]
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

    missing = max(total - embedded, 0)
    pct = (embedded / total * 100) if total else 100.0
    return CheckResult(
        name="vectors:coverage",
        subsystem="search",
        kind=Kind.COVERAGE,
        verdict=Verdict.LIVE if missing == 0 else Verdict.STALE,
        evidence=f"knowledge={total}, knowledge_vec_rowids={embedded}",
        detail=f"{embedded}/{total} embedded ({pct:.0f}%), {missing} missing",
        remedy="techne vectors backfill" if missing else None,
    )
