"""Exit codes that carry a contract across the process boundary.

A supervisor (a systemd unit, a container restart policy) decides whether to
respawn using the exit status *alone*. It has no way to tell "the disk went
away for a second" from "this install is missing a dependency" unless the
process says so.

A configuration error never becomes transient. Retrying it produces an
infinite loop whose journal fills with a traceback that reads like a runtime
fault, while the actual cause is a one-line install fix. `sysexits.h` reserves
78 for exactly that class, so we use it rather than inventing a private code.

THE CONTRACT, and both halves are required:

  * the entrypoint exits ``EX_CONFIG`` when a startup precondition cannot be
    satisfied by trying again, and
  * the unit supervising it sets ``RestartPreventExitStatus=78``.

Either half alone does nothing. `deploy/techne-watch.service` is the worked
example; any future supervised entrypoint (MCP server, daemons) should pair
the same two lines rather than growing a private convention.
"""

from __future__ import annotations

#: sysexits.h EX_CONFIG. A supervisor must NOT respawn on this status.
EX_CONFIG = 78


class StartupConfigError(RuntimeError):
    """A startup precondition that restarting cannot possibly satisfy.

    Missing optional extras, an unreadable config, a required binary that is
    not installed — anything where the next attempt fails identically.

    Subclasses ``RuntimeError`` deliberately: existing ``except RuntimeError``
    handlers keep catching it and keep their current behaviour. Callers that
    own a process-exit boundary catch this *first* and exit ``EX_CONFIG``, so
    the distinction is opt-in at the seam that can act on it.
    """
