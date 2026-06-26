"""Bounded-execution guard for techne hook sidecar processes.

A hook that blocks an agent turn is worse than a hook that silently skips a
capture.  This module provides a SIGALRM-based hard-kill backstop so no hook
process can wedge indefinitely — not from slow SQLite WAL contention, not from
a hung LLM API call, not from a stalled sync adapter.

Usage in a hook module::

    from techne.hooks._timeout import bounded_hook, DEFAULT_TIMEOUT_S

    def main():
        config = Config.load()
        try:
            with bounded_hook(config.hooks.timeout_s):
                do_all_the_blocking_work()
        except Exception as exc:
            print(f"techne: hook degraded: {exc}", file=sys.stderr)
            sys.exit(0)

Environment:
    TECHNE_HOOK_TIMEOUT  Wall-clock seconds before the hard kill fires.
                         Float; default 5.0.  Set to 0 to disable (tests only).
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Generator

# ---------------------------------------------------------------------------
# Timeout budget
# ---------------------------------------------------------------------------

_DEFAULT_S: float = 5.0

DEFAULT_TIMEOUT_S: float = float(os.environ.get("TECHNE_HOOK_TIMEOUT", str(_DEFAULT_S)))


class HookTimeout(RuntimeError):
    """Raised when the hook's wall-clock budget is exhausted."""


@contextlib.contextmanager
def bounded_hook(timeout_s: float = DEFAULT_TIMEOUT_S) -> Generator[None, None, None]:
    """Context manager that fires SIGALRM after *timeout_s* seconds.

    On alarm the running code receives a :class:`HookTimeout` exception.
    The caller is responsible for catching it (or catching ``Exception``
    broadly) and calling ``sys.exit(0)``.

    Design notes:
    - SIGALRM is Unix-only; on Windows the context manager is a transparent
      no-op so hook code is portable.
    - ``timeout_s <= 0`` is also a no-op (useful in unit tests that need to
      exercise slow paths without hitting the budget).
    - ``signal.setitimer`` provides sub-second resolution.  ``signal.alarm``
      (integer seconds) is intentionally *not* used.
    - The previous SIGALRM handler is restored unconditionally in the
      ``finally`` block, so nested uses are safe.
    """
    if not hasattr(signal, "SIGALRM") or timeout_s <= 0:
        yield
        return

    def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        raise HookTimeout(f"hook timed out after {timeout_s:.1f}s")

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)  # disarm before restoring
        signal.signal(signal.SIGALRM, old_handler)
