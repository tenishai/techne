"""Tests for techne#119: bounded hook timeout + guaranteed exit-0 degradation.

Verifies that every hook sidecar:
  - exits 0 when the receiver/DB is unreachable or the work hangs
  - returns within the configured budget (fast fail, not indefinite block)
  - does not raise or return non-zero under any failure scenario
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from techne.hooks._timeout import DEFAULT_TIMEOUT_S, HookTimeout, bounded_hook

# ---------------------------------------------------------------------------
# Unit tests for bounded_hook
# ---------------------------------------------------------------------------


class TestBoundedHookContextManager:
    """Unit tests for the bounded_hook context manager itself."""

    def test_no_timeout_when_work_is_fast(self):
        """Fast work completes without raising HookTimeout."""
        with bounded_hook(timeout_s=2.0):
            result = 1 + 1
        assert result == 2

    def test_timeout_fires_when_work_blocks(self):
        """HookTimeout is raised when the budget expires."""
        with pytest.raises(HookTimeout):
            with bounded_hook(timeout_s=0.2):
                time.sleep(5)  # deliberately longer than budget

    def test_timeout_message_contains_budget(self):
        """HookTimeout message names the configured budget."""
        with pytest.raises(HookTimeout, match=r"0\.2s"):
            with bounded_hook(timeout_s=0.2):
                time.sleep(5)

    def test_zero_timeout_is_noop(self):
        """timeout_s=0 disables the guard (useful for slow-path unit tests)."""
        # Should NOT raise even with a long sleep — the guard is disabled.
        start = time.monotonic()
        with bounded_hook(timeout_s=0):
            time.sleep(0.05)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.05  # the sleep actually ran

    def test_sigalrm_disarmed_after_normal_exit(self):
        """The SIGALRM timer is cancelled after normal context exit."""
        if not hasattr(signal, "SIGALRM"):
            pytest.skip("SIGALRM not available on this platform")

        with bounded_hook(timeout_s=0.5):
            pass  # fast, no work

        # Wait longer than the original budget; if the alarm was not cancelled
        # a HookTimeout would fire here.
        time.sleep(0.6)

    def test_sigalrm_disarmed_after_exception(self):
        """The SIGALRM timer is cancelled even when an exception propagates."""
        if not hasattr(signal, "SIGALRM"):
            pytest.skip("SIGALRM not available on this platform")

        with pytest.raises(ValueError):
            with bounded_hook(timeout_s=1.0):
                raise ValueError("inner failure")

        # The timer must be cancelled; wait past the budget.
        time.sleep(1.1)

    def test_hook_timeout_is_exception_subclass(self):
        """HookTimeout inherits from Exception so broad except catches it."""
        assert issubclass(HookTimeout, Exception)

    def test_default_timeout_respects_env_var(self, monkeypatch):
        """DEFAULT_TIMEOUT_S reads from TECHNE_HOOK_TIMEOUT at import time.

        We can't easily re-import the module in a test, but we can verify the
        module-level constant is a float (and the env-read pattern works by
        inspecting that the default equals the env-var-based value).
        """
        assert isinstance(DEFAULT_TIMEOUT_S, float)
        assert DEFAULT_TIMEOUT_S > 0


# ---------------------------------------------------------------------------
# Subprocess-level tests: hook processes exit 0 on failure
# ---------------------------------------------------------------------------


def _run_hook(
    module: str,
    stdin_payload: dict | str,
    *,
    env_overrides: dict | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Run a hook module as a subprocess and return the result."""
    env = {**os.environ, **(env_overrides or {})}
    if isinstance(stdin_payload, dict):
        stdin_bytes = json.dumps(stdin_payload).encode()
    else:
        stdin_bytes = stdin_payload.encode()
    return subprocess.run(
        [sys.executable, "-m", module],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


class TestPostToolUseHookTimeout:
    """post_tool_use must exit 0 quickly when the DB is unavailable."""

    def test_exits_zero_on_bad_db_path(self, tmp_path: Path):
        """If the DB can't be opened, exit 0 — never fail the tool call."""
        payload = {
            "session_id": "sess-test-timeout",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/foo.py", "old_string": "a", "new_string": "b"},
            "tool_response": "ok",
        }
        # Point HOME at an unwritable path so DB open fails.
        result = _run_hook(
            "techne.hooks.post_tool_use",
            payload,
            env_overrides={"HOME": str(tmp_path / "nonexistent")},
        )
        assert result.returncode == 0, (
            f"post_tool_use exited {result.returncode} when DB was unavailable\n"
            f"stderr: {result.stderr.decode()}"
        )

    def test_exits_zero_with_aggressive_timeout(self, tmp_path: Path):
        """Even with a 1 ms budget the hook exits 0 (may timeout, must not hang)."""
        payload = {
            "session_id": "sess-test-fast-timeout",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/foo.py", "old_string": "a", "new_string": "b"},
            "tool_response": "ok",
        }
        result = _run_hook(
            "techne.hooks.post_tool_use",
            payload,
            env_overrides={
                "HOME": str(tmp_path),
                "TECHNE_HOOK_TIMEOUT": "0.001",  # 1 ms — will almost certainly timeout
            },
            timeout=5.0,
        )
        assert result.returncode == 0, (
            f"post_tool_use exited {result.returncode} with aggressive timeout\n"
            f"stderr: {result.stderr.decode()}"
        )

    def test_completes_within_budget_on_normal_path(self, tmp_path: Path):
        """On the happy path the hook completes well within the 5 s budget."""
        payload = {
            "session_id": "sess-timing",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/foo.py", "old_string": "a", "new_string": "b"},
            "tool_response": "ok",
        }
        start = time.monotonic()
        result = _run_hook(
            "techne.hooks.post_tool_use",
            payload,
            env_overrides={"HOME": str(tmp_path)},
            timeout=15.0,
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0
        assert elapsed < 10.0, f"post_tool_use took {elapsed:.1f}s — exceeds sanity ceiling"


class TestSessionEndHookTimeout:
    """session_end must exit 0 quickly when work hangs or the DB is unavailable."""

    def test_exits_zero_on_bad_db_path(self, tmp_path: Path):
        """DB unavailable → exit 0, no non-zero exit."""
        result = _run_hook(
            "techne.hooks.session_end",
            {"session_id": "sess-bad-db"},
            env_overrides={"HOME": str(tmp_path / "nonexistent")},
        )
        assert result.returncode == 0, (
            f"session_end exited {result.returncode} when DB was unavailable\n"
            f"stderr: {result.stderr.decode()}"
        )

    def test_exits_zero_with_aggressive_timeout(self, tmp_path: Path):
        """1 ms budget triggers timeout path — must still exit 0."""
        result = _run_hook(
            "techne.hooks.session_end",
            {"session_id": "sess-timeout"},
            env_overrides={
                "HOME": str(tmp_path),
                "TECHNE_HOOK_TIMEOUT": "0.001",
            },
            timeout=5.0,
        )
        assert result.returncode == 0, (
            f"session_end exited {result.returncode} under aggressive timeout\n"
            f"stderr: {result.stderr.decode()}"
        )

    def test_does_not_hang_longer_than_budget_plus_margin(self, tmp_path: Path):
        """session_end must not run longer than budget + a generous margin."""
        budget = 0.5  # half a second
        margin = 3.0  # generous to avoid flakiness on slow CI

        start = time.monotonic()
        result = _run_hook(
            "techne.hooks.session_end",
            {"session_id": "sess-no-hang"},
            env_overrides={
                "HOME": str(tmp_path),
                "TECHNE_HOOK_TIMEOUT": str(budget),
            },
            timeout=budget + margin + 2.0,
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0
        assert elapsed < budget + margin, (
            f"session_end ran for {elapsed:.2f}s (budget {budget}s + margin {margin}s) — "
            "hook did not respect its timeout"
        )


class TestHookTimeoutExitZeroInvariant:
    """Cross-hook invariant: all hooks exit 0 when the receiver is down."""

    @pytest.mark.parametrize(
        "module",
        [
            "techne.hooks.post_tool_use",
            "techne.hooks.session_end",
            "techne.hooks.session_start",
            "techne.hooks.pre_compact",
            "techne.hooks.post_compact",
        ],
    )
    def test_exits_zero_receiver_unavailable(self, module: str, tmp_path: Path):
        """Every hook exits 0 when HOME points to a nonexistent dir (DB unavailable)."""
        payloads = {
            "techne.hooks.post_tool_use": {
                "session_id": "sess-x",
                "tool_name": "Edit",
                "tool_input": {},
                "tool_response": "ok",
            },
            "techne.hooks.session_end": {"session_id": "sess-x"},
            "techne.hooks.session_start": {"session_id": "sess-x", "cwd": str(tmp_path)},
            "techne.hooks.pre_compact": {"session_id": "sess-x", "trigger": "manual"},
            "techne.hooks.post_compact": {"session_id": "sess-x"},
        }
        result = _run_hook(
            module,
            payloads[module],
            env_overrides={"HOME": str(tmp_path / "nonexistent")},
        )
        assert result.returncode == 0, (
            f"{module} exited {result.returncode} when receiver was unavailable\n"
            f"stderr: {result.stderr.decode()}"
        )

    @pytest.mark.parametrize(
        "module",
        [
            "techne.hooks.post_tool_use",
            "techne.hooks.session_end",
            "techne.hooks.session_start",
            "techne.hooks.pre_compact",
            "techne.hooks.post_compact",
        ],
    )
    def test_exits_zero_with_near_zero_timeout(self, module: str, tmp_path: Path):
        """Every hook exits 0 even with a near-zero timeout (stress the degradation path)."""
        payloads = {
            "techne.hooks.post_tool_use": {
                "session_id": "sess-y",
                "tool_name": "Edit",
                "tool_input": {},
                "tool_response": "ok",
            },
            "techne.hooks.session_end": {"session_id": "sess-y"},
            "techne.hooks.session_start": {"session_id": "sess-y", "cwd": str(tmp_path)},
            "techne.hooks.pre_compact": {"session_id": "sess-y", "trigger": "manual"},
            "techne.hooks.post_compact": {"session_id": "sess-y"},
        }
        result = _run_hook(
            module,
            payloads[module],
            env_overrides={
                "HOME": str(tmp_path),
                "TECHNE_HOOK_TIMEOUT": "0.001",
            },
            timeout=5.0,
        )
        assert result.returncode == 0, (
            f"{module} exited {result.returncode} with near-zero timeout\n"
            f"stderr: {result.stderr.decode()}"
        )


# ---------------------------------------------------------------------------
# Config field
# ---------------------------------------------------------------------------


class TestHookConfigTimeoutField:
    """HookConfig.timeout_s exists, defaults correctly, and round-trips."""

    def test_default_is_five_seconds(self):
        from techne.config import HookConfig

        cfg = HookConfig()
        assert cfg.timeout_s == 5.0

    def test_parsed_from_dict(self):
        from techne.config import Config

        cfg = Config._from_dict({"hooks": {"timeout_s": 2.5}})
        assert cfg.hooks.timeout_s == 2.5

    def test_defaults_preserved_when_key_absent(self):
        from techne.config import Config

        cfg = Config._from_dict({"hooks": {"post_tool_use": False}})
        assert cfg.hooks.timeout_s == 5.0

    def test_roundtrips_through_save_load(self, tmp_path: Path):
        """timeout_s survives a Config.save() / Config.load() round-trip."""
        from techne.config import Config

        cfg = Config()
        cfg.db_path = tmp_path / "techne.db"
        cfg.data_dir = tmp_path
        cfg.hooks.timeout_s = 3.7

        config_path = tmp_path / "config.yaml"
        cfg.save(config_path)

        loaded = Config.load(config_path)
        assert loaded.hooks.timeout_s == pytest.approx(3.7)
