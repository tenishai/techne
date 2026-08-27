"""Library-callable lifecycle operations.

Hooks (techne/hooks/*) are thin wrappers around these functions. Use these
directly when embedding techne in a runtime that isn't Claude Code (e.g., a
custom agent process or notebook).
"""

import sys

from techne.config import Config

# Thread-summary outbox lanes. HISTORICALLY selected by topology (techne#224); since
# techne#552 raw summaries always take the sync lane, because the Archivist federates
# only the distilled. ARCHIVIST_SINK_TARGET is kept — delivered rows in that lane must
# remain drainable — but nothing enqueues into it any more.
#   sync:  — adapter-bound; flush(adapter) drains to the team backend.
#   sink:  — the Archivist sink. Legacy for thread summaries; still used by knowledge.
THREAD_SUMMARY_SYNC_TARGET = "sync:thread_summary"
ARCHIVIST_SINK_TARGET = "sink:archivist"
# Back-compat alias (was the only, sync-only, target before #224).
THREAD_SUMMARY_TARGET = THREAD_SUMMARY_SYNC_TARGET

#: Lanes retired by the RELEASE — nothing can feed them under any configuration.
#: `sink:archivist` stopped having a producer when techne#552 part 3 moved thread
#: summaries to the sync lane. Delivered rows stay (they must remain drainable), so the
#: lane keeps appearing in the outbox forever with a frozen MAX(created_at), and
#: anything judging liveness by "has this enqueued lately" calls it a dead producer.
_RELEASE_RETIRED_TARGETS = frozenset({ARCHIVIST_SINK_TARGET})


def _thread_summary_target(config) -> str:
    """The outbox lane for thread summaries.

    ALWAYS THE SYNC LANE NOW — techne#552 part 3, distilled-only egress.

    A thread summary is RAW operational memory. The chain the Archivist exists to
    serve is operational -> distill -> team knowledge, and §3 of
    memory-system-architecture.md says the Archivist "federates only the distilled" —
    but nothing enforced that at write time, so `sink:archivist` carried raw summaries
    across the seam whenever the instance was non-standalone. MEASURED on the author's
    store before this change: archivist.db held 17,819 observations against 1,104
    knowledge rows. RAW WAS 94% OF THE TEAM STORE.

    Durability and sharing are different concerns and this is where they were being
    conflated. Whether raw belongs somewhere else for durability is a separate open
    question (techne#552 leaves it open on purpose); what is settled is that it does
    not belong in the shared, federated, distilled tier.

    ARCHIVIST_SINK_TARGET IS NOT DELETED. 11,442 delivered rows sit in that lane on the
    author's box and must stay drainable and readable; `ensure_registered_for` still
    registers the sink for a store binding. This function simply stops FEEDING it.
    """
    return THREAD_SUMMARY_SYNC_TARGET


def _has_live_consumer(config) -> bool:
    """True when the SELECTED egress lane has a real drain — gate enqueue on it so a
    noop lane doesn't grow an undrainable backlog (#79, #224).

    Thread summaries now always take the `sync:` lane (see _thread_summary_target), so
    the only consumer that matters is the sync adapter — a `noop` adapter means nobody
    drains, and enqueueing into an undrainable lane is how a backlog grows that no one
    asked for (#79, #224).
    """
    return config.sync.adapter != "noop"


