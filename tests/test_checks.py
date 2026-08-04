"""Tests for the subsystem-liveness registry (techne#468).

The invariant under test throughout: **a check must never render as passing
when the subsystem's liveness is unknown.** Every failure in techne#942 was
silent, and silence was also what success looked like — so the taxonomy's job
is to make "cannot tell" a distinct, failing answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.checks import (
    CheckResult,
    Kind,
    Verdict,
    run_checks,
    summarize,
    temporal_verdict,
    worst,
)
from techne.core.database import init_db


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.db_path = tmp_path / "test.db"
    init_db(cfg.db_path).close()
    return cfg


def _iso(delta: timedelta) -> str:
    return (datetime.now(tz=timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")


class TestTemporalVerdict:
    def test_recent_is_live(self):
        assert temporal_verdict(_iso(timedelta(hours=1)), timedelta(days=3)) is Verdict.LIVE

    def test_old_is_stale(self):
        assert temporal_verdict(_iso(timedelta(days=30)), timedelta(days=3)) is Verdict.STALE

    def test_never_fired_is_unobservable_not_stale(self):
        """The load-bearing distinction: no timestamp at all cannot be
        distinguished from 'telemetry does not exist', so it must NOT be
        reported as merely overdue."""
        assert temporal_verdict(None, timedelta(days=3)) is Verdict.UNOBSERVABLE

    def test_unparseable_timestamp_is_unobservable(self):
        assert temporal_verdict("not-a-date", timedelta(days=3)) is Verdict.UNOBSERVABLE


class TestVerdictSeverity:
    def test_worst_picks_broken_over_stale(self):
        rs = [
            CheckResult("a", "s", Kind.TEMPORAL, Verdict.LIVE),
            CheckResult("b", "s", Kind.TEMPORAL, Verdict.STALE),
            CheckResult("c", "s", Kind.TEMPORAL, Verdict.BROKEN),
        ]
        assert worst(rs) is Verdict.BROKEN

    def test_unobservable_outranks_stale(self):
        """UNOBSERVABLE is a FAILING state by decision (#468): a rail that
        cannot report is unfinished, which is worse than one that is merely
        overdue — the overdue one at least proves it can speak."""
        rs = [
            CheckResult("a", "s", Kind.TEMPORAL, Verdict.STALE),
            CheckResult("b", "s", Kind.TEMPORAL, Verdict.UNOBSERVABLE),
        ]
        assert worst(rs) is Verdict.UNOBSERVABLE

    def test_dormant_does_not_escalate(self):
        """Deliberate dormancy is healthy — most default-off flags are correctly
        off (techne#941). It must not raise the banner headline."""
        rs = [
            CheckResult("a", "s", Kind.TEMPORAL, Verdict.LIVE),
            CheckResult("b", "s", Kind.TEMPORAL, Verdict.DORMANT),
        ]
        assert worst(rs) is Verdict.LIVE

    def test_ok_property_treats_dormant_as_fine_and_unobservable_as_not(self):
        assert CheckResult("a", "s", Kind.TEMPORAL, Verdict.DORMANT).ok
        assert not CheckResult("a", "s", Kind.TEMPORAL, Verdict.UNOBSERVABLE).ok
        assert not CheckResult("a", "s", Kind.TEMPORAL, Verdict.UNKNOWN).ok

    def test_worst_of_nothing_is_none(self):
        assert worst([]) is None


class TestRunChecksNeverRaises:
    def test_failing_check_becomes_unknown_not_absent(self, config):
        """A dropped check reads as 'nothing wrong' — the exact bug this module
        exists to catch. A raising probe must produce a RESULT."""
        from techne.core import checks as checks_mod

        def boom(cfg):
            raise RuntimeError("probe exploded")

        original = list(checks_mod._REGISTRY)
        try:
            checks_mod._REGISTRY[:] = [boom]
            results = run_checks(config)
        finally:
            checks_mod._REGISTRY[:] = original

        assert len(results) == 1
        assert results[0].verdict is Verdict.UNKNOWN
        assert "probe exploded" in results[0].evidence

    def test_unknown_is_not_ok(self, config):
        assert not CheckResult("x", "s", Kind.TEMPORAL, Verdict.UNKNOWN).ok


class TestBuiltinChecks:
    def test_fresh_install_is_dormant_not_failing(self, config):
        """A brand-new store has legitimately never run a hook, and we can SAY
        WHY — so it is explained, not undecidable.

        The distinction matters more than it looks: if every fresh `techne init`
        reported red, the fastest thing an operator would learn is to ignore the
        signal. UNOBSERVABLE has to mean "I cannot tell", not "nothing has
        happened yet".
        """
        results = run_checks(config)
        hooks = [r for r in results if r.name.startswith("hook:")]
        assert hooks, "expected per-hook checks"
        assert all(r.verdict is Verdict.DORMANT for r in hooks)
        assert all(r.ok for r in hooks)

    def test_active_store_with_no_markers_is_unobservable(self, config):
        """The real failing case: the store HAS activity, so a hook should have
        left a marker — and none did. That is undecidable and must fail.

        This is the shape of the 2026-07-31 incident (techne#942): sessions kept
        being recorded while two rails sat dead, and nothing distinguished them.
        """
        from techne.core.database import connect

        # Enable the config-gated rail too, so this covers the whole cohort —
        # a gated-off hook is correctly DORMANT and would otherwise mask the
        # assertion (see TestConfigGateBeatsMarker).
        config.hooks.codex_grep_assist = True

        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                ("s1", "2026-08-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        results = run_checks(config)
        hooks = [r for r in results if r.name.startswith("hook:")]
        assert all(r.verdict is Verdict.UNOBSERVABLE for r in hooks)
        assert not any(r.ok for r in hooks)

    def test_hook_marker_makes_it_live(self, config):
        from techne.hooks._liveness import record_hook_run

        record_hook_run(config.db_path, "session_start", outcome="ok")
        results = run_checks(config)
        hook = next(r for r in results if r.name == "hook:session_start")
        assert hook.verdict is Verdict.LIVE
        assert hook.evidence, "a result must carry re-derivable evidence"

    def test_recorded_error_reports_broken(self, config):
        from techne.hooks._liveness import record_hook_run

        record_hook_run(
            config.db_path, "session_end", outcome="failed", error={"stage": "s", "msg": "boom"}
        )
        results = run_checks(config)
        hook = next(r for r in results if r.name == "hook:session_end")
        assert hook.verdict is Verdict.BROKEN
        assert "boom" in hook.detail

    def test_disabled_reflect_is_dormant_not_stale(self, config):
        """Off-by-config is a healthy state and must name the key, so deliberate
        dormancy is cheap to confirm and forgotten dormancy is loud."""
        config.reflect.enabled = False
        results = run_checks(config)
        rail = next(r for r in results if r.name == "rail:reflect")
        assert rail.verdict is Verdict.DORMANT
        assert "reflect.enabled" in rail.detail

    def _enqueue(self, config, target, created_at, synced_at, status="synced"):
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO outbox (target, data_json, content_hash, status, created_at,"
                " synced_at) VALUES (?,?,?,?,?,?)",
                (target, "{}", f"h-{target}-{created_at}", status, created_at, synced_at),
            )
            conn.commit()
        finally:
            conn.close()

    def test_drained_and_recently_filled_is_live(self, config):
        """Nothing owed AND the producer is still enqueueing — genuinely fine."""
        recent = _iso(timedelta(hours=2))
        self._enqueue(config, "sync:t", recent, recent)

        target = next(r for r in run_checks(config) if r.name == "outbox:sync:t")
        assert target.verdict is Verdict.LIVE
        assert "nothing owed" in target.detail

    def test_drained_but_dead_producer_is_stale(self, config):
        """THE FALSE GREEN THIS CHECK SHIPPED WITH (#469 review, finding 2).

        `pending == 0` has two opposite causes — sync keeping up, or nothing
        being produced — and short-circuiting both to LIVE reported
        `thread_summary` (140 rows, last enqueued eight weeks earlier) as
        healthy: the very lane the check's own docstring cites as the motivating
        dead-for-eight-weeks incident. The backlog measures the DRAIN; it says
        nothing about the FILL.
        """
        from techne.core.database import connect

        old = _iso(timedelta(days=60))
        self._enqueue(config, "sync:t", old, old)

        # Sessions HAVE ENDED since the last enqueue — that, not the calendar, is
        # the evidence of a dead producer. Measuring wall-clock instead reported
        # a healthy lane as dead whenever the operator took a holiday
        # (#469 review 3, finding 3).
        #
        # FIXTURE CORRECTED (#469 review 4, Important): this previously inserted
        # a session with `started_at` only. That is an IN-FLIGHT session, and
        # these lanes are fed at session END — so a started-but-unended session
        # has not yet had its chance to feed the lane and is not evidence of
        # anything. Worse, the in-flight row is always ~now (collect_health runs
        # in Phase 5, after Phase 3 inserts it), which is what made the whole
        # predicate collapse back into `now > enq + 7d`.
        #
        # The assertion is UNCHANGED and the intent is unchanged — "a session ran
        # since the last enqueue and did not feed the lane". Only the way the
        # fixture expresses "a session ran" is corrected, from the buggy proxy
        # (started) to the real one (ended).
        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
                ("s-recent", _iso(timedelta(hours=2)), _iso(timedelta(hours=1))),
            )
            conn.commit()
        finally:
            conn.close()

        target = next(r for r in run_checks(config) if r.name == "outbox:sync:t")
        assert target.verdict is Verdict.STALE
        assert "producer may be dead" in target.detail

    def test_idle_operator_does_not_look_like_a_dead_producer(self, config):
        """The other edge: an old enqueue with NO sessions since is just idle."""
        old = _iso(timedelta(days=60))
        self._enqueue(config, "sync:t", old, old)

        target = next(r for r in run_checks(config) if r.name == "outbox:sync:t")
        assert target.verdict is Verdict.LIVE

    def test_declared_lane_that_never_enqueued_still_produces_a_result(self, config):
        """Deriving the lane list from `GROUP BY target` made a never-used lane
        VANISH from the report — reintroducing, one layer up, the exact
        absence-reads-as-healthy bug run_checks prevents for raising probes.
        """
        from techne.core.database import connect
        from techne.lifecycle import THREAD_SUMMARY_SYNC_TARGET

        # Age the store past the fresh-install grace so absence is a finding.
        conn = connect(config.db_path)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", ("2026-01-01 00:00:00",))
            conn.commit()
        finally:
            conn.close()

        # Assert the lane the PRODUCER actually writes, imported from the module
        # that defines it — NOT the checker's own constant. The previous version
        # iterated `_DECLARED_OUTBOX_TARGETS` and so asserted the constant
        # against itself, which is precisely why three wrong lane names shipped
        # (#469 review 3, finding 2): bare "thread_summary" is a pre-#51 legacy
        # name that `enqueue()` now rejects outright, and the real standalone
        # lane was missing entirely.
        assert config.standalone, "default topology assumed by this test"
        names = {r.name for r in run_checks(config)}
        assert f"outbox:{THREAD_SUMMARY_SYNC_TARGET}" in names

    def test_declared_lanes_are_reachable_under_their_topology(self, config):
        """Every declared lane must be one this install could actually write.

        Guards the failure mode directly: a declared lane that `enqueue()` would
        reject can never clear, so it is an un-actionable permanent red.
        """
        from techne.core.checks import _declared_outbox_targets
        from techne.sync.outbox import SINK_PREFIX, SYNC_PREFIX

        for target in _declared_outbox_targets(config):
            assert target.startswith((SINK_PREFIX, SYNC_PREFIX)), (
                f"{target!r} is not a namespaced target — enqueue() would reject it"
            )

        # Topology decides which lane is live; the other must not be declared.
        config.standalone = True
        assert not any(t.startswith("sink:") for t in _declared_outbox_targets(config)), (
            "sink lanes are no-ops when standalone and must not be declared"
        )

    def test_every_result_carries_evidence(self, config):
        """techne#941's 'demand proof of absence' rule: a verdict a reader
        cannot re-derive is indistinguishable from one nobody checked."""
        for r in run_checks(config):
            assert r.evidence, f"{r.name} has no evidence"

    def test_summarize_counts_by_verdict(self, config):
        counts = summarize(run_checks(config))
        assert sum(counts.values()) == len(run_checks(config))


class TestConfigGateBeatsMarker:
    """#469 review: config is read FIRST, the marker second.

    A disabled rail records nothing, so a rail switched OFF after use keeps its
    last marker forever. Reading the marker alone would report it healthy
    indefinitely — the marker must never be interpreted without its gate.
    """

    def test_disabled_rail_with_a_recent_marker_is_dormant_not_live(self, config):
        from techne.hooks._liveness import record_hook_run

        config.hooks.codex_grep_assist = True
        record_hook_run(config.db_path, "codex_grep_assist", outcome="ok")

        enabled = next(
            r for r in run_checks(config) if r.name == "hook:codex_grep_assist"
        )
        assert enabled.verdict is Verdict.LIVE

        # Operator turns it off. The marker is untouched and still recent.
        config.hooks.codex_grep_assist = False
        disabled = next(
            r for r in run_checks(config) if r.name == "hook:codex_grep_assist"
        )
        assert disabled.verdict is Verdict.DORMANT
        assert "hooks.codex_grep_assist" in disabled.detail


class TestNoFalseGreens:
    """Regression guards for the #469-review Critical findings.

    Richard's standard for this work: a check that reports green while a
    subsystem is dead is worse than no check at all. Each test here pins a
    state where an earlier version reported healthy.
    """

    def _age_store(self, config, applied_at="2026-01-01 00:00:00"):
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", (applied_at,))
            conn.commit()
        finally:
            conn.close()

    def test_old_store_that_never_recorded_a_session_is_not_green(self, config):
        """THE WORST BUG THIS MODULE SHIPPED WITH.

        The fresh-install DORMANT branch keyed on `sessions == 0` with NO time
        bound. If session_start never fires — mis-registered, wrong interpreter
        (techne#458, three times), or the techne#942 shape — no session row is
        ever written, so every hook stayed DORMANT and `techne doctor` printed
        "All subsystems confirmed live" FOREVER. It confirmed the ABSENCE of
        evidence and reported it as confirmation.
        """
        self._age_store(config)
        config.hooks.codex_grep_assist = True  # else correctly DORMANT by config

        hooks = [r for r in run_checks(config) if r.name.startswith("hook:")]
        assert hooks
        assert all(r.verdict is Verdict.UNOBSERVABLE for r in hooks), (
            "an old store with no sessions must NOT read as healthy"
        )
        assert not any(r.ok for r in hooks)

    def test_brand_new_store_is_still_quiet(self, config):
        """The other direction must keep working: a genuinely fresh install
        must not report red, or the operator learns to ignore the signal."""
        hooks = [r for r in run_checks(config) if r.name.startswith("hook:")]
        assert all(r.ok for r in hooks)

    def test_uninstrumented_hooks_are_reported_not_omitted(self, config):
        """post_tool_use IS the capture path; user_prompt_submit hosts
        per_turn_recall — half the founding incident. Omitting them let a dead
        capture rail produce byte-identical verdicts (#469 review, finding 3).
        """
        self._age_store(config)
        names = {r.name for r in run_checks(config)}
        for hook in ("post_tool_use", "user_prompt_submit", "pre_compact"):
            assert f"hook:{hook}" in names, f"{hook} is invisible to the health check"

    def test_expected_checks_are_all_present(self, config):
        """Guards against a check being DELETED WHOLESALE without a failure.

        `test_every_result_carries_evidence` and `test_summarize_counts_by_verdict`
        iterate whatever happens to be registered, so they pass undiminished
        when a check disappears — `check_vector_coverage` could be removed with
        zero failures (#469 review, finding 7).
        """
        names = {r.name for r in run_checks(config)}
        assert "vectors:coverage" in names
        assert any(n.startswith("hook:") for n in names)
        assert any(n.startswith("rail:") for n in names)
        assert any(n.startswith("outbox:") for n in names)

    def test_vector_coverage_reports_stale_when_rows_are_unembedded(self, config):
        """Pins check_vector_coverage's FAILING branch, which was unpinned."""
        from techne.core.database import connect
        from techne.core.knowledge import store

        store(config.db_path, "a durable fact for coverage", type="fact")
        conn = connect(config.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_vec_rowids (rowid INTEGER)")
            conn.commit()
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        assert cov.verdict is Verdict.STALE
        assert "missing" in cov.detail
        assert cov.remedy


class TestSurvivingMutations:
    """Pins for the four mutations that survived the #469 review-3 suite.

    Each corresponds to a feature that could be deleted wholesale without a
    single test failing — which is how two of them shipped broken.
    """

    def test_session_marker_staleness_is_detected(self, config):
        """M11: the session-marker branch had NOTHING asserting anything about
        it, which is why it shipped comparing against the wrong session.

        A session that has had its chance to write a marker and didn't is the
        actual failure. Critically, an operator GAP must not trigger it.
        """
        from techne.core.database import connect
        from techne.hooks._liveness import record_hook_run

        record_hook_run(config.db_path, "session_start", outcome="ok", ts=_iso(timedelta(days=9)))
        conn = connect(config.db_path)
        try:
            # Two settled sessions AFTER the marker: the hook stopped recording.
            for i, ago in enumerate((timedelta(days=3), timedelta(days=2))):
                conn.execute(
                    "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                    (f"s{i}", _iso(ago)),
                )
            conn.commit()
        finally:
            conn.close()

        hook = next(r for r in run_checks(config) if r.name == "hook:session_start")
        assert hook.verdict is Verdict.STALE

    def test_long_gap_between_sessions_is_not_stale(self, config):
        """M11's other edge, and the bug review 3 caught: comparing against the
        NEWEST session measured the inter-session gap, so a once-a-day operator
        went STALE every day. A 60-day gap with a healthy hook must stay LIVE.
        """
        from techne.core.database import connect
        from techne.hooks._liveness import record_hook_run

        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                ("old", _iso(timedelta(days=60))),
            )
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                ("now", _iso(timedelta(minutes=1))),
            )
            conn.commit()
        finally:
            conn.close()
        # Marker from the settled (old) session — the in-flight one has not
        # written its marker yet, exactly as at real check time.
        record_hook_run(config.db_path, "session_start", outcome="ok", ts=_iso(timedelta(days=60)))

        hook = next(r for r in run_checks(config) if r.name == "hook:session_start")
        assert hook.verdict is Verdict.LIVE, "an operator's absence is not a broken hook"


class TestDoctorOutputHonesty:
    """M2: the headline symptom of the worst bug was itself unpinned."""

    def test_never_claims_all_live_when_none_are(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from techne.cli import main
        from techne.core.database import connect, init_db

        home = tmp_path / "h"
        (home / ".techne").mkdir(parents=True)
        db = home / ".techne" / "techne.db"
        init_db(db).close()
        conn = connect(db)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", (_iso(timedelta(days=1)),))
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("HOME", str(home))

        out = CliRunner().invoke(main, ["doctor"]).output
        # ASSERT AGAINST WHAT THE CODE EMITS, NOT A PARAPHRASE (#469 review 4,
        # M20). The previous assertion was `"All subsystems confirmed live" not
        # in out` — a literal that appears nowhere in cli.py except inside a
        # COMMENT. Every emitted string interpolates a count
        # (f"All {live} subsystems confirmed live."), so the assertion could
        # never fail and the teeth-check it was written for never took.
        import re

        assert not re.search(r"All \d+ subsystems confirmed live", out), (
            f"must not confirm liveness it never observed; got: {out!r}"
        )
        # The positive half — silence is not proof. Assert the output actually
        # SAYS something honest, so a future change that emits nothing at all
        # cannot pass on the negative assertion alone.
        #
        # Note this store has uninstrumented hooks, so it takes the `failing > 0`
        # branch and reports a not-confirmed count; the "0 of N confirmed live"
        # line lives in the `failing == 0` branch and is not reachable here.
        assert re.search(r"\d+ subsystem\(s\) not confirmed working", out), (
            f"expected an explicit not-confirmed count; got: {out!r}"
        )


class TestResolveDbPathIsolation:
    """M9: finding 6 had no test at all."""

    def test_never_falls_back_to_a_bystander_home_store(self, tmp_path, monkeypatch):
        """Config supports an explicit db_path elsewhere; guessing ~/.techne
        lands on ANOTHER AGENT'S live store in the multi-agent layout (#112).
        """
        from techne.hooks._liveness import resolve_db_path

        bystander = tmp_path / "home" / ".techne"
        bystander.mkdir(parents=True)
        (bystander / "techne.db").write_text("i am someone else's store")

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("TECHNE_HOME", raising=False)
        # Config.load() must fail so the fallback path is the one under test.
        monkeypatch.setattr(
            "techne.config.Config.load", classmethod(lambda cls: (_ for _ in ()).throw(OSError()))
        )

        assert resolve_db_path() is None, "must not resolve to a bystander store"


class TestRetiredLanes:
    def test_legacy_unnamespaced_lane_is_dormant_not_stale(self, config):
        """A lane `enqueue()` would reject is retired by migration, not broken.

        `thread_summary` (bare) is pre-#51 residue that migration v10 left in
        place. Reporting it "producer may be dead" is true and permanently
        un-actionable — no operator action clears it — which is the nag that
        teaches someone to skip the whole report (#469 review 3, finding 2).
        """
        from techne.core.database import connect

        old = _iso(timedelta(days=60))
        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO outbox (target, data_json, content_hash, status, created_at,"
                " synced_at) VALUES (?,?,?,?,?,?)",
                ("thread_summary", "{}", "legacy1", "synced", old, old),
            )
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                ("s", _iso(timedelta(hours=1))),
            )
            conn.commit()
        finally:
            conn.close()

        lane = next(r for r in run_checks(config) if r.name == "outbox:thread_summary")
        assert lane.verdict is Verdict.DORMANT
        assert lane.ok
        assert "retired" in lane.detail


class TestProducerSilenceIsNotWallClock:
    """#469 review 4, Important: the producer test was wall-clock in a costume.

    `check_outbox_targets` judged a drained lane by comparing the newest session
    against the last enqueue. But it took MAX(started_at), which includes the
    IN-FLIGHT session — and because `collect_health` runs in Phase 5, after
    Phase 3 inserts that row, it was always ~now. So `newest > enq + 7d`
    reduced to `now > enq + 7d`: exactly the wall-clock check it replaced, at
    3x the sensitivity. A three-week holiday reported a healthy lane as
    "producer may be dead".

    The honest signal uses MAX(ended_at): these lanes are fed at SESSION END, so
    only a session that ENDED without feeding the lane is evidence.
    """

    def _store(self, tmp_path, *, ended_days_ago, enq_days_ago, inflight):
        import sqlite3

        from techne.config import Config
        from techne.core.database import init_db

        cfg = Config()
        cfg.db_path = tmp_path / f"t{ended_days_ago}-{enq_days_ago}-{inflight}.db"
        cfg.standalone = False
        cfg.sync.adapter = "noop"
        init_db(cfg.db_path)
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("UPDATE schema_version SET applied_at = datetime('now','-90 days')")
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at) VALUES "
            f"('settled', datetime('now','-{ended_days_ago} days'),"
            f" datetime('now','-{ended_days_ago} days'))"
        )
        if inflight:
            # The session the check is running INSIDE — started, not yet ended.
            conn.execute("INSERT INTO sessions (id, started_at) VALUES ('inflight', datetime('now'))")
        conn.execute(
            "INSERT INTO outbox (target, data_json, status, synced_at, created_at) VALUES "
            f"('sink:archivist', '{{}}', 'synced', datetime('now','-{enq_days_ago} days'),"
            f" datetime('now','-{enq_days_ago} days'))"
        )
        conn.commit()
        conn.close()
        return cfg

    def _verdict(self, cfg):
        from techne.core.checks import check_outbox_targets

        for r in check_outbox_targets(cfg):
            if r.name == "outbox:sink:archivist":
                return r.verdict
        raise AssertionError("lane not reported")

    def test_holiday_does_not_report_a_dead_producer(self, tmp_path):
        """Away three weeks: nothing ended recently, so nothing failed to feed."""
        from techne.core.checks import Verdict

        cfg = self._store(tmp_path, ended_days_ago=21, enq_days_ago=21, inflight=True)
        assert self._verdict(cfg) is Verdict.LIVE

    def test_sessions_ending_without_feeding_the_lane_is_stale(self, tmp_path):
        """Sessions ARE ending and the lane is not being fed — real evidence."""
        from techne.core.checks import Verdict

        cfg = self._store(tmp_path, ended_days_ago=1, enq_days_ago=30, inflight=True)
        assert self._verdict(cfg) is Verdict.STALE


