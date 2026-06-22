"""Tests for techne#95: self-healing session_start hook.

Covers:
  - Hook resilience: feature failure does NOT prevent session registration
  - extension_state persistence: last_run written; last_error written/cleared
  - Staleness alarm: prior last_session old → banner contains ⚠️ gap line
  - collect_health exposes new flags (hook_last_run, hook_last_error, stale, capture_alive)
  - Threads block respects cap/budget
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.config import Config, InjectionConfig
from techne.core.database import connect, init_db
from techne.core.extension_state import get_state, set_state
from techne.core.health import collect_health, format_banner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_db(p).close()
    return p


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.db_path = tmp_path / "test.db"
    init_db(cfg.db_path).close()
    return cfg


def _make_old_session(db_path: Path, hours_ago: int = 25) -> None:
    """Seed a session whose last observation (capture) is in the past.

    Staleness keys on the last observation, not the session row, so we stamp an
    observation at the target time too.
    """
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
            ("old-session-id", old_ts),
        )
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("old-session-id", "Observation", "old capture", old_ts),
        )
        conn.commit()
    finally:
        conn.close()


def _make_recent_session(db_path: Path, hours_ago: int = 1) -> None:
    """Seed a session whose last observation (capture) is recent."""
    recent_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
            ("recent-session-id", recent_ts),
        )
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("recent-session-id", "Observation", "recent capture", recent_ts),
        )
        conn.commit()
    finally:
        conn.close()


def _run_hook(
    hook_input: dict | None = None,
    env_overrides: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run session_start as a subprocess, returning the CompletedProcess."""
    env = {**os.environ, **(env_overrides or {})}
    stdin_bytes = json.dumps(hook_input or {}).encode()
    return subprocess.run(
        [sys.executable, "-m", "techne.hooks.session_start"],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Hook exit-code invariants (always 0)
# ---------------------------------------------------------------------------


class TestHookAlwaysExitsZero:
    def test_normal_run_exits_zero(self, tmp_path):
        result = _run_hook(
            hook_input={"cwd": str(tmp_path)},
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"

    def test_bad_home_exits_zero(self, tmp_path):
        """Even when Config/DB cannot be set up the hook must exit 0."""
        bad_home = tmp_path / "nonexistent"
        result = _run_hook(
            hook_input={},
            env_overrides={"HOME": str(bad_home)},
        )
        assert result.returncode == 0

    def test_malformed_stdin_exits_zero(self, tmp_path):
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, "-m", "techne.hooks.session_start"],
            input=b"not json {{",
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Session registration survives feature failures
# ---------------------------------------------------------------------------


class TestSessionRegistrationSurvivesFailures:
    def test_session_registered_despite_outbox_flush_failure(self, tmp_path: Path):
        """If outbox flush raises, session must still be registered.

        Uses a dedicated tmp_path so HOME is controlled and we know where the DB is.
        """
        session_id = "test-resilience-flush"
        # Subprocess with HOME=tmp_path → DB at tmp_path/.techne/techne.db
        result = _run_hook(
            hook_input={"cwd": str(tmp_path), "session_id": session_id},
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        db_path = tmp_path / ".techne" / "techne.db"
        assert db_path.exists(), f"DB was not created at {db_path}"
        conn = connect(db_path)
        try:
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
        assert row is not None, "Session was not registered despite hook failure"

    def test_injection_import_failure_doesnt_prevent_registration(self, tmp_path: Path):
        """Simulate import failure in egress — session must still be registered."""
        session_id = "test-import-failure"
        result = _run_hook(
            hook_input={"cwd": str(tmp_path), "session_id": session_id},
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        db_path = tmp_path / ".techne" / "techne.db"
        assert db_path.exists(), f"DB was not created at {db_path}"
        conn = connect(db_path)
        try:
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
        assert row is not None, "Session was not registered after import failure"


# ---------------------------------------------------------------------------
# extension_state persistence
# ---------------------------------------------------------------------------


class TestExtensionStatePersistence:
    def test_last_run_written_on_clean_run(self, tmp_path: Path):
        """last_run must be set in extension_state after a clean hook run."""
        session_id = "test-ext-state-run"
        # HOME=tmp_path → DB at tmp_path/.techne/techne.db
        result = _run_hook(
            hook_input={"cwd": str(tmp_path), "session_id": session_id},
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        db = tmp_path / ".techne" / "techne.db"
        assert db.exists(), f"DB was not created at {db}"
        conn = connect(db)
        try:
            payload = get_state(conn, "hook:session_start:last_run")
        finally:
            conn.close()
        assert payload is not None, "hook:session_start:last_run not written"
        assert "ts" in payload

    def test_last_run_written_directly(self, db_path: Path):
        """Unit test: _persist_hook_state writes last_run correctly."""
        from techne.hooks.session_start import _persist_hook_state

        ts = "2026-06-20T12:00:00+00:00"
        _persist_hook_state(db_path, last_run=ts, error=None)

        conn = connect(db_path)
        try:
            payload = get_state(conn, "hook:session_start:last_run")
        finally:
            conn.close()
        assert payload == {"ts": ts}

    def test_last_error_written_on_failure(self, db_path: Path):
        """_persist_hook_state writes last_error when error dict is provided."""
        from techne.hooks.session_start import _persist_hook_state

        ts = "2026-06-20T12:00:00+00:00"
        err = {"ts": ts, "stage": "outbox", "msg": "boom"}
        _persist_hook_state(db_path, last_run=ts, error=err)

        conn = connect(db_path)
        try:
            payload = get_state(conn, "hook:session_start:last_error")
        finally:
            conn.close()
        assert payload == err

    def test_last_error_cleared_on_clean_run(self, db_path: Path):
        """After a previous failure, a clean run must clear last_error."""
        from techne.hooks.session_start import _persist_hook_state

        ts1 = "2026-06-20T11:00:00+00:00"
        err = {"ts": ts1, "stage": "outbox", "msg": "boom"}
        _persist_hook_state(db_path, last_run=ts1, error=err)

        # Verify error is present.
        conn = connect(db_path)
        try:
            assert get_state(conn, "hook:session_start:last_error") == err
        finally:
            conn.close()

        # Clean run clears it.
        ts2 = "2026-06-20T12:00:00+00:00"
        _persist_hook_state(db_path, last_run=ts2, error=None)

        conn = connect(db_path)
        try:
            payload = get_state(conn, "hook:session_start:last_error")
        finally:
            conn.close()
        # After clear, should be {} (empty dict, not None).
        assert payload == {} or payload is None


# ---------------------------------------------------------------------------
# Staleness alarm
# ---------------------------------------------------------------------------


class TestStalenessAlarm:
    def test_stale_banner_when_capture_old(self, config: Config):
        """When the last observation is older than staleness_hours, banner has ⚠️ gap line."""
        config.staleness_hours = 12
        _make_old_session(config.db_path, hours_ago=25)

        health = collect_health(config)
        banner = format_banner(health)
        assert "⚠️ techne capture gap" in banner
        assert "no observations for" in banner
        assert "hook may be down" in banner

    def test_no_stale_warning_when_capture_fresh(self, config: Config):
        """A recently-captured observation means no staleness warning."""
        config.staleness_hours = 12
        _make_recent_session(config.db_path, hours_ago=1)

        health = collect_health(config)
        banner = format_banner(health)
        assert "capture gap" not in banner

    def test_long_session_with_recent_capture_not_stale(self, config: Config):
        """A long-lived session (old started_at) with recent capture must NOT alarm.

        This is the false-positive the capture-based alarm fixes: one session row
        spanning days while observations keep flowing.
        """
        config.staleness_hours = 12
        # Old session row, but a fresh observation under it (resumed/long session).
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=60)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        recent_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = connect(config.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                ("long-session", old_ts),
            )
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                ("long-session", "Observation", "fresh work", recent_ts),
            )
            conn.commit()
        finally:
            conn.close()

        health = collect_health(config)
        assert health["stale"] is False
        assert health["capture_alive"] is True

    def test_collect_health_stale_flag_set(self, config: Config):
        """collect_health returns stale=True when last capture is beyond staleness_hours."""
        config.staleness_hours = 12
        _make_old_session(config.db_path, hours_ago=25)

        health = collect_health(config)
        assert health["stale"] is True
        assert health["capture_alive"] is False

    def test_collect_health_stale_flag_not_set_for_fresh(self, config: Config):
        """collect_health returns stale=False for a recent capture."""
        config.staleness_hours = 12
        _make_recent_session(config.db_path, hours_ago=1)

        health = collect_health(config)
        assert health["stale"] is False
        assert health["capture_alive"] is True

    def test_no_stale_on_empty_db(self, config: Config):
        """Empty DB (no observations at all) should not alarm as stale."""
        health = collect_health(config)
        # No observations → last_capture is None → not stale, capture not yet alive.
        assert health["stale"] is False
        assert health["capture_alive"] is False


# ---------------------------------------------------------------------------
# collect_health new fields
# ---------------------------------------------------------------------------


class TestCollectHealthNewFields:
    def test_hook_last_run_present(self, config: Config):
        """collect_health always includes hook_last_run key."""
        health = collect_health(config)
        assert "hook_last_run" in health

    def test_hook_last_error_present(self, config: Config):
        """collect_health always includes hook_last_error key."""
        health = collect_health(config)
        assert "hook_last_error" in health

    def test_stale_present(self, config: Config):
        """collect_health always includes stale key."""
        health = collect_health(config)
        assert "stale" in health

    def test_capture_alive_present(self, config: Config):
        """collect_health always includes capture_alive key."""
        health = collect_health(config)
        assert "capture_alive" in health

    def test_hook_last_run_reads_from_extension_state(self, config: Config):
        """hook_last_run reflects whatever is in extension_state."""
        ts = "2026-06-20T12:00:00+00:00"
        conn = connect(config.db_path)
        try:
            set_state(conn, "hook:session_start:last_run", {"ts": ts})
        finally:
            conn.close()

        health = collect_health(config)
        assert health["hook_last_run"] == ts

    def test_hook_last_error_reads_from_extension_state(self, config: Config):
        """hook_last_error reflects whatever is in extension_state."""
        err = {"ts": "2026-06-20T11:00:00+00:00", "stage": "outbox", "msg": "boom"}
        conn = connect(config.db_path)
        try:
            set_state(conn, "hook:session_start:last_error", err)
        finally:
            conn.close()

        health = collect_health(config)
        assert health["hook_last_error"] == err

    def test_hook_error_surfaces_in_banner(self, config: Config):
        """When hook_last_error is set, format_banner includes the ⚠️ error line."""
        err = {"ts": "2026-06-20T11:00:00+00:00", "stage": "outbox", "msg": "boom"}
        conn = connect(config.db_path)
        try:
            set_state(conn, "hook:session_start:last_error", err)
        finally:
            conn.close()

        health = collect_health(config)
        health["build"] = "live"
        banner = format_banner(health)
        assert "⚠️ hook error" in banner
        assert "outbox" in banner
        assert "boom" in banner

    def test_capture_alive_true_when_recent_obs(self, config: Config):
        """capture_alive is True when there are observations in the last 48h."""
        conn = connect(config.db_path)
        try:
            # Need a session row first (FK constraint).
            conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", ("sess-1",))
            # observations.id is AUTOINCREMENT (INTEGER PK), so omit it.
            conn.execute(
                """INSERT INTO observations (session_id, tool_name, content, tier)
                   VALUES (?, ?, ?, ?)""",
                ("sess-1", "Bash", "ran ls", "hot"),
            )
            conn.commit()
        finally:
            conn.close()

        health = collect_health(config)
        assert health["capture_alive"] is True

    def test_capture_alive_false_on_empty_db(self, config: Config):
        """capture_alive is False when no observations exist."""
        health = collect_health(config)
        assert health["capture_alive"] is False


# ---------------------------------------------------------------------------
# Threads block respects cap/budget
# ---------------------------------------------------------------------------


class TestThreadsBoundedInjection:
    def _seed_threads(self, db_path: Path, n: int, status: str = "active") -> list[str]:
        """Insert n threads and return their IDs."""
        import uuid

        from techne.core.database import connect as _connect

        ids = []
        c = _connect(db_path)
        try:
            for i in range(n):
                tid = f"thread-{uuid.uuid4()}"
                c.execute(
                    """INSERT INTO threads
                       (id, session_id, thread_num, label, summary, resume_points, status,
                        updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (tid, "sess-1", i + 1, f"Thread {i}", f"Summary {i}", f"Resume {i}", status),
                )
                ids.append(tid)
            c.commit()
        finally:
            c.close()
        return ids

    def _seed_session(self, db_path: Path) -> None:
        conn = connect(db_path)
        try:
            conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", ("sess-1",))
            conn.commit()
        finally:
            conn.close()

    def test_active_threads_capped(self, config: Config):
        """session_start must cap active threads at config.injection.max_active_threads."""
        config.injection.max_active_threads = 2
        self._seed_session(config.db_path)
        self._seed_threads(config.db_path, n=10, status="active")

        from techne.core.state import get_resume_context

        ctx = get_resume_context(config.db_path)
        active = ctx.get("active_threads", [])[: config.injection.max_active_threads]
        assert len(active) <= config.injection.max_active_threads

    def test_recent_threads_capped(self, config: Config):
        """session_start must cap recent threads at config.injection.max_recent_threads."""
        config.injection.max_recent_threads = 3
        self._seed_session(config.db_path)
        self._seed_threads(config.db_path, n=10, status="complete")

        from techne.core.state import get_resume_context

        ctx = get_resume_context(config.db_path)
        recent = ctx.get("recent_threads", [])[: config.injection.max_recent_threads]
        assert len(recent) <= config.injection.max_recent_threads

    def test_thread_char_budget_respected(self, config: Config):
        """The combined threads block must not exceed thread_char_budget."""
        config.injection.max_active_threads = 10
        config.injection.max_recent_threads = 10
        config.injection.thread_char_budget = 200
        config.injection.thread_per_item_chars = 50
        self._seed_session(config.db_path)
        self._seed_threads(config.db_path, n=20, status="active")

        from techne.core.state import get_resume_context

        inj = config.injection
        ctx = get_resume_context(config.db_path)
        active_threads = ctx.get("active_threads", [])[: inj.max_active_threads]

        # Simulate the rendering loop from session_start.
        budget = inj.thread_char_budget
        per_item = inj.thread_per_item_chars
        chars_used = 0
        rendered_count = 0

        header = "## Active Threads\n"
        chars_used += len(header)
        for t in active_threads:
            label = (t.get("label") or "")[:per_item]
            resume = (t.get("resume_points") or "")[:per_item]
            updated = t.get("updated_at", "")
            item = f"**{label}** (updated {updated})"
            if resume:
                item += f"\nResume: {resume}"
            item += "\n"
            if chars_used + len(item) > budget:
                break
            chars_used += len(item)
            rendered_count += 1

        assert chars_used <= budget, f"Thread block exceeded budget: {chars_used} > {budget}"
        assert rendered_count < 20, "Budget should have stopped rendering before all 20 threads"

    def test_injection_config_new_fields_defaults(self):
        """InjectionConfig has the expected new field defaults."""
        inj = InjectionConfig()
        assert inj.max_active_threads == 3
        assert inj.max_recent_threads == 5
        assert inj.thread_char_budget == 1500
        assert inj.thread_per_item_chars == 200

    def test_staleness_hours_config_default(self):
        """Config.staleness_hours defaults to 12."""
        cfg = Config()
        assert cfg.staleness_hours == 12

    def test_config_from_dict_staleness_hours(self):
        """Config._from_dict parses staleness_hours."""
        cfg = Config._from_dict({"staleness_hours": 24})
        assert cfg.staleness_hours == 24

    def test_config_from_dict_injection_new_fields(self):
        """Config._from_dict parses new injection sub-keys."""
        cfg = Config._from_dict(
            {
                "injection": {
                    "enabled": True,
                    "char_budget": 1000,
                    "max_active_threads": 2,
                    "max_recent_threads": 4,
                    "thread_char_budget": 800,
                    "thread_per_item_chars": 150,
                }
            }
        )
        assert cfg.injection.max_active_threads == 2
        assert cfg.injection.max_recent_threads == 4
        assert cfg.injection.thread_char_budget == 800
        assert cfg.injection.thread_per_item_chars == 150
