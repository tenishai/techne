#!/usr/bin/env python3
"""SessionEnd (Stop) hook — archives session, runs thread detection, ages threads.

Must never fail session teardown: Claude Code treats any non-zero exit as a
hard hook failure. Every error path here exits 0 and surfaces the issue on
stderr.

Bounded timeout (techne#119): all blocking work (thread detection, LLM calls,
sync adapter network I/O) runs inside bounded_hook().  If the budget expires,
or any unhandled exception escapes, the hook exits 0 silently.
"""

import json
import sys

MIN_NEW_OBSERVATIONS = 10


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"techne: session_end received invalid JSON on stdin — teardown skipped: {exc}", file=sys.stderr)
        data = {}

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # Load config before arming the timer — reading a local YAML file is fast
    # and we need config.hooks.timeout_s to set the correct budget.
    from techne.hooks._timeout import bounded_hook

    try:
        from techne.config import Config

        config = Config.load()
        timeout_s = config.hooks.timeout_s
    except Exception as e:
        print(f"techne: session_end config load failed: {e}", file=sys.stderr)
        sys.exit(0)

    try:
        with bounded_hook(timeout_s):
            from techne.core.state import observation_count
            from techne.lifecycle import run_session_end
            from techne.sync.adapter import load_adapter
            from techne.sync.outbox import flush

            obs_count = observation_count(config.db_path, session_id)

            if obs_count < MIN_NEW_OBSERVATIONS:
                print(f"techne: session end skipped ({obs_count} observations)", file=sys.stderr)
                sys.exit(0)

            try:
                result = run_session_end(config, session_id, summary=data.get("summary"))
            except Exception as e:
                print(f"techne: session_end failed: {e}", file=sys.stderr)
                sys.exit(0)

            try:
                adapter = load_adapter(
                    config.sync.adapter, url=config.sync.url, path=config.sync.path
                )
                flush(config.db_path, adapter)
            except Exception as e:
                print(f"techne: outbox flush deferred: {e}", file=sys.stderr)

            try:
                from techne.core.archivist import ensure_registered_for
                from techne.sync.outbox import flush_sinks

                # So a row that failed immediate delivery is drainable here too (#346).
                ensure_registered_for(config)
                flush_sinks(config.db_path)
            except Exception as e:
                print(f"techne: sink flush deferred: {e}", file=sys.stderr)

            print(
                f"techne: session archived ({obs_count} obs, {result['threads']} threads)",
                file=sys.stderr,
            )
            if result["aged_completed"] or result["aged_stale"]:
                print(
                    f"techne: aged {result['aged_completed']} completed,"
                    f" {result['aged_stale']} stale",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"techne: session_end hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
