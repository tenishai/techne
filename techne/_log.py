"""structlog → stderr, configured on first use (issue 1218).

``get_logger`` returns a stand-in that imports structlog, routes it to STDERR and
resolves the real logger the first time any attribute is read (``log.info``,
``log.bind``...). Unconfigured, structlog prints to stdout, which would corrupt the
MCP server's stdio JSON-RPC stream, a hook's stdout protocol response, or any CLI
``--json`` output (techne#107); configuring before the first call keeps that
guarantee, because nothing can be emitted before an attribute is read.

Why lazily: structlog drags in asyncio, ~80 ms cumulative on its own under
``python -X importtime`` (Python 3.14, workstation). Before this, the import ran when a
logging module was imported, so the Stop hook paid it on every turn through
``techne.core.threads`` -> ``thread_orchestrator`` even on the gate path that
never logs: importing ``techne.core.threads`` there went from ~55 ms to ~6 ms.
Same shape as ``techne._lazylog`` (issue 1192), which does this for stdlib ``logging``.

Why this lives here and not in ``techne/__init__.py``: importing the top-level
package must stay cheap (see the PEP 562 note there).

The ``is_configured()`` guard means a consumer (e.g. limenal) that configured
structlog before techne first logs keeps its own setup.

This module must stay free of structlog at import time.
"""

from __future__ import annotations


def _configure():
    import sys

    import structlog

    if not structlog.is_configured():
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    return structlog


class LazyStructLogger:
    __slots__ = ("_args", "_kwargs", "_logger")

    def __init__(self, args: tuple, kwargs: dict) -> None:
        self._args = args
        self._kwargs = kwargs
        self._logger = None

    def _resolve(self):
        if self._logger is None:
            self._logger = _configure().get_logger(*self._args, **self._kwargs)
        return self._logger

    def __getattr__(self, attr: str):
        return getattr(self._resolve(), attr)

    def __setattr__(self, attr: str, value) -> None:
        if attr in LazyStructLogger.__slots__:
            object.__setattr__(self, attr, value)
        else:
            setattr(self._resolve(), attr, value)


def get_logger(*args, **kwargs) -> LazyStructLogger:
    """``structlog.get_logger``, imported and routed to stderr on first use."""
    return LazyStructLogger(args, kwargs)
