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
from techne.reminders import add, dismiss, list_due, touch_surfaced


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
        """A reminder surfaced within the throttle window is skipped on subsequent calls."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        reminder_id = add(db_path, "throttled note", past)

        # Simulate a very recent surface by writing a recent last_surfaced_at directly.
        # We use touch_surfaced() then override the stamp to "1 minute ago" to be
        # clearly within the 3h window.
        touch_surfaced(db_path, reminder_id)

        # Force-update last_surfaced_at to 1 minute ago (within the 3h throttle).
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
        # Use a fresh session that is ALREADY recorded as seen, so the session-start
        # bypass does not apply. We first call with session-s2 to register it, then
        # call again with the same session.
        _inject_due_reminders(cfg, "session-s2")  # registers session-s2 as seen
        result = _inject_due_reminders(cfg, "session-s2")  # second call — throttle active
        # The throttled reminder should not appear (may be None or empty).
        if result is not None:
            assert "throttled note" not in result

    def test_old_surface_bypasses_throttle(self, db_path: Path) -> None:
        """A reminder last surfaced >3h ago surfaces again on a non-session-start call."""
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
            # Register "session-s3" as already seen so the session-start bypass
            # does NOT apply — we want to test the time-based bypass only.
            set_state(conn, "reminders:last_session_id", {"session_id": "session-s3"})
        finally:
            conn.close()

        cfg = self._make_config(db_path)
        # Non-session-start call — stale surface is outside the throttle window.
        result = _inject_due_reminders(cfg, "session-s3")
        assert result is not None
        assert "stale reminder" in result

    def test_session_start_bypasses_throttle(self, db_path: Path) -> None:
        """A new session_id bypasses the throttle even for recently-surfaced reminders."""
        from techne.hooks.post_tool_use import _inject_due_reminders

        past = _iso(_utcnow() - timedelta(minutes=30))
        reminder_id = add(db_path, "new-session bypass", past)

        # Stamp a very recent last_surfaced_at (within throttle window).
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
        # First call with a brand-new session_id — must bypass throttle.
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
