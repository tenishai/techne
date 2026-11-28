"""Tests for techne durable reminders (techne#207).

Covers:
- add() inserts an active reminder and returns an int id
- list_due() returns due reminders, excludes not-yet-due and dismissed ones
- touch_surfaced() stamps last_surfaced_at
- dismiss() removes a reminder from list_due()
- post_tool_use hook injects due reminders into additionalContext
- post_tool_use hook throttles recently-surfaced reminders
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.reminders import add, dismiss, list_due, touch_surfaced, touch_surfaced_many


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Format a datetime as the TIMESTAMP text convention used throughout techne."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path).close()
    return path


# ---------------------------------------------------------------------------
# TestAdd
# ---------------------------------------------------------------------------


class TestAdd:
    def test_returns_int_id(self, db_path: Path) -> None:
        """add() returns an integer row id."""
        due = _iso(_utcnow() + timedelta(hours=1))
        reminder_id = add(db_path, "test note", due)
        assert isinstance(reminder_id, int)
        assert reminder_id > 0

    def test_sequential_ids(self, db_path: Path) -> None:
        """Two inserts return distinct ids."""
        due = _iso(_utcnow() + timedelta(hours=1))
        id1 = add(db_path, "first", due)
        id2 = add(db_path, "second", due)
        assert id1 != id2

    def test_inserted_as_active(self, db_path: Path) -> None:
        """Newly added reminders have status='active'."""
        due = _iso(_utcnow() + timedelta(minutes=5))
        reminder_id = add(db_path, "check something", due)
        now = _iso(_utcnow() + timedelta(minutes=10))
        due_list = list_due(db_path, now)
        match = next((r for r in due_list if r["id"] == reminder_id), None)
        assert match is not None
        assert match["status"] == "active"

    def test_note_is_stored_redacted(self, db_path: Path) -> None:
        """A credential-shaped string in the note never reaches the row (techne#1037)."""
        from techne.core.database import connect

        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
        reminder_id = add(db_path, f"rotate {token} before friday", _iso(_utcnow()))
        conn = connect(db_path)
        try:
            (stored,) = conn.execute(
                "SELECT note FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        finally:
            conn.close()
        assert token not in stored
        assert stored == "rotate <redacted:github> before friday"

    def test_plain_note_is_stored_unchanged(self, db_path: Path) -> None:
        """Text with no credential shape is stored exactly as given."""
        note = "check PR #1037 at 16:00 -- sk-short, ghp_ alone"
        reminder_id = add(db_path, note, _iso(_utcnow()))
        stored = next(r for r in list_due(db_path, _iso(_utcnow())) if r["id"] == reminder_id)
        assert stored["note"] == note


# ---------------------------------------------------------------------------
# TestListDue
# ---------------------------------------------------------------------------


class TestListDue:
    def test_due_reminder_appears(self, db_path: Path) -> None:
        """A reminder whose due_at is before 'now' appears in list_due."""
        past = _iso(_utcnow() - timedelta(minutes=5))
        reminder_id = add(db_path, "overdue task", past)
        now = _iso(_utcnow())
        due = list_due(db_path, now)
        ids = [r["id"] for r in due]
        assert reminder_id in ids

    def test_future_reminder_excluded(self, db_path: Path) -> None:
        """A reminder whose due_at is in the future does not appear in list_due."""
        future = _iso(_utcnow() + timedelta(hours=2))
        reminder_id = add(db_path, "future task", future)
        now = _iso(_utcnow())
        due = list_due(db_path, now)
        ids = [r["id"] for r in due]
        assert reminder_id not in ids

    def test_exactly_due_appears(self, db_path: Path) -> None:
        """A reminder with due_at == now is included (boundary: due_at <= now)."""
        now_str = _iso(_utcnow())
        reminder_id = add(db_path, "right-on-time task", now_str)
        due = list_due(db_path, now_str)
        ids = [r["id"] for r in due]
        assert reminder_id in ids

    def test_dismissed_excluded(self, db_path: Path) -> None:
        """A dismissed reminder does not appear in list_due."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        reminder_id = add(db_path, "dismissed task", past)
        dismiss(db_path, reminder_id)
        now = _iso(_utcnow())
        due = list_due(db_path, now)
        ids = [r["id"] for r in due]
        assert reminder_id not in ids

    def test_ordered_by_due_at(self, db_path: Path) -> None:
        """list_due returns reminders sorted by due_at ascending."""
        now = _utcnow()
        id_later = add(db_path, "later", _iso(now - timedelta(minutes=1)))
        id_earlier = add(db_path, "earlier", _iso(now - timedelta(minutes=10)))
        due = list_due(db_path, _iso(now))
        ids = [r["id"] for r in due]
        assert ids.index(id_earlier) < ids.index(id_later)

    def test_returns_expected_fields(self, db_path: Path) -> None:
        """Each dict in list_due has all required keys."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        add(db_path, "check fields", past)
        now = _iso(_utcnow())
        due = list_due(db_path, now)
        assert due, "expected at least one due reminder"
        row = due[0]
        assert set(row.keys()) == {"id", "note", "due_at", "status", "created_at", "last_surfaced_at"}

    def test_empty_when_no_due(self, db_path: Path) -> None:
        """list_due returns an empty list when nothing is due."""
        future = _iso(_utcnow() + timedelta(hours=99))
        add(db_path, "way future", future)
        now = _iso(_utcnow())
        assert list_due(db_path, now) == []


# ---------------------------------------------------------------------------
# TestTouchSurfaced
# ---------------------------------------------------------------------------


class TestTouchSurfaced:
    def test_updates_last_surfaced_at(self, db_path: Path) -> None:
        """touch_surfaced stamps last_surfaced_at with a non-None value."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        reminder_id = add(db_path, "remind me", past)

        # Before touch: last_surfaced_at is None.
        now_str = _iso(_utcnow())
        due_before = list_due(db_path, now_str)
        match_before = next(r for r in due_before if r["id"] == reminder_id)
        assert match_before["last_surfaced_at"] is None

        touch_surfaced(db_path, reminder_id)

        # After touch: last_surfaced_at is set.
        due_after = list_due(db_path, _iso(_utcnow()))
        match_after = next(r for r in due_after if r["id"] == reminder_id)
        assert match_after["last_surfaced_at"] is not None

    def test_updates_stamp_on_repeated_call(self, db_path: Path) -> None:
        """Calling touch_surfaced twice produces a later or equal stamp."""
        past = _iso(_utcnow() - timedelta(minutes=5))
        reminder_id = add(db_path, "surface me twice", past)

        touch_surfaced(db_path, reminder_id)
        now_str = _iso(_utcnow())
        first_stamp = next(
            r["last_surfaced_at"]
            for r in list_due(db_path, now_str)
            if r["id"] == reminder_id
        )

        touch_surfaced(db_path, reminder_id)
        now_str2 = _iso(_utcnow())
        second_stamp = next(
            r["last_surfaced_at"]
            for r in list_due(db_path, now_str2)
            if r["id"] == reminder_id
        )

        # Stamps are ISO text — lexicographic ≥ is correct for same-format strings.
        assert second_stamp >= first_stamp

    def test_reminder_still_active_after_touch(self, db_path: Path) -> None:
        """touch_surfaced does not dismiss or archive the reminder."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        reminder_id = add(db_path, "still active", past)
        touch_surfaced(db_path, reminder_id)
        now_str = _iso(_utcnow())
        due = list_due(db_path, now_str)
        ids = [r["id"] for r in due]
        assert reminder_id in ids


# ---------------------------------------------------------------------------
# TestDismiss
# ---------------------------------------------------------------------------


class TestDismiss:
    def test_dismissed_reminder_absent_from_list_due(self, db_path: Path) -> None:
        """After dismiss(), the reminder no longer appears in list_due."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        reminder_id = add(db_path, "dismiss me", past)
        dismiss(db_path, reminder_id)
        now_str = _iso(_utcnow())
        ids = [r["id"] for r in list_due(db_path, now_str)]
        assert reminder_id not in ids

    def test_dismiss_does_not_affect_others(self, db_path: Path) -> None:
        """Dismissing one reminder leaves the others intact."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        id_keep = add(db_path, "keep this", past)
        id_drop = add(db_path, "drop this", past)
        dismiss(db_path, id_drop)
        now_str = _iso(_utcnow())
        ids = [r["id"] for r in list_due(db_path, now_str)]
        assert id_keep in ids
        assert id_drop not in ids

    def test_dismiss_idempotent(self, db_path: Path) -> None:
        """Calling dismiss() twice does not raise."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        reminder_id = add(db_path, "double dismiss", past)
        dismiss(db_path, reminder_id)
        dismiss(db_path, reminder_id)  # must not raise


# ---------------------------------------------------------------------------
# TestPostToolUseReminderInjection
# ---------------------------------------------------------------------------


class TestPostToolUseReminderInjection:
    """Tests for the reminder-injection path in the post_tool_use hook (#207).

    We test _inject_due_reminders() directly rather than running a subprocess so
    we can use a tmp db and avoid real config dependencies.
    """

    def _make_config(self, db_path: Path):
        """Build a minimal Config with the given db_path."""
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db_path
        return cfg

    def test_due_unthrottled_reminder_appears_in_context(self, db_path: Path) -> None:
        """A due reminder with no prior surface appears in additionalContext."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        add(db_path, "buy groceries", past)
        cfg = self._make_config(db_path)
        result = _inject_due_reminders(cfg, "session-001")
        assert result is not None
        assert "buy groceries" in result
        assert "⏰ Reminder:" in result

    def test_recently_surfaced_reminder_is_throttled(self, db_path: Path) -> None:
        """A reminder surfaced within the throttle window is skipped on subsequent same-session calls."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        add(db_path, "throttled note", past)

        cfg = self._make_config(db_path)
        # First call: id not yet in session-s2's surfaced-set → surfaces, stamps
        # last_surfaced_at=now, records id in the session state.
        _inject_due_reminders(cfg, "session-s2")
        # Second call within the same session: id IS in session state AND
        # last_surfaced_at is very recent → throttled.
        result = _inject_due_reminders(cfg, "session-s2")
        if result is not None:
            assert "throttled note" not in result

    def test_old_surface_bypasses_throttle(self, db_path: Path) -> None:
        """A reminder last surfaced >3h ago re-surfaces even within the same session."""
        from techne.hooks.post_tool_use import _inject_due_reminders
        from techne.core.database import connect
        from techne.core.extension_state import set_state

        past = _iso(_utcnow() - timedelta(hours=10))
        reminder_id = add(db_path, "stale reminder", past)

        # Mark last_surfaced_at as 4 hours ago (outside the 3h throttle window)
        # WITHOUT going through _inject_due_reminders (which would update the stamp).
        old_surface = _iso(_utcnow() - timedelta(hours=4))
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE reminders SET last_surfaced_at = ? WHERE id = ?",
                (old_surface, reminder_id),
            )
            conn.commit()
            # Pre-populate session-s3's surfaced-set so the per-session "not yet seen"
            # bypass does NOT apply — we want to test the time-based bypass only.
            set_state(conn, "reminders:surfaced:session-s3", {"ids": [reminder_id]})
        finally:
            conn.close()

        cfg = self._make_config(db_path)
        # id IS in session state, but last_surfaced_at is outside the throttle window
        # → the time-based condition fires, so the reminder surfaces again.
        result = _inject_due_reminders(cfg, "session-s3")
        assert result is not None
        assert "stale reminder" in result

    def test_new_session_surfaces_despite_recent_stamp(self, db_path: Path) -> None:
        """A session whose surfaced-set is empty always surfaces due reminders,
        even if last_surfaced_at is within the throttle window (from another session)."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        reminder_id = add(db_path, "new-session bypass", past)

        # Stamp a very recent last_surfaced_at (within throttle window) — simulates
        # this reminder having been surfaced by a different session moments ago.
        from techne.core.database import connect
        recent = _iso(_utcnow() - timedelta(minutes=1))
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE reminders SET last_surfaced_at = ? WHERE id = ?",
                (recent, reminder_id),
            )
            conn.commit()
        finally:
            conn.close()

        cfg = self._make_config(db_path)
        # "brand-new-session" has an empty surfaced-set → id not in set → bypass throttle.
        result = _inject_due_reminders(cfg, "brand-new-session")
        assert result is not None
        assert "new-session bypass" in result

    def test_no_due_reminders_returns_none(self, db_path: Path) -> None:
        """_inject_due_reminders returns None when nothing is due."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        future = _iso(_utcnow() + timedelta(hours=5))
        add(db_path, "not yet due", future)
        cfg = self._make_config(db_path)
        result = _inject_due_reminders(cfg, "session-none")
        assert result is None

    def test_dismissed_reminder_not_injected(self, db_path: Path) -> None:
        """A dismissed reminder is never injected by the hook."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=5))
        reminder_id = add(db_path, "already done", past)
        dismiss(db_path, reminder_id)
        cfg = self._make_config(db_path)
        result = _inject_due_reminders(cfg, "session-done")
        assert result is None

    def test_two_concurrent_sessions_each_surface_same_reminder(self, db_path: Path) -> None:
        """Two different session_ids each surface the same due reminder on their first call.

        With the old global last-session-id approach, alternating sessions would
        each look like a "new" session on every call, flooding both. With the
        per-session surfaced-set approach, each session independently sees the
        reminder once and then throttles within its own session.
        """
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        add(db_path, "concurrent reminder", past)
        cfg = self._make_config(db_path)

        # Session A surfaces the reminder (first time for A's surfaced-set).
        result_a = _inject_due_reminders(cfg, "session-concurrent-A")
        assert result_a is not None
        assert "concurrent reminder" in result_a

        # Session B is independent — its surfaced-set is empty, so it also surfaces.
        # The ~3h throttle on last_surfaced_at would block it with the old design,
        # but with per-session state it surfaces correctly.
        result_b = _inject_due_reminders(cfg, "session-concurrent-B")
        assert result_b is not None
        assert "concurrent reminder" in result_b

    def test_within_session_second_call_is_throttled(self, db_path: Path) -> None:
        """Within one session, a second call within the throttle window does not re-surface."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        add(db_path, "within-session note", past)
        cfg = self._make_config(db_path)

        # First call: id not in session-ws's set → surfaces, records id, stamps now.
        result1 = _inject_due_reminders(cfg, "session-ws")
        assert result1 is not None
        assert "within-session note" in result1

        # Second call: id IS in session-ws's set, last_surfaced_at=now → throttled.
        result2 = _inject_due_reminders(cfg, "session-ws")
        if result2 is not None:
            assert "within-session note" not in result2


# ---------------------------------------------------------------------------
# TestTouchSurfacedMany
# ---------------------------------------------------------------------------


class TestTouchSurfacedMany:
    """Tests for the batch touch_surfaced_many helper (techne#207)."""

    def test_batch_stamps_multiple_reminders(self, db_path: Path) -> None:
        """touch_surfaced_many stamps last_surfaced_at for all ids in one call."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        id1 = add(db_path, "first batch", past)
        id2 = add(db_path, "second batch", past)

        touch_surfaced_many(db_path, [id1, id2])

        now_str = _iso(_utcnow())
        due = list_due(db_path, now_str)
        stamped = {r["id"]: r["last_surfaced_at"] for r in due}
        assert stamped[id1] is not None, "id1 not stamped by touch_surfaced_many"
        assert stamped[id2] is not None, "id2 not stamped by touch_surfaced_many"

    def test_empty_list_is_noop(self, db_path: Path) -> None:
        """touch_surfaced_many([]) raises no error and changes nothing."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        rid = add(db_path, "untouched", past)
        touch_surfaced_many(db_path, [])  # must not raise
        now_str = _iso(_utcnow())
        row = next(r for r in list_due(db_path, now_str) if r["id"] == rid)
        assert row["last_surfaced_at"] is None  # unchanged

    def test_single_id_equivalent_to_touch_surfaced(self, db_path: Path) -> None:
        """touch_surfaced_many([id]) is equivalent to touch_surfaced(id)."""
        past = _iso(_utcnow() - timedelta(minutes=5))
        rid = add(db_path, "solo batch", past)
        touch_surfaced_many(db_path, [rid])
        now_str = _iso(_utcnow())
        row = next(r for r in list_due(db_path, now_str) if r["id"] == rid)
        assert row["last_surfaced_at"] is not None

    def test_reminder_still_active_after_batch_touch(self, db_path: Path) -> None:
        """touch_surfaced_many does not dismiss or archive the reminders."""
        past = _iso(_utcnow() - timedelta(minutes=1))
        id1 = add(db_path, "keep active 1", past)
        id2 = add(db_path, "keep active 2", past)
        touch_surfaced_many(db_path, [id1, id2])
        now_str = _iso(_utcnow())
        active_ids = {r["id"] for r in list_due(db_path, now_str)}
        assert id1 in active_ids
        assert id2 in active_ids


class TestReminderBudget:
    """techne#1239: reminders fill only the budget the hook has left, and the ones
    that do not fit collapse to one line and stay unsurfaced for a later call."""

    def _cfg(self, db_path: Path):
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db_path
        return cfg

    def test_overflow_collapses_to_one_line_and_comes_back_later(self, db_path: Path) -> None:
        from techne.hooks.post_tool_use import _REMINDER_OVERFLOW_RESERVE, _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        for i in range(6):
            add(db_path, f"reminder number {i} " + "x" * 80, past)
        cfg = self._cfg(db_path)
        budget = _REMINDER_OVERFLOW_RESERVE + 2 * 120  # room for two
        first = _inject_due_reminders(cfg, "s-budget", budget=budget)
        assert first is not None
        shown = [ln for ln in first.splitlines() if ln.startswith("⏰ Reminder:")]
        assert len(shown) == 2
        assert "4 more reminder(s) due" in first and "techne_reminder_list" in first
        assert len(first) <= budget + _REMINDER_OVERFLOW_RESERVE

        # The four not shown were not marked surfaced: the next call shows them.
        second = _inject_due_reminders(cfg, "s-budget", budget=10_000)
        assert second is not None
        assert len([ln for ln in second.splitlines() if ln.startswith("⏰ Reminder:")]) == 4

    def test_no_budget_shows_everything(self, db_path: Path) -> None:
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        for i in range(3):
            add(db_path, f"note {i}", past)
        result = _inject_due_reminders(self._cfg(db_path), "s-all")
        assert result is not None and "more reminder" not in result
        assert len(result.splitlines()) == 3
