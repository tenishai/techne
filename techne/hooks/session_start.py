#!/usr/bin/env python3
"""SessionStart hook — injects relevant context at session beginning."""

import json
import os
import sys

from techne.config import Config
from techne.core.database import init_db
from techne.core.state import start_session, get_resume_context
from techne.sync.adapter import load_adapter, SyncQuery
from techne.sync.outbox import flush


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}

    config = Config.load()
    db = init_db(config.db_path)
    db.close()

    project_path = data.get("cwd", os.getcwd())
    agent_identity = os.environ.get("TECHNE_IDENTITY", "")
    platform = config.platform

    session_id = start_session(
        config.db_path,
        project_path=project_path,
        agent_identity=agent_identity,
        platform=platform,
    )

    # Flush outbox from prior session
    try:
        adapter = load_adapter(config.sync.adapter, url=config.sync.url, path=config.sync.path)
        synced, failed = flush(config.db_path, adapter)
        if synced:
            print(f"techne: flushed {synced} outbox entries", file=sys.stderr)
    except Exception as e:
        print(f"techne: outbox flush failed: {e}", file=sys.stderr)

    # Build resume context
    ctx = get_resume_context(config.db_path, project_path=project_path)

    # Inject context
    output_parts = []

    if ctx["active_threads"]:
        output_parts.append("## Active Threads\n")
        for t in ctx["active_threads"]:
            output_parts.append(f"**{t['label']}** (updated {t['updated_at']})")
            if t.get("resume_points"):
                output_parts.append(f"Resume: {t['resume_points']}")
            output_parts.append("")

    if ctx["recent_threads"]:
        output_parts.append("## Recent Threads (48h)\n")
        for t in ctx["recent_threads"]:
            status = f"[{t['status']}]" if t["status"] != "active" else ""
            output_parts.append(f"- **{t['label']}** {status} — {t['summary'][:200]}")
        output_parts.append("")

    if ctx["knowledge"]:
        output_parts.append("## Key Knowledge\n")
        for k in ctx["knowledge"]:
            output_parts.append(f"- [{k['type']}] {k['content'][:200]}")
        output_parts.append("")

    if output_parts:
        print("\n".join(output_parts))

    # Store session_id for downstream hooks
    print(json.dumps({"session_id": session_id}), file=sys.stderr)


if __name__ == "__main__":
    main()
