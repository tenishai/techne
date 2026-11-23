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


def _add_observation_raw(conn, content: str, tool_name: str = "Edit", tags: str = "") -> int:
    """Insert an observation via direct SQL (bypasses embedding inference in
    add_observation) so the corpus is deterministic and dependency-free. The
    observations_fts index is populated by the table's INSERT trigger.

    `tags` exists so scale padding can be counted by a marker of its own
    (techne#945). Counting it by `tool_name` instead looked fine and was wrong:
    `seed()` writes a genuine `Read` observation, so a `tool_name='Read'` count
    returned 2001 for 2000 pad rows — caught by this arm's own instrument
    assertion, which is what those assertions are for.
    """
    cur = conn.execute(
        "INSERT INTO observations (session_id, content, tool_name, tags) VALUES (?, ?, ?, ?)",
        (_SESSION, content, tool_name, tags),
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
        "host-b; the techne open queue was cleared.",
        type="fact",
        tags="platform-state",
        source="eval",
    )

    # --- DISTRACTOR knowledge (share words with the gold queries) -----------
    store(
        db_path,
        "cabal#11 AbortGraph handles transitive parent->child cancellation across nested dispatch.",
        type="decision",
        tags="cabal",
        source="eval",
    )
    store(
        db_path,
        "cabal#12 covers A2A streaming foundations: message/stream and "
        "tasks/resubscribe with cursor-based replay.",
        type="decision",
        tags="cabal",
        source="eval",
    )
    store(
        db_path,
        "The MMR reranker adds diversity to search results via a greedy "
        "second pass over BM25 candidates.",
        type="fact",
        tags="search",
        source="eval",
    )
    store(
        db_path,
        "Outbox routing should close the flush/flush_sinks race via "
        "enqueue-time target namespacing.",
        type="decision",
        tags="outbox",
        source="eval",
    )

    # --- GOLD: tracking (supersede chain → only the latest revision is live) ---
    # techne#118: the deterministic acceptance test for #90 supersession. search()
    # filters `superseded_by IS NULL` (search.py), so a query for the current value
    # must return v3 and NEVER the stale v1/v2. A regression that breaks supersession
    # filtering would surface a stale revision and fail this task's must_exclude.
    _v1 = store(
        db_path,
        "The eval-stack deploy target is host host-old (initial).",
        type="fact",
        tags="deploy",
        source="eval",
    )
    _v2 = supersede(db_path, _v1, "The eval-stack deploy target is host host-mid (migrated).")
    _v3 = supersede(db_path, _v2, "The eval-stack deploy target is host host-current (live).")
    gold["deploy_target"] = "knowledge:%d" % _v3

    # --- GOLD: aggregation (one topic, N pieces — ALL must be recalled) ---
    gold["agg_step1"] = "knowledge:%d" % store(
        db_path,
        "Eval rollout runbook step one: freeze the synthetic corpus and tag the baseline.",
        type="procedure",
        tags="runbook",
        source="eval",
    )
    gold["agg_step2"] = "knowledge:%d" % store(
        db_path,
        "Eval rollout runbook step two: run the kill-gate and compare to the frozen aggregate.",
        type="procedure",
        tags="runbook",
        source="eval",
    )
    gold["agg_step3"] = "knowledge:%d" % store(
        db_path,
        "Eval rollout runbook step three: archive last_run.jsonl and refresh PHASE0.",
        type="procedure",
        tags="runbook",
        source="eval",
    )
    # same-topic distractor so aggregation isn't a ceiling task (partial recall reachable)
    store(
        db_path,
        "Draft eval runbook (abandoned): the single-pass approach was replaced "
        "by the three-step rollout.",
        type="procedure",
        tags="runbook",
        source="eval",
    )

    # --- GOLD + DISTRACTOR observations -------------------------------------
    conn = connect(db_path)
    try:
        gold["adapter_touch"] = "observation:%d" % _add_observation_raw(
            conn,
            "Edited: techne/sync/adapter.py — relocate SourceAdapter Protocol "
            "to adapter.py (closes #32)",
        )
        _add_observation_raw(
            conn, "Edited: techne/core/search.py — OR fallback for empty implicit-AND FTS queries"
        )
        _add_observation_raw(conn, "Ran pytest tests/test_search.py — 44 passed", tool_name="Bash")
        _add_observation_raw(
            conn, "Read: techne/core/redaction.py credential patterns", tool_name="Read"
        )

        # --- GOLD thread (direct SQL with a fixed id; threads_ai trigger
        #     populates threads_fts) ---
        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-reflect",
                _SESSION,
                1,
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
                "eval-thread-search",
                _SESSION,
                2,
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
        "Eval deploy pipeline config (v1): deploy path is /opt/eval/v1 on host-b.",
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
        "Eval deploy pipeline config (v2): deploy path is /opt/eval/v2 on host-b "
        "(migrated from host-b in the infra reshuffle).",
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

    # =========================================================================
    # #385 gold block — retrieval-correctness regression gate
    # (tests/eval/retrieval_gold.yaml + test_retrieval_gate.py)
    #
    # APPEND-ONLY: nothing above this line is touched. New rows only, new
    # logical gold keys only — the #68/#118 frozen baselines must not shift.
    # Vocabulary is deliberately disjoint from the #68/#118 corpus above
    # (no "cabal"/"outbox"/"reflect"/"deploy"/"runbook"/"adapter" terms) so
    # BM25 document-frequency stats for those queries stay unperturbed.
    #
    # Four query classes, each exercised via search(..., use_vectors=False):
    #   exact       — query shares literal tokens with the gold doc
    #   paraphrase  — query shares ZERO literal (post-porter-stem) tokens
    #                 with the gold doc; a natural-language rephrase
    #   filter      — gold doc reachable only when table= narrows correctly;
    #                 a same-text distractor lives in a different table
    #   negative    — a lexically-tempting distractor (deleted or a stale
    #                 superseded revision) that must NEVER surface
    # =========================================================================

    # --- exact + paraphrase: one gold knowledge doc per topic serves BOTH an
    #     exact-token query and a zero-overlap paraphrase query (retrieval_gold.yaml).
    gold["g385_ratelimiter"] = "knowledge:%d" % store(
        db_path,
        "The public API enforces a token bucket rate limiter: each client key "
        "refills at 100 tokens per minute, capping burst traffic.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )
    gold["g385_cacheinvalidation"] = "knowledge:%d" % store(
        db_path,
        "Cache invalidation uses a write-through strategy so every update lands "
        "in the database and the cache simultaneously, avoiding stale reads.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )
    gold["g385_circuitbreaker"] = "knowledge:%d" % store(
        db_path,
        "Circuit breaker trips open after five consecutive downstream failures "
        "and stays open for thirty seconds before allowing a trial request.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )
    gold["g385_retrybackoff"] = "knowledge:%d" % store(
        db_path,
        "Retry backoff uses exponential delay with full jitter, doubling the "
        "wait time after each failed attempt up to a sixty second cap.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )
    gold["g385_tlsrotation"] = "knowledge:%d" % store(
        db_path,
        "TLS certificate rotation runs automatically every ninety days and "
        "reloads the web server without dropping active connections.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )
    gold["g385_dbpool"] = "knowledge:%d" % store(
        db_path,
        "Database connection pooling caps the pool at twenty five connections "
        "per service and recycles idle connections after ten minutes.",
        type="decision",
        tags="g385,exact",
        source="eval",
    )

    # --- filter: gold + a same-text distractor in a DIFFERENT table. The
    #     distractor is structurally unreachable when the task's table= param
    #     is passed through, which is exactly the mechanism under test. Two
    #     pairs per table direction (knowledge<->observations, observations
    #     <->knowledge, threads<->knowledge).
    # --- lexical-only gold (techne#935) -------------------------------------
    # A POSITIVE task at table="all" whose gold is reachable ONLY through the
    # LEXICAL channel. The eval_vec gate could not observe the fusion tail
    # without one: `search()` builds `head` from vec_results and computes
    # `fts_only` by EXCLUDING every key already in vec_keys, so the tail can only
    # ever hold rows the vector channel did not return. Instrumented before this
    # row existed, `_curated_first` REORDERED NOTHING across the whole gate.
    # Instrumented by wrapping it and replaying every task: it is called ONCE PER
    # TASK (25 calls, 9 of them seeing a non-empty lexical tail) and reordered 0
    # of 25. An earlier note said "called 6 times", which was wrong -- it is
    # reached unconditionally whenever the vector channel returns anything.
    #
    # WHICH HALF OF THE TAIL THIS ACTUALLY COVERS, because the two are not the
    # same and an earlier header ran them together (Prudence, techne#935):
    #
    #   THE RESERVATION (techne#827/#831) IS COVERED. Reverting `_fts_floor` to
    #   `return 0` takes lexical r@10 from 1.0 to 0.0 and fails the gate.
    #
    #   THE CURATED-VS-RAW ORDERING (techne#934) IS NOT. Reverting `_curated_first`
    #   to `return fts_only` leaves both gates GREEN. This task's tail is
    #   `['knowledge']` -- a single curated row, so the partition is still a
    #   structural no-op and the totals are unchanged from base: 0 reorders of 25,
    #   0 mixed tails. Covering that needs a task whose tail is MIXED, which this
    #   one is not.
    #
    # WHY THE FILLER, AND WHY THIS MUCH OF IT. The pinned backend is
    # model2vec/potion-base-8M, a STATIC TOKEN-EMBEDDING AVERAGE, so a short row
    # containing the query's rare token scores well on the VECTOR channel too --
    # measured: the same identifier in a one-sentence row arrives `fts+vec` at
    # rank 1 and never reaches the tail. Diluting the identifier among enough
    # unrelated tokens drops its contribution to the averaged vector until the
    # vector channel stops returning the row, while FTS still matches the literal
    # token exactly.
    #
    # THE MULTIPLIER IS EMPIRICAL, AND A PRE-PASS SHOWED THE FIRST ACCOUNT OF IT
    # WAS WRONG. That account gave a table of multipliers against match_source and
    # called x6 "mid-band":
    #
    #     x3 fts+vec | x4 fts+vec | x5 fts | x6 fts | x8 fts+vec
    #
    # x7 was never measured, so "middle" was the unmeasured claim used to justify
    # the chosen value. Measuring it exposed something larger: re-running the sweep
    # with a DIFFERENT identifier string gave fts+vec at x5, x6, x7 AND x8. The
    # fts-only property is therefore a JOINT property of the identifier and the
    # filler, not a function of the multiplier, and there is no "band" to sit in
    # the middle of.
    #
    # WHAT IS ACTUALLY ESTABLISHED, and it is enough: THIS row -- this identifier
    # at this dilution against this corpus and this pinned model -- returns
    # match_source="fts" at rank 10, verified repeatedly. Nothing generalises from
    # that to other identifiers, and the gate does not need it to.
    #
    # WHICH IS EXACTLY WHY THE GATE ASSERTS match_source == "fts" DIRECTLY. A
    # property this contingent cannot be protected by a recall number; if any
    # change moves the row onto the vector channel the task silently stops testing
    # the tail, and only the channel assertion notices.
    #
    # THE FILLER'S TOPIC IS DELIBERATELY FAR FROM EVERY OTHER TASK (techne#1083).
    # The first filler was infrastructure prose (service mesh, TLS, "connection
    # pooling ... sixty four sockets", retries, metrics): the same vocabulary as
    # the #385 golds. Its averaged vector landed on the page of 18 other tasks,
    # at rank 1 for paraphrase-dbpool, pushing that gold from vector rank 9 to
    # 10, where the reserved lexical slot took it off the page. paraphrase-dbpool
    # read 0.0 from #977 until this change, and the 0.8 floor hid it.
    #
    # Replaced with sailing prose that shares no topic with any gold. Measured
    # with the same pinned model: at x4, x5, x6 and x8 this row still arrives
    # match_source="fts" at rank 10 via lexical_tail for CHG-7742-KM, and it
    # appears on NO other task's page. paraphrase-dbpool is back at rank 9, 1.0.
    # Keep it that way: a filler that reads like any gold's topic will compete
    # with that gold on the vector channel.
    _G935_FILLER = (
        "The sailboat left the harbour at dawn with a light westerly breeze and a "
        "crew of four. By noon the coastline had faded into haze and dolphins "
        "followed the bow wave for an hour. The skipper reefed the mainsail when the "
        "swell grew and the galley served lentil stew with bread. At dusk the "
        "lighthouse on the headland blinked green twice every ten seconds. They "
        "anchored in a sheltered cove, rowed the dinghy ashore and gathered "
        "driftwood for a beach fire. Stars filled the sky and gulls slept on the "
        "rocks below the cliffs. "
    )
    gold["g935_lexical_ticket"] = "knowledge:%d" % store(
        db_path,
        _G935_FILLER * 6 + "The rollback that restored checkout throughput is recorded "
        "under change ticket CHG-7742-KM, applied during the Tuesday freeze window.",
        type="decision",
        tags="g935,lexical",
        source="eval",
    )

    gold["g385_filter_batch"] = "knowledge:%d" % store(
        db_path,
        "The nightly batch compaction job reclaims deleted row space in the "
        "archive table and runs at 03:00 UTC.",
        type="decision",
        tags="g385,filter",
        source="eval",
    )
    gold["g385_filter_logretention"] = "knowledge:%d" % store(
        db_path,
        "Log retention policy keeps application logs for fourteen days in hot "
        "storage before rolling to cold archive storage.",
        type="decision",
        tags="g385,filter",
        source="eval",
    )
    # distractors for the two knowledge-gold docs above go in observations;
    # the two observations-gold docs + two threads-gold docs below carry
    # their own distractors in knowledge (added after this conn block).
    conn = connect(db_path)
    try:
        _batch_distractor_id = _add_observation_raw(
            conn,
            "Ran: nightly batch compaction job -- reclaimed deleted row space "
            "in archive table at 03:00 UTC (dry run)",
        )
        _logretention_distractor_id = _add_observation_raw(
            conn,
            "Checked: log retention policy keeps logs fourteen days in hot "
            "storage before rolling to cold archive (verified)",
        )

        # --- filter: gold in observations, distractor in knowledge ---
        gold["g385_filter_slidingwindow"] = "observation:%d" % _add_observation_raw(
            conn,
            "Edited: ratelimit module -- added sliding window counter for burst control",
        )
        gold["g385_filter_testsuite"] = "observation:%d" % _add_observation_raw(
            conn,
            "Ran pytest ratelimit test suite -- twelve passed, sliding window counter verified",
            tool_name="Bash",
        )

        # --- filter: gold in threads, distractor in knowledge ---
        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-g385-flagrollout",
                _SESSION,
                3,
                "Feature flag rollout percentage gating",
                "Tracking the staged rollout of feature flag percentage "
                "gating; starting at five percent and ramping to one hundred "
                "percent over two weeks pending error rate checks.",
                "Last action: ramped to twenty five percent",
            ),
        )
        gold["g385_filter_flagrollout"] = "thread:eval-thread-g385-flagrollout"

        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-g385-queuedepth",
                _SESSION,
                4,
                "Queue depth backpressure alerting",
                "Tuning the queue depth backpressure alert threshold; alarms "
                "fire when depth exceeds five hundred messages for over two "
                "minutes.",
                "Last action: raised threshold to five hundred",
            ),
        )
        gold["g385_filter_queuedepth"] = "thread:eval-thread-g385-queuedepth"
        conn.commit()
    finally:
        conn.close()

    gold["g385_filter_slidingwindow_distractor"] = "knowledge:%d" % store(
        db_path,
        "Sliding window counter for burst control was added to the ratelimit "
        "module, replacing the old fixed window approach.",
        type="decision",
        tags="g385,filter",
        source="eval",
    )
    gold["g385_filter_testsuite_distractor"] = "knowledge:%d" % store(
        db_path,
        "The sliding window counter test suite has twelve passing cases verifying burst control.",
        type="fact",
        tags="g385,filter",
        source="eval",
    )
    gold["g385_filter_flagrollout_distractor"] = "knowledge:%d" % store(
        db_path,
        "Feature flag rollout percentage gating starts at five percent and "
        "ramps to one hundred percent over two weeks pending error rate checks.",
        type="decision",
        tags="g385,filter",
        source="eval",
    )
    gold["g385_filter_queuedepth_distractor"] = "knowledge:%d" % store(
        db_path,
        "Queue depth backpressure alert threshold fires when depth exceeds "
        "five hundred messages for over two minutes.",
        type="fact",
        tags="g385,filter",
        source="eval",
    )
    gold["g385_filter_batch_distractor"] = "observation:%d" % _batch_distractor_id
    gold["g385_filter_logretention_distractor"] = "observation:%d" % _logretention_distractor_id

    # --- negative (deletion): store then hard-delete. A regression that drops
    #     the delete-filter would resurface these via pure keyword overlap.
    gold["g385_neg_del_backup"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Backup schedule policy runs a nightly full backup at 02:00 UTC "
            "via pg_dump uploaded to the S3 cold bucket.",
            type="fact",
            tags="g385,scratch",
            source="eval",
        )
    )
    delete(db_path, _del_id)

    gold["g385_neg_del_authmigration"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Auth provider migration replaced Okta with WorkOS for SSO login starting Q1 2026.",
            type="decision",
            tags="g385,scratch",
            source="eval",
        )
    )
    delete(db_path, _del_id)

    gold["g385_neg_del_stagingregion"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Staging environment region moved from AWS us-west-1 to us-east-2 "
            "for latency improvements.",
            type="fact",
            tags="g385,scratch",
            source="eval",
        )
    )
    delete(db_path, _del_id)

    # --- negative (supersession): the OLD revision is lexically closest to
    #     the query (that's the trap) but must never surface once superseded;
    #     the NEW revision is the live, correct answer.
    _oncall_v1 = store(
        db_path,
        "On-call rotation cadence is weekly, rotating every Monday at 09:00.",
        type="fact",
        tags="g385,oncall",
        source="eval",
    )
    gold["g385_neg_sup_oncall_old"] = "knowledge:%d" % _oncall_v1
    _oncall_v2 = supersede(
        db_path,
        _oncall_v1,
        "On-call rotation cadence is now biweekly, rotating every other Monday at 09:00.",
        source="eval",
    )
    gold["g385_neg_sup_oncall_new"] = "knowledge:%d" % _oncall_v2

    _redis_v1 = store(
        db_path,
        "Primary datastore for session state is Redis single-node.",
        type="fact",
        tags="g385,redis",
        source="eval",
    )
    gold["g385_neg_sup_redis_old"] = "knowledge:%d" % _redis_v1
    _redis_v2 = supersede(
        db_path,
        _redis_v1,
        "Primary datastore for session state is Redis Cluster with three "
        "shards for high availability.",
        source="eval",
    )
    gold["g385_neg_sup_redis_new"] = "knowledge:%d" % _redis_v2

    _ci_v1 = store(
        db_path,
        "CI runner pool size is 10 concurrent workers.",
        type="fact",
        tags="g385,ci",
        source="eval",
    )
    gold["g385_neg_sup_ci_old"] = "knowledge:%d" % _ci_v1
    _ci_v2 = supersede(
        db_path,
        _ci_v1,
        "CI runner pool size is 25 concurrent workers after the autoscaling upgrade.",
        source="eval",
    )
    gold["g385_neg_sup_ci_new"] = "knowledge:%d" % _ci_v2

    _seed_identifier_rows(db_path, gold)
    _seed_phrase_rows(db_path, gold)
    return gold


