"""Library-callable lifecycle operations.

Hooks (techne/hooks/*) are thin wrappers around these functions. Use these
directly when embedding techne in a runtime that isn't Claude Code (e.g., a
custom agent process or notebook).
"""

from techne.config import Config

# Thread summaries are adapter-bound: enqueued under the `sync:` namespace so
# flush(adapter) drains them to the team backend, and flush_sinks never touches
# them (#51). No sink stub needed — the `sync:` prefix declares the lane.
THREAD_SUMMARY_TARGET = "sync:thread_summary"


def _has_live_consumer(config) -> bool:
    """True when a real team backend can drain the outbox.

    With the noop adapter there is no consumer for `sync:` rows, so enqueuing
    only grows an undrainable backlog (#79). Gate enqueue on this.
    """
    return config.sync.adapter != "noop"


def run_session_start(
    config: Config,
    project_path: str | None = None,
    agent_identity: str | None = None,
    *,
    session_id: str | None = None,
) -> str:
    """Start a new session. Returns the session_id.

    Idempotent w.r.t. DB initialization — calls init_db.

    Pass `session_id` to use a caller-supplied id (e.g., an A2A Task ID, a
    Slack thread id) instead of a generated UUID. See
    `init_session_for_external_id` for the idempotent wrapper.
    """
    from techne.core.database import init_db
    from techne.core.state import start_session

    init_db(config.db_path)
    return start_session(
        config.db_path,
        project_path=project_path or "",
        agent_identity=agent_identity or "",
        platform=config.platform,
        session_id=session_id,
    )


def init_session_for_external_id(
    config: Config,
    external_id: str,
    *,
    project_path: str | None = None,
    agent_identity: str | None = None,
) -> str:
    """Idempotently bind a caller-supplied identifier to a techne session.

    Used when techne is embedded in a runtime that has its own conversation
    / task / dispatch IDs (e.g., A2A Task IDs in Cabal, thread IDs in Slack
    bots) and wants to map them 1:1 onto techne sessions. The external_id is
    stored verbatim as the session's primary key.

    If a session with this id already exists, return its id without modifying
    it. This makes it safe to call multiple times during a single dispatch
    (e.g., on agent restart mid-Task).

    Caller is responsible for namespacing the external_id to avoid collisions
    across runtimes sharing one techne DB.
    """
    from techne.core.database import init_db
    from techne.core.state import ensure_session

    init_db(config.db_path)

    # Atomic + idempotent: ensure_session is INSERT OR IGNORE, so it can't race the
    # way the old SELECT-then-(maybe)-INSERT did — two concurrent callers both seeing
    # "no row" and both inserting would make the second raise IntegrityError on the
    # sessions PK. INSERT OR IGNORE no-ops on an existing row, preserving the
    # "return the id without modifying it" contract. (techne#170)
    ensure_session(
        config.db_path,
        external_id,
        project_path=project_path or "",
        agent_identity=agent_identity or "",
        platform=config.platform,
    )
    return external_id


def run_session_end(
    config: Config,
    session_id: str,
    summary: str | None = None,
) -> dict:
    """End a session: detect threads, link, save, age, enqueue outbox sync.

    Returns a dict: {threads, linked, aged_completed, aged_stale, queued}.

    Note: sessions with fewer than MIN_NEW_OBSERVATIONS observations are still
    processed — the minimum-observation guard lives in the hook, not here.
    """
    import os

    from techne.core.database import connect
    from techne.core.extension_state import set_state
    from techne.core.heuristic_threads import (
        boundary_indices_from_segments,
        save_boundaries,
    )
    from techne.core.state import end_session, get_observations
    from techne.core.threads import (
        age_threads,
        detect_summarize_with_metadata,
        format_combined_summary,
        link_threads,
        save_threads,
        thread_row_id,
    )
    from techne.sync.outbox import enqueue

    observations = get_observations(config.db_path, session_id)
    threads, scores = detect_summarize_with_metadata(observations, config)

    if scores:
        # Heuristic detector ran — persist the per-pair score breakdown so
        # `techne threads show` can read it back without re-running detection.
        boundary_a_indices = boundary_indices_from_segments(
            [t.observation_indices for t in threads]
        )
        conn = connect(config.db_path)
        try:
            # Replace, don't accumulate. run_session_end fires once per turn
            # (Claude Code's Stop hook, opencode's session.idle), so re-inserting
            # the boundary set without clearing the prior one grows
            # thread_boundaries unboundedly — the #79 backlog pathology on a
            # different table (seen live: one session at 37k rows). DELETE+insert
            # keeps it idempotent, mirroring redetect_threads. Same transaction
            # as save_boundaries (which commits) so there's no empty window.
            conn.execute("DELETE FROM thread_boundaries WHERE session_id = ?", (session_id,))
            save_boundaries(conn, session_id, scores, boundary_a_indices)
            # Persist the observation count processed so external tooling (and
            # future incremental passes) can tell how far the detector has run.
            ext_key = f"heuristic_threads.{session_id}"
            set_state(conn, ext_key, {"last_obs_count": len(observations)})
        finally:
            conn.close()

    parent_links = link_threads(threads, session_id, config.db_path, config)
    save_threads(threads, session_id, config.db_path, parent_links=parent_links)

    aged = age_threads(config.db_path, config)

    agent_identity = os.environ.get("TECHNE_IDENTITY", "")
    queued = 0
    if _has_live_consumer(config):
        for t in threads:
            enqueue(
                config.db_path,
                THREAD_SUMMARY_TARGET,
                {
                    "session_id": session_id,
                    # Real threads.id (heuristic threads are `t-<hash>`, not the legacy
                    # `{session}-{num}`) so sync consumers reference a row that exists (#165).
                    "thread_id": thread_row_id(session_id, t),
                    "thread_label": t.label,
                    "summary": t.summary,
                    "resume_points": t.resume_points,
                    "agent_identity": agent_identity,
                },
            )
            queued += 1

    # Reflection pass — opt-in, never blocks session teardown. Writes proposals
    # to knowledge_candidates for human review (config.reflect.write_to controls
    # whether they also land in the live knowledge / identity tables).
    reflect_result: dict = {
        "threads_considered": 0,
        "threads_reflected": 0,
        "candidates_proposed": 0,
        "candidates_written": 0,
        "candidates_promoted": 0,
        "errors": 0,
    }
    if config.reflect.enabled:
        try:
            from techne.core.reflect import reflect_on_session

            reflect_result = reflect_on_session(config, session_id, threads, observations)
        except Exception as e:
            import sys

            print(f"techne: reflect pass failed: {e}", file=sys.stderr)

    if summary:
        end_session(config.db_path, session_id, summary=summary, source="manual")
    else:
        end_session(
            config.db_path,
            session_id,
            summary=format_combined_summary(threads),
            source="heuristic",
        )

    return {
        "threads": len(threads),
        "linked": sum(1 for _, p in parent_links if p),
        "aged_completed": aged.get("completed", 0),
        "aged_stale": aged.get("stale", 0),
        "queued": queued,
        "reflect": reflect_result,
    }


