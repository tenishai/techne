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

#: `techne backup --keep N`: THE BACKUP WAS WRITTEN AND RETENTION WAS NOT DONE.
#:
#: Lives here rather than as a bare ``3`` at the call site, because this module's whole
#: point is that a private convention scattered across call sites is not a contract
#: (techne#742, Prudence on #755). A scheduled backup is a supervised entrypoint, which is
#: exactly the class this file exists for.
#:
#: NOT a sysexits.h code, and the deviation is deliberate rather than an oversight of the
#: rule stated above. sysexits has no code for PARTIAL SUCCESS — its vocabulary is kinds of
#: *failure* (EX_IOERR, EX_CANTCREAT, EX_TEMPFAIL), and every one of them would tell a
#: monitor the backup did not happen. It did. What a timer must be able to distinguish is
#: "no backup tonight" from "backup fine, housekeeping broken", and nothing in sysexits
#: expresses the second.
#:
#: Why it matters at all: `systemctl --user status` and `launchctl list` surface exit status
#: and NOTHING else. Both units this project ships route stderr to the journal or a log file
#: that nobody reads until something is already wrong, so at exit 0 a permanently failing
#: prune reads green forever while the disk fills — the outcome `--keep` exists to prevent.
#:
#: A unit SHOULD let this mark the job failed; that is the signal. Do not add
#: `SuccessExitStatus=3` to the shipped units, which would restore the silence.
EX_RETENTION_INCOMPLETE = 3


class StartupConfigError(RuntimeError):
    """A startup precondition that restarting cannot possibly satisfy.

    Missing optional extras, an unreadable config, a required binary that is
    not installed — anything where the next attempt fails identically.

    Subclasses ``RuntimeError`` deliberately: existing ``except RuntimeError``
    handlers keep catching it and keep their current behaviour. Callers that
    own a process-exit boundary catch this *first* and exit ``EX_CONFIG``, so
    the distinction is opt-in at the seam that can act on it.
    """
