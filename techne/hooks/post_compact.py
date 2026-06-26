#!/usr/bin/env python3
"""PostCompact hook — rehydrates context from checkpoint after compaction."""

import json
import sys

from techne.config import Config
from techne.core.database import connect
from techne.core.state import get_latest_checkpoint


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    config = Config.load()

    try:
        with bounded_hook(config.hooks.timeout_s):
            # Get the latest checkpoint
            checkpoint = get_latest_checkpoint(config.db_path, session_id)
            if not checkpoint:
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
    except Exception as e:
        print(f"techne: post_compact failed: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
