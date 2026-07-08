#!/usr/bin/env python3
"""PostToolUse hook — captures tool observations to the database.

Extended by techne#207 to surface due reminders via hookSpecificOutput.additionalContext.

Throttle: a reminder that was surfaced within the last REMINDER_THROTTLE_SECONDS
(~3h) is skipped in subsequent tool-call invocations so the agent isn't flooded.
Per-session guarantee: each reminder surfaces at least once per session. A
per-session surfaced-set (extension_state key "reminders:surfaced:<session_id>")
tracks which reminder ids this session has already seen, keeping the guarantee
concurrency-safe across simultaneous sessions.
"""

import json
import sys
import time
from datetime import datetime, timezone

from techne.config import Config

# Throttle window: skip a due reminder whose last_surfaced_at is within this many
# seconds. ~3h keeps the signal fresh without repeating on every tool call. (#207)
REMINDER_THROTTLE_SECONDS = 3 * 60 * 60

# Per-session surfaced-state keys older than this are pruned to keep extension_state
# bounded. 7 days >> longest realistic session gap.
_SESSION_STATE_TTL_SECONDS = 7 * 24 * 60 * 60

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


def _inject_due_reminders(config: Config, session_id: str) -> str | None:
    """Return an additionalContext string for any due, un-throttled reminders.

    Hot-path design (techne#207):
    - Single indexed SELECT on (status, due_at) — no heavy work.
    - Throttle filter in Python after the fetch (avoids a subquery on last_surfaced_at,
      keeping the WHERE clause index-only on the covering index).
    - Fail-safe: any exception returns None so the hook always exits normally.

    Per-session tracking (concurrency-safe, techne#207 fix):
    Each session independently tracks which reminder ids it has already surfaced,
    stored in extension_state under "reminders:surfaced:<session_id>" as
    {"ids": [<int>, ...]}.

    A due reminder is surfaced when:
      (a) its id is NOT in this session's surfaced-set (first time in this session), OR
      (b) its last_surfaced_at is older than REMINDER_THROTTLE_SECONDS (throttle expired).
    It is skipped when (a) its id IS in the set AND (b) the throttle window is active.

    This guarantees "each reminder surfaces at least once per session, then throttles"
    and is safe under concurrent sessions because each session has independent state
    (no global last-session-id that alternating sessions fight over).

    Bounded growth: one extension_state row per session_id, pruned after
    _SESSION_STATE_TTL_SECONDS (7 days). Prune runs on each write so it only
    fires when reminders are actually being surfaced (rare compared to hook calls).
    """
    try:
        from techne.core.database import connect
        from techne.core.extension_state import get_state, set_state
        from techne.reminders import list_due, touch_surfaced_many

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        session_state_key = f"reminders:surfaced:{session_id}"

        # Load this session's set of already-surfaced reminder ids.
        conn = connect(config.db_path)
        try:
            st = get_state(conn, session_state_key) or {}
        finally:
            conn.close()
        surfaced_in_session: set[int] = set(st.get("ids", []))

        due = list_due(config.db_path, now)
        if not due:
            return None

        lines: list[str] = []
        newly_surfaced: list[int] = []
        now_dt = datetime.now(timezone.utc)

        for reminder in due:
            rid = reminder["id"]
            in_session = rid in surfaced_in_session

            # Determine whether the throttle window is active for this reminder.
            within_throttle = False
            last = reminder.get("last_surfaced_at")
            if last:
                try:
                    last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    if (now_dt - last_dt).total_seconds() < REMINDER_THROTTLE_SECONDS:
                        within_throttle = True
                except ValueError:
                    pass  # unparseable timestamp — treat as outside throttle

            # Surface if: first time in this session OR throttle has expired.
            # Skip only if already seen in this session AND still within throttle.
            if in_session and within_throttle:
                continue  # throttled for this session

            lines.append(f"⏰ Reminder: {reminder['note']}")
            newly_surfaced.append(rid)

        if newly_surfaced:
            # Batch-stamp last_surfaced_at for all reminders surfaced this call.
            touch_surfaced_many(config.db_path, newly_surfaced)
            # Update this session's surfaced-set and prune stale session keys.
            updated_ids = list(surfaced_in_session | set(newly_surfaced))
            stale_cutoff = int(time.time()) - _SESSION_STATE_TTL_SECONDS
            conn = connect(config.db_path)
            try:
                set_state(conn, session_state_key, {"ids": updated_ids}, commit=False)
                conn.execute(
                    "DELETE FROM extension_state"
                    " WHERE name LIKE 'reminders:surfaced:%'"
                    "   AND updated_at < ?",
                    (stale_cutoff,),
                )
                conn.commit()
            finally:
                conn.close()

        return "\n".join(lines) if lines else None
    except Exception as exc:  # noqa: BLE001 — reminders must never break the hook
        print(f"techne: reminder injection failed (non-fatal): {exc}", file=sys.stderr)
        return None


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"techne: post_tool_use received invalid JSON on stdin — capture skipped: {exc}", file=sys.stderr)
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

    # Inject due reminders into additionalContext — runs outside bounded_hook so a
    # slow reminder check doesn't count against the observation-capture timeout, but
    # errors are always swallowed so the hook exit code stays 0. (#207)
    reminder_context = _inject_due_reminders(config, session_id)
    if reminder_context:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": reminder_context,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
