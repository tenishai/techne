"""Deterministic synthetic corpus for the recall-effectiveness eval (#68).

Builds a small, frozen techne store with known gold evidence + distractors so
retrieval scoring is reproducible and portable — never the live
~/.techne/techne.db (which mutates every session and carries real content).

`seed()` returns a mapping of logical evidence keys -> "type:id" gold
references, which recall_baseline.yaml resolves via each task's `gold_keys`.
Pinning gold by returned id (not a hardcoded number) keeps the baseline robust
if insertion order ever changes.
"""

from pathlib import Path

from techne.core.database import connect, init_db
from techne.core.knowledge import _add_dep_in_conn, delete, store, supersede

_SESSION = "eval-sess"


def _add_observation_raw(conn, content: str, tool_name: str = "Edit") -> int:
    """Insert an observation via direct SQL (bypasses embedding inference in
    add_observation) so the corpus is deterministic and dependency-free. The
    observations_fts index is populated by the table's INSERT trigger."""
    cur = conn.execute(
        "INSERT INTO observations (session_id, content, tool_name) VALUES (?, ?, ?)",
        (_SESSION, content, tool_name),
    )
    return cur.lastrowid


def seed(db_path: Path) -> dict[str, str]:
    """Build the frozen eval corpus. Returns logical-key -> 'type:id' gold map."""
    init_db(db_path).close()
    gold: dict[str, str] = {}

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, CURRENT_TIMESTAMP)",
            (_SESSION,),
        )
        conn.commit()
    finally:
        conn.close()

    # --- GOLD knowledge -----------------------------------------------------
    gold["cabal14"] = "knowledge:%d" % store(
        db_path,
        "cabal#14 settled the archivist outbox sink as a receiver-side distill "
        "with mailbox-as-tag-slice delivery; chosen over per-target fan-out "
        "because tag-slicing reuses the existing outbox table.",
        type="decision",
        tags="cabal,outbox",
        source="eval",
    )
    gold["reflect_candidates"] = "knowledge:%d" % store(
        db_path,
        "The end-of-session reflect pass writes proposals to the "
        "knowledge_candidates table for human review; it is off by default and "
        "never blocks session teardown.",
        type="decision",
        tags="reflect,candidates",
        source="eval",
    )
    gold["canonical_remote"] = "knowledge:%d" % store(
        db_path,
        "techne's canonical git origin is github.com/tenishai/techne. The gitea "
        "remote was dropped on 2026-06-17 — github is canonical, gitea is a dead "
        "archive on a divergent v0.5.0 line.",
        type="decision",
        tags="git,infra",
        source="eval",
    )
    gold["platform_state"] = "knowledge:%d" % store(
        db_path,
        "Platform-state delta since 2026-06-15: mailbox infra designed across "
        "techne#22 and cabal#14; the rookery-mcp inter-agent mailbox shipped on "
        "dev02; the techne open queue was cleared.",
        type="fact",
        tags="platform-state",
        source="eval",
    )

    # --- DISTRACTOR knowledge (share words with the gold queries) -----------
    store(db_path, "cabal#11 AbortGraph handles transitive parent->child "
          "cancellation across nested dispatch.", type="decision", tags="cabal", source="eval")
    store(db_path, "cabal#12 covers A2A streaming foundations: message/stream and "
          "tasks/resubscribe with cursor-based replay.",
          type="decision", tags="cabal", source="eval")
    store(db_path, "The MMR reranker adds diversity to search results via a greedy "
          "second pass over BM25 candidates.", type="fact", tags="search", source="eval")
    store(db_path, "Outbox routing should close the flush/flush_sinks race via "
          "enqueue-time target namespacing.", type="decision", tags="outbox", source="eval")

    # --- GOLD: tracking (supersede chain → only the latest revision is live) ---
    # techne#118: the deterministic acceptance test for #90 supersession. search()
    # filters `superseded_by IS NULL` (search.py), so a query for the current value
    # must return v3 and NEVER the stale v1/v2. A regression that breaks supersession
    # filtering would surface a stale revision and fail this task's must_exclude.
    _v1 = store(db_path, "The eval-stack deploy target is host dev01-old (initial).",
                type="fact", tags="deploy", source="eval")
    _v2 = supersede(db_path, _v1, "The eval-stack deploy target is host dev02-mid (migrated).")
    _v3 = supersede(db_path, _v2, "The eval-stack deploy target is host dev03-current (live).")
    gold["deploy_target"] = "knowledge:%d" % _v3

    # --- GOLD: aggregation (one topic, N pieces — ALL must be recalled) ---
    gold["agg_step1"] = "knowledge:%d" % store(
        db_path, "Eval rollout runbook step one: freeze the synthetic corpus and tag the baseline.",
        type="procedure", tags="runbook", source="eval")
    gold["agg_step2"] = "knowledge:%d" % store(
        db_path, "Eval rollout runbook step two: run the kill-gate and compare to the "
        "frozen aggregate.", type="procedure", tags="runbook", source="eval")
    gold["agg_step3"] = "knowledge:%d" % store(
        db_path, "Eval rollout runbook step three: archive last_run.jsonl and refresh PHASE0.",
        type="procedure", tags="runbook", source="eval")
    # same-topic distractor so aggregation isn't a ceiling task (partial recall reachable)
    store(db_path, "Draft eval runbook (abandoned): the single-pass approach was replaced "
          "by the three-step rollout.", type="procedure", tags="runbook", source="eval")

    # --- GOLD + DISTRACTOR observations -------------------------------------
    conn = connect(db_path)
    try:
        gold["adapter_touch"] = "observation:%d" % _add_observation_raw(
            conn,
            "Edited: techne/sync/adapter.py — relocate SourceAdapter Protocol "
            "to adapter.py (closes #32)",
        )
        _add_observation_raw(
            conn, "Edited: techne/core/search.py — OR fallback for empty "
            "implicit-AND FTS queries"
        )
        _add_observation_raw(conn, "Ran pytest tests/test_search.py — 44 passed", tool_name="Bash")
        _add_observation_raw(
            conn, "Read: techne/core/redaction.py credential patterns", tool_name="Read")

        # --- GOLD thread (direct SQL with a fixed id; threads_ai trigger
        #     populates threads_fts) ---
        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-reflect", _SESSION, 1,
                "Reflect experiment observation window",
                "Tracking the experiment/reflect-pass branch; reflect.enabled "
                "stays false during the two-week observation window before any "
                "candidate is promoted to live knowledge.",
                "Last action: flipped reflect.write_to=candidates",
            ),
        )
        gold["reflect_thread"] = "thread:eval-thread-reflect"

        # distractor thread
        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-search", _SESSION, 2,
                "Search reranker work",
                "Implemented MMR diversity and the OR fallback in the FTS path.",
                "Last action: merged #70",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # -----------------------------------------------------------------------
    # Stage-2 negative-polarity seeds (techne#118 PR3+)
    # -----------------------------------------------------------------------

    # --- deletion (`deleted-fact`): store then immediately hard-delete so the
    #     row is absent from live recall.  The answerer must not surface it.
    _scratch_id = store(
        db_path,
        "Scratch note: the abandoned vector index used a custom FAISS wrapper "
        "with an in-process index; the experiment was scrapped in favour of "
        "sqlite-vec (techne#19). [EVAL SCRATCH — to be deleted]",
        type="fact",
        tags="vector,index,scratch",
        source="eval",
    )
    delete(db_path, _scratch_id)
    # _scratch_id is NOT added to gold — the answer for deleted-fact should
    # be "I don't know" / "it's been deleted".

    # --- cascade (`cascade-uncertain`): seed a trigger fact + a derived fact
    #     that depends on it, then supersede the trigger so the derived row
    #     goes status='uncertain' via the cascade (#117). Recall annotates it
    #     uncertain; the answerer should flag uncertainty rather than asserting
    #     the derived value as current.
    _trigger_id = store(
        db_path,
        "Eval deploy pipeline config (v1): deploy path is /opt/eval/v1 on dev02.",
        type="fact",
        tags="deploy,eval,pipeline",
        source="eval",
    )
    _derived_id = store(
        db_path,
        "Derived: the eval stack log archive is at /opt/eval/v1/logs — "
        "inferred from the pipeline deploy path (eval deploy path v1 config).",
        type="fact",
        tags="deploy,eval,logs",
        source="eval",
    )
    # Write dep edge via _add_dep_in_conn (add_dependency removed as dead code, #251).
    _conn = connect(db_path)
    try:
        _add_dep_in_conn(_conn, _derived_id, _trigger_id)
        _conn.commit()
    finally:
        _conn.close()
    # Superseding the trigger cascades `uncertain` to the derived row.
    supersede(
        db_path,
        _trigger_id,
        "Eval deploy pipeline config (v2): deploy path is /opt/eval/v2 on dev03 "
        "(migrated from dev02 in the infra reshuffle).",
        source="eval",
    )
    # _derived_id is live (superseded_by IS NULL, status='uncertain') — search
    # returns it with an uncertainty annotation; the answerer must flag that.
    # Not in gold_keys for the cascade task (gold_keys=[]) because the assertion
    # is about the *answer quality* (flagging uncertainty), not retrieval.

    # --- absence (`absent-feature`): intentionally NO seed data.
    #     Techne does NOT have auto-bisect functionality; the absence IS the
    #     test.  A model with no relevant memory should reply "I don't know."
    #     Adding a row here would defeat the task (it would be a positive hint).

    return gold
