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
        type="decision", tags="g385,exact", source="eval",
    )
    gold["g385_cacheinvalidation"] = "knowledge:%d" % store(
        db_path,
        "Cache invalidation uses a write-through strategy so every update lands "
        "in the database and the cache simultaneously, avoiding stale reads.",
        type="decision", tags="g385,exact", source="eval",
    )
    gold["g385_circuitbreaker"] = "knowledge:%d" % store(
        db_path,
        "Circuit breaker trips open after five consecutive downstream failures "
        "and stays open for thirty seconds before allowing a trial request.",
        type="decision", tags="g385,exact", source="eval",
    )
    gold["g385_retrybackoff"] = "knowledge:%d" % store(
        db_path,
        "Retry backoff uses exponential delay with full jitter, doubling the "
        "wait time after each failed attempt up to a sixty second cap.",
        type="decision", tags="g385,exact", source="eval",
    )
    gold["g385_tlsrotation"] = "knowledge:%d" % store(
        db_path,
        "TLS certificate rotation runs automatically every ninety days and "
        "reloads the web server without dropping active connections.",
        type="decision", tags="g385,exact", source="eval",
    )
    gold["g385_dbpool"] = "knowledge:%d" % store(
        db_path,
        "Database connection pooling caps the pool at twenty five connections "
        "per service and recycles idle connections after ten minutes.",
        type="decision", tags="g385,exact", source="eval",
    )

    # --- filter: gold + a same-text distractor in a DIFFERENT table. The
    #     distractor is structurally unreachable when the task's table= param
    #     is passed through, which is exactly the mechanism under test. Two
    #     pairs per table direction (knowledge<->observations, observations
    #     <->knowledge, threads<->knowledge).
    gold["g385_filter_batch"] = "knowledge:%d" % store(
        db_path,
        "The nightly batch compaction job reclaims deleted row space in the "
        "archive table and runs at 03:00 UTC.",
        type="decision", tags="g385,filter", source="eval",
    )
    gold["g385_filter_logretention"] = "knowledge:%d" % store(
        db_path,
        "Log retention policy keeps application logs for fourteen days in hot "
        "storage before rolling to cold archive storage.",
        type="decision", tags="g385,filter", source="eval",
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
            "Edited: ratelimit module -- added sliding window counter for "
            "burst control",
        )
        gold["g385_filter_testsuite"] = "observation:%d" % _add_observation_raw(
            conn,
            "Ran pytest ratelimit test suite -- twelve passed, sliding window "
            "counter verified",
            tool_name="Bash",
        )

        # --- filter: gold in threads, distractor in knowledge ---
        conn.execute(
            """INSERT INTO threads (id, session_id, thread_num, label, summary,
                                    resume_points, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                "eval-thread-g385-flagrollout", _SESSION, 3,
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
                "eval-thread-g385-queuedepth", _SESSION, 4,
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
        type="decision", tags="g385,filter", source="eval",
    )
    gold["g385_filter_testsuite_distractor"] = "knowledge:%d" % store(
        db_path,
        "The sliding window counter test suite has twelve passing cases "
        "verifying burst control.",
        type="fact", tags="g385,filter", source="eval",
    )
    gold["g385_filter_flagrollout_distractor"] = "knowledge:%d" % store(
        db_path,
        "Feature flag rollout percentage gating starts at five percent and "
        "ramps to one hundred percent over two weeks pending error rate checks.",
        type="decision", tags="g385,filter", source="eval",
    )
    gold["g385_filter_queuedepth_distractor"] = "knowledge:%d" % store(
        db_path,
        "Queue depth backpressure alert threshold fires when depth exceeds "
        "five hundred messages for over two minutes.",
        type="fact", tags="g385,filter", source="eval",
    )
    gold["g385_filter_batch_distractor"] = "observation:%d" % _batch_distractor_id
    gold["g385_filter_logretention_distractor"] = (
        "observation:%d" % _logretention_distractor_id
    )

    # --- negative (deletion): store then hard-delete. A regression that drops
    #     the delete-filter would resurface these via pure keyword overlap.
    gold["g385_neg_del_backup"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Backup schedule policy runs a nightly full backup at 02:00 UTC "
            "via pg_dump uploaded to the S3 cold bucket.",
            type="fact", tags="g385,scratch", source="eval",
        )
    )
    delete(db_path, _del_id)

    gold["g385_neg_del_authmigration"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Auth provider migration replaced Okta with WorkOS for SSO login "
            "starting Q1 2026.",
            type="decision", tags="g385,scratch", source="eval",
        )
    )
    delete(db_path, _del_id)

    gold["g385_neg_del_stagingregion"] = "knowledge:%d" % (
        _del_id := store(
            db_path,
            "Staging environment region moved from AWS us-west-1 to us-east-2 "
            "for latency improvements.",
            type="fact", tags="g385,scratch", source="eval",
        )
    )
    delete(db_path, _del_id)

    # --- negative (supersession): the OLD revision is lexically closest to
    #     the query (that's the trap) but must never surface once superseded;
    #     the NEW revision is the live, correct answer.
    _oncall_v1 = store(
        db_path,
        "On-call rotation cadence is weekly, rotating every Monday at 09:00.",
        type="fact", tags="g385,oncall", source="eval",
    )
    gold["g385_neg_sup_oncall_old"] = "knowledge:%d" % _oncall_v1
    _oncall_v2 = supersede(
        db_path, _oncall_v1,
        "On-call rotation cadence is now biweekly, rotating every other "
        "Monday at 09:00.",
        source="eval",
    )
    gold["g385_neg_sup_oncall_new"] = "knowledge:%d" % _oncall_v2

    _redis_v1 = store(
        db_path,
        "Primary datastore for session state is Redis single-node.",
        type="fact", tags="g385,redis", source="eval",
    )
    gold["g385_neg_sup_redis_old"] = "knowledge:%d" % _redis_v1
    _redis_v2 = supersede(
        db_path, _redis_v1,
        "Primary datastore for session state is Redis Cluster with three "
        "shards for high availability.",
        source="eval",
    )
    gold["g385_neg_sup_redis_new"] = "knowledge:%d" % _redis_v2

    _ci_v1 = store(
        db_path,
        "CI runner pool size is 10 concurrent workers.",
        type="fact", tags="g385,ci", source="eval",
    )
    gold["g385_neg_sup_ci_old"] = "knowledge:%d" % _ci_v1
    _ci_v2 = supersede(
        db_path, _ci_v1,
        "CI runner pool size is 25 concurrent workers after the autoscaling "
        "upgrade.",
        source="eval",
    )
    gold["g385_neg_sup_ci_new"] = "knowledge:%d" % _ci_v2

    return gold


# --------------------------------------------------------------------------
# SCALE PADDING (techne#883)
# --------------------------------------------------------------------------

#: Combinatorial parts for synthetic distractors. Deliberately drawn from THIS
#: project's own vocabulary — a corpus of lorem ipsum would not crowd the
#: embedding space the way real neighbours do, and crowding is the whole
#: variable under test. See `pad_corpus` for what that choice costs in
#: interpretation: it is a confound, not just a realism win.
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
    "the reflect rail", "the outbox sweeper", "the codex indexer", "the coord bus",
    "the session-start hook", "the archivist sink", "the FTS trigger", "the vec backfill",
    "the supersede path", "the thread detector", "the schema reconciler", "the doctor check",
)
_PAD_VERB = (
    "was rewritten to", "now refuses to", "silently skipped", "was measured to",
    "no longer needs to", "was changed so it can", "began to", "stopped being able to",
    "was proven to", "must never", "was observed to",
)
_PAD_OBJECT = (
    "write a row before the transaction commits", "resolve a symbol across files",
    "drop a payload larger than the cap", "re-read a module already imported",
    "hold a lock across an await", "emit a warning on an empty result",
    "count a superseded row twice", "retry after a connection reset",
    "parse a timestamp without a timezone", "cache a value past its TTL",
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


def pad_corpus(db_path: Path, n: int) -> int:
    """Insert `n` deterministic synthetic knowledge rows as retrieval distractors.

    WHY THIS EXISTS (techne#883). The #385/#399 gates score retrieval against
    `seed()`'s 27-document corpus. Discrimination is trivial at that size — the
    one right answer ranks top-k almost regardless of embedding quality — so the
    gates report `paraphrase recall@10 = 1.0` while the same code on the live
    2,740-row store measured 0.2. The gate was blind to the one variable that
    actually breaks retrieval in production: how many plausible neighbours the
    gold document has to outrank.

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
        subject = _PAD_SUBJECT[i % len(_PAD_SUBJECT)]
        verb = _PAD_VERB[(i // len(_PAD_SUBJECT)) % len(_PAD_VERB)]
        obj = _PAD_OBJECT[(i // (len(_PAD_SUBJECT) * len(_PAD_VERB))) % len(_PAD_OBJECT)]
        tail = _PAD_TAIL[
            (i // (len(_PAD_SUBJECT) * len(_PAD_VERB) * len(_PAD_OBJECT)))
            % len(_PAD_TAIL)
        ]
        store(
            db_path,
            f"{subject.capitalize()} {verb} {obj}. {tail}",
            type="fact",
            tags="pad,synthetic",
            source="eval-pad",
        )
    return n