def retired_producer_targets(config) -> dict[str, str]:
    """Lanes with NO PRODUCER UNDER THE CURRENT BINDING, mapped to WHY (techne#715/#716).

    A PREDICATE, NOT A SET. techne#714 introduced a static set, correct for
    `sink:archivist` — nothing can feed it under any configuration — and wrong for
    `sink:archivist_knowledge`, fed only under a STORE binding, and for
    `sync:thread_summary`, fed only when a sync adapter is live. Both have no producer
    under a CONFIGURATION rather than under a RELEASE, which a static set cannot say.

    AND IT RETURNS THE REASON, NOT JUST MEMBERSHIP, which is the second half of the same
    lesson. The first version generalised the MECHANISM to three lanes while the caller
    still re-derived the message from `archivist_binding` — so on a store-bound box with
    a noop adapter the sync lane reported "no producer under binding=store", naming the
    one knob the operator had already set correctly and omitting the knob that actually
    retired it. `remedy` is None on these results, so evidence and detail ARE the entire
    actionable payload. A cause that travels with the lane cannot drift from it.
    (Prudence, #716 r2.)
    """
    from techne.core.archivist import (
        ARCHIVIST_KNOWLEDGE_TARGET,
        ARCHIVIST_STORE,
        archivist_binding,
    )

    reasons: dict[str, str] = {
        t: "retired by techne#552 — no producer by design, not a dead one"
        for t in _RELEASE_RETIRED_TARGETS
    }
    if archivist_binding(config) != ARCHIVIST_STORE:
        # Distilled knowledge egresses only to a SEPARATE store. Under `local` the team
        # knowledge IS this instance's knowledge, so there is nothing to push.
        reasons[ARCHIVIST_KNOWLEDGE_TARGET] = (
            f"no producer under archivist binding={archivist_binding(config)} — "
            "set archivist_db_path (or archivist: store) to revive it"
        )
    if not _has_live_consumer(config):
        # THE THIRD LANE, AND THE ONE THIS CHECK'S OWN DOCSTRING IS ABOUT (techne#716).
        # `sync:thread_summary`'s only producer is `run_session_end`, gated on
        # `_has_live_consumer` -> `sync.adapter != "noop"` — and noop is the DEFAULT.
        # Keyed on that function rather than restating it, so changing the gate moves
        # both the producer and this together.
        #
        # Its inclusion also resolved a self-contradiction: `check_outbox_targets`
        # asked this question in its declared-but-never-enqueued branch and not in the
        # discovered-rows loop, so the SAME lane under the SAME config reported
        # dormant/ok with zero rows and stale/ok=False with three drained ones — a
        # verdict about producers decided by history.
        reasons[THREAD_SUMMARY_SYNC_TARGET] = (
            # NO `or "noop"` FALLBACK: this block is gated on `not _has_live_consumer`,
            # which is `adapter == "noop"` exactly — None and "" both resolve as LIVE and
            # never reach here. The fallback read as if it guarded an unset adapter and
            # did not. (Prudence, #716 r3.)
            f"no producer under sync.adapter={config.sync.adapter!r} — "
            "configure a sync adapter to revive it"
        )
    return reasons


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
    from techne.core.state import close_abandoned_sessions, start_session

    init_db(config.db_path)

    # SELF-HEAL THE REPLAY INDEX (#516). `ended_at` has THREE writers: end_session
    # (state.py, stamps 'clean'), this sweep (stamps 'derived'), and
    # transcript_import (stamps 'derived'). Only the first is reached by a clean
    # exit, which an unclean one never gets to — so the column decayed to 7%
    # coverage in August and no time window could resolve to a session.
    # An earlier version of this comment said "one writer, run_session_end", while
    # calling a second one TEN LINES BELOW (#533 r5). The whole subject of this
    # PR is a miscounted writer set; do not re-narrow it without grepping by
    # EFFECT — INSERT, UPDATE, INSERT OR REPLACE — not by function name. Sweeping here means the
    # index repairs itself on ordinary use instead of needing anyone to remember a
    # maintenance verb, which is the failure mode #514 and #488 are both about.
    #
    # Safe to run with peers live: it only touches sessions silent for hours, so a
    # concurrent session cannot be closed by this one. Failure is non-fatal — a
    # missing ended_at is the status quo, and it must never cost a session start.
    try:
        close_abandoned_sessions(config.db_path)
    except Exception as e:  # pragma: no cover - defensive
        import sys

        print(f"techne: abandoned-session sweep failed: {e}", file=sys.stderr)

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


