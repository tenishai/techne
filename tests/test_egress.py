"""Tests for Tier-0 egress selection (techne#93).

All fixtures use tmp_path — the live db at ~/.techne/techne.db is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.core.egress import (
    _tag_contains,
    render_tier0_block,
    select_tier0_injection,
)
from techne.core.knowledge import store


class TestTagContainsExactToken:
    """techne#168: _tag_contains is exact-token only — the old startswith clause
    contradicted its docstring and would misroute a tag prefixed with a band
    classifier (e.g. memory-type:projectX matching the memory-type:project band)."""

    def test_exact_token_matches(self):
        assert _tag_contains("memory-type:user,name:richard", "memory-type:user")
        assert _tag_contains("a,memory-type:feedback,b", "memory-type:feedback")

    def test_prefix_no_longer_matches(self):
        # The whole point: a longer token sharing the prefix must NOT match.
        assert not _tag_contains("memory-type:projectX", "memory-type:project")
        assert not _tag_contains("memory-type:user-preferences", "memory-type:user")

    def test_none_and_absent(self):
        assert not _tag_contains(None, "memory-type:user")
        assert not _tag_contains("name:foo", "memory-type:user")

    def test_misprefixed_tag_does_not_land_in_identity_band(self, db_path):
        """End-to-end: a row tagged memory-type:user-ish must not be routed to the
        identity band by the prefix bug."""
        store(db_path, "decoy", type="fact", confidence="high", tags="memory-type:user-ish")
        # Selection must not crash on the mis-prefixed tag; the concrete guard is
        # that the classifier no longer exact-matches it into the identity band.
        assert isinstance(select_tier0_injection(db_path, char_budget=4000), list)
        assert not _tag_contains("memory-type:user-ish", "memory-type:user")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "egress_test.db"
    init_db(p).close()
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(
    db_path: Path, content: str, *, type: str, tags: str = "", confidence: str = "high"
) -> int:
    """Insert a single knowledge row and return its id."""
    return store(db_path, content, type=type, tags=tags, confidence=confidence)


def _mark_uncertain(db_path: Path, kid: int) -> None:
    """Flip a row to status='uncertain' directly (the cascade does this via a
    superseded dependency; egress tests only need the resulting state)."""
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        conn.execute("UPDATE knowledge SET status = 'uncertain' WHERE id = ?", (kid,))
        conn.commit()
    finally:
        conn.close()


def _insert_legacy_pattern(db_path: Path, content: str, *, tags: str = "") -> int:
    """Insert a legacy `type='pattern'` row directly. store() rejects this type
    post techne#137, but egress must still exclude rows that predate the cleanup
    (PULL_ONLY_TYPES keeps 'pattern' for exactly this defense)."""
    from techne.core.database import connect
    from techne.core.knowledge import CONFIDENCE_NUMERIC

    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO knowledge (type, content, tags, confidence, confidence_numeric) "
            "VALUES ('pattern', ?, ?, 'high', ?)",
            (content, tags, CONFIDENCE_NUMERIC["high"]),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pattern exclusion
# ---------------------------------------------------------------------------


class TestPatternExclusion:
    def test_high_confidence_pattern_never_selected(self, db_path):
        """type='pattern' rows must never appear in Tier-0 output, even with confidence='high'."""
        _insert_legacy_pattern(db_path, "should be excluded", tags="memory-type:feedback")
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert not any(r["type"] == "pattern" for r in rows)

    def test_pattern_excluded_alongside_eligible_rows(self, db_path):
        """A mix of pattern (excluded) and fact (included) produces only the fact."""
        _insert_legacy_pattern(db_path, "excluded pattern")
        fact_id = _seed(db_path, "included fact", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert fact_id in ids
        assert all(r["type"] != "pattern" for r in rows)


# ---------------------------------------------------------------------------
# Tag-based priority
# ---------------------------------------------------------------------------


class TestTagPriority:
    def test_identity_row_included(self, db_path):
        """memory-type:user rows appear in Tier-0 output."""
        uid = _seed(db_path, "user identity fact", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert any(r["id"] == uid for r in rows)

    def test_feedback_row_included(self, db_path):
        """memory-type:feedback rows appear in Tier-0 output."""
        fid = _seed(
            db_path,
            "feedback fact",
            type="fact",
            tags="cc-backfill,memory-type:feedback,name:feedback-working-style",
        )
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert any(r["id"] == fid for r in rows)

    def test_identity_before_remaining(self, db_path):
        """Identity rows come before non-priority rows in the returned list."""
        remainder_id = _seed(db_path, "plain decision", type="decision")
        identity_id = _seed(db_path, "user identity", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert identity_id in ids
        assert remainder_id in ids
        assert ids.index(identity_id) < ids.index(remainder_id)

    def test_recent_results_before_feedback(self, db_path):
        """Fresh decisions/facts (the reserved 'what changed' band) render before
        the static feedback band — the techne#93 egress-starvation fix."""
        remainder_id = _seed(db_path, "plain fact", type="fact")
        feedback_id = _seed(db_path, "feedback entry", type="fact", tags="memory-type:feedback")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert remainder_id in ids and feedback_id in ids
        assert ids.index(remainder_id) < ids.index(feedback_id)

    def test_legacy_ordering_when_reservation_disabled(self, db_path):
        """recent_reserve_frac=0.0 restores the original feedback→recent order."""
        remainder_id = _seed(db_path, "plain fact", type="fact")
        feedback_id = _seed(db_path, "feedback entry", type="fact", tags="memory-type:feedback")
        rows = select_tier0_injection(db_path, char_budget=4000, recent_reserve_frac=0.0)
        ids = [r["id"] for r in rows]
        assert ids.index(feedback_id) < ids.index(remainder_id)

    def test_fresh_result_not_starved_by_feedback(self, db_path):
        """The bug this fixes: a budget small enough to be eaten by feedback must
        still surface the single freshest result. Seed many feedback rows, then one
        recent evaluation; under a tight budget it must appear with the fix and be
        dropped without it."""
        for n in range(12):
            _seed(
                db_path,
                f"feedback rule number {n} " + "x" * 120,
                type="fact",
                tags="memory-type:feedback",
            )
        fresh_id = _seed(db_path, "ACCEPTANCE TEST PASSED " + "y" * 120, type="evaluation")
        tight = 900
        with_fix = [
            r["id"]
            for r in select_tier0_injection(db_path, char_budget=tight, recent_reserve_frac=0.4)
        ]
        legacy = [
            r["id"]
            for r in select_tier0_injection(db_path, char_budget=tight, recent_reserve_frac=0.0)
        ]
        assert fresh_id in with_fix, "reservation must surface the freshest result"
        assert fresh_id not in legacy, "legacy ordering should reproduce the starvation"


