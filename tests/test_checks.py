"""Tests for the subsystem-liveness registry (techne#468).

The invariant under test throughout: **a check must never render as passing
when the subsystem's liveness is unknown.** Every failure in techne#942 was
silent, and silence was also what success looked like — so the taxonomy's job
is to make "cannot tell" a distinct, failing answer.
"""

from __future__ import annotations

import os
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

        # Enable every config-gated rail, so this covers the whole cohort —
        # a gated-off hook is correctly DORMANT and would otherwise mask the
        # assertion (see TestConfigGateBeatsMarker).
        _enable_all_config_gated_hooks(config)

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
        _assert_no_hook_is_gated_off(hooks)
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


def _enable_all_config_gated_hooks(config):
    """Turn ON every rail behind a config flag, so a cohort assertion covers all
    of them. A gated-off hook is correctly DORMANT and would otherwise silently
    shrink the cohort a test believes it is checking.

    Kept as one helper because the alternative — a flag list inline in each test —
    drifted the moment techne#479 added two gates: two cohort tests started
    passing over a smaller set than they claimed. Pair it with
    ``_assert_no_hook_is_gated_off`` so an unlisted future gate fails loudly here
    rather than quietly narrowing the assertion.
    """
    config.hooks.codex_grep_assist = True
    config.hooks.per_turn_recall = True


