#!/usr/bin/env python3
"""PreCompact hook — checkpoints current state before context compaction."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from techne.config import Config
from techne.core.state import save_checkpoint


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "")
    trigger = data.get("trigger", "unknown")
    transcript_path = data.get("transcript_path", "")

    if not session_id:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    config = Config.load()

    try:
        with bounded_hook(config.hooks.timeout_s):
            # Parse transcript for state
            transcript_data: dict = {}
            if transcript_path:
                transcript_path = os.path.expanduser(transcript_path)
                if Path(transcript_path).exists():
                    transcript_data = _parse_transcript(transcript_path)

            # Build checkpoint
            checkpoint = {
                "trigger": trigger,
                "session_id": session_id,
                "files_modified": list(transcript_data.get("files_modified", set())),
                "tools_used": dict(transcript_data.get("tools_used", {})),
                "recent_commands": transcript_data.get("bash_commands", [])[-5:],
                "last_task": _find_last_task(transcript_data.get("user_messages", [])),
                "git_branch": transcript_data.get("git_branch", ""),
            }

            save_checkpoint(config.db_path, session_id, trigger, checkpoint)
            print(f"techne: checkpoint saved ({trigger})", file=sys.stderr)
    except Exception as e:
        print(f"techne: pre_compact failed: {e}", file=sys.stderr)
        sys.exit(0)


def _parse_transcript(transcript_path: str, max_lines: int = 500) -> dict:
    result = {
        "user_messages": [],
        "files_modified": set(),
        "tools_used": defaultdict(int),
        "bash_commands": [],
        "git_branch": "",
    }

    try:
        # Transcripts are JSONL written by Claude Code; always UTF-8. The
        # rest of the codebase pins encoding="utf-8" — match it so this
        # hook doesn't break on a host whose default encoding isn't UTF-8.
        lines = Path(transcript_path).read_text(encoding="utf-8").strip().split("\n")
        for line in lines[-max_lines:]:
            try:
                d = json.loads(line)
                t = d.get("type")

                if d.get("gitBranch"):
                    result["git_branch"] = d["gitBranch"]

                if t == "user":
                    content = d.get("message", {}).get("content", [])
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "").strip()
                            if text and not text.startswith("<"):
                                result["user_messages"].append(text[:200])

                elif t == "assistant":
                    content = d.get("message", {}).get("content", [])
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            name = c.get("name", "")
                            inp = c.get("input", {})
                            result["tools_used"][name] += 1

                            if name in ("Write", "Edit"):
                                fp = inp.get("file_path", "")
                                if fp:
                                    result["files_modified"].add(fp)

                            elif name == "Bash":
                                cmd = inp.get("command", "")[:100]
                                if cmd:
                                    result["bash_commands"].append(cmd)

            except json.JSONDecodeError:
                continue
    except Exception as e:
        # Don't raise — caller depends on the empty default for a graceful
        # checkpoint — but surface the failure rather than silently writing
        # an empty checkpoint as if nothing happened.
        print(f"techne: pre_compact transcript parse failed: {e}", file=sys.stderr)

    return result


def _find_last_task(messages: list) -> str:
    for msg in reversed(messages):
        if len(msg) < 10:
            continue
        if msg.lower() in ("yes", "no", "ok", "okay", "thanks", "thank you"):
            continue
        return msg
    return ""


if __name__ == "__main__":
    main()
