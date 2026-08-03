#!/usr/bin/env python3
"""PostCompact hook — rehydrates context from checkpoint after compaction."""

import json
import sys

from techne.config import Config
from techne.core.database import connect
from techne.core.state import get_latest_checkpoint

# Liveness key namespace: `hook:post_compact:last_run` (techne#468). Unlike its
# pre-side sibling, this hook only READS (a checkpoint) and prints — it writes
# nothing, so before #468 a restore that silently produced nothing was
# indistinguishable from one that was never invoked.
HOOK_NAME = "post_compact"


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"techne: post_compact received invalid JSON on stdin — restore skipped: {exc}", file=sys.stderr)
        data = {}

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    # Config.load() can raise (corrupt/unreadable config). A hook that throws here
    # would break the host's post-compaction restore; degrade to a no-op. (techne#166)
    try:
        config = Config.load()
    except Exception as e:
        print(f"techne: config load failed, skipping restore: {e}", file=sys.stderr)
        sys.exit(0)

    from techne.hooks._liveness import record_hook_run

    try:
        with bounded_hook(config.hooks.timeout_s, label="post_compact"):
            # Get the latest checkpoint
            checkpoint = get_latest_checkpoint(config.db_path, session_id)
            if not checkpoint:
                # pre_compact writes checkpoints, so this hook is observable only
                # by its ABSENCE of effect (techne#468). A missing checkpoint here
                # means the pre-side never ran or never stored — worth a fact,
                # since the restore silently producing nothing is exactly what a
                # working restore with nothing to say also looks like.
                record_hook_run(config.db_path, HOOK_NAME, outcome="noop:no-checkpoint")
                sys.exit(0)

            cp_data = checkpoint["data"]
            output_parts = []

            # Restore thread context from database (unaffected by compaction)
            conn = connect(config.db_path)
            try:
                threads = conn.execute(
                    """SELECT label, summary, resume_points FROM threads
                       WHERE session_id = ? AND status = 'active'
                       ORDER BY thread_num""",
                    (session_id,),
                ).fetchall()
            finally:
                conn.close()

            if threads:
                output_parts.append("## Current Session Threads\n")
                for t in threads:
                    output_parts.append(f"**{t['label']}** — {t['summary'][:300]}")
                    if t["resume_points"]:
                        output_parts.append(f"Resume: {t['resume_points']}")
                    output_parts.append("")

            # Restore checkpoint context
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

            if output_parts:
                print("\n".join(output_parts))
            record_hook_run(
                config.db_path,
                HOOK_NAME,
                outcome="ok" if output_parts else "noop:nothing-to-restore",
                detail=f"{len(threads)} threads" if threads else None,
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
