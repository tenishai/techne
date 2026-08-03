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
    "codex_grep_assist": (timedelta(days=14), "fires on Grep, when enabled"),
    "post_compact": (timedelta(days=30), "fires only after a compaction"),
}

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
        # A store with no sessions has legitimately never run a hook. That is
        # NOT the same as "this rail emits no telemetry" — here we CAN say why
        # the marker is missing, so it is not UNDECIDABLE and must not be
        # reported as failing. Without this, every fresh `techne init` reports
        # red, which is the fastest way to teach an operator to ignore the
        # signal entirely.
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

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
            elif not last and sessions == 0:
                verdict = Verdict.DORMANT
                detail = "no sessions recorded yet — nothing has run"
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
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending') "
            "FROM outbox GROUP BY target"
        ).fetchall()
    finally:
        conn.close()

    for target, total, last_synced, pending in rows:
        pending = pending or 0

        # THE DISCRIMINATOR IS THE BACKLOG, NOT THE CLOCK. An old `last_synced`
        # with nothing pending means the target is owed nothing — no rows have
        # been created for it — which is IDLE, not broken. Only a backlog that
        # is not draining is a sync failure. An earlier draft of this check
        # keyed on age alone and would have reported a target with 140 rows and
        # 0 pending as stale purely because it had been quiet.
        if pending == 0:
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