class TestSurvivingMutants:
    """Mutants that survived a sweep of the verdict decisions (#469 review 4).

    Each of these is a one-line change to a verdict that the suite did NOT
    notice. They are the load-bearing decisions in the module — the ones that
    decide whether a dead rail reads as healthy — so an unkilled mutant here is
    the module failing at its own stated job.
    """

    def _sessions(self, config, n=2):
        """Two sessions: the session-scoped branch needs a SETTLED one (the
        second-newest), so a single-session store silently takes a different
        path — which is why the existing coverage missed this branch entirely.
        """
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            for i in range(n):
                conn.execute(
                    "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
                    (f"s{i}", _iso(timedelta(days=n - i)), _iso(timedelta(days=n - i))),
                )
            conn.commit()
        finally:
            conn.close()

    def test_session_scoped_hook_with_no_marker_is_unobservable(self, config):
        """M-survivor 1: UNOBSERVABLE -> LIVE went unnoticed on the session-scoped
        branch. This is the single most important verdict in the module — a hook
        that had its chance and left no marker must never read as working.
        """
        self._sessions(config)
        hooks = [r for r in run_checks(config) if r.name.startswith("hook:")]
        scoped = [r for r in hooks if r.name.endswith(("session_start", "session_end"))]
        assert scoped, "expected session-scoped hooks in the cohort"
        assert all(r.verdict is Verdict.UNOBSERVABLE for r in scoped), (
            f"a hook with settled sessions and no marker must be UNOBSERVABLE, got "
            f"{[(r.name, r.verdict.value) for r in scoped]}"
        )
        assert not any(r.ok for r in scoped)

    def test_lane_without_a_consumer_is_dormant_not_live(self, config):
        """M-survivor 2: DORMANT -> LIVE went unnoticed on the no-consumer lane.

        DORMANT and LIVE are both `ok`, so the mutation is invisible to any
        assertion that only checks pass/fail — it has to be pinned on the verdict
        itself. The distinction is the whole point: LIVE claims the lane was
        OBSERVED working, which is a claim this store cannot support.
        """
        config.standalone = True
        config.sync.adapter = "noop"
        self._sessions(config)
        lanes = [r for r in run_checks(config) if r.name.startswith("outbox:")]
        assert lanes, "expected a declared lane"
        assert all(r.verdict is Verdict.DORMANT for r in lanes), (
            f"a lane with no consumer is correctly idle, not observed-live, got "
            f"{[(r.name, r.verdict.value) for r in lanes]}"
        )
        assert all("no live consumer" in (r.detail or "") for r in lanes)

    def test_declared_lane_with_a_consumer_that_never_enqueued_is_unobservable(self, config):
        """M-survivor 3: UNOBSERVABLE -> DORMANT went unnoticed.

        The difference is exactly RULE 1 (techne#959): a lane that SHOULD be
        emitting and never has is a FAILING state, not a benign idle one.
        Collapsing it to DORMANT is how a dead lane goes quiet for eight weeks.
        """
        from techne.core.database import connect

        config.standalone = False  # team topology -> the sink lane has a consumer
        self._sessions(config)
        conn = connect(config.db_path)
        try:  # age the store past the fresh-install grace
            conn.execute("UPDATE schema_version SET applied_at = ?", (_iso(timedelta(days=60)),))
            conn.commit()
        finally:
            conn.close()

        lanes = [r for r in run_checks(config) if r.name.startswith("outbox:")]
        assert lanes, "expected a declared lane"
        assert any(r.verdict is Verdict.UNOBSERVABLE for r in lanes), (
            f"a declared lane with a live consumer that never enqueued must FAIL, got "
            f"{[(r.name, r.verdict.value) for r in lanes]}"
        )