def _assert_no_hook_is_gated_off(hooks):
    """Guard for the helper above: every hook check must have been reachable."""
    from techne.core.checks import Verdict

    gated = [
        r.name
        for r in hooks
        if r.verdict is Verdict.DORMANT and "disabled by config" in r.detail
    ]
    assert not gated, (
        f"these hooks are gated off, so the cohort assertion below does not cover them: {gated}. "
        "Add their flag to _enable_all_config_gated_hooks."
    )


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
        _enable_all_config_gated_hooks(config)  # else correctly DORMANT by config

        hooks = [r for r in run_checks(config) if r.name.startswith("hook:")]
        assert hooks
        _assert_no_hook_is_gated_off(hooks)
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

    def test_a_superseded_row_does_not_count_as_missing_forever(self, config):
        """techne#496 — THE CHECK COULD NEVER GO GREEN.

        `backfill_vectors` embeds only `superseded_by IS NULL` and purges the
        embeddings of superseded rows (techne#168: a stale vector occupies a
        top-k slot). The denominator was ALL knowledge, so every superseded row
        was counted as permanently "missing" — 16 of 786 on the live store,
        verdict STALE forever, with a remedy that provably embedded nothing.
        Superseding is normal operation, so the phantom count only ever grew.

        Pins the arithmetic on LITERALS, not on the implementation's own query:
        two rows, one superseded, one live-and-embedded => LIVE, 1/1.
        """
        from techne.core.database import connect
        from techne.core.knowledge import store

        keep = store(config.db_path, "the live row that must be embedded", type="fact")
        gone = store(config.db_path, "the superseded row that must not count", type="fact")

        conn = connect(config.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_vec_rowids (rowid INTEGER)")
            conn.execute("UPDATE knowledge SET superseded_by = ? WHERE id = ?", (keep, gone))
            # Only the live row is embedded — exactly what backfill_vectors leaves behind.
            conn.execute("INSERT INTO knowledge_vec_rowids (rowid) VALUES (?)", (keep,))
            conn.commit()
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        assert cov.verdict is Verdict.LIVE, f"superseded row counted as missing: {cov.detail}"
        assert "1/1" in cov.detail
        assert cov.remedy is None

    def test_a_superseded_row_that_kept_its_embedding_is_reported(self, config):
        """The other half of techne#496: scoping must not HIDE an orphan.

        Nothing auto-runs backfill in production, so a row superseded AFTER being
        embedded keeps a live ANN vector until the next explicit run. Scoping both
        counts to live rows would make that invisible. It is counted separately
        and fails the check, because the same remedy genuinely fixes it — the
        purge runs at the head of backfill's knowledge pass.
        """
        from techne.core.database import connect
        from techne.core.knowledge import store

        keep = store(config.db_path, "a live embedded row", type="fact")
        gone = store(config.db_path, "superseded but still carrying a vector", type="fact")

        conn = connect(config.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_vec_rowids (rowid INTEGER)")
            conn.execute("UPDATE knowledge SET superseded_by = ? WHERE id = ?", (keep, gone))
            # BOTH embedded — the superseded one's vector was never purged.
            conn.executemany(
                "INSERT INTO knowledge_vec_rowids (rowid) VALUES (?)", [(keep,), (gone,)]
            )
            conn.commit()
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        assert cov.verdict is Verdict.STALE, "an unpurged superseded embedding must be reported"
        assert "superseded row(s) still embedded" in cov.detail
        assert cov.remedy == "techne vectors backfill"

    def test_an_edited_row_is_not_covered_just_because_it_is_present(self, config):
        """techne#496 / #497 review — THE MIRROR-IMAGE FALSE GREEN.

        `backfill_vectors` re-embeds `(NOT in vec OR vec_stale = 1) AND
        superseded_by IS NULL`. Scoping the denominator while counting only
        "is it present" counted two of that population's three parts, so a row
        edited in place — `vec_stale = 1`, set on ANY in-place content edit —
        read as covered. Measured before the fix: 2/2, 100%, remedy None, while
        a backfill had real work to do. That is the false-green shape this check
        exists to remove, arriving from the opposite side of the never-green one.
        """
        from techne.core.database import connect
        from techne.core.knowledge import store

        edited = store(config.db_path, "this row gets rewritten in place", type="fact")
        clean = store(config.db_path, "this row is untouched", type="fact")

        conn = connect(config.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_vec_rowids (rowid INTEGER)")
            conn.executemany(
                "INSERT INTO knowledge_vec_rowids (rowid) VALUES (?)", [(edited,), (clean,)]
            )
            # Exactly what an in-place content edit does (see cards.py).
            conn.execute(
                "UPDATE knowledge SET content = 'rewritten', vec_stale = 1 WHERE id = ?",
                (edited,),
            )
            conn.commit()
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        assert cov.verdict is Verdict.STALE, f"edited row read as covered: {cov.detail}"
        assert "1/2" in cov.detail
        assert "1 stale (edited since embedding)" in cov.detail
        assert cov.remedy == "techne vectors backfill"

    def test_a_dangling_vector_is_reported_but_carries_no_remedy(self, config):
        """The mutation that survived the whole suite (#497 review, finding 1).

        Reverting `embedded` to a raw `COUNT(*) FROM knowledge_vec_rowids` passed
        all 2449 tests, because on a healthy store the two counts coincide. The
        state that separates them is a vec rowid with NO knowledge row behind it,
        which is reachable: `knowledge.delete()` and the pattern cleanup both
        delete through a plain `connect()`, which cannot touch a vec0 table.

        AND IT MUST NOT FAIL THE CHECK. The purge deletes `WHERE rowid IN (SELECT
        id FROM knowledge WHERE superseded_by IS NOT NULL)` — it selects FROM
        knowledge, so a rowid with no row behind it is never matched (verified:
        purge removed 0 of 2). Failing here would attach a remedy that cannot
        clear the finding, which is precisely the never-green defect #496 fixed.
        Visible in the detail, absent from the verdict.
        """
        from techne.core.database import connect
        from techne.core.knowledge import store

        kept = store(config.db_path, "the row that survives", type="fact")
        doomed = store(config.db_path, "hard-deleted after being embedded", type="fact")

        conn = connect(config.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS knowledge_vec_rowids (rowid INTEGER)")
            conn.executemany(
                "INSERT INTO knowledge_vec_rowids (rowid) VALUES (?)", [(kept,), (doomed,)]
            )
            conn.execute("DELETE FROM knowledge WHERE id = ?", (doomed,))
            conn.commit()
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        # The live population is whole, so the check is GREEN...
        assert cov.verdict is Verdict.LIVE, f"dangling must not create an unclearable red: {cov}"
        assert cov.remedy is None
        # ...but the dangling vector is NOT swallowed.
        assert "1 dangling vector(s) with no knowledge row" in cov.detail
        assert "dangling=1" in cov.evidence

    def test_the_check_agrees_with_a_REAL_vec0_shadow_table(self, config):
        """#497 review nit 4 — the other tests supply the schema they assert against.

        This PR strengthened the dependency on `knowledge_vec_rowids` from
        CARDINALITY (`COUNT(*)`) to IDENTITY (`id IN (SELECT rowid ...)`). Every
        other test in this class creates that table itself as a plain table, so
        none of them can fail if the real vec0 shadow table disagrees — the
        assertion and the fixture share an author. This one builds a REAL vec0
        virtual table via sqlite_vec and checks the same query works against it,
        which is the only way the identity assumption is actually pinned.

        Skips where sqlite_vec is unavailable (the repo's existing pattern —
        tests/test_cli_vectors.py), so it is a bonus pin, never a false green:
        a skip here does not weaken the other tests, it just does not strengthen
        them.
        """
        import struct

        sqlite_vec = pytest.importorskip("sqlite_vec")

        from techne.core.database import connect
        from techne.core.knowledge import store
        from techne.core.search import _ensure_vec_table

        live = store(config.db_path, "embedded in a real vec0 table", type="fact")
        store(config.db_path, "not embedded at all", type="fact")  # makes total=2

        conn = connect(config.db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            _ensure_vec_table(conn, "knowledge_vec", 8)
            conn.execute(
                "INSERT INTO knowledge_vec(rowid, embedding) VALUES (?, ?)",
                (live, struct.pack("8f", *([0.1] * 8))),
            )
            conn.commit()
            # The shadow table must expose rowid the way the check assumes.
            rowids = [r[0] for r in conn.execute("SELECT rowid FROM knowledge_vec_rowids")]
            assert rowids == [live], f"real vec0 rowid shape differs: {rowids}"
        finally:
            conn.close()

        cov = next(r for r in run_checks(config) if r.name == "vectors:coverage")
        assert cov.verdict is Verdict.STALE
        assert "1/2" in cov.detail
        assert "1 missing" in cov.detail
        assert "embedded=1" in cov.evidence
        assert "dangling=0" in cov.evidence, "a real vec0 row must not read as dangling"


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

    @staticmethod
    def _doctor_on_a_store_aged(tmp_path, monkeypatch, age: timedelta) -> tuple[str, object]:
        """Run `doctor` against a fresh store of a chosen age, and nothing else.

        READS THE STORE IT BUILDS — the property this helper exists to guarantee.
        Setting HOME is not enough: ``Config.load()`` honors ``TECHNE_HOME``
        FIRST, and conftest deliberately touches no environment variable (for
        good reason — redirecting it leaks into the many tests that relocate via
        their own env). So on any techne-instrumented machine, including the
        session that authored this PR, this test ran `doctor` against the
        DEVELOPER'S REAL LIVE STORE and every assertion below described that
        instead of the fixture. Harmless to the store (no --vacuum) and lethal to
        the verdict: on a machine whose techne is genuinely healthy the positive
        assertion went red on a perfectly good PR. (#469 review 7, issue 4.)
        """
        from click.testing import CliRunner

        from techne.cli import main
        from techne.core.database import connect, init_db

        home = tmp_path / "h"
        (home / ".techne").mkdir(parents=True)
        db = home / ".techne" / "techne.db"
        init_db(db).close()
        conn = connect(db)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", (_iso(age),))
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("TECHNE_HOME", raising=False)

        out = CliRunner().invoke(main, ["doctor"]).output
        # The store under test is the store that was graded. Without this the
        # whole class can pass or fail on the developer's ambient environment.
        assert str(db) in out, f"doctor graded a different store; got: {out!r}"
        return out, db

    def test_never_claims_all_live_when_none_are(self, tmp_path, monkeypatch):
        """The DORMANT branch — a young store where nothing has run yet.

        ONE DAY OLD ON PURPOSE, inside _NEW_STORE_GRACE. That is the exact state
        finding 1's symptom needed: every check DORMANT, `failing == 0`, and
        doctor nonetheless printing "All N subsystems confirmed live". Ageing the
        store past the grace (which is what I did first, and what this comment
        exists to stop the next person repeating) moves the test into the
        `failing > 0` branch where that string is UNREACHABLE BY CONSTRUCTION —
        so the negative assertion could no longer fail whatever the other branch
        does, and the test passed on code containing the bug it was written for.
        The red came from the POSITIVE assertion, added in review 4 for a branch
        this setup never reached; the fix is to match the assertion to the
        branch, not the branch to the assertion. (#469 review 7, issue 3.)
        """
        import re

        # DRIVEN WITH AN ALL-DORMANT RESULT SET, not with a young store.
        #
        # A young store USED to produce that state, and this test relied on it.
        # It no longer does: checks that observe MACHINE STATE rather than store
        # activity (mcp:registration in #370, runtime:revision in #473) are
        # legitimately LIVE on a brand-new store, and each one broke this test in
        # turn. The premise was never "the store is young" — it was "nothing is
        # confirmable", which is the state finding 1's symptom needed. Building
        # that state directly keeps the test about the RENDERING rule and stops it
        # failing every time a state-based check is added.
        #
        # The end-to-end path stays covered by the 30-day sibling below.
        from techne.core import checks as _checks_mod

        def _all_dormant(config):
            return [
                CheckResult(
                    name=f"hook:{n}",
                    subsystem="hooks",
                    kind=Kind.TEMPORAL,
                    verdict=Verdict.DORMANT,
                    detail="nothing has run",
                )
                for n in ("a", "b", "c")
            ]

        monkeypatch.setattr(_checks_mod, "run_checks", _all_dormant)
        out, _db = self._doctor_on_a_store_aged(tmp_path, monkeypatch, timedelta(days=1))

        # ASSERT AGAINST WHAT THE CODE EMITS, NOT A PARAPHRASE (#469 review 4,
        # M20). The previous assertion was `"All subsystems confirmed live" not
        # in out` — a literal that appears nowhere in cli.py except inside a
        # COMMENT. Every emitted string interpolates a count
        # (f"All {live} subsystems confirmed live."), so the assertion could
        # never fail and the teeth-check it was written for never took.
        assert not re.search(r"All \d+ subsystems confirmed live", out), (
            f"must not confirm liveness it never observed; got: {out!r}"
        )
        # The positive half — silence is not proof. Assert the output actually
        # SAYS something honest, so a future change that emits nothing at all
        # cannot pass on the negative assertion alone. This is the line the
        # `failing == 0` branch really emits for a store with zero live checks.
        assert re.search(r"0 of \d+ subsystems confirmed live", out), (
            f"expected the honest zero-confirmed line; got: {out!r}"
        )

    def test_reports_a_not_confirmed_count_once_the_grace_expires(self, tmp_path, monkeypatch):
        """The failing branch — the other half, pinned separately rather than
        by moving the first test's setup.

        Past _NEW_STORE_GRACE, "no session has ever been recorded" stops being a
        young store and becomes a FINDING, so the hooks escalate to UNOBSERVABLE
        and doctor must both exit non-zero and say how many.
        """
        import re

        out, _db = self._doctor_on_a_store_aged(tmp_path, monkeypatch, timedelta(days=30))

        assert re.search(r"\d+ subsystem\(s\) not confirmed working", out), (
            f"expected an explicit not-confirmed count; got: {out!r}"
        )
        assert not re.search(r"All \d+ subsystems confirmed live", out), (
            f"must not confirm liveness it never observed; got: {out!r}"
        )


class TestSeverityInvariants:
    """The founding invariant, previously held by an untested constant.

    Both `_SEVERITY[UNKNOWN]` and `CheckResult.ok` were unpinned in both
    directions: setting the first to 0 makes a RAISED probe tie with a healthy
    rail (banner renders ✅, failure never named), and admitting STALE to the
    second makes `techne doctor` print "All checks passed" and exit 0 on a cold
    rail. (#469 review 9.)
    """

    def test_a_probe_that_could_not_run_outranks_a_healthy_one(self):
        from techne.core.checks import Verdict, worst

        def _r(v):
            from techne.core.checks import CheckResult, Kind

            return CheckResult(name="x", subsystem="s", kind=Kind.TEMPORAL, verdict=v, detail="")

        assert worst([_r(Verdict.LIVE), _r(Verdict.UNKNOWN)]) is Verdict.UNKNOWN
        assert worst([_r(Verdict.DORMANT), _r(Verdict.UNKNOWN)]) is Verdict.UNKNOWN
        # ...and loses to a real finding, which is more informative than a
        # broken probe. This direction is deliberate, not incidental.
        assert worst([_r(Verdict.UNKNOWN), _r(Verdict.STALE)]) is Verdict.STALE
        assert worst([_r(Verdict.UNKNOWN), _r(Verdict.BROKEN)]) is Verdict.BROKEN

    def test_only_live_and_dormant_are_ok(self):
        from techne.core.checks import CheckResult, Kind, Verdict

        def _ok(v):
            return CheckResult(
                name="x", subsystem="s", kind=Kind.TEMPORAL, verdict=v, detail=""
            ).ok

        assert _ok(Verdict.LIVE) and _ok(Verdict.DORMANT)
        for bad in (Verdict.STALE, Verdict.UNOBSERVABLE, Verdict.BROKEN, Verdict.UNKNOWN):
            assert not _ok(bad), bad


class TestInitRemedy:
    """One builder for the `techne init` advice, because the same defect — a
    remedy that is precise, confident and wrong — appeared at three sites in one
    PR. (#469 review 7, critical 2.)"""

    def test_no_remedy_off_claude_code(self):
        from techne.config import Config
        from techne.core.checks import init_remedy

        cfg = Config()
        cfg.platform = "opencode"
        assert init_remedy(cfg) is None

    def test_remedy_carries_the_operators_posture_not_a_literal(self, tmp_path):
        """An ESTABLISHED posture is preserved verbatim.

        `loaded_from` is what makes spool-primary a choice rather than an
        accident of the dataclass: without a config.yaml there is nothing to
        preserve, and this test would be asserting that we pin a default
        (techne#480).
        """
        from techne.config import Config
        from techne.core.checks import init_remedy

        config_file = tmp_path / "config.yaml"
        config_file.write_text("platform: claude-code\nposture:\n  mode: spool-primary\n")

        cfg = Config()
        cfg.platform = "claude-code"
        cfg.posture.mode = "spool-primary"
        cfg.loaded_from = config_file
        assert init_remedy(cfg) == "techne init --platform claude-code --posture spool-primary"

    def test_remedy_asks_for_auto_when_the_machine_was_never_initialised(self):
        """techne#480 — the neighbouring case the posture guard was over-applied to.

        No config.yaml means posture.mode is the dataclass default `observe`,
        not a preserved answer. Pinning it there suppresses the auto-detection
        that would have found spool-primary from the CC memory on disk, and
        freezes a first install into the weakest posture — doing the exact
        harm the guard exists to prevent, one case over.
        """
        from techne.config import Config
        from techne.core.checks import init_remedy

        cfg = Config()
        cfg.platform = "claude-code"
        assert cfg.loaded_from is None, "a directly-constructed Config has no file"
        assert cfg.posture.mode == "observe", "the default this must NOT pin"

        assert init_remedy(cfg) == "techne init --platform claude-code --posture auto"

    def test_never_initialised_still_inherits_the_platform_guard(self):
        """The new branch must not become a second surface that skips a guard.

        init_remedy has ONE builder precisely so a new case cannot be added
        without inheriting both guards (#469 review 7, critical 2). The
        never-initialised branch is inside it, so an off-platform machine still
        gets no remedy at all.
        """
        from techne.config import Config
        from techne.core.checks import init_remedy

        cfg = Config()
        cfg.platform = "opencode"
        assert cfg.loaded_from is None
        assert init_remedy(cfg) is None

    def test_healthy_hooks_carry_no_remedy(self, tmp_path):
        """The verdict gate, which was unpinned.

        Dropping `if verdict in (UNOBSERVABLE, BROKEN)` made doctor print
        "remedy: techne init ..." beneath six green ✓ live hooks — telling the
        operator to reinstall rails that demonstrably work. (Review 8, nit 6.)
        """
        from techne.config import Config
        from techne.core.checks import Verdict, check_hooks
        from techne.core.database import connect, init_db
        from techne.hooks._liveness import record_hook_run

        db = tmp_path / "techne.db"
        init_db(db).close()
        conn = connect(db)
        try:
            conn.execute(
                "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
                ("s1", _iso(timedelta(hours=2)), _iso(timedelta(hours=1))),
            )
            conn.commit()
        finally:
            conn.close()
        # A marker newer than that session, written through the real recorder.
        for hook in ("session_start", "session_end"):
            record_hook_run(db, hook, ts=_iso(timedelta(minutes=1)))

        cfg = Config()
        cfg.db_path = db
        results = {r.name: r for r in check_hooks(cfg)}
        live = [r for r in results.values() if r.verdict is Verdict.LIVE]
        assert live, [(r.name, r.verdict) for r in results.values()]
        assert all(r.remedy is None for r in live), [(r.name, r.remedy) for r in live]

    def test_hook_checks_carry_no_remedy_off_claude_code(self, tmp_path):
        """The site that was printing it six times per run.

        An UNOBSERVABLE hook on an opencode config used to advise `techne init
        --platform claude-code --posture observe` — wrong platform AND a
        hardcoded posture that demotes a spool-primary operator who follows it.
        """
        from techne.config import Config
        from techne.core.checks import Verdict, check_hooks
        from techne.core.database import connect, init_db

        db = tmp_path / "techne.db"
        init_db(db).close()
        conn = connect(db)
        try:
            conn.execute("UPDATE schema_version SET applied_at = ?", (_iso(timedelta(days=30)),))
            conn.commit()
        finally:
            conn.close()

        cfg = Config()
        cfg.db_path = db
        cfg.platform = "opencode"
        results = check_hooks(cfg)
        escalated = [r for r in results if r.verdict in (Verdict.UNOBSERVABLE, Verdict.BROKEN)]
        assert escalated, "expected at least one escalated hook on an aged, sessionless store"
        assert all(r.remedy is None for r in escalated), [r.remedy for r in escalated]


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


class TestCoreRailsAreInstrumented:
    """techne#479 — the capture and per-turn-recall rails must REPORT.

    They worked for months and said nothing, so a dead rail and an idle operator
    produced identical evidence. These tests drive each hook's real `main()`
    rather than calling `record_hook_run` directly: the defect being pinned is
    "the caller never records", and a test that records on the hook's behalf
    would pass against exactly the code it is meant to reject.
    """

    @staticmethod
    def _run_hook(module, payload, config, monkeypatch):
        """Drive a hook's main() with a stdin payload, returning its SystemExit."""
        import io
        import json as _json

        from techne.config import Config

        monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
        monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(payload)))
        # Some paths sys.exit(0), others simply return — both are "exited 0" as
        # far as the host is concerned, and asserting on one shape would make
        # these tests brittle against a refactor that is not the thing under test.
        try:
            module.main()
        except SystemExit as exc:
            assert exc.code in (0, None), f"a hook must never exit non-zero: {exc.code}"
            return exc.code
        return 0

    @staticmethod
    def _seed_session(config, session_id="s1"):
        """observations.session_id is a FOREIGN KEY — a capture against a session
        row that does not exist fails at the DB, which is a real error and not
        the state under test."""
        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at) VALUES (?, ?)",
                (session_id, "2026-08-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _marker(config, hook):
        from techne.core.database import connect
        from techne.core.extension_state import get_state

        conn = connect(config.db_path)
        try:
            return get_state(conn, f"hook:{hook}:last_run") or {}
        finally:
            conn.close()

    def test_post_tool_use_records_a_capture(self, config, monkeypatch, tmp_path):
        from techne.hooks import post_tool_use

        config.hooks.post_tool_use = True
        self._seed_session(config)
        code = self._run_hook(
            post_tool_use,
            {
                "session_id": "s1",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "a.py")},
                "tool_response": "ok",
            },
            config,
            monkeypatch,
        )
        assert code == 0 or code is None
        marker = self._marker(config, "post_tool_use")
        assert marker.get("outcome") == "ok", marker
        assert marker.get("ts")

    def test_post_tool_use_records_a_filtered_call_as_a_noop_not_silence(
        self, config, monkeypatch
    ):
        """A tool the capture filter skips is still a RUN. Recording nothing here
        is what made a quiet rail indistinguishable from a dead one."""
        from techne.hooks import post_tool_use

        config.hooks.post_tool_use = True
        self._seed_session(config)
        # Read with capture_read OFF (the default) — the filter that fires most
        # often in practice, so this is the ordinary quiet case rather than a
        # contrived one.
        config.hooks.capture_read = False
        self._run_hook(
            post_tool_use,
            {
                "session_id": "s1",
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x"},
                "tool_response": "",
            },
            config,
            monkeypatch,
        )
        assert self._marker(config, "post_tool_use").get("outcome") == "noop:filtered"

    def test_post_tool_use_marker_makes_doctor_report_it_live(self, config, monkeypatch, tmp_path):
        """End to end: the marker must move the REPORTED VERDICT, which is the
        only thing the operator ever sees."""
        from techne.hooks import post_tool_use

        config.hooks.post_tool_use = True
        self._seed_session(config)
        before = next(r for r in run_checks(config) if r.name == "hook:post_tool_use")
        assert before.verdict is not Verdict.LIVE

        self._run_hook(
            post_tool_use,
            {
                "session_id": "s1",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "a.py")},
                "tool_response": "ok",
            },
            config,
            monkeypatch,
        )
        after = next(r for r in run_checks(config) if r.name == "hook:post_tool_use")
        assert after.verdict is Verdict.LIVE, after.detail

    def test_capture_disabled_still_records_because_the_hook_still_runs(
        self, config, monkeypatch, tmp_path
    ):
        """`hooks.post_tool_use` turns off CAPTURE, not the hook.

        main() goes on to run the reminder (#207), coord (#376) and codex edit
        (#335) rails regardless. Reporting DORMANT-by-config there would leave
        those three unobservable behind a tidy "disabled by config" — a WRONG
        verdict, not a cheap one, which is why this hook carries no entry in
        _HOOK_CONFIG_GATE. (#479 review 1, found by driving the real main() with
        the flag off and a due reminder seeded.)
        """
        from techne.hooks import post_tool_use

        config.hooks.post_tool_use = False
        self._seed_session(config)
        self._run_hook(
            post_tool_use,
            {
                "session_id": "s1",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / "a.py")},
                "tool_response": "ok",
            },
            config,
            monkeypatch,
        )
        assert self._marker(config, "post_tool_use").get("outcome") == "noop:capture-disabled"

        verdict = next(r for r in run_checks(config) if r.name == "hook:post_tool_use")
        assert "disabled by config" not in (verdict.detail or ""), (
            "a hook that still runs three other rails must not read as config-dormant"
        )

    def test_user_prompt_submit_records_an_empty_recall_as_a_noop(self, config, monkeypatch):
        """"Injected nothing" is a legal outcome (#414), and it is the ONLY thing
        that distinguishes a working-but-quiet retrieval rail from a dead one."""
        from techne.hooks import user_prompt_submit

        config.hooks.per_turn_recall = True
        self._run_hook(
            user_prompt_submit, {"prompt": "nothing will match this"}, config, monkeypatch
        )
        assert self._marker(config, "user_prompt_submit").get("outcome") == "noop:nothing-to-inject"

    def test_user_prompt_submit_writes_nothing_when_the_rail_is_disabled(
        self, config, monkeypatch
    ):
        """Both flags default OFF and this fires on EVERY prompt — a marker write
        for a rail the operator has not enabled would be a per-turn write for no
        signal. The config gate answers that case without one."""
        from techne.hooks import user_prompt_submit

        config.hooks.per_turn_recall = False
        config.hooks.per_turn_seed = False
        self._run_hook(user_prompt_submit, {"prompt": "hello"}, config, monkeypatch)
        assert self._marker(config, "user_prompt_submit") == {}

        verdict = next(r for r in run_checks(config) if r.name == "hook:user_prompt_submit")
        assert verdict.verdict is Verdict.DORMANT
        assert "disabled by config" in verdict.detail

    def test_a_degraded_recall_does_not_read_as_a_clean_run(self, config, monkeypatch):
        """The recall block is caught and swallowed by design, so the seed block
        can still emit. Without folding the failure into the marker, a rail whose
        every search raises would write outcome=ok forever and report LIVE."""
        from techne.hooks import user_prompt_submit

        config.hooks.per_turn_recall = True
        config.hooks.per_turn_seed = True  # so the hook still emits a block

        def _boom(*a, **kw):
            raise RuntimeError("search exploded")

        monkeypatch.setattr("techne.core.search.search", _boom)
        self._run_hook(user_prompt_submit, {"prompt": "anything"}, config, monkeypatch)

        marker = self._marker(config, "user_prompt_submit")
        assert marker.get("outcome") == "failed", marker
        verdict = next(r for r in run_checks(config) if r.name == "hook:user_prompt_submit")
        assert verdict.verdict is Verdict.BROKEN

    def test_pre_compact_records_a_checkpoint(self, config, monkeypatch):
        from techne.hooks import pre_compact

        self._run_hook(
            pre_compact, {"session_id": "s1", "trigger": "auto"}, config, monkeypatch
        )
        marker = self._marker(config, "pre_compact")
        assert marker.get("outcome") == "ok", marker

    def test_no_hook_is_left_uninstrumented(self):
        """The list and the code must not drift apart again.

        Every hook `checks` expects a marker from must contain a real
        `record_hook_run` call — otherwise doctor reports UNOBSERVABLE forever
        with a remedy that cannot clear it, which is a red the operator cannot
        act on (#469 review 10, nit 2).
        """
        import ast
        import pathlib

        from techne.core.checks import _HOOK_EXPECTATIONS

        root = pathlib.Path(__file__).resolve().parents[1] / "techne" / "hooks"
        # session_start/session_end/codex_grep_assist live in differently-named
        # modules; map the ones whose file name is not the hook name.
        module_for = {"codex_grep_assist": "pre_tool_use"}

        def _real_calls(path: pathlib.Path) -> int:
            """COUNT CALLS, NOT MENTIONS.

            This was a substring check, and post_tool_use.py names
            `record_hook_run` in a COMMENT — so that file satisfied the guard
            with every real call deleted. A guard that a comment can pass is not
            a guard. Parsing is the only version that cannot be talked into
            passing: an import, a docstring and a prose mention are all invisible
            to it. (#479 review 1.)
            """
            tree = ast.parse(path.read_text())
            return sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "record_hook_run"
            )

        missing = []
        for hook in _HOOK_EXPECTATIONS:
            src = root / f"{module_for.get(hook, hook)}.py"
            if not src.exists():
                missing.append(f"{hook} (no module {src.name})")
            elif _real_calls(src) == 0:
                missing.append(hook)
        assert not missing, f"expected a liveness marker, found no record_hook_run call: {missing}"


