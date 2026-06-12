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
    init_session_for_external_id,
)

# Core types
from techne.core.threads import Thread

# Database initialization
from techne.core.database import init_db, connect

# Knowledge type vocabulary (stable contract for consumers like limenal)
from techne.core.knowledge import VALID_TYPES

# Knowledge store (namespaced — call as techne.knowledge.store(...))
from techne.core import knowledge
from techne.core import identity
from techne.core import search
from techne import lifecycle

__all__ = [
    "__version__",
    # config
    "Config", "Platform", "ThreadConfig", "HookConfig", "SyncConfig",
    # lifecycle
    "run_session_start", "run_session_end", "capture_tool_use",
    "init_session_for_external_id",
    # core types
    "Thread",
    # db
    "init_db", "connect",
    # knowledge type vocabulary
    "VALID_TYPES",
    # subpackages exposed as namespaces
    "knowledge", "identity", "search",
    # lifecycle module (consumers may import techne.lifecycle directly)
    "lifecycle",
]
