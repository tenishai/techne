#!/usr/bin/env python3
"""Stop and SessionEnd hook — archives session, runs thread detection, ages threads.

Registered on BOTH events (techne#1187). Claude Code fires Stop at the end of every
turn, so on Stop this runs as a CHECKPOINT: threads are re-detected, but a thread
summary is enqueued only once it passes the checkpoint gate, and `ended_at` is left
alone. SessionEnd fires once, when the session really ends, and runs the FINAL pass.

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

# Liveness key namespace: `hook:session_end:last_run` (techne#468).
#
# This hook does the most consequential work of any of them — thread detection,
# tier aging, outbox + sink flush — and, before #468, recorded NOTHING about
# whether any of it happened. Its fail-silence is deliberate and must stay
# (Claude Code treats a non-zero hook exit as a hard failure, so a raising
# teardown hook is worse than a quiet one), which is exactly why the failure has
# to be WRITTEN DOWN instead: silence is the correct behaviour and therefore
# cannot also be the failure signal.
HOOK_NAME = "session_end"


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(
            f"techne: session_end received invalid JSON on stdin — teardown skipped: {exc}",
            file=sys.stderr,
        )
        data = {}

    from techne.hooks._liveness import record_hook_run, resolve_db_path
    from techne.hooks._timeout import HookTimeout, bounded_hook

    session_id = data.get("session_id", "")
    if not session_id:
        # Reached BOTH when session_id is genuinely absent and when stdin was
        # malformed above (that path sets data = {} and falls through here), so
        # "teardown was skipped entirely" must leave a trace. This is the hook
        # doing the most consequential work; a silent skip here is precisely the
        # undecidable state techne#468 exists to remove.
        db = resolve_db_path()
        if db:
            record_hook_run(db, HOOK_NAME, outcome="noop:no-session-id")
        sys.exit(0)

    # Load config before arming the timer — reading a local YAML file is fast
    # and we need config.hooks.timeout_s to set the correct budget.
    try:
        from techne.config import Config

        config = Config.load()
        timeout_s = config.hooks.timeout_s
    except Exception as e:
        print(f"techne: session_end config load failed: {e}", file=sys.stderr)
        # Cannot ask Config for the path — Config is what just failed — so this
        # falls back to the conventional store location (see resolve_db_path).
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
        with bounded_hook(timeout_s, label="session_end"):
            from techne.core.state import observation_count
            from techne.lifecycle import run_session_end
            from techne.platforms.claude_code.transcript_import import backfill_uncaptured
            from techne.sync.adapter import load_adapter
            from techne.sync.outbox import flush

            # Backfill the tools the live hook skipped (Bash/Read) from this
            # session's JSONL BEFORE counting/threading, so shell work counts
            # toward the minimum and participates in thread detection. Best-effort:
            # never let backfill block session teardown.
            try:
                bf = backfill_uncaptured(config, session_id)
                if bf.get("backfilled"):
                    print(
                        f"techne: backfilled {bf['backfilled']} uncaptured obs"
                        f" ({', '.join(bf['gap_tools'])})",
                        file=sys.stderr,
                    )
            except HookTimeout:
                raise  # don't swallow the bounded_hook alarm — let teardown abort
            except Exception as e:
                print(f"techne: backfill deferred: {e}", file=sys.stderr)

            obs_count = observation_count(config.db_path, session_id)

            if obs_count < MIN_NEW_OBSERVATIONS:
                print(f"techne: session end skipped ({obs_count} observations)", file=sys.stderr)
                record_hook_run(
                    config.db_path,
                    HOOK_NAME,
                    outcome="noop:below-threshold",
                    detail=f"{obs_count} obs < {MIN_NEW_OBSERVATIONS}",
                )
                sys.exit(0)

            from techne.lifecycle import MODE_CHECKPOINT, MODE_FINAL, finalize_quiet_sessions

            mode = MODE_FINAL if data.get("hook_event_name") == "SessionEnd" else MODE_CHECKPOINT
            try:
                result = run_session_end(
                    config, session_id, summary=data.get("summary"), mode=mode
                )
            except Exception as e:
                print(f"techne: session_end failed: {e}", file=sys.stderr)
                record_hook_run(
                    config.db_path,
                    HOOK_NAME,
                    outcome="failed",
                    error={"stage": "run_session_end", "msg": f"{type(e).__name__}: {e}"},
                )
                sys.exit(0)

            # Recovery for sessions that died without SessionEnd (killed, crashed, or
            # an install that has not re-run `techne init` since #1187). Stop used to
            # stamp `ended_at` every turn, so such a session still got an end; now
            # only the final pass does, and this sweep is what gives it one. It only
            # touches sessions silent for ABANDONED_SESSION_IDLE_HOURS, so a live peer
            # is never closed. Then their owed sync: one per run, never this session.
            try:
                from techne.core.state import close_abandoned_sessions

                close_abandoned_sessions(config.db_path)
                finalize_quiet_sessions(config, exclude_session_id=session_id)
            except HookTimeout:
                raise
            except Exception as e:
                print(f"techne: quiet-session finalize deferred: {e}", file=sys.stderr)

            try:
                adapter = load_adapter(
                    config.sync.adapter,
                    url=config.sync.url,
                    path=config.sync.path,
                    allow_unlisted=config.sync.allow_unlisted_adapter,
                )
                flush(config.db_path, adapter)
            except Exception as e:
                print(f"techne: outbox flush deferred: {e}", file=sys.stderr)

            try:
                from techne.sync.outbox import flush_sinks
                from techne.sync.sinks import ensure_sinks_registered

                # So a row that failed immediate delivery is drainable here too
                # (#346), installed packages' sinks included (#1172).
                ensure_sinks_registered(config)
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
            record_hook_run(
                config.db_path,
                HOOK_NAME,
                outcome="ok",
                detail=(
                    f"{mode}: {obs_count} obs, {result['threads']} threads,"
                    f" {result['queued']} queued"
                ),
            )
    except Exception as e:
        # Catches HookTimeout too. This is the path that mattered most: before
        # #468 a teardown that blew its wall-clock budget mid-way left NOTHING
        # behind, so a session whose threads were never detected looked exactly
        # like one with nothing to detect.
        print(f"techne: session_end hook error: {e}", file=sys.stderr)
        record_hook_run(
            config.db_path,
            HOOK_NAME,
            outcome="failed",
            error={"stage": "teardown", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