class TestNoopExcusesOnlySyncLanes:
    """#469 review 5, Critical: `sync.adapter == "noop"` excused a pending
    backlog on ANY lane, including `sink:` ones.

    `sync.adapter` governs `flush(adapter)`, which touches only
    `WHERE target LIKE 'sync:%'`; `sink:` rows are drained by `flush_sinks`. And
    on the TEAM topology `sync.adapter` is EXPECTED to stay "noop" — so this was
    not a corner case, it was the normal configuration of the lane the check's
    own docstring is about. A 56-day-old pending `sink:archivist` row read as
    `dormant, ok=True`.

    Same reasoning error as knowledge#967: "pending + noop, therefore parked",
    applied to a lane the adapter does not govern.
    """

    def _pending(self, config, target, days_old):
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", (_iso(timedelta(days=90)),))
            conn.execute(
                "INSERT INTO outbox (target, data_json, status, created_at) VALUES (?,?,?,?)",
                (target, "{}", "pending", _iso(timedelta(days=days_old))),
            )
            conn.commit()
        finally:
            conn.close()

    def test_sink_backlog_is_not_excused_by_the_sync_adapter(self, config):
        config.standalone = False  # team topology — noop here is EXPECTED
        config.sync.adapter = "noop"
        self._pending(config, "sink:archivist", 56)
        lane = next(r for r in run_checks(config) if r.name == "outbox:sink:archivist")
        assert lane.verdict is not Verdict.DORMANT, (
            "a sink backlog is not the sync adapter's business; noop must not excuse it"
        )
        assert not lane.ok

    def test_sync_backlog_is_still_excused_by_noop(self, config):
        """The other half — noop genuinely does park `sync:` rows on purpose, and
        that must keep reading as healthy or the fix trades one false verdict for
        another."""
        config.standalone = True
        config.sync.adapter = "noop"
        self._pending(config, "sync:thread_summary", 56)
        lane = next(r for r in run_checks(config) if r.name == "outbox:sync:thread_summary")
        assert lane.verdict is Verdict.DORMANT
        assert lane.ok


