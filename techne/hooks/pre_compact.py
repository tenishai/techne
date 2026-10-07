#!/usr/bin/env python3
"""PreCompact hook — checkpoints current state before context compaction."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from techne.config import Config
from techne.core.state import save_checkpoint

#: Liveness key — must match the entry in ``checks._HOOK_EXPECTATIONS``, since
#: that is what turns a written marker into a reported verdict (techne#479).
HOOK_NAME = "pre_compact"


def main():
    # LIVENESS (techne#479). Every exit below records WHAT THIS RUN DID, because
    # a checkpoint rail that silently stops is indistinguishable from an operator
    # who simply never compacted. `resolve_db_path` is used for the exits that
    # happen before (or instead of) a successful config load — the marker must
    # survive the paths where the config itself is the thing that failed.
    from techne.hooks._liveness import record_hook_run, resolve_db_path

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"techne: pre_compact received invalid JSON on stdin — checkpoint skipped: {exc}", file=sys.stderr)
        data = {}

    session_id = data.get("session_id", "")
    trigger = data.get("trigger", "unknown")
    transcript_path = data.get("transcript_path", "")

    if not session_id:
        # NOT a failure: a compaction with no session id is nothing to check
        # point against. Recorded so it reads as "ran, had nothing to do".
        db = resolve_db_path()
        if db:
            record_hook_run(db, HOOK_NAME, outcome="noop:no-session-id")
        sys.exit(0)

    from techne.hooks._timeout import HookTimeout, bounded_hook

    # Config.load() can raise (corrupt/unreadable config). A hook that throws here
    # would break the host's compaction; degrade to a no-op. (techne#166)
    try:
        config = Config.load()
    except Exception as e:
        print(f"techne: config load failed, skipping checkpoint: {e}", file=sys.stderr)
        db = resolve_db_path()
        if db:
            record_hook_run(
                db,
                HOOK_NAME,
                outcome="failed",
                error={"stage": "config_load", "msg": str(e)[:200]},
            )
        sys.exit(0)

    try:
        with bounded_hook(config.hooks.timeout_s, label="pre_compact"):
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

            # Thread-summary checkpoint (techne#1187): compaction is where a long
            # session most often dies, so send every thread with new work now.
            # Its own try: a sync failure must not mark the checkpoint failed.
            try:
                from techne.core.state import observation_count
                from techne.hooks.session_end import MIN_NEW_OBSERVATIONS
                from techne.lifecycle import MODE_COMPACT, _has_live_consumer, run_session_end

                if _has_live_consumer(config) and (
                    observation_count(config.db_path, session_id) >= MIN_NEW_OBSERVATIONS
                ):
                    run_session_end(config, session_id, mode=MODE_COMPACT)
            except HookTimeout:
                raise  # out of budget: let the outer handler record it
            except Exception as e:  # noqa: BLE001 — never fail the checkpoint over sync
                print(f"techne: thread-summary checkpoint deferred: {e}", file=sys.stderr)
        record_hook_run(config.db_path, HOOK_NAME, outcome="ok", detail=f"trigger={trigger}")
    except Exception as e:
        # INSIDE the bounded_hook's except, so this also catches HookTimeout —
        # a checkpoint that ran out of budget must read BROKEN, not silent.
        print(f"techne: pre_compact failed: {e}", file=sys.stderr)
        record_hook_run(
            config.db_path,
            HOOK_NAME,
            outcome="failed",
            error={"stage": "checkpoint", "msg": str(e)[:200]},
        )
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
