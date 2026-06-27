"""techne — agentic memory and knowledge system."""

import sys as _sys
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _pkg_version

import structlog as _structlog

try:
    __version__ = _pkg_version("techne")
except _PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Public API. Stable for library consumers.

# Configuration
from techne import lifecycle
from techne.config import Config, HookConfig, Platform, SyncConfig, ThreadConfig

# Knowledge store (namespaced — call as techne.knowledge.store(...))
from techne.core import identity, knowledge, search

# Database initialization
from techne.core.database import connect, init_db

# Knowledge type vocabulary (stable contract for consumers like limenal)
from techne.core.knowledge import VALID_TYPES

# Core types
from techne.core.threads import Thread

# Lifecycle (the highest-level library entry points)
from techne.lifecycle import (
    capture_tool_use,
    init_session_for_external_id,
    run_session_end,
    run_session_start,
)

# Route structlog to STDERR (configure-once, after imports so E402 stays clean).
# Unconfigured, structlog prints to stdout — which would corrupt the MCP server's stdout
# JSON-RPC stream and any CLI `--json` output (techne#107). Logs belong on stderr.
_structlog.configure(logger_factory=_structlog.PrintLoggerFactory(file=_sys.stderr))

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
