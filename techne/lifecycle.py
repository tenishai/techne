"""Library-callable lifecycle operations.

Hooks (techne/hooks/*) are thin wrappers around these functions. Use these
directly when embedding techne in a runtime that isn't Claude Code (e.g., a
custom agent process or notebook).
"""

import sys

from techne.config import Config

# Thread-summary outbox lanes, selected by topology (techne#224). One flag picks
# the lane; techne behaviour is otherwise identical.
#   standalone → sync:  — adapter-bound; flush(adapter) drains to the team backend.
#   team       → sink:  — delivered to the Archivist sink, which owns the push.
THREAD_SUMMARY_SYNC_TARGET = "sync:thread_summary"
ARCHIVIST_SINK_TARGET = "sink:archivist"
# Back-compat alias (was the only, sync-only, target before #224).
THREAD_SUMMARY_TARGET = THREAD_SUMMARY_SYNC_TARGET


def _thread_summary_target(config) -> str:
    """The outbox lane for thread summaries, by topology (techne#224)."""
    return THREAD_SUMMARY_SYNC_TARGET if config.standalone else ARCHIVIST_SINK_TARGET


def _has_live_consumer(config) -> bool:
    """True when the SELECTED egress lane has a real drain — gate enqueue on it so a
    noop lane doesn't grow an undrainable backlog (#79, #224).

    standalone → the sync adapter drains `sync:` rows (noop = no consumer).
    team       → a registered `sink:archivist` delivers `sink:` rows.
    """
    if config.standalone:
        return config.sync.adapter != "noop"
    from techne.sync.sinks import get_sink

    return get_sink(ARCHIVIST_SINK_TARGET) is not None


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
    from techne.core.extension_state import get_state, set_state
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

    # Incremental gate (perf): the SessionEnd hook fires once per turn and would
    # otherwise reprocess the ENTIRE session history on every firing — full
    # thread detection over the whole observation list, dominated by filesystem
    # walks (see _detect_summarize_heuristic). Observations are append-only, so
    # an unchanged count since the last completed detection means the detector's
    # inputs — and therefore the threads, boundaries, and reflection derived from
    # them — are identical to the prior run; recomputing and re-saving them is
    # pure waste. Skip straight to the time-based tail (age_threads depends on
    # wall-clock, not on this session's observations). The hook's
    # MIN_NEW_OBSERVATIONS check is a floor on TOTAL obs, not a per-turn delta,
    # so it does not cover this. Only the heuristic path records last_obs_count
    # (below), so the LLM detector is never gated here.
    ext_key = f"heuristic_threads.{session_id}"
    if observations:
        gate_conn = connect(config.db_path)
        try:
            prev = get_state(gate_conn, ext_key)
        finally:
            gate_conn.close()
        if prev is not None and prev.get("last_obs_count") == len(observations):
            # Detection is skipped, but a caller-supplied summary must STILL be
            # persisted (and ended_at stamped): the CLI/plugin session-end path
            # (`techne session-end --summary`, opencode session.idle) passes one
            # on turns that add no new observations, and before the gate that
            # write always ran. Dropping it here would be a silent data loss
            # (Prudence, #356). The heuristic-summary branch is intentionally NOT
            # re-run — that's the recompute the gate exists to avoid.
            if summary:
                end_session(config.db_path, session_id, summary=summary, source="manual")
            aged = age_threads(config.db_path, config)
            return {
                "threads": 0,
                "linked": 0,
                "aged_completed": aged.get("completed", 0),
                "aged_stale": aged.get("stale", 0),
                "queued": 0,
                # The incremental gate skipped detection, so no NEW threads exist
                # to owe reflection on. Anything already owed stays queued — the
                # debt lives in the table, not in this return value.
                "queued_for_reflection": 0,
                "knowledge_pushed": 0,
                "reflect": {
                    "threads_considered": 0,
                    "threads_reflected": 0,
                    "candidates_proposed": 0,
                    "candidates_written": 0,
                    "candidates_promoted": 0,
                    "errors": 0,
                },
                "skipped": "no_new_observations",
            }

    threads, scores = detect_summarize_with_metadata(observations, config)

    # Compute parent links before opening the write connection — link_threads
    # does read-only FTS queries on its own connection so there's no conflict.
    parent_links = link_threads(threads, session_id, config.db_path, config)

    if scores:
        # Heuristic detector ran — persist the per-pair score breakdown so
        # `techne threads show` can read it back without re-running detection.
        boundary_a_indices = boundary_indices_from_segments(
            [t.observation_indices for t in threads]
        )
        # Write thread_boundaries AND threads in ONE transaction so a crash
        # between the two writes never leaves committed boundaries with no
        # threads (#248). This mirrors redetect_threads exactly: one
        # connection, commit=False on the intermediate writes, single commit
        # at the end, rollback on any exception.
        #
        # Replace, don't accumulate. run_session_end fires once per turn
        # (Claude Code's Stop hook, opencode's session.idle), so re-inserting
        # the boundary set without clearing the prior one grows
        # thread_boundaries unboundedly — the #79 backlog pathology on a
        # different table (seen live: one session at 37k rows). DELETE+insert
        # keeps it idempotent, mirroring redetect_threads.
        conn = connect(config.db_path)
        try:
            conn.execute("DELETE FROM thread_boundaries WHERE session_id = ?", (session_id,))
            save_boundaries(conn, session_id, scores, boundary_a_indices, commit=False)
            # Persist the observation count processed so external tooling (and
            # future incremental passes) can tell how far the detector has run.
            ext_key = f"heuristic_threads.{session_id}"
            set_state(conn, ext_key, {"last_obs_count": len(observations)}, commit=False)
            save_threads(threads, session_id, config.db_path, parent_links=parent_links, conn=conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        save_threads(threads, session_id, config.db_path, parent_links=parent_links)

    # techne#514: record the reflection debt DURABLY, not just in the log line
    # below. Until now this function counted threads, printed "N obs, M threads"
    # and enqueued nothing — so reflection survived only the polite exit path,
    # and every crash/timeout dropped it silently. Best-effort: a queue-write
    # failure must never take down teardown, which is the one thing that still
    # has to persist the session's actual work.
    queued_for_reflection = 0
    try:
        from techne.core.reflect import emittable_thread_ids, enqueue_reflection_debt

        # Only threads emit will actually produce a prompt for. Queueing every
        # thread left sub-threshold ones queued, never emittable and therefore
        # never drainable (#527 r4).
        queued_for_reflection = enqueue_reflection_debt(
            config.db_path,
            session_id,
            thread_ids=emittable_thread_ids(config, session_id, threads, observations),
        )
    except Exception as e:  # pragma: no cover - defensive, mirrors the reflect path
        print(f"techne: reflection debt enqueue deferred: {e}", file=sys.stderr)

    aged = age_threads(config.db_path, config)

    agent_identity = os.environ.get("TECHNE_IDENTITY", "")
    queued = 0
    from techne.core.archivist import ensure_registered_for

    ensure_registered_for(config)  # team topology: stand up sink:archivist (#224/#346)
    if _has_live_consumer(config):
        target = _thread_summary_target(config)
        for t in threads:
            enqueue(
                config.db_path,
                target,
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

    # Team topology: egress live knowledge to the base Archivist AFTER reflection
    # (which may have promoted new live knowledge). No-op when standalone; the
    # outbox dedups so re-pushing is cheap (techne#346 PR2).
    knowledge_pushed = 0
    try:
        from techne.core.archivist import push_knowledge_to_archivist

        knowledge_pushed = push_knowledge_to_archivist(config)
    except Exception as e:  # noqa: BLE001 — egress must never break session teardown
        print(f"techne: archivist knowledge push deferred: {e}", file=sys.stderr)

    return {
        "threads": len(threads),
        "linked": sum(1 for _, p in parent_links if p),
        "aged_completed": aged.get("completed", 0),
        "aged_stale": aged.get("stale", 0),
        "queued": queued,
        # Distinct from "queued" (outbox sync) — this is reflection debt
        # persisted to reflection_queue (techne#514). Reported so a caller can
        # tell "nothing was owed" from "something was owed and we dropped it";
        # before #514 those two were indistinguishable from outside.
        "queued_for_reflection": queued_for_reflection,
        "knowledge_pushed": knowledge_pushed,
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
    from techne.core.reflect import emittable_thread_ids, enqueue_reflection_debt
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
        prior_drained = {
            r["thread_id"]: r["drained_at"]
            for r in conn.execute(
                "SELECT thread_id, drained_at FROM reflection_queue "
                "WHERE session_id = ? AND drained_at IS NOT NULL",
                (session_id,),
            )
        }
        conn.execute("DELETE FROM threads WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM thread_boundaries WHERE session_id = ?", (session_id,))
        if scores and boundary_a_indices is not None:
            save_boundaries(conn, session_id, scores, boundary_a_indices, commit=False)
        save_threads(threads, session_id, config.db_path, parent_links=parent_links, conn=conn)
        # Re-enqueue INSIDE the transaction: the DELETE above cascades
        # reflection_queue away and session_end does not re-run for a redetect,
        # so without this the debt is lost permanently (#527 r1).
        #
        # Both the id lookup and the insert must ride THIS conn (#527 r5) —
        # persisted_thread_id used to open its own and could not see the
        # uncommitted rows, which is what made an inline enqueue raise a FOREIGN
        # KEY failure and forced it after the commit. Do not move it back out:
        # nothing re-derives debt for an emittable thread with no queue row, so
        # a failure out there is a permanent half-applied redetect, while in
        # here it rolls back to a state the operator can re-run.
        enqueue_reflection_debt(
            config.db_path,
            session_id,
            thread_ids=emittable_thread_ids(
                config, session_id, threads, observations, conn=conn
            ),
            conn=conn,
        )
        # Re-enqueueing alone resurrects PAID debt as pending, re-firing the
        # banner and making its "oldest from" age wrong (#527 r2). Same
        # transaction as the enqueue it corrects: a restore that can fail
        # separately from the insert it repairs is the r2 regression again.
        for tid, drained in prior_drained.items():
            conn.execute(
                "UPDATE reflection_queue SET drained_at = ? WHERE thread_id = ?",
                (drained, tid),
            )
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
    if sync:
        from techne.core.archivist import ensure_registered_for

        ensure_registered_for(config)
    if sync and _has_live_consumer(config):
        agent_identity = os.environ.get("TECHNE_IDENTITY", "")
        target = _thread_summary_target(config)
        for t in threads:
            enqueue(
                config.db_path,
                target,
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
    if not should_capture(config, tool_name, tool_input):
        return False
    content = summarize(tool_name, tool_input, tool_output)
    add_observation(config.db_path, session_id, tool_name, content, event_at=event_at)
    return True
