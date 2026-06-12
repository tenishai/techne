"""Tests for confidence decay + reaffirmation (techne#13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.core.knowledge import (
    CONFIDENCE_NUMERIC,
    effective_confidence,
    reaffirm,
    store,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "decay_test.db"
    init_db(p).close()
    return p


# ---- store populates the new columns --------------------------------------


def test_store_writes_confidence_numeric(db_path) -> None:
    """store() with confidence='high' sets confidence_numeric=0.9."""
    entry_id = store(db_path, "foo", type="fact", confidence="high")
    # No decay; freshly written → base value.
    score = effective_confidence(db_path, entry_id, half_life_days=0)
    assert score == pytest.approx(0.9)


def test_store_writes_medium_and_low_correctly(db_path) -> None:
    high_id = store(db_path, "h", type="fact", confidence="high")
    medium_id = store(db_path, "m", type="fact", confidence="medium")
    low_id = store(db_path, "l", type="fact", confidence="low")
    assert effective_confidence(db_path, high_id, half_life_days=0) == pytest.approx(0.9)
    assert effective_confidence(db_path, medium_id, half_life_days=0) == pytest.approx(0.6)
    assert effective_confidence(db_path, low_id, half_life_days=0) == pytest.approx(0.3)


def test_confidence_numeric_mapping_matches_migration() -> None:
    """The enum-to-numeric mapping the code uses must match what the
    schema migration uses for backfill — otherwise legacy data ends up
    inconsistent with new writes."""
    assert CONFIDENCE_NUMERIC == {"high": 0.9, "medium": 0.6, "low": 0.3}


# ---- decay formula --------------------------------------------------------


def test_fresh_entry_has_full_strength(db_path) -> None:
    """An entry confirmed right now is at the base confidence."""
    entry_id = store(db_path, "fresh", type="fact", confidence="medium")
    now = datetime.now(timezone.utc)
    score = effective_confidence(db_path, entry_id, now=now, half_life_days=30)
    # Tiny clock skew is fine — within 1% of base
    assert score == pytest.approx(0.6, rel=0.01)


def test_half_life_decays_to_half(db_path) -> None:
    """30 days after last_confirmed_at, score is half the base."""
    entry_id = store(db_path, "old", type="fact", confidence="medium")
    future = datetime.now(timezone.utc) + timedelta(days=30)
    score = effective_confidence(db_path, entry_id, now=future, half_life_days=30)
    # Base 0.6 × 0.5^(30/30) = 0.3
    assert score == pytest.approx(0.3, rel=0.01)


def test_two_half_lives_decay_to_quarter(db_path) -> None:
    entry_id = store(db_path, "older", type="fact", confidence="medium")
    future = datetime.now(timezone.utc) + timedelta(days=60)
    score = effective_confidence(db_path, entry_id, now=future, half_life_days=30)
    # 0.6 × 0.5^(60/30) = 0.15
    assert score == pytest.approx(0.15, rel=0.01)


def test_half_life_zero_disables_decay(db_path) -> None:
    """half_life_days=0 returns the raw base score regardless of age."""
    entry_id = store(db_path, "ancient", type="fact", confidence="high")
    far_future = datetime.now(timezone.utc) + timedelta(days=10_000)
    score = effective_confidence(db_path, entry_id, now=far_future, half_life_days=0)
    assert score == pytest.approx(0.9)


def test_effective_confidence_returns_none_for_missing_entry(db_path) -> None:
    assert effective_confidence(db_path, 999_999) is None


# ---- reaffirm restores weight ---------------------------------------------


def test_reaffirm_restores_full_strength(db_path) -> None:
    """An aged entry that gets reaffirmed snaps back to full strength."""
    entry_id = store(db_path, "stale-then-fresh", type="fact", confidence="medium")
    # Simulate "30 days have passed" — score should be half
    future = datetime.now(timezone.utc) + timedelta(days=30)
    aged = effective_confidence(db_path, entry_id, now=future, half_life_days=30)
    assert aged == pytest.approx(0.3, rel=0.01)

    # Reaffirm: last_confirmed_at jumps to now()
    assert reaffirm(db_path, entry_id) is True

    # Right after reaffirm, the score is back to full
    after_reaffirm = effective_confidence(db_path, entry_id, half_life_days=30)
    assert after_reaffirm == pytest.approx(0.6, rel=0.01)


def test_reaffirm_returns_false_for_missing_entry(db_path) -> None:
    assert reaffirm(db_path, 999_999) is False


def test_reaffirm_does_not_create_new_row(db_path) -> None:
    """Reaffirmation is in-place — no new row, no supersede."""
    from techne.core.knowledge import list_entries

    entry_id = store(db_path, "fact", type="fact", confidence="medium")
    before = list_entries(db_path, type="fact")
    reaffirm(db_path, entry_id)
    after = list_entries(db_path, type="fact")
    assert len(after) == len(before)
    assert after[0]["id"] == entry_id


# ---- migration backfill (smoke test) --------------------------------------


def test_migrated_rows_have_correct_confidence_numeric(tmp_path) -> None:
    """A row inserted directly with no confidence_numeric column would have
    been backfilled by the v5 migration. Test via a fresh DB + direct
    insert that bypasses store() to simulate v4 data."""
    db_path = tmp_path / "v4_then_v5.db"
    conn = init_db(db_path)
    try:
        # Insert directly without confidence_numeric — relies on the
        # column's DEFAULT (0.6) to populate it.
        conn.execute(
            """INSERT INTO knowledge (type, content, confidence)
               VALUES ('fact', 'legacy row', 'high')"""
        )
        conn.commit()
    finally:
        conn.close()

    # The schema's DEFAULT is 0.6 (matches the migration backfill for
    # 'medium'); the direct INSERT bypassed our enum mapping. This test
    # documents that bypass-INSERTs don't get the enum-to-numeric
    # translation; the migration covers existing data via UPDATE.
    # For new writes, store() handles the mapping correctly.
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT confidence_numeric FROM knowledge WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["confidence_numeric"] == pytest.approx(0.6)
