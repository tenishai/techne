"""Claude Code platform adapter — hook registration and MCP setup."""

import json
import os
import shlex
import sys
from pathlib import Path

from techne.config import Config
from techne.platforms.claude_code.paths import claude_config_dir, claude_json_path

# Import-time snapshots, honoring CLAUDE_CONFIG_DIR set before launch. The hook
# installer runs as a fresh `techne` process per invocation, so the env is fixed
# by then; tests monkeypatch these module attributes directly.
SETTINGS_PATH = claude_config_dir() / "settings.json"
MCP_CONFIG_PATH = claude_json_path()

# The standalone editable-install guard, shipped alongside this adapter. Wired as
# the first SessionStart hook; see preflight_guard.py for why it must be invoked
# by absolute path (it heals the very pointer that `-m techne...` depends on).
PREFLIGHT_GUARD = Path(__file__).resolve().parent / "preflight_guard.py"


def _install_root() -> Path:
    """Repo root of the running techne install — the editable pointer's target.

    ``<repo>/techne/platforms/claude_code/hooks.py`` → ``parents[3]`` is ``<repo>``.
    """
    return Path(__file__).resolve().parents[3]


def _is_worktree_path(p: Path) -> bool:
    """True if ``p`` sits under a ``.claude/worktrees/`` directory."""
    parts = p.resolve().parts
    return any(
        parts[i] == ".claude" and parts[i + 1] == "worktrees"
        for i in range(len(parts) - 1)
    )


def _preflight_command() -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(PREFLIGHT_GUARD))}"


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
    # Refuse to bake a worktree path into the config. If techne is installed from
    # a git worktree, its editable pointer — and everything we're about to wire —
    # will break the moment that worktree is pruned (the -32000 footgun). Fail
    # loud with the remediation rather than register a self-destructing install.
    root = _install_root()
    if _is_worktree_path(root):
        raise RuntimeError(
            f"techne is installed from a git worktree ({root}). That editable "
            "pointer will break when the worktree is pruned, taking the MCP server "
            "and all techne hooks down with it. Reinstall from your canonical "
            "checkout, then re-run `techne init`:\n"
            f"  {sys.executable} -m pip install -e <canonical-techne-checkout>"
        )

    settings = _load_json(SETTINGS_PATH)
    if settings is None:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # SessionStart chains the preflight guard and session_start into ONE shell
    # command joined by `;`. This is load-bearing: Claude Code runs all hooks
    # matching an event IN PARALLEL with no ordering guarantee — even multiple
    # commands inside a single group's `hooks` array
    # (https://code.claude.com/docs/en/hooks-guide). So two separate hooks would
    # RACE: session_start's `import techne` could fire before the guard heals the
    # pointer. Sequencing them in one shell process is the only way to guarantee
    # the guard runs to completion first; session_start then starts as a fresh
    # process and reads the just-healed `.pth` at its own interpreter startup.
    # `;` (not `&&`) so a guard crash can never suppress session_start — a broken
    # guard must not regress the normal hook when the pointer was fine anyway.
    session_start = (
        f"{_preflight_command()} ; {_hook_command('techne.hooks.session_start')}"
    )
    commands_per_event = {
        "SessionStart": session_start,
        "PreCompact": _hook_command("techne.hooks.pre_compact"),
        "PostCompact": _hook_command("techne.hooks.post_compact"),
        "Stop": _hook_command("techne.hooks.session_end"),
    }
    if config.hooks.post_tool_use:
        commands_per_event["PostToolUse"] = _hook_command("techne.hooks.post_tool_use")

    for event, command in commands_per_event.items():
        groups = hooks.setdefault(event, [])
        # Strip any prior techne entry, in either the wrapped or legacy shape.
        # This also migrates a hand-wired preflight hook: its command contains
        # "techne", so it's recognized, removed, and replaced by the packaged one.
        groups[:] = [g for g in groups if not _group_contains_techne(g)]
        groups.append(
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": command}],
            }
        )

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
    # Remove hooks (handle both wrapped and legacy shapes). Uninstall must be
    # resilient: a corrupt settings.json shouldn't strand the user — _load_json
    # raises on it (correct for register, which must not clobber), but here we
    # warn and press on so the MCP-config cleanup below still runs (techne#166).
    try:
        settings = _load_json(SETTINGS_PATH)
    except RuntimeError as e:
        print(f"techne: skipping hook cleanup: {e}", file=sys.stderr)
        settings = None
    if settings:
        hooks = settings.get("hooks", {})
        for event in list(hooks.keys()):
            hooks[event] = [g for g in hooks[event] if not _group_contains_techne(g)]
            if not hooks[event]:
                del hooks[event]
        _save_json(SETTINGS_PATH, settings)

    # Remove MCP server
    try:
        mcp_config = _load_json(MCP_CONFIG_PATH)
    except RuntimeError as e:
        print(f"techne: skipping MCP cleanup: {e}", file=sys.stderr)
        mcp_config = None
    if mcp_config:
        servers = mcp_config.get("mcpServers", {})
        servers.pop("techne", None)
        _save_json(MCP_CONFIG_PATH, mcp_config)


def _load_json(path: Path) -> dict | None:
    """Load a JSON config. Returns None ONLY when the file genuinely does not exist
    (caller starts fresh). An EXISTING-but-unparseable file RAISES — returning None
    there would make register_hooks treat it as empty and OVERWRITE the user's real
    Claude Code settings with just ours (techne#166). Fail loud, lose nothing."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"{path} exists but could not be read/parsed ({e}); refusing to overwrite it. "
            "Fix or remove the file, then re-run."
        ) from e


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must never leave the file truncated/empty — the
    # next _load_json would then (now) raise, or pre-#166 silently wipe the user's
    # settings. Write a sibling temp then os.replace (POSIX-atomic rename). The pid
    # suffix keeps two racing writers (e.g. concurrent `techne init`) from sharing
    # one temp and interleaving bytes.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