# --- identifier (techne#1078) -------------------------------------------------
# Exact-identifier lookups whose gold the vec-first page used to bury. Each task has
# near-topic distractors (short rows on the same subject, no identifier) and a gold
# that carries the identifier after filler, the same dilution as g935. That is the
# live-store shape: a long observation holding the exact token loses on cosine
# distance to short on-topic rows. Measured with identifier_head_slots=0 (the
# pre-#1078 page): cfgkey had one gold in the lexical_tail slot and the other off
# the page, sha and flag were off the page. With the default both cfgkey golds sit
# at 1-2 and sha and flag at 1. Rows go in OBSERVATIONS so the table="knowledge"
# classes (exact, paraphrase, two filter tasks) see an unchanged corpus. The filler
# avoids every word of the table="all" negative queries: an earlier draft said
# "weekly", sent negative-stale-oncall to its OR fallback on the FTS-only gate, and
# took that task's MRR from 1.0 to 0.5.
_G1078_FILLER = (
    "Ran the housekeeping pass across the build hosts. Disk usage on the artifact "
    "volume dropped after old images were pruned. Queued the dependency refresh and "
    "left a note for the next shift about the flaky integration job. "
)
_G1078_VERBS = (
    "stalled",
    "recovered",
    "was paused",
    "was retried",
    "slowed down",
    "finished early",
    "was rolled back",
    "was reviewed",
)
_G1078_FOLLOWUPS = (
    "changed",
    "raised",
    "reverted",
    "tested",
    "reviewed",
    "rolled out",
    "documented",
    "audited",
)
#: key -> (gold rows, distractor template). The queries live in retrieval_gold.yaml.
_G1078_TASKS = {
    "cfgkey": (
        [
            "Edge proxy timeouts traced to retry_budget_ms being left at the default "
            "in the gateway settings.",
            "Checked retry_budget_ms on the gateway edge proxy after the timeouts cleared.",
        ],
        "The gateway edge proxy timeouts {v} after the retry settings were {w}.",
    ),
    "sha": (
        ["The dashboard rollout was merged as 4e8a1f3 and deployed the same afternoon."],
        "The dashboard rollout {v} once the merge was {w}.",
    ),
    "flag": (
        ["Deploys now pass --skip-warmup so the cache warmup does not block the rollout."],
        "The cache warmup during deploy {v} and the team {w} it.",
    ),
}


