#!/usr/bin/env python3
"""PostCompact hook, plus the restore that SessionStart(compact) delivers.

Claude Code drops a PostCompact hook's stdout: only UserPromptSubmit,
UserPromptExpansion, SessionStart and PostModelSwitch put plain stdout in context
(hooks reference, checked 2026-10-12). Until techne#1242 this hook printed the
restore anyway and recorded `ok`, so the restore never reached the model. The
restore is now built by `build_restore` and sent by session_start when it fires
with `source == "compact"`. This hook only records whether a checkpoint exists to
restore from.
"""

import json
import sys

from techne.config import Config
from techne.core.database import connect
from techne.core.state import get_latest_checkpoint

# Liveness key namespace: `hook:post_compact:last_run` (techne#468). `ok` means a
# checkpoint was there to restore from, not that anything reached the model: this
# event has no channel to the model (techne#1242).
HOOK_NAME = "post_compact"

#: Active threads restored after a compaction, newest last. Compaction exists to
#: reclaim context, so the restore keeps the most recent threads, not all of them.
MAX_RESTORED_THREADS = 10
#: Per-field cut for a thread's summary and resume points.
MAX_THREAD_FIELD_CHARS = 300


def build_restore(db_path, session_id: str) -> tuple[str, int] | None:
    """Render the post-compaction restore for `session_id`.

    Returns None when there is no checkpoint, else (text, thread_count); text is
    empty when the checkpoint and threads hold nothing to restore.
    """
    checkpoint = get_latest_checkpoint(db_path, session_id)
    if not checkpoint:
        return None

    cp_data = checkpoint["data"]
    output_parts = []

    # Thread context comes from the database, which compaction does not touch.
    conn = connect(db_path)
    try:
        threads = conn.execute(
            """SELECT label, summary, resume_points FROM threads
               WHERE session_id = ? AND status = 'active'
               ORDER BY thread_num DESC LIMIT ?""",
            (session_id, MAX_RESTORED_THREADS),
        ).fetchall()
    finally:
        conn.close()
    threads = list(reversed(threads))

    if threads:
        output_parts.append("## Current Session Threads\n")
        for t in threads:
            output_parts.append(
                f"**{t['label']}** — {(t['summary'] or '')[:MAX_THREAD_FIELD_CHARS]}"
            )
            if t["resume_points"]:
                output_parts.append(f"Resume: {t['resume_points'][:MAX_THREAD_FIELD_CHARS]}")
            output_parts.append("")

    if cp_data.get("last_task"):
        output_parts.append(f"## Last Task\n{cp_data['last_task']}\n")

    if cp_data.get("files_modified"):
        output_parts.append("## Files Modified\n")
        for f in cp_data["files_modified"][-10:]:
            output_parts.append(f"- `{f}`")
        output_parts.append("")

    if cp_data.get("recent_commands"):
        output_parts.append("## Recent Commands\n")
        for cmd in cp_data["recent_commands"]:
            output_parts.append(f"- `{cmd}`")
        output_parts.append("")

    return "\n".join(output_parts), len(threads)


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(
            f"techne: post_compact received invalid JSON on stdin — restore skipped: {exc}",
            file=sys.stderr,
        )
        data = {}

    from techne.hooks._liveness import record_hook_run, resolve_db_path
    from techne.hooks._timeout import bounded_hook

    session_id = data.get("session_id", "")
    if not session_id:
        # Also the landing point for malformed stdin above (which sets data = {}).
        db = resolve_db_path()
        if db:
            record_hook_run(db, HOOK_NAME, outcome="noop:no-session-id")
        sys.exit(0)

    # Config.load() can raise (corrupt/unreadable config). A hook that throws here
    # would break the host's post-compaction restore; degrade to a no-op. (techne#166)
    try:
        config = Config.load()
    except Exception as e:
        print(f"techne: config load failed, skipping restore: {e}", file=sys.stderr)
        db = resolve_db_path()
        if db:
            record_hook_run(
                db,
                HOOK_NAME,
                outcome="failed",
                error={"stage": "config-load", "msg": f"{type(e).__name__}: {e}"},
            )
        sys.exit(0)

    try:
        with bounded_hook(config.hooks.timeout_s, label="post_compact"):
            restore = build_restore(config.db_path, session_id)
            if restore is None:
                # pre_compact writes checkpoints, so a missing one means the
                # pre-side never ran or never stored (techne#468).
                record_hook_run(config.db_path, HOOK_NAME, outcome="noop:no-checkpoint")
                sys.exit(0)
            text, n_threads = restore
            # Nothing is printed: PostCompact stdout goes to Claude Code's debug
            # log, never to the model. SessionStart(compact) sends the restore.
            record_hook_run(
                config.db_path,
                HOOK_NAME,
                outcome="ok" if text else "noop:nothing-to-restore",
                detail=f"{n_threads} threads" if n_threads else None,
            )
    except Exception as e:
        print(f"techne: post_compact failed: {e}", file=sys.stderr)
        record_hook_run(
            config.db_path,
            HOOK_NAME,
            outcome="failed",
            error={"stage": "restore", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
