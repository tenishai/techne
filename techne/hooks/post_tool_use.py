#!/usr/bin/env python3
"""PostToolUse hook — captures tool observations to the database."""

import json
import sys

from techne.config import Config

SKIP_TOOLS = {
    "AskUserQuestion",
}


def should_capture(config: Config, tool_name: str) -> bool:
    if not config.hooks.post_tool_use:
        return False
    if tool_name == "Bash" and not config.hooks.capture_bash:
        return False
    if tool_name == "Read" and not config.hooks.capture_read:
        return False
    if tool_name in SKIP_TOOLS:
        return False
    if tool_name.startswith("mcp__memory__"):
        return False
    return True


def summarize(tool_name: str, tool_input: dict, tool_output) -> str:
    if isinstance(tool_output, dict):
        tool_output = json.dumps(tool_output, default=str)
    elif not isinstance(tool_output, str):
        tool_output = str(tool_output) if tool_output else ""
    output_preview = tool_output[:2000]

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"Ran: {cmd}\nOutput: {output_preview}"
    elif tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")[:100]
        new = tool_input.get("new_string", "")[:100]
        return f"Edited: {fp}\nChange: {old} → {new}"
    elif tool_name == "Write":
        fp = tool_input.get("file_path", "")
        content = tool_input.get("content", "")[:500]
        return f"Wrote: {fp}\nContent: {content}..."
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"Searched for: {pattern}\nFound: {output_preview}"
    elif tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"Searched web: {query}\nResults: {output_preview}"
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return f"Fetched: {url}\nContent: {output_preview}"
    else:
        input_summary = json.dumps(tool_input)[:500]
        return f"Tool: {tool_name}\nInput: {input_summary}\nOutput: {output_preview}"


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = data.get("session_id", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_output = data.get("tool_response", "")

    if not session_id or not tool_name:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    # Config.load() can raise (corrupt/unreadable config, bad TOML, perms). A hook
    # that throws here would surface as a tool-call failure in the user's session;
    # a memory hook must NEVER break the host. Degrade to a no-op. (techne#166)
    try:
        config = Config.load()
    except Exception as e:
        print(f"techne: config load failed, skipping capture: {e}", file=sys.stderr)
        sys.exit(0)

    try:
        with bounded_hook(config.hooks.timeout_s):
            from techne.lifecycle import capture_tool_use

            capture_tool_use(config, session_id, tool_name, tool_input, tool_output)
    except Exception as e:
        print(f"techne: capture failed: {e}", file=sys.stderr)
        sys.exit(0)  # don't fail the tool call


if __name__ == "__main__":
    main()
