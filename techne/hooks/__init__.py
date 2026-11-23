"""Claude Code hook entry points (``python -m techne.hooks.<name>``).

``_T0`` is the hook's start time as this package sees it: importing any hook
module imports this package first, so it is stamped before the hook's own
imports run. ``_liveness.record_hook_run`` subtracts it to record how long the
run took (issue 1217). It misses the interpreter's own boot (~75 ms on a workstation),
which ``cpu_ms`` (``time.process_time()``, counted from process start) covers.
"""

import time

_T0 = time.monotonic()
