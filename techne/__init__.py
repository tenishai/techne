"""techne — agentic memory and knowledge system."""

from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PackageNotFoundError

try:
    __version__ = _pkg_version("techne")
except _PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Public API. Stable for library consumers.

# Configuration
from techne.config import Config, Platform, ThreadConfig, HookConfig, SyncConfig

# Lifecycle (the highest-level library entry points)
from techne.lifecycle import (
    run_session_start,
    run_session_end,
    capture_tool_use,
)

# Core types
from techne.core.threads import Thread

# Database initialization
from techne.core.database import init_db, connect

# Knowledge store (namespaced — call as techne.knowledge.store(...))
from techne.core import knowledge
from techne.core import identity
from techne.core import search

__all__ = [
    "__version__",
    # config
    "Config", "Platform", "ThreadConfig", "HookConfig", "SyncConfig",
    # lifecycle
    "run_session_start", "run_session_end", "capture_tool_use",
    # core types
    "Thread",
    # db
    "init_db", "connect",
    # subpackages exposed as namespaces
    "knowledge", "identity", "search",
]