class TestMarkerIsStampedAtWriteTime:
    """#469 review 5: reverting critical 1 (`last_run=_now_iso()` back to
    `run_ts`) left the whole suite GREEN. The fix had no pin at all.

    `run_ts` is captured as the first statement of main(); the session row is
    INSERTed in Phase 3. A marker carrying `run_ts` is therefore older than the
    row `check_hooks` compares it against, which rendered a healthy hook STALE
    on roughly 7% of sessions. Driving the phases with a deliberately ancient
    `run_ts` makes the ordering deterministic instead of a ~70ms race.
    """

    def test_marker_is_not_older_than_the_session_it_registered(self, config, tmp_path):
        """DRIVES THE REAL CALL PATH. My first attempt at this pin called
        `_persist_hook_state` directly with `_now_iso()` — which is what main()
        does, so it passed even with critical 1 reverted. It tested the helper
        with the right argument instead of testing that the caller supplies it.

        `_run_phases_2_to_9` is where the persist call lives, so driving it with
        a deliberately ancient `run_ts` makes the ordering deterministic rather
        than a ~70ms race: if the marker is stamped from `run_ts`, it lands a
        year before the session row it just registered.
        """
        from techne.core.database import connect
        from techne.core.extension_state import get_state
        from techne.hooks import session_start as ss

        ancient = _iso(timedelta(days=365))
        ss._run_phases_2_to_9(
            config,
            {"cwd": str(tmp_path), "session_id": "cc-sess"},
            run_ts=ancient,
            db_path=config.db_path,
            output_parts=[],
            failures=[],
        )

        conn = connect(config.db_path)
        try:
            marker = get_state(conn, "hook:session_start:last_run")
            row = conn.execute("SELECT MAX(started_at) FROM sessions").fetchone()
        finally:
            conn.close()

        assert marker and marker.get("ts"), "session_start must leave a marker"
        assert marker["ts"] != ancient, (
            "marker was stamped from run_ts (hook entry), not write time — "
            "this is critical 1 reverting"
        )
        assert row[0] and marker["ts"] >= row[0], (
            f"marker {marker['ts']} predates the session row {row[0]} it registered"
        )
