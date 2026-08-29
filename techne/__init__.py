"""techne — agentic memory and knowledge system."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Seen only by type checkers / IDEs — never executed at runtime, so the lazy
    # import savings below are preserved while static analysis still resolves the
    # public API and consumers keep autocomplete.
    __version__: str
    from techne import lifecycle
    from techne.config import Config, HookConfig, Platform, SyncConfig, ThreadConfig
    from techne.core import identity, knowledge, search
    from techne.core.database import connect, init_db
    from techne.core.knowledge import VALID_TYPES
    from techne.core.threads import Thread
    from techne.lifecycle import (
        capture_tool_use,
        init_session_for_external_id,
        run_session_end,
        run_session_start,
    )

# Lazy public API (PEP 562). Importing `techne` — or any submodule like
# `techne.config`, which imports the parent package first — must stay cheap.
# Hooks fire a fresh interpreter per tool call and each needs only a slice of
# the API; eagerly importing the lifecycle/search/knowledge stack, structlog,
# and importlib.metadata added ~120ms to every hook process for work most of
# them never touch. Each public name is resolved on first access instead
# (and then cached in the module dict, so __getattr__ runs at most once per
# name). structlog→stderr routing now lives in `techne._log`, imported for its
# side effect by exactly the modules that log — so structlog configures the
# instant it enters a process without being dragged onto structlog-free hot
# paths (e.g. the post_tool_use capture path).

# name -> (module, attr). attr=None exposes the module itself as a namespace.
_LAZY: dict[str, tuple[str, str | None]] = {
    # config
    "Config": ("techne.config", "Config"),
    "HookConfig": ("techne.config", "HookConfig"),
    "Platform": ("techne.config", "Platform"),
    "SyncConfig": ("techne.config", "SyncConfig"),
    "ThreadConfig": ("techne.config", "ThreadConfig"),
    # db
    "connect": ("techne.core.database", "connect"),
    "init_db": ("techne.core.database", "init_db"),
    # knowledge type vocabulary (stable contract for consumers like limenal)
    "VALID_TYPES": ("techne.core.knowledge", "VALID_TYPES"),
    # core types
    "Thread": ("techne.core.threads", "Thread"),
    # lifecycle entry points
    "run_session_start": ("techne.lifecycle", "run_session_start"),
    "run_session_end": ("techne.lifecycle", "run_session_end"),
    "capture_tool_use": ("techne.lifecycle", "capture_tool_use"),
    "init_session_for_external_id": ("techne.lifecycle", "init_session_for_external_id"),
    # subpackages exposed as namespaces
    "identity": ("techne.core.identity", None),
    "knowledge": ("techne.core.knowledge", None),
    "search": ("techne.core.search", None),
    "lifecycle": ("techne.lifecycle", None),
}

__all__ = [
    "__version__",
    # config
    "Config",
    "Platform",
    "ThreadConfig",
    "HookConfig",
    "SyncConfig",
    # lifecycle
    "run_session_start",
    "run_session_end",
    "capture_tool_use",
    "init_session_for_external_id",
    # core types
    "Thread",
    # db
    "init_db",
    "connect",
    # knowledge type vocabulary
    "VALID_TYPES",
    # subpackages exposed as namespaces
    "knowledge",
    "identity",
    "search",
    # lifecycle module (consumers may import techne.lifecycle directly)
    "lifecycle",
]


def _resolve_version() -> str:
    """Resolve the version of the code that is actually IMPORTED.

    ``importlib.metadata`` reads the dist-info written at install time, and for
    an EDITABLE install that stamp never moves when the checkout does — measured
    four days apart on this machine (#568), then again three days after #763 was
    closed (#825), where dist-info said ``0.15.0`` while the checkout ran
    ``0.16.0``. Every surface built on ``__version__`` inherited the lie: the
    SessionStart banner paired a STALE version with a LIVE revision, so it read
    as authoritative while half wrong, and ``backup_all_dbs`` stamped the wrong
    ``techne_version`` into every manifest it wrote.

    For a CHECKOUT the honest source is therefore the checkout's own
    ``pyproject.toml``, never the dist-info beside it. The build test is
    ``"site-packages" not in <package path>``, deliberately the SAME rule as
    ``core/health.py::_revision_info`` — a version string that disagrees with the
    build/revision line printed next to it is precisely the failure #825
    describes, so these two must not be allowed to drift apart.

    Falls back to ``importlib.metadata`` when the file is missing or unparseable,
    and to ``0.0.0+unknown`` when that fails too: an unreadable checkout is a
    reason to report less confidently, not to raise on ``import techne``.

    Cost, MEASURED rather than assumed (2026-08-29, this machine, median of 7
    cold subprocesses each — the hook case, since hooks fire a fresh interpreter
    per tool call): ``importlib.metadata.version()`` 48.8ms, ``tomllib`` parse of
    ``pyproject.toml`` 7.5ms. So the checkout path is ~41ms CHEAPER than what it
    replaces, and the lazy-resolution budget this module is built around is
    preserved rather than spent. The installed path is unchanged.
    """
    from pathlib import Path

    package_dir = Path(__file__).resolve().parent
    if "site-packages" not in str(package_dir):
        try:
            import tomllib

            data = tomllib.loads((package_dir.parent / "pyproject.toml").read_text())
            declared = data["project"]["version"]
            if isinstance(declared, str) and declared:
                return declared
        except (OSError, KeyError, ValueError, ImportError):
            pass  # unreadable checkout — fall through and report what we can

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("techne")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def __getattr__(name: str):
    # __version__ is resolved lazily too — a version read is wasted on the hook
    # processes that never look at it. See _resolve_version for WHICH source is
    # consulted and why it is not always importlib.metadata (#825).
    if name == "__version__":
        value = _resolve_version()
        globals()["__version__"] = value
        return value

    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module_name, attr = target
    module = importlib.import_module(module_name)
    value = module if attr is None else getattr(module, attr)
    globals()[name] = value  # cache so __getattr__ won't fire for this name again
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