def _seed_identifier_rows(db_path: Path, gold: dict[str, str]) -> None:
    conn = connect(db_path)
    try:
        for key, (golds, template) in _G1078_TASKS.items():
            for n, text in enumerate(golds):
                suffix = "" if len(golds) == 1 else "_" + "ab"[n]
                gold[f"g1078_{key}{suffix}"] = "observation:%d" % _add_observation_raw(
                    conn, _G1078_FILLER * 4 + text, tool_name="Bash"
                )
            for v, w in zip(_G1078_VERBS, _G1078_FOLLOWUPS):
                _add_observation_raw(conn, template.format(v=v, w=w))
        conn.commit()
    finally:
        conn.close()


# --- exact phrase (techne#1142) -----------------------------------------------
# The two queries from the issue, on rows built to the live-store shape: the row
# holding the quoted phrase is long (the #1078 filler), so twelve short on-topic
# rows per task crowd it out of the vector channel and it arrives through FTS only.
# Measured with the seat off (the pre-#1142 page, and every per-turn page):
# `pypi` sits in the #827 lexical_tail slot at rank 10, as 47085 did on the
# 2026-09-28 snapshot; `litestream` is off the page, because a stronger FTS row
# holding the same four words apart (`_G1142_TAIL_RIVAL`) takes that slot, as a
# thread row did on the snapshot. With the seat on both sit at rank 2 via
# `phrase_head`. Observations only, so the table="knowledge" classes see an
# unchanged corpus; the words avoid the table="all" negative queries (see the
# #1078 note above).
_G1142_VERBS = _G1078_VERBS + ("was restarted", "was upgraded", "was patched", "was rebuilt")
_G1142_FOLLOWUPS = _G1078_FOLLOWUPS + ("pinned", "trimmed", "renamed", "merged")
_G1142_TASKS = {
    "litestream": (
        "Started the replication sidecar: exec litestream replicate -config "
        "/etc/litestream.yml, watching techne.db.",
        "The litestream sidecar for techne.db {v} after the replication settings were {w}.",
    ),
    "pypi": (
        "pip install 'techne[vec]' printed: WARNING: techne 0.17.0 does not provide "
        "the extra 'vec'.",
        "The optional vector extra for techne {v} after the packaging metadata was {w}.",
    ),
}
#: Holds `exec`, `techne.db`, `litestream` and `replicate` but never two of them
#: side by side. Shorter than the gold, so FTS ranks it first, and its sailing
#: filler keeps it out of the vector channel.
_G1142_TAIL_RIVAL = (
    "The sailboat left the harbour at dawn with a light westerly breeze and a crew of "
    "four. By noon the coastline had faded into haze and dolphins followed the bow "
    "wave for an hour. "
) * 2 + (
    "Loose ends: exec bit fixed on the hook, techne.db vacuumed, litestream docs "
    "skimmed, and we may replicate the config later."
)


