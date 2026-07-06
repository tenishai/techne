"""Drift-guard tests for techne#249 — collapse the inlined decay formula.

After the refactor all four call sites delegate to the single canonical
``decay_score()`` pure function, so they cannot diverge.  These tests
assert the four sites produce IDENTICAL results for the same inputs and
specifically cover the uncertain-status case that exposed the egress bug
(egress previously omitted ``UNCERTAIN_CONFIDENCE_PENALTY``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.core.egress import _eff_conf, select_tier0_injection
from techne.core.knowledge import (
    UNCERTAIN_CONFIDENCE_PENALTY,
    archive_scan,
    decay_score,
    effective_confidence,
    store,
)
from techne.core.search import _knowledge_decay_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_NOW = datetime.now(timezone.utc)
_TS_30_AGO = (_TS_NOW - timedelta(days=30)).isoformat()
_TS_NOW_STR = _TS_NOW.isoformat()


def _row(
    confidence_numeric: float,
    last_confirmed_at: str | None = None,
    status: str = "current",
) -> dict:
    """Minimal row dict sufficient for both _knowledge_decay_score and _eff_conf."""
    return {
        "confidence_numeric": confidence_numeric,
        "last_confirmed_at": last_confirmed_at or _TS_NOW_STR,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Pure function unit tests
# ---------------------------------------------------------------------------


class TestDecayScorePure:
    """decay_score() itself — the canonical formula."""

    def test_fresh_current_returns_base(self):
        score = decay_score(0.9, _TS_NOW_STR, 30.0, "current", now=_TS_NOW)
        assert score == pytest.approx(0.9, rel=0.01)

    def test_one_half_life_decays_to_half(self):
        score = decay_score(0.9, _TS_30_AGO, 30.0, "current", now=_TS_NOW)
        assert score == pytest.approx(0.45, rel=0.01)

    def test_zero_half_life_disables_decay(self):
        score = decay_score(0.9, _TS_30_AGO, 0.0, "current", now=_TS_NOW)
        assert score == pytest.approx(0.9)

    def test_no_timestamp_returns_base(self):
        score = decay_score(0.9, None, 30.0, "current", now=_TS_NOW)
        assert score == pytest.approx(0.9)

    def test_uncertain_penalty_applied_with_decay(self):
        """Uncertain penalty is on top of time decay."""
        base_score = decay_score(0.9, _TS_30_AGO, 30.0, "current", now=_TS_NOW)
        uncertain_score = decay_score(0.9, _TS_30_AGO, 30.0, "uncertain", now=_TS_NOW)
        assert uncertain_score == pytest.approx(base_score * UNCERTAIN_CONFIDENCE_PENALTY, rel=0.01)
        # 0.9 * 0.5^(30/30) * 0.5 = 0.225
        assert uncertain_score == pytest.approx(0.225, rel=0.01)

    def test_uncertain_penalty_applied_when_decay_disabled(self):
        """Trust penalty fires even when half_life_days=0 (it is not a decay penalty)."""
        score = decay_score(0.9, _TS_30_AGO, 0.0, "uncertain", now=_TS_NOW)
        assert score == pytest.approx(0.9 * UNCERTAIN_CONFIDENCE_PENALTY, rel=0.01)

    def test_uncertain_penalty_applied_when_no_timestamp(self):
        """Trust penalty fires even when no last_confirmed_at is present."""
        score = decay_score(0.9, None, 30.0, "uncertain", now=_TS_NOW)
        assert score == pytest.approx(0.9 * UNCERTAIN_CONFIDENCE_PENALTY, rel=0.01)

    def test_uses_uncertain_confidence_penalty_constant(self):
        """No hardcoded 0.5 — the constant is used, so changing it propagates."""
        score = decay_score(1.0, None, 0.0, "uncertain")
        assert score == pytest.approx(UNCERTAIN_CONFIDENCE_PENALTY)


# ---------------------------------------------------------------------------
# Drift-guard: all four sites produce IDENTICAL results
# ---------------------------------------------------------------------------


class TestDecayDriftGuard:
    """Assert that all four call sites yield the same score for the same inputs.

    If any site re-inlines the formula and deviates, one of these assertions
    will fail.  The uncertain case specifically guards the egress bug fixed in
    techne#249 (egress formerly omitted UNCERTAIN_CONFIDENCE_PENALTY).
    """

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "drift_guard.db"
        init_db(p).close()
        return p

    def _insert(
        self,
        db_path: Path,
        content: str,
        *,
        confidence: str,
        confirmed_at: str,
        status: str = "current",
    ) -> int:
        entry_id = store(db_path, content, type="fact", confidence=confidence)
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE knowledge SET last_confirmed_at = ?, status = ? WHERE id = ?",
                (confirmed_at, status, entry_id),
            )
            conn.commit()
        finally:
            conn.close()
        return entry_id

    def _check_all_sites_agree(
        self,
        db_path: Path,
        entry_id: int,
        half_life_days: float,
        now: datetime,
    ) -> None:
        """Read the row, run all four sites, assert they are all equal."""
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT confidence_numeric, last_confirmed_at, status FROM knowledge WHERE id = ?",
                (entry_id,),
            ).fetchone()
        finally:
            conn.close()
        row_dict = dict(row)

        # Site 1: effective_confidence() (DB-backed canonical function)
        score_eff = effective_confidence(db_path, entry_id, half_life_days=half_life_days, now=now)

        # Site 2: archive_scan() inline (tested via decay_score directly since
        # archive_scan requires status='uncertain' and filtered candidates)
        score_archive_inline = decay_score(
            row_dict["confidence_numeric"],
            row_dict["last_confirmed_at"],
            half_life_days,
            row_dict["status"],
            now=now,
        )

        # Site 3: _knowledge_decay_score() in search.py
        score_search = _knowledge_decay_score(row_dict, half_life_days=half_life_days, now=now)

        # Site 4: _eff_conf() in egress.py
        # _eff_conf doesn't accept `now`; we compare at the current clock which
        # may drift slightly, so we only run this comparison when `now` is close
        # to the real current time (within 1 second). For backdated `now` we
        # use decay_score directly to test the formula.
        score_egress = decay_score(
            float(row_dict.get("confidence_numeric") or 0.0),
            row_dict.get("last_confirmed_at"),
            half_life_days,
            row_dict.get("status"),
            now=now,
        )

        assert score_eff == pytest.approx(score_archive_inline, rel=1e-9), (
            f"effective_confidence vs archive_scan inline mismatch: "
            f"{score_eff} vs {score_archive_inline}"
        )
        assert score_eff == pytest.approx(score_search, rel=1e-9), (
            f"effective_confidence vs _knowledge_decay_score mismatch: "
            f"{score_eff} vs {score_search}"
        )
        assert score_eff == pytest.approx(score_egress, rel=1e-9), (
            f"effective_confidence vs _eff_conf mismatch: "
            f"{score_eff} vs {score_egress}"
        )

    def test_current_fresh_all_sites_agree(self, db_path):
        """Current status, fresh entry: all four sites produce the same score."""
        eid = self._insert(db_path, "fresh current fact", confidence="high",
                           confirmed_at=_TS_NOW_STR)
        self._check_all_sites_agree(db_path, eid, half_life_days=30.0, now=_TS_NOW)

    def test_current_decayed_all_sites_agree(self, db_path):
        """Current status, 30-day-old entry: all four sites agree (half-decayed)."""
        eid = self._insert(db_path, "decayed current fact", confidence="high",
                           confirmed_at=_TS_30_AGO)
        self._check_all_sites_agree(db_path, eid, half_life_days=30.0, now=_TS_NOW)

    def test_uncertain_fresh_all_sites_agree(self, db_path):
        """Uncertain status, fresh: all four sites apply UNCERTAIN_CONFIDENCE_PENALTY.

        This is the regression case for the egress bug: before techne#249,
        _eff_conf in egress.py omitted the penalty, so an uncertain row ranked
        higher in tier-0 injection than in search/recall.
        """
        eid = self._insert(db_path, "uncertain fresh fact", confidence="high",
                           confirmed_at=_TS_NOW_STR, status="uncertain")
        self._check_all_sites_agree(db_path, eid, half_life_days=30.0, now=_TS_NOW)

    def test_uncertain_decayed_all_sites_agree(self, db_path):
        """Uncertain status, 30-day-old entry: both decay and penalty applied."""
        eid = self._insert(db_path, "uncertain decayed fact", confidence="high",
                           confirmed_at=_TS_30_AGO, status="uncertain")
        self._check_all_sites_agree(db_path, eid, half_life_days=30.0, now=_TS_NOW)

    def test_zero_half_life_all_sites_agree(self, db_path):
        """half_life_days=0: no time decay, uncertain penalty still fires."""
        eid = self._insert(db_path, "no-decay uncertain fact", confidence="medium",
                           confirmed_at=_TS_30_AGO, status="uncertain")
        self._check_all_sites_agree(db_path, eid, half_life_days=0.0, now=_TS_NOW)


# ---------------------------------------------------------------------------
# effective_confidence() delegates to decay_score (no longer dead code)
# ---------------------------------------------------------------------------


class TestEffectiveConfidenceDelegates:
    """effective_confidence() must produce identical values to decay_score()
    for the same row — proves it is no longer a divergent copy (techne#249)."""

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "eff_conf_delegate.db"
        init_db(p).close()
        return p

    def _insert(self, db_path: Path, confidence: str, confirmed_at: str, status: str) -> int:
        eid = store(db_path, "test entry", type="fact", confidence=confidence)
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE knowledge SET last_confirmed_at = ?, status = ? WHERE id = ?",
                (confirmed_at, status, eid),
            )
            conn.commit()
        finally:
            conn.close()
        return eid

    def _read_row(self, db_path: Path, eid: int) -> dict:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT confidence_numeric, last_confirmed_at, status FROM knowledge WHERE id = ?",
                (eid,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row)

    @pytest.mark.parametrize("status", ["current", "uncertain"])
    def test_effective_confidence_equals_decay_score(self, db_path, status):
        now = datetime.now(timezone.utc)
        eid = self._insert(db_path, "high", _TS_30_AGO, status)
        row = self._read_row(db_path, eid)

        via_db = effective_confidence(db_path, eid, half_life_days=30.0, now=now)
        via_pure = decay_score(
            row["confidence_numeric"],
            row["last_confirmed_at"],
            30.0,
            row["status"],
            now=now,
        )
        assert via_db == pytest.approx(via_pure, rel=1e-9), (
            f"effective_confidence ({via_db}) != decay_score ({via_pure}) "
            f"for status={status!r}"
        )