def persist_detection(
    config: Config,
    session_id: str,
    threads: list,
    observations: list[dict],
    *,
    scores=None,
    parent_links=None,
) -> int:
    """Replace a session's persisted detection — threads, boundary scores, and the
    reflection debt derived from them — in ONE transaction.

    ONE IMPLEMENTATION, TWO CALLERS (techne#646). `run_session_end` and
    `redetect_threads` both persist a detection, and they had drifted: redetect
    deleted `threads` AND `thread_boundaries` before saving, while session_end
    deleted only `thread_boundaries`. Its comment even reasons about the accumulation
    ("Replace, don't accumulate ... seen live: one session at 37k rows") and applies
    the fix to one table.

    WHY THAT ONE MISSING DELETE PRODUCED #646. `save_threads` writes with
    `INSERT OR IGNORE` on an id derived from `bucket_hash(session_id, first_obs_id,
    boundary_signal)`, and `run_session_end` fires ONCE PER TURN, re-detecting
    whenever the observation count moved. Shifted boundaries give the same logical
    thread a NEW id, the old row is never removed, and the session accumulates one
    superseded partition per re-detection (measured: 86 rows in `threads` against 51
    threads in the current detection). Reflection then reads the two apart —
    `load_session_for_reflection` re-detects from scratch while the queue holds ids
    from a partition that no longer exists — so the row is real debt whose prompt is
    impossible. `reflection_queue.thread_id` is `ON DELETE CASCADE` precisely to
    retire those rows; with nothing deleting the stale partition it never fired.

    THE RESTORE IS NOT OPTIONAL. The CASCADE takes DRAINED rows too, so a
    delete-and-re-enqueue without it resurrects paid debt as pending and makes the
    banner's "oldest from" wrong — techne#527 r2, fixed once in redetect and trivially
    reintroduced here. It rides the SAME transaction as the enqueue it corrects: a
    restore that can fail separately from the insert it repairs IS the r2 regression.

    Returns the number of reflection-queue rows newly inserted.

    Raises on failure, having rolled back. Callers must not paper over that: a
    half-applied detection (threads saved, debt not enqueued) is the #514 pathology,
    and nothing re-derives debt for an emittable thread with no queue row. The
    session_end hook already catches every exception from `run_session_end`, records a
    failed hook run and exits 0, so teardown is protected at that boundary rather than
    by swallowing this.
    """
    from techne.core.database import connect
    from techne.core.heuristic_threads import boundary_indices_from_segments, save_boundaries
    from techne.core.reflect import emittable_thread_ids, enqueue_reflection_debt
    from techne.core.threads import save_threads

    boundary_a_indices = (
        boundary_indices_from_segments([t.observation_indices for t in threads])
        if scores
        else None
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
        # Both the id lookup and the insert must ride THIS conn (#527 r5) —
        # persisted_thread_id used to open its own and could not see the uncommitted
        # rows, which made an inline enqueue raise a FOREIGN KEY failure.
        queued = enqueue_reflection_debt(
            config.db_path,
            session_id,
            thread_ids=emittable_thread_ids(config, session_id, threads, observations, conn=conn),
            conn=conn,
        )
        for tid, drained in prior_drained.items():
            conn.execute(
                "UPDATE reflection_queue SET drained_at = ? WHERE thread_id = ?",
                (drained, tid),
            )
        conn.commit()
        return queued
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    from techne.core.state import end_session, get_observations
    from techne.core.threads import (
        age_threads,
        detect_summarize_with_metadata,
        format_combined_summary,
        link_threads,
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

    # Replace the whole detection — threads, boundaries and the debt derived from
    # them — through the ONE helper redetect_threads also uses (#646). This path
    # used to DELETE thread_boundaries and then `save_threads` on top of whatever
    # was already there, so every re-detection left a superseded partition behind.
    queued_for_reflection = persist_detection(
        config,
        session_id,
        threads,
        observations,
        scores=scores,
        parent_links=parent_links,
    )
    if scores:
        # Persist the observation count processed so external tooling (and future
        # incremental passes) can tell how far the detector has run. Separate write:
        # it is a cursor about the detector, not part of the detection itself.
        conn = connect(config.db_path)
        try:
            set_state(
                conn,
                f"heuristic_threads.{session_id}",
                {"last_obs_count": len(observations)},
            )
        finally:
            conn.close()

    # techne#514: record the reflection debt DURABLY, not just in the log line
    # below. Until now this function counted threads, printed "N obs, M threads"
    # and enqueued nothing — so reflection survived only the polite exit path,
    # and every crash/timeout dropped it silently. Best-effort: a queue-write
    # failure must never take down teardown, which is the one thing that still
    # has to persist the session's actual work.
    # The enqueue now rides persist_detection's transaction (#646), so it is atomic
    # with the thread write rather than a best-effort follow-up. It used to be
    # wrapped so a queue failure "must never take down teardown" — but the
    # session_end HOOK already catches everything from run_session_end, records a
    # failed hook run and exits 0, so that protection was doubled up, and the cost
    # was a window where threads were saved and the debt silently was not: the exact
    # #514 pathology, since nothing re-derives debt for a thread with no queue row.

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
    # (which may have promoted new live knowledge). No-op unless bound to a store; the
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

    from techne.core.state import get_observations, set_session_summary
    from techne.core.threads import (
        detect_summarize_with_metadata,
        format_combined_summary,
        link_threads,
        thread_row_id,
    )
    from techne.sync.outbox import enqueue

    observations = get_observations(config.db_path, session_id)
    threads, scores = detect_summarize_with_metadata(observations, config)

    # Compute parent links before opening the write connection (link_threads
    # does read-only FTS queries on its own connection; no conflict).
    parent_links = link_threads(threads, session_id, config.db_path, config)

    # ONE implementation, shared with run_session_end (#646). This path always
    # replaced both tables; session_end replaced only thread_boundaries, and that
    # asymmetry is what accumulated superseded partitions. Keeping them on one
    # function is the point — a two-implementation rail drifts on its BOOKKEEPING
    # first, because that half is invisible to whoever runs it.
    persist_detection(
        config,
        session_id,
        threads,
        observations,
        scores=scores,
        parent_links=parent_links,
    )

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
