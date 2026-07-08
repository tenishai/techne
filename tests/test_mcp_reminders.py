"""Tests for the reminder MCP tools added in techne#207 arming.

Covers:
- _parse_due_at: relative offsets (+1d, +3h, +30m), absolute ISO, malformed input
- techne_reminder_add via handle_request: creates a DB row with a UTC due_at
- techne_reminder_list via handle_request: returns all active reminders
- techne_reminder_dismiss via handle_request: removes reminder from active list
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.mcp_server import _parse_due_at, handle_request
from techne.reminders import add, list_active


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


@pytest.fixture
def config(db_path: Path) -> Config:
    cfg = Config()
    cfg.db_path = db_path
    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TestParseDueAt
# ---------------------------------------------------------------------------


class TestParseDueAt:
    def test_plus_1d_returns_iso_string(self) -> None:
        """+1d returns a well-formed ISO timestamp."""
        result = _parse_due_at("+1d")
        assert _ISO_RE.match(result), f"bad format: {result!r}"

    def test_plus_1d_is_approx_one_day_from_now(self) -> None:
        # Truncate to seconds: strftime drops sub-second precision, so 'before'
        # microseconds would make the lower-bound comparison flaky.
        before = _utcnow().replace(microsecond=0)
        result = _parse_due_at("+1d")
        after = _utcnow().replace(microsecond=0)
        parsed = _parse_iso(result)
        assert before + timedelta(days=1) <= parsed <= after + timedelta(days=1, seconds=1)

    def test_plus_3h_is_approx_three_hours_from_now(self) -> None:
        before = _utcnow().replace(microsecond=0)
        result = _parse_due_at("+3h")
        after = _utcnow().replace(microsecond=0)
        parsed = _parse_iso(result)
        assert before + timedelta(hours=3) <= parsed <= after + timedelta(hours=3, seconds=1)

    def test_plus_30m_is_approx_30_minutes_from_now(self) -> None:
        before = _utcnow().replace(microsecond=0)
        result = _parse_due_at("+30m")
        after = _utcnow().replace(microsecond=0)
        parsed = _parse_iso(result)
        assert before + timedelta(minutes=30) <= parsed <= after + timedelta(minutes=30, seconds=1)

    def test_absolute_iso_is_passed_through(self) -> None:
        """An absolute 'YYYY-MM-DD HH:MM:SS' is returned verbatim."""
        absolute = "2027-01-15 09:30:00"
        assert _parse_due_at(absolute) == absolute

    def test_absolute_with_whitespace_is_stripped(self) -> None:
        """Leading/trailing whitespace around an absolute timestamp is tolerated."""
        assert _parse_due_at("  2027-01-15 09:30:00  ") == "2027-01-15 09:30:00"

    def test_relative_with_whitespace_is_tolerated(self) -> None:
        """+1d surrounded by spaces is accepted."""
        result = _parse_due_at("  +1d  ")
        assert _ISO_RE.match(result)

    def test_malformed_raises_value_error(self) -> None:
        """Unrecognised input raises ValueError with a helpful message."""
        with pytest.raises(ValueError, match="unrecognised format"):
            _parse_due_at("tomorrow")

    def test_invalid_iso_raises_value_error(self) -> None:
        """A date-only string (missing time) raises ValueError."""
        with pytest.raises(ValueError, match="unrecognised format"):
            _parse_due_at("2027-01-15")

    def test_plus_0d_raises_value_error(self) -> None:
        """+0d is rejected — offset must be positive."""
        with pytest.raises(ValueError, match="positive"):
            _parse_due_at("+0d")

    def test_unit_only_raises_value_error(self) -> None:
        """'+d' (no number) raises ValueError."""
        with pytest.raises(ValueError, match="unrecognised format"):
            _parse_due_at("+d")

    def test_no_plus_prefix_raises_value_error(self) -> None:
        """'1d' without '+' is not a valid relative offset."""
        with pytest.raises(ValueError, match="unrecognised format"):
            _parse_due_at("1d")


# ---------------------------------------------------------------------------
# TestReminderAddMCP
# ---------------------------------------------------------------------------


class TestReminderAddMCP:
    """techne_reminder_add via handle_request creates a DB row with a UTC due_at."""

    def _call(self, config: Config, **args):
        return handle_request(
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "techne_reminder_add", "arguments": args},
            },
            config,
        )

    def test_add_returns_id_and_due_at_utc(self, config: Config) -> None:
        """A successful add returns id (int) and due_at_utc (ISO string)."""
        resp = self._call(config, note="write tests", due_at="+1h")
        payload = resp["result"]["content"][0]["text"]
        import json

        data = json.loads(payload)
        assert isinstance(data["id"], int) and data["id"] > 0
        assert _ISO_RE.match(data["due_at_utc"])

    def test_add_relative_stores_utc_row(self, config: Config) -> None:
        """A +1d add stores a row whose due_at is approximately 1 day in the future."""
        before = _utcnow().replace(microsecond=0)
        resp = self._call(config, note="a future note", due_at="+1d")
        import json

        data = json.loads(resp["result"]["content"][0]["text"])
        reminder_id = data["id"]
        after = _utcnow().replace(microsecond=0)

        # Verify the row landed in the DB.
        rows = list_active(config.db_path)
        row = next(r for r in rows if r["id"] == reminder_id)
        stored_dt = _parse_iso(row["due_at"])
        assert before + timedelta(days=1) <= stored_dt <= after + timedelta(days=1, seconds=1)

    def test_add_absolute_stores_exact_due_at(self, config: Config) -> None:
        """An absolute due_at is stored verbatim."""
        absolute = "2028-06-01 12:00:00"
        resp = self._call(config, note="fixed date", due_at=absolute)
        import json

        data = json.loads(resp["result"]["content"][0]["text"])
        rows = list_active(config.db_path)
        row = next(r for r in rows if r["id"] == data["id"])
        assert row["due_at"] == absolute

    def test_missing_note_returns_is_error(self, config: Config) -> None:
        """Omitting 'note' returns an isError response."""
        resp = self._call(config, due_at="+1h")
        assert resp["result"]["isError"] is True

    def test_missing_due_at_returns_is_error(self, config: Config) -> None:
        """Omitting 'due_at' returns an isError response."""
        resp = self._call(config, note="something")
        assert resp["result"]["isError"] is True

    def test_malformed_due_at_returns_is_error(self, config: Config) -> None:
        """A malformed due_at returns an isError response."""
        resp = self._call(config, note="bad due", due_at="not-a-date")
        assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# TestReminderListMCP
# ---------------------------------------------------------------------------


class TestReminderListMCP:
    """techne_reminder_list returns all active reminders, including future ones."""

    def _call(self, config: Config):
        return handle_request(
            {
                "id": 2,
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "techne_reminder_list", "arguments": {}},
            },
            config,
        )

    def test_empty_db_returns_empty_list(self, config: Config) -> None:
        """list returns empty when no reminders exist."""
        import json

        resp = self._call(config)
        data = json.loads(resp["result"]["content"][0]["text"])
        assert data["reminders"] == []

    def test_returns_active_reminder(self, config: Config) -> None:
        """A reminder added directly appears in the list result."""
        import json
        from datetime import timedelta

        due = (_utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        rid = add(config.db_path, "list me", due)
        resp = self._call(config)
        data = json.loads(resp["result"]["content"][0]["text"])
        ids = [r["id"] for r in data["reminders"]]
        assert rid in ids

    def test_includes_future_reminder(self, config: Config) -> None:
        """A future (not-yet-due) reminder is included (list_active, not list_due)."""
        import json

        far_future = "2099-01-01 00:00:00"
        rid = add(config.db_path, "way future", far_future)
        resp = self._call(config)
        data = json.loads(resp["result"]["content"][0]["text"])
        ids = [r["id"] for r in data["reminders"]]
        assert rid in ids

    def test_dismissed_reminder_excluded(self, config: Config, db_path: Path) -> None:
        """A dismissed reminder does not appear in the list."""
        import json
        from techne.reminders import dismiss

        due = (_utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        rid = add(db_path, "dismiss me", due)
        dismiss(db_path, rid)
        resp = self._call(config)
        data = json.loads(resp["result"]["content"][0]["text"])
        ids = [r["id"] for r in data["reminders"]]
        assert rid not in ids


# ---------------------------------------------------------------------------
# TestReminderDismissMCP
# ---------------------------------------------------------------------------


class TestReminderDismissMCP:
    """techne_reminder_dismiss marks a reminder done and removes it from active list."""

    def _add(self, config: Config) -> int:
        due = (_utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        return add(config.db_path, "dismiss me via MCP", due)

    def _call(self, config: Config, reminder_id: int):
        return handle_request(
            {
                "id": 3,
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "techne_reminder_dismiss", "arguments": {"id": reminder_id}},
            },
            config,
        )

    def test_dismiss_returns_dismissed_id(self, config: Config) -> None:
        """Dismiss returns {dismissed: id}."""
        import json

        rid = self._add(config)
        resp = self._call(config, rid)
        data = json.loads(resp["result"]["content"][0]["text"])
        assert data["dismissed"] == rid

    def test_dismissed_reminder_absent_from_list_active(self, config: Config) -> None:
        """After dismiss, the reminder no longer appears in list_active."""
        rid = self._add(config)
        self._call(config, rid)
        rows = list_active(config.db_path)
        ids = [r["id"] for r in rows]
        assert rid not in ids

    def test_missing_id_returns_is_error(self, config: Config) -> None:
        """Omitting 'id' returns an isError response."""
        resp = handle_request(
            {
                "id": 4,
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "techne_reminder_dismiss", "arguments": {}},
            },
            config,
        )
        assert resp["result"]["isError"] is True
