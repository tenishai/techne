"""A module logger that imports `logging` on first use (issue 1192).

`import logging` costs ~21 ms on Python 3.14 (it pulls in `traceback`, which
pulls in `_colorize`), and every hook process paid it through the seven modules
on the post_tool_use path that create a logger at import time, even on calls
that never log. `get_logger(__name__)` returns a stand-in that resolves the real
`logging.getLogger(name)` the first time any attribute is read, so a process
that never logs never imports `logging`.

It resolves to the same `Logger` object `logging.getLogger(name)` returns, so
handlers, filters, levels and caplog behave as before, and attribute assignment
(`log.propagate = False`) is forwarded to that logger. The one difference:
`null_handler=True` attaches the NullHandler at that first use rather than at
import, which is still before any record is emitted.

This module must stay stdlib-free at import time: importing anything here
would put the cost back.
"""

from __future__ import annotations


class LazyLogger:
    __slots__ = ("_name", "_null_handler", "_logger")

    def __init__(self, name: str, *, null_handler: bool = False) -> None:
        self._name = name
        self._null_handler = null_handler
        self._logger = None

    def _resolve(self):
        if self._logger is None:
            import logging

            logger = logging.getLogger(self._name)
            if self._null_handler and not any(
                isinstance(h, logging.NullHandler) for h in logger.handlers
            ):
                logger.addHandler(logging.NullHandler())
            self._logger = logger
        return self._logger

    def __getattr__(self, attr: str):
        return getattr(self._resolve(), attr)

    def __setattr__(self, attr: str, value) -> None:
        # Own slots are set in __init__; anything else (`log.propagate = False`,
        # `log.disabled = True`) belongs to the real logger, as it would with a
        # plain module logger. Without this, __slots__ makes it an AttributeError.
        if attr in LazyLogger.__slots__:
            object.__setattr__(self, attr, value)
        else:
            setattr(self._resolve(), attr, value)


def get_logger(name: str, *, null_handler: bool = False) -> LazyLogger:
    return LazyLogger(name, null_handler=null_handler)