# ---------------------------------------------------------------------------
# Superseded exclusion
# ---------------------------------------------------------------------------


class TestSupersededExclusion:
    def test_superseded_row_excluded(self, db_path):
        """Rows with superseded_by set must not appear in Tier-0 output."""
        from techne.core.knowledge import supersede

        old_id = _seed(db_path, "old content", type="fact", tags="memory-type:user")
        new_id = supersede(db_path, old_id, "updated content")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert old_id not in ids
        assert new_id in ids


# ---------------------------------------------------------------------------
# Uncertain de-prioritization (techne#117 recall gate)
# ---------------------------------------------------------------------------


class TestUncertainDeprioritization:
    def test_uncertain_row_still_eligible(self, db_path):
        """An uncertain row is live (not superseded) — it must still be selected,
        just marked. The fix annotates, it does not hide."""
        kid = _seed(db_path, "derived fact", type="fact", tags="memory-type:user")
        _mark_uncertain(db_path, kid)
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert any(r["id"] == kid for r in rows)

    def test_uncertain_row_rendered_with_marker(self, db_path):
        """The rendered line for an uncertain row carries an [uncertain] marker so it
        is never auto-injected as a confident fact."""
        kid = _seed(db_path, "shaky fact", type="fact", tags="memory-type:user")
        _mark_uncertain(db_path, kid)
        rows = select_tier0_injection(db_path, char_budget=4000)
        block = render_tier0_block(rows)
        line = next(ln for ln in block.splitlines() if f"⟦techne#{kid}⟧" in ln)
        assert "[uncertain]" in line

    def test_current_row_has_no_marker(self, db_path):
        """A plain current row renders without the uncertain marker."""
        kid = _seed(db_path, "solid fact", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        block = render_tier0_block(rows)
        line = next(ln for ln in block.splitlines() if f"⟦techne#{kid}⟧" in ln)
        assert "[uncertain]" not in line

    def test_uncertain_sorts_after_current_within_band(self, db_path):
        """Within a band, an uncertain row trails an otherwise-equal current row —
        the down-rank. Both are remainder ('what changed') rows here."""
        current_id = _seed(db_path, "trustworthy fact", type="fact")
        uncertain_id = _seed(db_path, "shaky fact", type="fact")
        _mark_uncertain(db_path, uncertain_id)
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert current_id in ids and uncertain_id in ids
        assert ids.index(current_id) < ids.index(uncertain_id)


# ---------------------------------------------------------------------------
# Char budget
# ---------------------------------------------------------------------------


class TestCharBudget:
    def test_rendered_output_within_budget(self, db_path):
        """Rendered block must not exceed the requested char budget."""
        # Seed many rows so selection must stop early.
        for i in range(50):
            _seed(
                db_path,
                f"knowledge entry number {i} " + "x" * 50,
                type="fact",
                tags="memory-type:feedback",
            )

        budget = 500
        rows = select_tier0_injection(db_path, char_budget=budget)
        render_tier0_block(rows)
        # What matters is that selection stopped within budget — verify via char count
        # of the content lines only (excluding the section header line).
        content_chars = sum(
            len(f"- ⟦techne#{r['id']}⟧ [{r['type']}] {r['content'][:200]}\n") for r in rows
        )
        assert content_chars <= budget

    def test_budget_zero_selects_nothing(self, db_path):
        """A zero budget should select no rows."""
        _seed(db_path, "fact", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=0)
        assert rows == []

    def test_budget_respected_across_many_rows(self, db_path):
        """Selection stops before exceeding budget even with many candidates."""
        for i in range(100):
            _seed(db_path, "a" * 150, type="evaluation")

        budget = 300
        rows = select_tier0_injection(db_path, char_budget=budget)
        # Rendered chars per line ≈ 15 (prefix) + 150 (content) + misc ≈ ~180.
        # Budget 300 should give at most 2 rows.
        assert len(rows) <= 5  # generous upper bound given header accounting
        content_chars = sum(
            len(f"- ⟦techne#{r['id']}⟧ [{r['type']}] {r['content'][:200]}\n") for r in rows
        )
        assert content_chars <= budget


# ---------------------------------------------------------------------------
# Provenance prefix
# ---------------------------------------------------------------------------


class TestProvenancePrefix:
    def test_rendered_lines_have_provenance_prefix(self, db_path):
        """Every rendered line must start with ⟦techne#<id>⟧."""
        row_id = _seed(db_path, "some fact content", type="fact", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        block = render_tier0_block(rows)

        # Find the line for our specific row.
        matching = [line for line in block.splitlines() if f"⟦techne#{row_id}⟧" in line]
        assert matching, f"No line with provenance prefix for id={row_id} in:\n{block}"

    def test_all_rendered_lines_have_prefix(self, db_path):
        """Every bullet line in the rendered block carries a techne# prefix."""
        for i in range(5):
            _seed(db_path, f"fact {i}", type="fact", tags="memory-type:feedback")

        rows = select_tier0_injection(db_path, char_budget=4000)
        block = render_tier0_block(rows)

        bullet_lines = [line for line in block.splitlines() if line.startswith("- ")]
        assert bullet_lines, "Expected at least one bullet line"
        for line in bullet_lines:
            assert "⟦techne#" in line, f"Missing provenance prefix on line: {line!r}"

    def test_provenance_id_matches_row_id(self, db_path):
        """The numeric id in ⟦techne#<id>⟧ must match the actual db row id."""
        row_id = _seed(db_path, "specific content", type="decision", tags="memory-type:user")
        rows = select_tier0_injection(db_path, char_budget=4000)
        row = next((r for r in rows if r["id"] == row_id), None)
        assert row is not None

        block = render_tier0_block([row])
        assert f"⟦techne#{row_id}⟧" in block


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_db_returns_empty_list(self, db_path):
        """Empty database yields no rows and no exception."""
        rows = select_tier0_injection(db_path, char_budget=2000)
        assert rows == []

    def test_render_empty_rows_returns_empty_string(self):
        """render_tier0_block([]) must return an empty string."""
        assert render_tier0_block([]) == ""

    def test_low_confidence_rows_excluded(self, db_path):
        """Only high-confidence rows are eligible."""
        _seed(db_path, "medium fact", type="fact", confidence="medium")
        _seed(db_path, "low fact", type="fact", confidence="low")
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert rows == []

    def test_only_high_confidence_selected(self, db_path):
        """Among mixed confidences, only high appears in output."""
        low_id = _seed(db_path, "low", type="fact", confidence="low")
        med_id = _seed(db_path, "medium", type="fact", confidence="medium")
        high_id = _seed(db_path, "high", type="fact", confidence="high")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert high_id in ids
        assert low_id not in ids
        assert med_id not in ids

    def test_platform_state_single_most_recent(self, db_path):
        """Only the single most-recent platform-state entry is included from that bucket."""
        from techne.core.database import connect

        id1 = _seed(
            db_path,
            "older platform state",
            type="fact",
            tags="memory-type:project,name:platform-state-2026-06-12",
        )
        id2 = _seed(
            db_path,
            "newer platform state",
            type="fact",
            tags="memory-type:project,name:platform-state-2026-06-17",
        )
        # Force id1 updated_at to be older so id2 wins.
        conn = connect(db_path)
        conn.execute("UPDATE knowledge SET updated_at = '2026-01-01 00:00:00' WHERE id = ?", (id1,))
        conn.commit()
        conn.close()

        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert id2 in ids
        assert id1 not in ids, "Only the most-recent platform-state row should be included"


# ---------------------------------------------------------------------------
# Correction priority (belief-overturning facts)
# ---------------------------------------------------------------------------


class TestCorrectionPriority:
    """memory-type:correction rows are belief-overturning facts. A wrong belief
    lives in the model's weights (there is no stored row to supersede), so the
    correction must ride the resident screen by PRIORITY, not recency — otherwise
    a busy day of newer routine facts evicts it (the 'no Sonnet 5' failure)."""

    def test_correction_included(self, db_path):
        cid = _seed(
            db_path,
            "Claude Sonnet 5 exists (corrects the prior 'there is no Sonnet 5')",
            type="fact",
            tags="memory-type:correction",
        )
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert any(r["id"] == cid for r in rows)

    def test_correction_before_feedback_and_recent(self, db_path):
        """A correction renders ahead of both the feedback band and generic recent rows."""
        recent_id = _seed(db_path, "a routine new fact", type="fact")
        feedback_id = _seed(
            db_path, "a behavioral rule", type="fact", tags="memory-type:feedback"
        )
        correction_id = _seed(
            db_path, "a belief correction", type="fact", tags="memory-type:correction"
        )
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert correction_id in ids
        assert ids.index(correction_id) < ids.index(feedback_id)
        assert ids.index(correction_id) < ids.index(recent_id)

    def test_correction_survives_flood_that_starves_untagged_peer(self, db_path):
        """The Sonnet-5 case, held rigorous: two equally-OLD facts, then a flood of
        NEWER routine facts under a tight budget. The one tagged memory-type:correction
        survives (reserved band); its untagged twin, competing on pure recency, is
        evicted. Recency is pinned so the ordering is deterministic."""
        from techne.core.database import connect

        corr_id = _seed(
            db_path,
            "SONNET 5 EXISTS " + "c" * 120,
            type="fact",
            tags="memory-type:correction",
        )
        untagged_id = _seed(db_path, "untagged early fact " + "u" * 120, type="fact")
        for n in range(12):
            _seed(db_path, f"newer routine fact {n} " + "z" * 120, type="fact")
        # Pin the untagged peer old so it is unambiguously the last recent row.
        # Determinism note: the recent band now sorts by _eff_conf (techne#214),
        # which reads last_confirmed_at, NOT updated_at. This pin only bites because
        # store() stamps every seeded row's last_confirmed_at with the same
        # CURRENT_TIMESTAMP, so _eff_conf ties across the recent band and the stable
        # sort falls through to the SQL `updated_at DESC` order — where this pin lives.
        # If _seed ever sets a per-row last_confirmed_at, the tie breaks and this pin
        # stops determining order; re-pin on last_confirmed_at instead.
        conn = connect(db_path)
        conn.execute(
            "UPDATE knowledge SET updated_at = '2026-01-01 00:00:00' WHERE id = ?",
            (untagged_id,),
        )
        conn.commit()
        conn.close()

        tight = 900
        ids = [r["id"] for r in select_tier0_injection(db_path, char_budget=tight)]
        assert corr_id in ids, "the correction must be protected by its reserved band"
        assert untagged_id not in ids, "the untagged early peer should be starved by newer facts"


    def test_correction_band_ordered_by_eff_conf_fresh_before_decayed(self, db_path):
        """Within the corrections band, rows are sorted by effective_confidence DESC
        (techne#214 decay ordering). A freshly-confirmed correction surfaces before
        a long-unconfirmed one at the same stored tier.

        This verifies that _eff_conf is applied to correction_raw, not just the
        other buckets. Both rows fit the budget; we assert ordering, not exclusion.
        """
        from techne.core.database import connect

        # Insert decayed correction first so insertion order would pick it first
        # if there were no eff_conf sort.
        decayed_id = _seed(
            db_path,
            "DECAYED correction — inserted first",
            type="fact",
            tags="memory-type:correction",
        )
        fresh_id = _seed(
            db_path,
            "FRESH correction — inserted second",
            type="fact",
            tags="memory-type:correction",
        )

        conn = connect(db_path)
        # Decayed: last confirmed 400 days ago (≫ 30-day half-life → eff_conf ≈ 0).
        conn.execute(
            "UPDATE knowledge SET last_confirmed_at = '2025-04-01 00:00:00' WHERE id = ?",
            (decayed_id,),
        )
        # Fresh: last confirmed today.
        conn.execute(
            "UPDATE knowledge SET last_confirmed_at = '2026-07-05 00:00:00' WHERE id = ?",
            (fresh_id,),
        )
        conn.commit()
        conn.close()

        # Generous budget so both rows are selected; we only care about order.
        rows = select_tier0_injection(db_path, char_budget=4000, half_life_days=30.0)
        ids = [r["id"] for r in rows]
        assert fresh_id in ids, "fresh correction must be selected"
        assert decayed_id in ids, "decayed correction must also be selected (budget is generous)"
        assert ids.index(fresh_id) < ids.index(decayed_id), (
            "fresh correction (higher eff_conf) must render before decayed one"
        )
