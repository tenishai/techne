#!/usr/bin/env python3
"""SessionEnd (Stop) hook — archives session, runs thread detection, ages threads."""

import json
import sys

from techne.core.state import observation_count
from techne.sync.adapter import load_adapter
from techne.sync.outbox import flush

MIN_NEW_OBSERVATIONS = 10


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    from techne.config import Config
    from techne.lifecycle import run_session_end

    config = Config.load()

    obs_count = observation_count(config.db_path, session_id)
    if obs_count < MIN_NEW_OBSERVATIONS:
        print(f"techne: session end skipped ({obs_count} observations)", file=sys.stderr)
        sys.exit(0)

    try:
        result = run_session_end(config, session_id, summary=data.get("summary"))
    except Exception as e:
        print(f"techne: session_end failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Flush outbox
    try:
        adapter = load_adapter(config.sync.adapter, url=config.sync.url, path=config.sync.path)
        flush(config.db_path, adapter)
    except Exception as e:
        print(f"techne: outbox flush deferred: {e}", file=sys.stderr)

    print(
        f"techne: session archived ({obs_count} obs, {result['threads']} threads)",
        file=sys.stderr,
    )
    if result["aged_completed"] or result["aged_stale"]:
        print(
            f"techne: aged {result['aged_completed']} completed, {result['aged_stale']} stale",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