def _seed_phrase_rows(db_path: Path, gold: dict[str, str]) -> None:
    conn = connect(db_path)
    try:
        for key, (text, template) in _G1142_TASKS.items():
            gold[f"g1142_{key}"] = "observation:%d" % _add_observation_raw(
                conn, _G1078_FILLER * 4 + text, tool_name="Bash"
            )
        for v, w in zip(_G1142_VERBS, _G1142_FOLLOWUPS):
            for _, template in _G1142_TASKS.values():
                _add_observation_raw(conn, template.format(v=v, w=w))
        _add_observation_raw(conn, _G1142_TAIL_RIVAL)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# SCALE PADDING (techne#883)
# --------------------------------------------------------------------------

#: Combinatorial parts for synthetic distractors, in TWO vocabularies.
#:
#: `near` is drawn from THIS project's own vocabulary — a corpus of lorem ipsum
#: would not crowd the embedding space the way real neighbours do, and crowding
#: is the whole variable under test. That realism is also a CONFOUND: these rows
#: are lexically and semantically adjacent to the gold docs (which are generic
#: backend-infrastructure decisions), so a score drop under `near` padding mixes
#: "more neighbours" with "neighbours that look like the answer".
#:
#: `far` exists to separate those two (techne#945). Same SHAPE — same four
#: positions, same list lengths, same index arithmetic, so the only variable is
#: the words — over a domain with no software vocabulary at all. Measured token
#: overlap against the gold QUERIES, content words only (the corpus the pinning
#: test uses, and what a distractor actually competes against): `near` shares 24
#: (including `connection`, `cache`, `closed`, `hold`, `retry`, `session`),
#: `far` shares 0. `test_pad_vocabularies_differ_in_overlap`
#: pins that gap so "disjoint" cannot silently rot into "mildly different".
#:
#: Read the two arms together: `far` is the closer estimate of SCALE ALONE, the
#: near/far gap is the vocabulary contribution, and neither is a substitute for
#: real content (see `pad_from_store`).
#:
#: 12 x 11 x 11 x 11 = 15,972 combinations, and reaching that number requires
#: all FOUR indices to be independent digits. An earlier version indexed `tail`
#: as `i % 11` while the other three were the mixed-radix digits of `i` in base
#: (12, 11, 11) — period 1,452. Because 11 divides 1,452, `tail` was a pure
#: function of the other three and added no variety at all: `count(distinct
#: content)` at n=2000 measured 1,452, so a "2,000-row" corpus was 548
#: duplicates, and n=4000 was 2,548 of 4,000. Past the period the padding stops
#: adding novel neighbours and adds only copies, which is a different crowding
#: mechanism than the one the curve is read as showing (Prudence, PR #884).
_PAD_SUBJECT = (
    "the reflect rail",
    "the outbox sweeper",
    "the codex indexer",
    "the coord bus",
    "the session-start hook",
    "the archivist sink",
    "the FTS trigger",
    "the vec backfill",
    "the supersede path",
    "the thread detector",
    "the schema reconciler",
    "the doctor check",
)
_PAD_VERB = (
    "was rewritten to",
    "now refuses to",
    "silently skipped",
    "was measured to",
    "no longer needs to",
    "was changed so it can",
    "began to",
    "stopped being able to",
    "was proven to",
    "must never",
    "was observed to",
)
_PAD_OBJECT = (
    "write a row before the transaction commits",
    "resolve a symbol across files",
    "drop a payload larger than the cap",
    "re-read a module already imported",
    "hold a lock across an await",
    "emit a warning on an empty result",
    "count a superseded row twice",
    "retry after a connection reset",
    "parse a timestamp without a timezone",
    "cache a value past its TTL",
    "fan out to more than one target",
)
_PAD_TAIL = (
    "The fix landed behind a flag and the flag defaults off.",
    "Measured before and after; the delta was inside noise.",
    "Caught in review, not by a test, which is the part worth remembering.",
    "The remedy it prints is the one the check itself accepts.",
    "This is the third recurrence of the same shape.",
    "Verified against the source rather than recalled.",
    "The guard fails closed when the input is malformed.",
    "Left deliberately unbuilt until the measurement justifies it.",
    "The observed rate was one in five over thirty days.",
    "Rolled back the same day; the branch is retained.",
    "No behavioural change — the label was what misled.",
)

