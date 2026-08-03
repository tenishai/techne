"""Claude Code platform adapter.

Public surface is re-exported here so call sites can keep
`from techne.platforms.claude_code import register_hooks, register_mcp`.
"""

from techne.platforms.claude_code.hooks import (
    MCP_CONFIG_PATH,
    SETTINGS_PATH,
    _group_contains_techne,
    check_hook_drift,
    register_hooks,
    register_mcp,
    settings_unusable_reason,
    unregister,
)
from techne.platforms.claude_code.transcript_import import (
    default_transcripts_dir,
    import_directory,
    import_transcript,
    project_slug_for_cwd,
)

__all__ = [
    "MCP_CONFIG_PATH",
    "SETTINGS_PATH",
    "_group_contains_techne",
    "check_hook_drift",
    "settings_unusable_reason",
    "register_hooks",
    "register_mcp",
    "unregister",
    "import_transcript",
    "import_directory",
    "default_transcripts_dir",
    "project_slug_for_cwd",
]
