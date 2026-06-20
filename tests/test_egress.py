"""Tests for Tier-0 egress selection (techne#93).

All fixtures use tmp_path — the live db at ~/.techne/techne.db is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.core.egress import (
    render_tier0_block,
    select_tier0_injection,
)
from techne.core.knowledge import store

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


# ---------------------------------------------------------------------------
# Pattern exclusion
# ---------------------------------------------------------------------------


class TestPatternExclusion:
    def test_high_confidence_pattern_never_selected(self, db_path):
        """type='pattern' rows must never appear in Tier-0 output, even with confidence='high'."""
        _seed(db_path, "should be excluded", type="pattern", tags="memory-type:feedback")
        rows = select_tier0_injection(db_path, char_budget=4000)
        assert not any(r["type"] == "pattern" for r in rows)

    def test_pattern_excluded_alongside_eligible_rows(self, db_path):
        """A mix of pattern (excluded) and fact (included) produces only the fact."""
        _seed(db_path, "excluded pattern", type="pattern")
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

    def test_feedback_before_remaining(self, db_path):
        """Feedback rows come before non-priority rows."""
        remainder_id = _seed(db_path, "plain fact", type="fact")
        feedback_id = _seed(db_path, "feedback entry", type="fact", tags="memory-type:feedback")
        rows = select_tier0_injection(db_path, char_budget=4000)
        ids = [r["id"] for r in rows]
        assert ids.index(feedback_id) < ids.index(remainder_id)


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