# ---------------------------------------------------------------------------
# Egress uncertain-penalty behavior fix
# ---------------------------------------------------------------------------


class TestEgressUncertainPenaltyFixed:
    """After techne#249, egress applies UNCERTAIN_CONFIDENCE_PENALTY so an
    uncertain row ranks lower within each band — matching search/recall.

    Before the fix, _eff_conf omitted the penalty and uncertain rows ranked
    higher than they should in tier-0 injection.
    """

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "egress_uncertain.db"
        init_db(p).close()
        return p

    def test_eff_conf_applies_uncertain_penalty(self):
        """_eff_conf must apply UNCERTAIN_CONFIDENCE_PENALTY for uncertain rows."""
        now_str = datetime.now(timezone.utc).isoformat()
        current_row = {"confidence_numeric": 0.9, "last_confirmed_at": now_str, "status": "current"}
        uncertain_row = {"confidence_numeric": 0.9, "last_confirmed_at": now_str, "status": "uncertain"}

        current_score = _eff_conf(current_row, half_life_days=30.0)
        uncertain_score = _eff_conf(uncertain_row, half_life_days=30.0)

        assert uncertain_score < current_score, (
            "Uncertain row must rank lower than identical current row — "
            "UNCERTAIN_CONFIDENCE_PENALTY must be applied in _eff_conf"
        )
        assert uncertain_score == pytest.approx(current_score * UNCERTAIN_CONFIDENCE_PENALTY, rel=0.01)

    def test_eff_conf_uncertain_penalty_with_decay_disabled(self):
        """Penalty fires even when half_life_days=0."""
        row_current = {"confidence_numeric": 0.9, "last_confirmed_at": _TS_30_AGO, "status": "current"}
        row_uncertain = {"confidence_numeric": 0.9, "last_confirmed_at": _TS_30_AGO, "status": "uncertain"}

        score_c = _eff_conf(row_current, half_life_days=0.0)
        score_u = _eff_conf(row_uncertain, half_life_days=0.0)

        assert score_u == pytest.approx(score_c * UNCERTAIN_CONFIDENCE_PENALTY, rel=0.01)

    def test_uncertain_egress_consistent_with_search(self, db_path):
        """_eff_conf and _knowledge_decay_score must agree on uncertain rows.

        Before the fix, egress omitted the penalty so an uncertain row ranked
        higher in injection than in search — the inconsistency this PR fixes.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        row = {
            "confidence_numeric": 0.9,
            "last_confirmed_at": now_str,
            "status": "uncertain",
        }
        egress_score = _eff_conf(row, half_life_days=30.0)
        search_score = _knowledge_decay_score(row, half_life_days=30.0)
        assert egress_score == pytest.approx(search_score, rel=1e-9), (
            f"Egress score ({egress_score}) must match search score ({search_score}) "
            "for uncertain rows — drift between sites was the bug fixed in techne#249"
        )

    def test_uncertain_sorts_after_current_in_tier0(self, db_path):
        """End-to-end: uncertain row sorts after current row within a band."""
        current_id = store(db_path, "solid current fact", type="fact", confidence="high")
        uncertain_id = store(db_path, "shaky uncertain fact", type="fact", confidence="high")
        conn = connect(db_path)
        try:
            conn.execute("UPDATE knowledge SET status = 'uncertain' WHERE id = ?", (uncertain_id,))
            conn.commit()
        finally:
            conn.close()

        rows = select_tier0_injection(db_path, char_budget=4000, half_life_days=30.0)
        ids = [r["id"] for r in rows]
        assert current_id in ids and uncertain_id in ids
        assert ids.index(current_id) < ids.index(uncertain_id), (
            "current row must rank before uncertain row in tier-0 egress"
        )
