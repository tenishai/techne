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
        self._enqueue(config, "t", recent, recent)

        target = next(r for r in run_checks(config) if r.name == "outbox:t")
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
        old = _iso(timedelta(days=60))
        self._enqueue(config, "t", old, old)

        target = next(r for r in run_checks(config) if r.name == "outbox:t")
        assert target.verdict is Verdict.STALE
        assert "producer may be dead" in target.detail

    def test_declared_lane_that_never_enqueued_still_produces_a_result(self, config):
        """Deriving the lane list from `GROUP BY target` made a never-used lane
        VANISH from the report — reintroducing, one layer up, the exact
        absence-reads-as-healthy bug run_checks prevents for raising probes.
        """
        from techne.core import checks as checks_mod
        from techne.core.database import connect

        # Age the store past the fresh-install grace so absence is a finding.
        conn = connect(config.db_path)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", ("2026-01-01 00:00:00",))
            conn.commit()
        finally:
            conn.close()

        names = {r.name for r in run_checks(config)}
        for declared in checks_mod._DECLARED_OUTBOX_TARGETS:
            assert f"outbox:{declared}" in names, f"{declared} vanished from the report"

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
