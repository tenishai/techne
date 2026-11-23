"""Load the sqlite-vec extension without importing the ``sqlite_vec`` package (issue 1217).

``import sqlite_vec`` runs the package's ``__init__``, which does
``import numpy.typing`` to offer ``register_numpy``. techne never calls that, and
on the per-turn hook path the import cost ~55 ms (``-X importtime``, workstation), all
of it numpy. The extension itself is a shared library next to that ``__init__``:
``sqlite_vec.load(conn)`` is ``conn.load_extension(<package dir>/vec0)``. This
module finds the package with ``importlib.util.find_spec`` and loads the same
file, so nothing is executed in Python.

IF THE PACKAGE IS ALREADY IMPORTED, ITS OWN ``load`` IS USED. A process that has
imported it has paid for numpy already, and tests stub or wrap
``sys.modules["sqlite_vec"]`` (a ``None`` entry is the usual "not installed").

Only the query path uses this (``search._query_embedding``/``search_vec``).
Backfill and ``vector_status`` still import the package: they embed with numpy
anyway, so there is nothing to save there.
"""

from __future__ import annotations

import sqlite3
import sys

_MODULE = "sqlite_vec"


def _spec_origin() -> str | None:
    """The package's ``__init__`` path, or None when it is not installed."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(_MODULE)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return spec.origin


def available() -> bool:
    """Is sqlite-vec installed? Answers without importing it."""
    if _MODULE in sys.modules:
        return sys.modules[_MODULE] is not None
    return _spec_origin() is not None


def load(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into ``conn`` (extension loading must already be enabled).

    Raises ImportError when the package is not installed, and whatever
    ``load_extension`` raises (``sqlite3.OperationalError`` for a missing or
    unloadable library) otherwise, the same errors ``sqlite_vec.load`` gives.
    """
    if _MODULE in sys.modules:
        mod = sys.modules[_MODULE]
        if mod is None:
            raise ImportError(f"{_MODULE} is blocked in sys.modules")
        mod.load(conn)
        return
    origin = _spec_origin()
    if origin is None:
        raise ImportError(f"{_MODULE} is not installed")
    import os.path

    # Same path sqlite_vec.loadable_path() builds (sqlite-vec 0.1.x).
    conn.load_extension(os.path.normpath(os.path.join(os.path.dirname(origin), "vec0")))
