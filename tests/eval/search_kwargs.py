"""Which optional search() kwargs a gold task sets, and whether a search() accepts them.

techne#1086. CI's retrieval diff (#832) runs HEAD's tests/ against origin/main's
search(). When a PR adds a search() kwarg and a task uses it, main's search()
raises TypeError at call time, the base leg writes no metrics, and the diff step
fails for exactly the PRs that change the search interface.

So the harness passes an optional kwarg only when a task sets it, and on the base
leg a task whose kwargs the base search() does not accept is skipped and named
("new in HEAD, no base") instead of killing the run. On any other run an
unsupported kwarg is an error: a task field the code under test cannot honour is a
broken task, and skipping it there would shrink the gate silently.
"""

from __future__ import annotations

import inspect
from typing import Callable

#: Task fields forwarded to search() as keyword arguments, only when the task sets
#: them. Add a name here when a task needs a new search() kwarg.
OPTIONAL_SEARCH_KWARGS: tuple[str, ...] = ("identifier_head_slots", "phrase_seat")

#: Set to a non-empty value by CI's base leg (#832), and only there.
BASE_LEG_ENV = "TECHNE_EVAL_BASE_LEG"


def task_search_kwargs(task: dict) -> dict:
    """The optional search() kwargs this task sets, and no others."""
    return {k: task[k] for k in OPTIONAL_SEARCH_KWARGS if k in task}


def unsupported_kwargs(fn: Callable, kwargs: dict) -> list[str]:
    """Names in `kwargs` that `fn` cannot accept, sorted. Empty if it takes **kwargs."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return []
    return sorted(
        k
        for k in kwargs
        if k not in params
        or params[k].kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL)
    )