#: The DISJOINT vocabulary (techne#945). Same four positions and the same list
#: lengths as the `near` block above (12/11/11/11), so `_pad_text` produces the
#: same 15,972-combination period and the same distinctness guarantees; only the
#: words change. Domain chosen to share no software/infrastructure vocabulary
#: with the gold corpus — smallholding and beekeeping.
_FAR_SUBJECT = (
    "the queen bee",
    "the north orchard",
    "the pollen store",
    "the grafted rootstock",
    "the cider press",
    "the winter hive",
    "the hedgerow margin",
    "the seed drill",
    "the milking parlour",
    "the hay meadow",
    "the drainage ditch",
    "the lambing pen",
)
_FAR_VERB = (
    "was moved to",
    "has ceased to favour",
    "quietly shed",
    "was weighed against",
    "stopped yielding",
    "was grafted onto",
    "began attracting",
    "was pruned clear of",
    "was sown across",
    "was fenced away from",
    "was found sheltering",
)
_FAR_OBJECT = (
    "a shaded frame before the rain returned",
    "the lower terrace beside the stream",
    "bracken cleared in early spring",
    "two rows of blossom left unharvested",
    "the clay soil where nothing rooted",
    "a windbreak of hawthorn and elder",
    "the straw bedding laid down at dusk",
    "an unfenced verge grazed by deer",
    "sacks of chaff left from the threshing",
    "the coldest corner of the paddock",
    "wild thyme spreading past the wall",
)
_FAR_TAIL = (
    "The yield was noted in the ledger by lantern.",
    "Frost arrived early and the blossom did not set.",
    "Neighbouring farms reported the same pattern that season.",
    "The older hands had warned about the slope.",
    "It was left fallow and the ground recovered.",
    "Rain stayed away while the cut dried.",
    "The wind turned and the swarm settled elsewhere.",
    "Nothing was recorded until the following autumn.",
    "Roughly a sixth of the hives did not overwinter.",
    "The hedge was laid afresh and held.",
    "No remedy was tried; the weather simply changed.",
)

