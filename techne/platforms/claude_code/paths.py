"""Resolution of host Claude Code config paths, honoring CLAUDE_CONFIG_DIR.

CLAUDE_CONFIG_DIR is **believed** to relocate Claude Code's whole config root —
both the ``~/.claude/`` directory and the ``~/.claude.json`` file, the latter to
``${CLAUDE_CONFIG_DIR}.json`` (a sibling of the dir). This is OBSERVED behavior,
not officially documented (tracked in claude-code GitHub issues #33430 / #25762),
so treat the sibling-``.json`` rule as load-bearing-but-unverified-in-tree: the
MCP register/unregister path in ``hooks.py`` depends on it. Worth confirming
against a live relocated install before relying on it heavily.

techne reads these host files for transcript import, hook installation, and the
native-memory probe; honoring the env var lets a second agent on one OS user
read its own state instead of the default agent's (techne#113).
"""

import os
from pathlib import Path


def claude_config_dir() -> Path:
    """The Claude Code config directory (``~/.claude`` unless relocated)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def claude_json_path() -> Path:
    """The ``~/.claude.json`` equivalent. When CLAUDE_CONFIG_DIR is set, Claude
    Code is believed to store it at ``${CLAUDE_CONFIG_DIR}.json`` (sibling of the
    dir) — see module docstring on why this is observed-but-undocumented."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return Path.home() / ".claude.json"
    # with_name appends '.json' to the final path component, so a trailing slash
    # (normalized away by pathlib) and '~' (expanded above) are both handled.
    p = Path(env).expanduser()
    return p.with_name(p.name + ".json")


def claude_projects_dir() -> Path:
    """The transcripts/projects dir under the (possibly relocated) config dir."""
    return claude_config_dir() / "projects"
