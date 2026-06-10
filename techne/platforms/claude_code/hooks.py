"""Claude Code platform adapter — hook registration and MCP setup."""

import json
import shlex
import sys
from pathlib import Path

from techne.config import Config

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
MCP_CONFIG_PATH = Path.home() / ".claude.json"


def _hook_command(module: str) -> str:
    # Pin to the interpreter that techne is installed in, not whatever `python`
    # happens to resolve to on the user's PATH (which may be missing, or may
    # point at a system python without techne installed).
    return f"{shlex.quote(sys.executable)} -m {module}"


def _group_contains_techne(group: dict) -> bool:
    """True if a settings.json hook-group entry references techne anywhere.

    Handles both the wrapped shape ``{matcher, hooks: [{type, command}, ...]}``
    and the legacy flat shape ``{type, command}`` that earlier techne versions
    (pre-0.4.1) wrote — we still need to recognize and replace those so a
    re-init cleans up an old install."""
    if "command" in group and "techne" in (group.get("command") or ""):
        return True
    for h in group.get("hooks") or []:
        if "techne" in (h.get("command") or ""):
            return True
    return False


def register_hooks(config: Config):
    """Register techne hooks in Claude Code settings.json.

    Writes the current Claude Code hook shape:
    ``{<Event>: [{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}]}``.
    Empty matcher means "every tool / every fire of this event".
    """
    settings = _load_json(SETTINGS_PATH)
    if settings is None:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    commands_per_event = {
        "SessionStart": _hook_command("techne.hooks.session_start"),
        "PreCompact": _hook_command("techne.hooks.pre_compact"),
        "PostCompact": _hook_command("techne.hooks.post_compact"),
        "Stop": _hook_command("techne.hooks.session_end"),
    }
    if config.hooks.post_tool_use:
        commands_per_event["PostToolUse"] = _hook_command("techne.hooks.post_tool_use")

    for event, command in commands_per_event.items():
        groups = hooks.setdefault(event, [])
        # Strip any prior techne entry, in either the wrapped or legacy shape.
        groups[:] = [g for g in groups if not _group_contains_techne(g)]
        groups.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": command}],
        })

    _save_json(SETTINGS_PATH, settings)


def register_mcp(config: Config):
    """Register the techne MCP server in Claude's MCP config."""
    mcp_config = _load_json(MCP_CONFIG_PATH)
    if mcp_config is None:
        mcp_config = {}

    servers = mcp_config.setdefault("mcpServers", {})
    servers["techne"] = {
        "command": sys.executable,
        "args": ["-m", "techne.mcp_server"],
        "env": {},
    }

    _save_json(MCP_CONFIG_PATH, mcp_config)


def unregister(config: Config):
    """Remove techne hooks and MCP registration."""
    # Remove hooks (handle both wrapped and legacy shapes)
    settings = _load_json(SETTINGS_PATH)
    if settings:
        hooks = settings.get("hooks", {})
        for event in list(hooks.keys()):
            hooks[event] = [g for g in hooks[event] if not _group_contains_techne(g)]
            if not hooks[event]:
                del hooks[event]
        _save_json(SETTINGS_PATH, settings)

    # Remove MCP server
    mcp_config = _load_json(MCP_CONFIG_PATH)
    if mcp_config:
        servers = mcp_config.get("mcpServers", {})
        servers.pop("techne", None)
        _save_json(MCP_CONFIG_PATH, mcp_config)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