#: Vocabulary name -> the four positional lists. `pad_corpus`/`pad_observations`
#: take the NAME, so a caller cannot half-swap one position.
_PAD_VOCABULARIES = {
    "near": (_PAD_SUBJECT, _PAD_VERB, _PAD_OBJECT, _PAD_TAIL),
    "far": (_FAR_SUBJECT, _FAR_VERB, _FAR_OBJECT, _FAR_TAIL),
}


def _pad_text(i: int, vocabulary: str = "near") -> str:
    """Row `i` of the padding corpus, as a pure function of `i` and the vocabulary.

    Shared by `pad_corpus` (knowledge) and `pad_observations` (observations) so
    the two channels crowd with IDENTICAL text at the same index — the channel is
    then the only variable between those two arms, which is the whole point of
    having both (techne#945).
    """
    try:
        subjects, verbs, objects, tails = _PAD_VOCABULARIES[vocabulary]
    except KeyError:
        raise ValueError(
            f"unknown pad vocabulary {vocabulary!r}; have {sorted(_PAD_VOCABULARIES)}"
        ) from None
    subject = subjects[i % len(subjects)]
    verb = verbs[(i // len(subjects)) % len(verbs)]
    obj = objects[(i // (len(subjects) * len(verbs))) % len(objects)]
    tail = tails[(i // (len(subjects) * len(verbs) * len(objects))) % len(tails)]
    return f"{subject.capitalize()} {verb} {obj}. {tail}"


def pad_corpus(db_path: Path, n: int, vocabulary: str = "near") -> int:
    """Insert `n` deterministic synthetic KNOWLEDGE rows as retrieval distractors.

    WHY THIS EXISTS (techne#883). The #385/#399 gates score retrieval against
    `seed()`'s 36-row corpus. Discrimination is trivial at that size — the
    one right answer ranks top-k almost regardless of embedding quality — so the
    gates report `paraphrase recall@10 = 1.0` while the same code on the live
    2,740-row store measured 0.2. The gate was blind to the one variable that
    actually breaks retrieval in production: how many plausible neighbours the
    gold document has to outrank.

    `vocabulary` selects WHICH neighbours (techne#945). `near` is this project's
    own vocabulary — realistic crowding, and a confound, because those rows look
    like the answers. `far` is the same shape over a disjoint domain, so the pair
    separates "more neighbours" from "neighbours that resemble the gold". Neither
    is real content; `pad_from_store` is.

    Determinism is by INDEX ARITHMETIC, not a seeded RNG: `random.Random` makes
    guarantees about its own stream, not about how a future refactor consumes it,
    and a gate whose corpus quietly changes between versions is worse than no
    gate. Row i is a pure function of i, so any two runs at the same `n` build a
    byte-identical corpus.

    Rows are written with `store()` (not raw SQL) so the FTS triggers fire and
    the padding is reachable by the same query path as the gold — padding that
    the retriever cannot see would make the corpus look large and score small.

    Returns the number of rows inserted.
    """
    for i in range(n):
        store(
            db_path,
            _pad_text(i, vocabulary),
            type="fact",
            tags="pad,synthetic",
            source="eval-pad",
        )
    return n


def pad_observations(db_path: Path, n: int, vocabulary: str = "near") -> int:
    """Insert `n` deterministic synthetic OBSERVATION rows as distractors (techne#945).

    THE CHANNEL IS THE POINT. `pad_corpus` writes knowledge rows only, so the
    #883 curve exercises the knowledge channel and nothing else — and the gold
    set's `filter` tasks that query observations/threads were scored while never
    having been crowded at all (the caveat `test_retrieval_gate_scale` already
    carries). An external evaluation of a 35,096-observation store measured 50%
    paraphrase recall with ZERO knowledge rows, i.e. entirely on the observations
    channel, which the synthetic arm had no way to compare against.

    Same text as `pad_corpus` at the same index (both call `_pad_text`), so the
    two arms differ only in which table the crowd lives in.

    Written by direct SQL for the reason `_add_observation_raw` gives: it keeps
    the corpus deterministic and skips embedding inference at write time. The
    observations_fts trigger fires on INSERT, and `backfill_vectors` embeds the
    rows afterwards exactly as it does for knowledge.

    Returns the number of rows inserted.
    """
    conn = connect(db_path)
    try:
        for i in range(n):
            _add_observation_raw(conn, _pad_text(i, vocabulary), tool_name="Read", tags=PAD_OBS_TAG)
        conn.commit()
    finally:
        conn.close()
    return n


#: Marks observation rows written as scale padding (techne#945) — knowledge padding
#: is identifiable by `source='eval-pad'`, but observations have no source column,
#: so the tag is the equivalent marker. Must not collide with a tag `seed()` writes.
PAD_OBS_TAG = "pad,synthetic"

#: Where the real-content arm reads from (techne#945). Overridable so the arm can
#: be pointed at a copy or a colleague's export instead of the live store.
REAL_STORE_ENV = "TECHNE_EVAL_REAL_STORE"


def real_store_path() -> Path | None:
    """The live store to borrow distractors from, or None when unavailable.

    None is the CI answer and the answer on any machine without a techne store,
    which is why every caller must skip rather than fail.
    """
    import os

    override = os.environ.get(REAL_STORE_ENV)
    candidate = Path(override).expanduser() if override else Path.home() / ".techne" / "techne.db"
    return candidate if candidate.exists() else None


def pad_from_store(db_path: Path, n: int, source_db: Path, table: str = "knowledge") -> int:
    """Pad the eval corpus with `n` rows of REAL content borrowed from `source_db`.

    WHY (techne#945). Both synthetic vocabularies are templates: twelve subjects
    crossed with eleven verbs, which crowds an embedding space far more politely
    than real prose does. The evidence that this UNDERSTATES production is the
    whole reason #883 refused to set a floor — the live store measured paraphrase
    recall@10 = 0.200 where the synthetic arm at n=4,000 measured 0.500. This
    function closes that gap by using the real rows as the crowd, while the
    queries and the gold stay the committed set.

    THE CONTENT NEVER LEAVES THE MACHINE. Rows are read from the operator's store
    into a `tmp_path` database that pytest deletes, and nothing derived from them
    is committed, printed in full, or used as a query. The gold documents and the
    queries remain `seed()` + `retrieval_gold.yaml`; real rows appear ONLY as
    distractors to be outranked. That is what keeps this arm publishable while
    the store is not.

    READ-ONLY BY CONSTRUCTION, and deliberately not via `connect()`: `connect()`
    runs the migration ladder and the schema reconciler, so pointing it at the
    operator's live store would MUTATE the thing this arm is only supposed to
    read. Raw sqlite3 with a `mode=ro` URI cannot.

    Deterministic by `ORDER BY id LIMIT n` — the same store yields the same
    crowd, so a re-run compares like with like.

    Returns the number of rows actually inserted, which may be FEWER than `n`
    when the source holds fewer rows. Callers report the real number rather than
    the requested one (the #884 lesson: never print a size you did not observe).
    """
    import sqlite3

    if table not in ("knowledge", "observations"):
        raise ValueError(f"pad_from_store: unsupported table {table!r}")

    uri = f"file:{source_db}?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    try:
        if table == "knowledge":
            rows = src.execute(
                "SELECT content FROM knowledge WHERE superseded_by IS NULL "
                "AND content IS NOT NULL ORDER BY id LIMIT ?",
                (n,),
            ).fetchall()
        else:
            rows = src.execute(
                "SELECT content FROM observations WHERE content IS NOT NULL ORDER BY id LIMIT ?",
                (n,),
            ).fetchall()
    finally:
        src.close()

    if table == "knowledge":
        for (content,) in rows:
            store(db_path, content, type="fact", tags="pad,real", source="eval-pad")
    else:
        conn = connect(db_path)
        try:
            for (content,) in rows:
                _add_observation_raw(conn, content, tool_name="Read", tags=PAD_OBS_TAG)
            conn.commit()
        finally:
            conn.close()
    return len(rows)
