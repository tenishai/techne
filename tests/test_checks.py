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

    def test_outbox_target_with_no_backlog_is_not_stale(self, config):
        """The discriminator is the BACKLOG, not the clock: an old target that
        owes nothing is idle, not broken."""
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO outbox (target, data_json, content_hash, status, created_at,"
                " synced_at) VALUES (?,?,?,?,?,?)",
                ("t", "{}", "h1", "synced", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        results = run_checks(config)
        target = next(r for r in results if r.name == "outbox:t")
        assert target.verdict is Verdict.LIVE
        assert "nothing owed" in target.detail

    def test_every_result_carries_evidence(self, config):
        """techne#941's 'demand proof of absence' rule: a verdict a reader
        cannot re-derive is indistinguishable from one nobody checked."""
        for r in run_checks(config):
            assert r.evidence, f"{r.name} has no evidence"

    def test_summarize_counts_by_verdict(self, config):
        counts = summarize(run_checks(config))
        assert sum(counts.values()) == len(run_checks(config))