def redetect_threads(
    config: Config,
    session_id: str,
    *,
    sync: bool = False,
) -> dict:
    """Re-run heuristic thread detection on an already-recorded session.

    Replaces existing threads + per-pair boundary scores for the session and
    refreshes summary_heuristic. Does NOT change `ended_at` or touch the
    manual / LLM summary slots — this is a re-analysis, not a re-end.

    Use it after the detector itself changed (new signals, retuned weights)
    or after late-arriving observations were ingested via `techne import`.

    Outbox sync defaults to OFF — redetection happens iteratively (tuning a
    weight, re-running) and flooding the outbox each pass would queue stale
    syncs that consumers immediately supersede. Pass `sync=True` once the
    detector tuning settles and downstream consumers need to see the new
    thread structure.

    Returns: {threads, boundaries, observations, queued}.
    """
    import os

    from techne.core.database import connect
    from techne.core.heuristic_threads import (
        boundary_indices_from_segments,
        save_boundaries,
    )
    from techne.core.state import get_observations, set_session_summary
    from techne.core.threads import (
        detect_summarize_with_metadata,
        format_combined_summary,
        link_threads,
        save_threads,
        thread_row_id,
    )
    from techne.sync.outbox import enqueue

    observations = get_observations(config.db_path, session_id)
    threads, scores = detect_summarize_with_metadata(observations, config)

    # Compute parent links before opening the write connection (link_threads
    # does read-only FTS queries on its own connection; no conflict).
    parent_links = link_threads(threads, session_id, config.db_path, config)

    # Delete old rows AND insert new ones in a single transaction so a crash
    # mid-way never leaves the session with no threads.
    boundary_a_indices = (
        boundary_indices_from_segments([t.observation_indices for t in threads]) if scores else None
    )
    conn = connect(config.db_path)
    try:
        conn.execute("DELETE FROM threads WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM thread_boundaries WHERE session_id = ?", (session_id,))
        if scores and boundary_a_indices is not None:
            save_boundaries(conn, session_id, scores, boundary_a_indices, commit=False)
        save_threads(threads, session_id, config.db_path, parent_links=parent_links, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    set_session_summary(
        config.db_path,
        session_id,
        format_combined_summary(threads),
        source="heuristic",
    )

    queued = 0
    if sync and _has_live_consumer(config):
        agent_identity = os.environ.get("TECHNE_IDENTITY", "")
        for t in threads:
            enqueue(
                config.db_path,
                THREAD_SUMMARY_TARGET,
                {
                    "session_id": session_id,
                    # Real threads.id (heuristic threads are `t-<hash>`, not the legacy
                    # `{session}-{num}`) so sync consumers reference a row that exists (#165).
                    "thread_id": thread_row_id(session_id, t),
                    "thread_label": t.label,
                    "summary": t.summary,
                    "resume_points": t.resume_points,
                    "agent_identity": agent_identity,
                },
            )
            queued += 1

    return {
        "threads": len(threads),
        "boundaries": len(scores),
        "observations": len(observations),
        "queued": queued,
    }


def capture_tool_use(
    config: Config,
    session_id: str,
    tool_name: str,
    tool_input: dict,
    tool_output: str | None,
    *,
    event_at: str | None = None,
) -> bool:
    """Capture a tool use as an observation. Returns True if captured, False if filtered.

    Applies the same `should_capture` filtering used by the Claude Code hook.
    Also respects config.hooks.post_tool_use — returns False if hook is disabled.

    `event_at` is an optional ISO-8601 timestamp for the real-world event time.
    For live hook captures the Claude Code post_tool_use payload does not include
    a distinct tool-event timestamp, so callers may omit it — the observation's
    `timestamp` column (DB write time, sub-second after the event) is then the
    canonical event time. Pass it explicitly for replay/import paths where the
    event time differs meaningfully from the write time.
    """
    from techne.core.state import add_observation
    from techne.hooks.post_tool_use import should_capture, summarize

    if not config.hooks.post_tool_use:
        return False
    if not should_capture(config, tool_name):
        return False
    content = summarize(tool_name, tool_input, tool_output)
    add_observation(config.db_path, session_id, tool_name, content, event_at=event_at)
    return True
