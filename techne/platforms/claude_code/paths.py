"""Resolution of host Claude Code config paths, honoring CLAUDE_CONFIG_DIR.

Claude Code relocates its whole config root when CLAUDE_CONFIG_DIR is set —
both the ``~/.claude/`` directory and the ``~/.claude.json`` file (the latter
moves to ``${CLAUDE_CONFIG_DIR}.json``, a sibling of the dir). techne reads
these host files for transcript import, hook installation, and the native-memory
probe; honoring the env var lets a second agent on one OS user read its own
state instead of the default agent's (techne#113).
"""

import os
from pathlib import Path


def claude_config_dir() -> Path:
    """The Claude Code config directory (``~/.claude`` unless relocated)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def claude_json_path() -> Path:
    """The ``~/.claude.json`` equivalent. When CLAUDE_CONFIG_DIR is set, Claude
    Code stores it at ``${CLAUDE_CONFIG_DIR}.json`` (sibling of the dir)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return Path.home() / ".claude.json"
    p = Path(env).expanduser()
    return p.with_name(p.name + ".json")


def claude_projects_dir() -> Path:
    """The transcripts/projects dir under the (possibly relocated) config dir."""
    return claude_config_dir() / "projects"