class TestRuntimeRevision:
    """techne#473 — a merged fix is not a deployed fix, and nothing said so.

    Demonstrated live on 2026-08-03: PR #472 merged to main while the machine's
    interpreter pointed at a shared working tree sitting on a peer's branch, so
    the machine kept running the bug. Merging changed nothing about what
    executed.
    """

    @staticmethod
    def _repo(tmp_path, behind: int = 0, dirty: bool = False):
        """A real git checkout with a real origin/main, N commits ahead of HEAD."""
        import subprocess

        def g(cwd, *a):
            subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, check=True)

        origin = tmp_path / "origin"
        origin.mkdir()
        g(origin, "init", "-q", "-b", "main")
        subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(origin), "config", "user.name", "t"], check=True)
        pkg = origin / "techne"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        g(origin, "add", "-A")
        subprocess.run(
            ["git", "-C", str(origin), "-c", "commit.gpgsign=false", "commit", "-qm", "base"],
            capture_output=True, check=True,
        )

        clone = tmp_path / "runtime"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)], capture_output=True, check=True
        )
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)

        for i in range(behind):
            (origin / f"new{i}.txt").write_text("x")
            g(origin, "add", "-A")
            subprocess.run(
                ["git", "-C", str(origin), "-c", "commit.gpgsign=false", "commit", "-qm", f"c{i}"],
                capture_output=True, check=True,
            )
        if behind:
            subprocess.run(
                ["git", "-C", str(clone), "fetch", "-q", "origin"], capture_output=True, check=True
            )
        if dirty:
            (clone / "techne" / "__init__.py").write_text("# edited\n")
        return clone

    @staticmethod
    def _check(config, runtime, monkeypatch):
        import techne
        from techne.core.checks import check_runtime_revision

        monkeypatch.setattr(techne, "__file__", str(runtime / "techne" / "__init__.py"))
        return check_runtime_revision(config)

    def test_a_runtime_behind_main_is_stale_not_live(self, config, tmp_path, monkeypatch):
        """THE BUG. Two commits sit on origin/main that this runtime is not
        running, and before this nothing reported it."""
        r = self._check(config, self._repo(tmp_path, behind=2), monkeypatch)
        assert r.verdict is Verdict.STALE, r.detail
        assert "2 commit(s) BEHIND" in r.detail, r.detail
        assert "not deployed" in r.detail
        assert r.remedy and "pull" in r.remedy

    def test_a_current_runtime_is_live(self, config, tmp_path, monkeypatch):
        """The other direction must stay quiet, or the check gets ignored."""
        r = self._check(config, self._repo(tmp_path, behind=0), monkeypatch)
        assert r.verdict is Verdict.LIVE, r.detail
        assert "level with" in r.detail

    def test_a_dirty_runtime_says_so(self, config, tmp_path, monkeypatch):
        """Level with main but locally edited: still running something nobody
        reviewed, which the operator should know."""
        r = self._check(config, self._repo(tmp_path, behind=0, dirty=True), monkeypatch)
        assert "DIRTY" in r.detail, r.detail

    def test_a_packaged_install_is_dormant_not_broken(self, config, tmp_path, monkeypatch):
        """A real non-editable install has no revision to compare. That is a
        healthy state — reporting it red would be a red nobody can clear."""
        plain = tmp_path / "sitepkgs"
        (plain / "techne").mkdir(parents=True)
        r = self._check(config, plain, monkeypatch)
        assert r.verdict is Verdict.DORMANT, r.detail
        assert r.ok, "a packaged install must not fail the command"

    #: The ONLY git subcommands this check may run. An ALLOW-LIST, because the
    #: previous guard asserted `"fetch" not in args and "pull" not in args` —
    #: a spelling check, not a network check. Two genuine network commands walked
    #: through it and survived every test in this class (#487 review): `git remote
    #: update`, which fetches without the word "fetch" (demonstrated advancing
    #: origin/main against a file:// origin), and `git ls-remote`, which dials out
    #: (demonstrated blocking the full timeout against an unroutable host).
    #: Enumerating what is permitted cannot be outflanked by a synonym.
    ALLOWED_GIT = {"rev-parse", "rev-list", "status", "log"}

    def test_the_comparison_does_not_hit_the_network(self, config, tmp_path, monkeypatch):
        """A health check that fetches fails on a plane and stops being a
        diagnostic. The detail says the comparison is against the local ref."""
        import subprocess

        # Build the scenario FIRST — the fixture itself fetches, and arming the
        # guard around it trips on my own setup rather than on the code.
        runtime = self._repo(tmp_path, behind=1)

        real = subprocess.run
        seen = []

        def guard(args, *a, **kw):
            # FAILS CLOSED. The first version only inspected argv whose first
            # element was literally "git", so it SKIPPED anything it did not
            # recognise — and a real `git fetch` got past it two ways in review:
            # `/usr/bin/git ...` (argv[0] is not the string "git") and
            # `shell=True` with a command STRING rather than a list. Neither is
            # reachable through `_git`, which always builds ["git", "-C", ...],
            # so the realistic regression was caught; but a guard that vouches
            # only for shapes it recognises is the same defect this PR fixed in
            # the check itself. Now anything unrecognised is a failure.
            assert not kw.get("shell"), f"shell=True bypasses argv inspection: {args!r}"
            assert isinstance(args, list), f"non-list argv cannot be inspected: {args!r}"
            assert len(args) > 3, f"unexpectedly short argv: {args!r}"
            assert os.path.basename(args[0]) == "git", f"non-git subprocess: {args!r}"
            sub = args[3]
            seen.append(sub)
            assert sub in self.ALLOWED_GIT, f"git subcommand not on the allow-list: {args}"
            return real(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", guard)
        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.STALE
        assert "local ref" in r.detail, r.detail
        assert seen, "the guard never saw a git call — it would pass vacuously"

    def test_the_live_branch_states_the_freshness_caveat_too(
        self, config, tmp_path, monkeypatch
    ):
        """THE FALSE-GREEN CASE IS THE ONE THAT NEEDED THE CAVEAT AND LACKED IT.

        "level with origin/main" against a ref last fetched three weeks ago reads
        as up to date. The caveat was on STALE only (#487 review finding 3).
        """
        import subprocess

        # behind=1 so the fixture's fetch actually MOVES origin/main and writes a
        # reflog entry; then fast-forward so the verdict is LIVE. A plain clone
        # has no reflog for origin/main (that fallback is the next test).
        runtime = self._repo(tmp_path, behind=1)
        subprocess.run(
            ["git", "-C", str(runtime), "merge", "-q", "--ff-only", "origin/main"],
            capture_output=True, check=True,
        )
        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.LIVE, r.detail
        assert "local ref" in r.detail, r.detail
        assert "last fetched" in r.detail, r.detail

    def test_a_never_refetched_clone_says_the_fetch_time_is_unknown(
        self, config, tmp_path, monkeypatch
    ):
        """The reflog is empty in a clone that has never re-fetched (verified in
        #487 review), so the caveat degrades to naming what it CAN see — the tip's
        commit date, labelled as such — rather than silently claiming a fetch time
        it does not have."""
        r = self._check(config, self._repo(tmp_path, behind=0), monkeypatch)
        assert r.verdict is Verdict.LIVE, r.detail
        assert "last fetch time unknown" in r.detail, r.detail
        assert "tip committed" in r.detail, r.detail

    def test_the_age_reported_is_the_fetch_not_the_tip_commit_date(
        self, config, tmp_path, monkeypatch
    ):
        """A backdated commit fetched seconds ago must not read as ancient.

        This reported the tip's COMMITTER DATE, so a commit authored years ago and
        fetched moments ago rendered "last known origin/main commit 2 years, 7
        months ago" — describing the comparison as stale when it was current.
        """
        import subprocess

        runtime = self._repo(tmp_path, behind=0)
        origin = tmp_path / "origin"
        (origin / "old.txt").write_text("x")
        subprocess.run(["git", "-C", str(origin), "add", "-A"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(origin), "-c", "commit.gpgsign=false", "commit", "-qm", "backdated"],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": "2019-01-01T00:00:00",
                "GIT_COMMITTER_DATE": "2019-01-01T00:00:00",
            },
        )
        subprocess.run(
            ["git", "-C", str(runtime), "fetch", "-q", "origin"], capture_output=True, check=True
        )

        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.STALE, r.detail
        assert "last fetched" in r.detail, r.detail
        assert "years" not in r.detail, f"reported the tip's commit date, not the fetch: {r.detail}"

    def test_a_failed_commit_count_is_unknown_not_live(self, config, tmp_path, monkeypatch):
        """A PROBE THAT FAILED IS NOT A HEALTHY RUNTIME.

        `rev-list` returning non-zero (in practice: the 5s timeout) fell through
        to LIVE "level with the local origin/main" — the check asserting exactly
        what it had just failed to determine, thirty lines below an identical case
        handled correctly as UNKNOWN (#487 review finding 4).
        """
        import subprocess

        runtime = self._repo(tmp_path, behind=0)
        real = subprocess.run

        def break_rev_list(args, *a, **kw):
            if isinstance(args, list) and "rev-list" in args:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return real(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", break_rev_list)
        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.UNKNOWN, r.detail
        assert not r.ok, "a failed probe must not read as healthy"
        assert "could not count" in r.detail, r.detail

    def test_a_failed_branch_probe_is_unknown_not_a_working_checkout(
        self, config, tmp_path, monkeypatch
    ):
        """Without the branch, this cannot tell a deployment from a working
        checkout — so it must assert neither. It used to render the failure into
        the sentence ("on None — a working checkout"), which looked malformed
        rather than authoritative; "obviously broken" is not the standard."""
        import subprocess

        runtime = self._repo(tmp_path, behind=0)
        real = subprocess.run

        def break_abbrev_ref(args, *a, **kw):
            if isinstance(args, list) and "--abbrev-ref" in args:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return real(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", break_abbrev_ref)
        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.UNKNOWN, r.detail
        assert "None" not in r.detail, f"rendered the failed probe into the sentence: {r.detail}"

    def test_a_failed_status_probe_does_not_read_as_a_clean_tree(
        self, config, tmp_path, monkeypatch
    ):
        """`_git` returns None on failure and "" when clean — two different
        answers that both read as falsy. Collapsing them made a failed `git
        status` indistinguishable from a clean tree on a genuinely dirty one."""
        import subprocess

        runtime = self._repo(tmp_path, behind=0, dirty=True)
        real = subprocess.run

        def break_status(args, *a, **kw):
            if isinstance(args, list) and "status" in args:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return real(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", break_status)
        r = self._check(config, runtime, monkeypatch)
        assert "LOCAL EDITS UNKNOWN" in r.detail, r.detail
        assert "WORKING TREE DIRTY" not in r.detail, "claimed to know the tree state"

    def test_ahead_of_main_is_named_not_called_level_with(
        self, config, tmp_path, monkeypatch
    ):
        """On a deployment, a local commit nobody pushed is worth naming — and it
        is the state that let this PR pass locally while CI went red."""
        import subprocess

        runtime = self._repo(tmp_path, behind=0)
        (runtime / "local.txt").write_text("x")
        subprocess.run(["git", "-C", str(runtime), "add", "-A"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(runtime), "-c", "commit.gpgsign=false", "commit", "-qm", "local"],
            capture_output=True,
            check=True,
        )
        r = self._check(config, runtime, monkeypatch)
        assert r.verdict is Verdict.LIVE, r.detail
        assert "AHEAD" in r.detail, r.detail
        assert "level with" not in r.detail, r.detail

    def test_behind_and_dirty_reports_both(self, config, tmp_path, monkeypatch):
        """`dirty` was computed before the branch and rendered only on LIVE, so
        the most alarming combination — behind AND locally edited — was the one
        told least."""
        r = self._check(config, self._repo(tmp_path, behind=2, dirty=True), monkeypatch)
        assert r.verdict is Verdict.STALE, r.detail
        assert "BEHIND" in r.detail and "DIRTY" in r.detail, r.detail


class TestExpensiveChecksStayOutOfTheHotPath:
    """techne#468 constraint 2, finally held by something other than a comment.

    `check_runtime_revision` (#487) shipped a docstring asserting TWICE that it
    stayed out of `collect_health`, while `collect_health` ran the whole registry
    unfiltered — so its six git subprocesses ran inside the SessionStart hook.
    Ceiling 6 x timeout=5 = 30s against `hooks.timeout_s = 5.0`, on a fail-open
    path where one overrun drops the ENTIRE banner silently, taking every cheap
    check with it. Prose asserted the constraint; nothing enforced it.
    """

    def test_cheap_only_skips_a_check_marked_expensive(self, config, monkeypatch):
        from techne.core import checks as checks_mod

        ran: list[str] = []

        def cheap(cfg):
            ran.append("cheap")
            return None

        @checks_mod.expensive
        def costly(cfg):
            ran.append("costly")
            return None

        monkeypatch.setattr(checks_mod, "_REGISTRY", [cheap, costly])

        checks_mod.run_checks(config, cheap_only=True)
        assert ran == ["cheap"], "an @expensive check ran in the cheap-only path"

        ran.clear()
        checks_mod.run_checks(config)
        assert ran == ["cheap", "costly"], "doctor must still run everything"

    def test_the_runtime_revision_check_is_marked_expensive(self):
        """Pins the marker itself. Without this, removing the decorator silently
        puts the git subprocesses back into the banner."""
        from techne.core.checks import check_runtime_revision

        assert getattr(check_runtime_revision, "expensive", False) is True

    def test_collect_health_spawns_no_git_subprocesses(self, config, monkeypatch):
        """THE ACTUAL INVARIANT, asserted where it broke.

        Instruments `subprocess.run` for the whole `collect_health` call rather
        than trusting the registry wiring — this is the assertion whose absence
        let the docstring and the behaviour disagree.
        """
        import subprocess

        from techne.core.health import collect_health

        real = subprocess.run
        git_calls: list = []

        def watch(args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                git_calls.append(list(args))
            return real(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", watch)
        health = collect_health(config)

        assert git_calls == [], f"git ran inside the SessionStart path: {git_calls}"
        names = {c["name"] for c in (health.get("checks") or [])}
        # ANTI-VACUITY. If collect_health's try-arm ever blew up, `checks` would be
        # absent, `names` empty, and the assertion below would pass for the wrong
        # reason — non-vacuous in fact, but not by construction (#487 review round
        # 2, nit 3). Same protection as `assert seen` in the network test.
        assert names, f"no checks in the payload: checks_error={health.get('checks_error')!r}"
        assert "runtime:revision" not in names, "the expensive check reached the banner payload"


def test_a_feature_branch_is_not_reported_as_an_undeployed_runtime(config, tmp_path, monkeypatch):
    """A working checkout is not a deployment.

    CI checks out the PR branch and every dev worktree sits on a feature branch —
    both are legitimately behind main. Reporting that as "a merged fix is not
    deployed" is a red nobody can clear, on the surface that most needs to stay
    trustworthy. CI caught this on this check's own PR; the local suite passed
    only because the branch happened to be AHEAD of main.
    """
    import subprocess

    from techne.core.checks import Verdict

    runtime = TestRuntimeRevision._repo(tmp_path, behind=2)
    subprocess.run(
        ["git", "-C", str(runtime), "checkout", "-q", "-b", "feat/something"],
        capture_output=True, check=True,
    )
    r = TestRuntimeRevision._check(config, runtime, monkeypatch)
    assert r.verdict is Verdict.DORMANT, r.detail
    assert r.ok, "a developer on a branch must not fail the command"
    assert "not a deployment" in r.detail, r.detail
    assert "feat/something" in r.detail


def test_a_working_checkout_still_states_how_far_behind_it_is(config, tmp_path, monkeypatch):
    """THIS IS THE #473 INCIDENT STATE, and it used to report green in silence.

    An editable install pointing at a shared tree parked on a peer's branch,
    behind the merge — exactly the machine that kept running the #472 crash for
    hours. The check written to make that legible reported DORMANT with no
    distance and no number, rendering identically to a healthy runtime: the #486
    shape (an un-healed thing indistinguishable from a healed one) reproduced
    inside its own fix (#487 review finding 2).

    The verdict deliberately stays DORMANT and `ok` — a red here is one CI and
    every dev worktree could never clear. What changes is that the operator
    reading `doctor` now sees the count.
    """
    import subprocess

    from techne.core.checks import Verdict

    runtime = TestRuntimeRevision._repo(tmp_path, behind=3)
    subprocess.run(
        ["git", "-C", str(runtime), "checkout", "-q", "-b", "peer/session-work"],
        capture_output=True, check=True,
    )
    r = TestRuntimeRevision._check(config, runtime, monkeypatch)
    assert r.verdict is Verdict.DORMANT and r.ok, r.detail
    assert "3 commit(s) behind" in r.detail, f"the distance is the whole point: {r.detail}"


def test_a_detached_head_is_not_a_deployment_either(config, tmp_path, monkeypatch):
    """CI checks out detached, so exempting only named branches would have kept
    the false red on every CI run while looking like the bug was fixed."""
    import subprocess

    from techne.core.checks import Verdict

    runtime = TestRuntimeRevision._repo(tmp_path, behind=2)
    sha = subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(runtime), "checkout", "-q", sha], capture_output=True, check=True
    )
    r = TestRuntimeRevision._check(config, runtime, monkeypatch)
    assert r.verdict is Verdict.DORMANT and r.ok, r.detail
    assert "detached" in r.detail, r.detail
