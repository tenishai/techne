"""Bounded-execution guard for techne hook sidecar processes.

A hook that blocks an agent turn is worse than a hook that silently skips a
capture.  This module provides a SIGALRM-based hard-kill backstop that bounds a
hook's blocking section — slow SQLite WAL contention, a hung LLM API call, a
stalled sync adapter — to a timeout instead of letting it run unbounded.

SIGALRM only fires when Python regains control between bytecodes, so a hook
blocked inside a single long, signal-deaf C call isn't interrupted until that
call returns; in practice the blocking paths here (SQLite busy-waits, socket
I/O) are interruptible, so the realistic wedge cases are covered.

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
import sys
import time
from collections.abc import Generator

# ---------------------------------------------------------------------------
# Timeout budget
# ---------------------------------------------------------------------------

_DEFAULT_S: float = 5.0

DEFAULT_TIMEOUT_S: float = float(os.environ.get("TECHNE_HOOK_TIMEOUT", str(_DEFAULT_S)))


class HookTimeout(RuntimeError):
    """Raised when the hook's wall-clock budget is exhausted."""


@contextlib.contextmanager
def bounded_hook(
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    label: str = "hook",
    warn_ratio: float = 0.7,
) -> Generator[None, None, None]:
    """Context manager that fires SIGALRM after *timeout_s* seconds.

    On alarm the running code receives a :class:`HookTimeout` exception.
    The caller is responsible for catching it (or catching ``Exception``
    broadly) and calling ``sys.exit(0)``.

    Budget observability (techne#357): on exit, if the block consumed at least
    ``warn_ratio`` of its budget WITHOUT timing out, a warning is written to
    stderr naming ``label`` and the elapsed/budget — so a hook creeping toward
    the cliff is visible *before* it silently starts losing work (the #350
    pathology: thread detection sitting at 4.45s under a 5s budget dropped
    detection every turn with zero signal). On an actual timeout, a stronger
    warning records that ``label`` exceeded its budget and work was dropped.
    Both are stderr-only and never change control flow.

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
        raise HookTimeout(f"{label} timed out after {timeout_s:.1f}s")

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    start = time.monotonic()
    timed_out = False
    try:
        yield
    except HookTimeout:
        timed_out = True
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)  # disarm before restoring
        signal.signal(signal.SIGALRM, old_handler)
        elapsed = time.monotonic() - start
        if timed_out:
            print(
                f"techne: {label} exceeded its {timeout_s:.1f}s budget "
                f"(ran ~{elapsed:.1f}s) — work was dropped this run",
                file=sys.stderr,
            )
        elif elapsed >= warn_ratio * timeout_s:
            pct = int(round(100 * elapsed / timeout_s))
            print(
                f"techne: {label} used {elapsed:.1f}s of its {timeout_s:.1f}s "
                f"budget ({pct}%) — approaching timeout",
                file=sys.stderr,
            )
