"""Library-callable lifecycle operations.

Hooks (techne/hooks/*) are thin wrappers around these functions. Use these
directly when embedding techne in a runtime that isn't Claude Code (e.g., a
custom agent process or notebook).
"""

from techne.config import Config


def run_session_start(
    config: Config,
    project_path: str | None = None,
    agent_identity: str | None = None,
) -> str:
    """Start a new session. Returns the session_id.

    Idempotent w.r.t. DB initialization — calls init_db.
    """
    from techne.core.database import init_db
    from techne.core.state import start_session

    init_db(config.db_path)
    return start_session(
        config.db_path,
        project_path=project_path or "",
        agent_identity=agent_identity or "",
    )


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
            save_boundaries(conn, session_id, scores, boundary_a_indices)
        finally:
            conn.close()

    parent_links = link_threads(threads, session_id, config.db_path, config)
    save_threads(threads, session_id, config.db_path, parent_links=parent_links)

    aged = age_threads(config.db_path, config)

    agent_identity = os.environ.get("TECHNE_IDENTITY", "")
    queued = 0
    for t in threads:
        enqueue(config.db_path, "thread_summary", {
            "session_id": session_id,
            "thread_id": f"{session_id}-{t.thread_num}",
            "thread_label": t.label,
            "summary": t.summary,
            "resume_points": t.resume_points,
            "agent_identity": agent_identity,
        })
        queued += 1

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
    )
    from techne.sync.outbox import enqueue

    observations = get_observations(config.db_path, session_id)
    threads, scores = detect_summarize_with_metadata(observations, config)

    conn = connect(config.db_path)
    try:
        # Clear prior detection output before writing fresh rows.
        # save_threads uses INSERT OR REPLACE on PK so a same-count rerun would
        # overwrite cleanly, but a rerun that produces FEWER threads (signal
        # tightening) would leave orphans behind.
        conn.execute("DELETE FROM threads WHERE session_id = ?", (session_id,))
        conn.execute(
            "DELETE FROM thread_boundaries WHERE session_id = ?", (session_id,)
        )
        if scores:
            boundary_a_indices = boundary_indices_from_segments(
                [t.observation_indices for t in threads]
            )
            save_boundaries(conn, session_id, scores, boundary_a_indices)
        conn.commit()
    finally:
        conn.close()

    parent_links = link_threads(threads, session_id, config.db_path, config)
    save_threads(threads, session_id, config.db_path, parent_links=parent_links)
    set_session_summary(
        config.db_path,
        session_id,
        format_combined_summary(threads),
        source="heuristic",
    )

    queued = 0
    if sync:
        agent_identity = os.environ.get("TECHNE_IDENTITY", "")
        for t in threads:
            enqueue(config.db_path, "thread_summary", {
                "session_id": session_id,
                "thread_id": f"{session_id}-{t.thread_num}",
                "thread_label": t.label,
                "summary": t.summary,
                "resume_points": t.resume_points,
                "agent_identity": agent_identity,
            })
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
) -> bool:
    """Capture a tool use as an observation. Returns True if captured, False if filtered.

    Applies the same `should_capture` filtering used by the Claude Code hook.
    Also respects config.hooks.post_tool_use — returns False if hook is disabled.
    """
    from techne.core.state import add_observation
    from techne.hooks.post_tool_use import should_capture, summarize

    if not config.hooks.post_tool_use:
        return False
    if not should_capture(config, tool_name):
        return False
    content = summarize(tool_name, tool_input, tool_output)
    add_observation(config.db_path, session_id, tool_name, content)
    return True
