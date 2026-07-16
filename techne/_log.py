"""structlog → stderr, configured on import.

Imported for its side effect by exactly the modules that log. This routes
structlog to STDERR (unconfigured, structlog prints to stdout — which would
corrupt the MCP server's stdio JSON-RPC stream, a hook's stdout protocol
response, or any CLI ``--json`` output; techne#107).

Why this lives here and not in ``techne/__init__.py``: importing the top-level
package must stay cheap. Hooks spawn a fresh interpreter per tool call and the
hot ones (e.g. ``post_tool_use``'s capture path) touch no logging module at
all, so they should never pay structlog's import cost (~60ms, and it drags in
asyncio). By configuring here, structlog is set up the instant it actually
enters a process — the first ``get_logger`` module import — and stays absent
from structlog-free paths.

The ``is_configured()`` guard means a consumer (e.g. limenal) that configured
structlog before importing techne keeps its own setup.
"""

from __future__ import annotations

import sys

import structlog

if not structlog.is_configured():
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def get_logger(*args, **kwargs):
    """``structlog.get_logger``, with stderr routing guaranteed by import."""
    return structlog.get_logger(*args, **kwargs)
