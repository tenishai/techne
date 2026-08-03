"""Integration tests for the full session-end lifecycle."""


def test_session_end_lifecycle_happy_path(tmp_path):
    """Full session-end sequence: observations → detect → link → save → end.
    Uses a mock generate_fn to avoid real LLM calls.
    """
    from techne.config import Config
    from techne.core.database import init_db, connect
    from techne.core.state import start_session, end_session, add_observation
    from techne.core.threads import detect_and_summarize, link_threads, save_threads

    db_path = tmp_path / "lifecycle.db"
    init_db(db_path)

    config = Config()
    config.db_path = db_path
    config.threads.min_observations = 10
    # This test exercises the LLM detector path explicitly (it asserts
    # behavior that comes from parsing the mocked LLM response).
    config.threads.detector = "llm"
    config.threads.summarizer = "llm"

    session_id = start_session(db_path, project_path="/tmp/test", agent_identity="test-agent")

    # Simulate observations: two distinct topics
    # Note: add_observation signature is (db_path, session_id, content, tool_name="")
    for i in range(8):
        add_observation(db_path, session_id, "Bash", f"working on auth module step {i}")
    for i in range(8):
        add_observation(db_path, session_id, "Edit", f"refactoring database layer step {i}")

    # Retrieve as the lifecycle would
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT tool_name, content, timestamp FROM observations WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    observations = [
        {"tool_name": r["tool_name"], "content": r["content"], "timestamp": r["timestamp"]}
        for r in rows
    ]

    # Mock generate_fn returns a parseable two-thread response
    # Summaries intentionally have no trailing periods to avoid FTS5 syntax errors
    def fake_generate(prompt):
        return """## Thread 1: auth module work
Observations: 0,1,2,3,4,5,6,7
Summary: Working on the auth module across 8 steps
Resume: continue auth integration

## Thread 2: database refactor
Observations: 8,9,10,11,12,13,14,15
Summary: Refactoring the database layer
Resume: None"""

    threads = detect_and_summarize(observations, config, generate_fn=fake_generate)
    assert len(threads) == 2
    assert "auth module" in threads[0].label.lower()
    assert threads[1].resume_points == ""  # "None" parses to empty

    # Save with no parent links for this first session
    parent_links = link_threads(threads, session_id, db_path, config)
    save_threads(threads, session_id, db_path, parent_links=parent_links)
    end_session(db_path, session_id, summary="test session")

    # Verify persistence
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT id, label, status FROM threads WHERE session_id = ? ORDER BY thread_num",
        (session_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "active"
    conn.close()


def test_session_end_lifecycle_with_link(tmp_path):
    """Verify that a second session's thread can link back to a prior similar thread."""
    from techne.config import Config
    from techne.core.database import init_db, connect
    from techne.core.state import start_session, end_session
    from techne.core.threads import Thread, link_threads, save_threads

    db_path = tmp_path / "lifecycle_link.db"
    init_db(db_path)
    config = Config()
    config.threads.link_threshold = 0.05  # very lenient — let weak matches through

    # First session: save a thread about "kubernetes deployment"
    # Summaries have no trailing periods to avoid FTS5 syntax errors in link_threads
    session1 = start_session(db_path, project_path="/tmp/p", agent_identity="agent")
    threads1 = [Thread(thread_num=1, label="kubernetes deployment work",
                       summary="Deploying services to kubernetes cluster")]
    links1 = link_threads(threads1, session1, db_path, config)
    save_threads(threads1, session1, db_path, parent_links=links1)
    end_session(db_path, session1, summary="session 1")

    # Second session: try to link a similar thread
    session2 = start_session(db_path, project_path="/tmp/p", agent_identity="agent")
    threads2 = [Thread(thread_num=1, label="kubernetes cluster deployment",
                       summary="Deploying kubernetes services")]
    links2 = link_threads(threads2, session2, db_path, config)

    # Should have linked to the first session's thread
    assert len(links2) == 1
    thread_id_2, parent_id_2 = links2[0]
    assert parent_id_2 is not None
    assert parent_id_2.startswith(session1)  # parent is from session 1


def test_run_session_end_persists_boundaries_under_heuristic(tmp_path):
    """run_session_end should persist boundary scores when the heuristic detector ran.

    The loop we want closed:
      observations -> heuristic detect -> threads + scores -> save_threads + save_boundaries
      -> `techne threads show` reads stored boundaries.
    """
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import add_observation, start_session
    from techne.lifecycle import run_session_end

    db_path = tmp_path / "loop.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    # Heuristic detector is the default, but spell it out.
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/tmp/p", agent_identity="t")

    # Two clusters separated by a 2-hour gap → should produce ≥ 2 boundaries
    # below the boundary line and at least one boundary across the gap.
    base = datetime(2026, 1, 1, 12, 0)
    for i in range(8):
        ts = (base + timedelta(minutes=i)).isoformat()
        # We can't backdate add_observation, but for this test the order
        # matters more than the absolute timestamps — the heuristic uses
        # the timestamp field on the row. Set it manually.
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Edit", f"/proj/a.py line {i}"),
        )
        conn.commit()
        conn.close()
    for i in range(8):
        ts = (base + timedelta(minutes=120 + i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Bash", f"echo step {i}"),
        )
        conn.commit()
        conn.close()

    result = run_session_end(config, session_id, summary="done")
    assert result["threads"] == 2, result

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT obs_index_a, total_score, is_boundary FROM thread_boundaries "
            "WHERE session_id = ? ORDER BY obs_index_a",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    # 16 observations → 15 consecutive pairs → 15 boundary rows
    assert len(rows) == 15
    # At least one is_boundary=1 (the gap split)
    assert sum(r["is_boundary"] for r in rows) >= 1


def test_skip_gate_still_persists_caller_summary(tmp_path):
    """The incremental-skip gate must NOT drop a caller-supplied summary when the
    observation count is unchanged since the last detection (Prudence, #356). The
    CLI/plugin path (`techne session-end --summary`, opencode session.idle) passes
    a summary on turns that add no new observations."""
    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    db_path = tmp_path / "skip.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/tmp/p", agent_identity="t")
    for i in range(6):
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content) VALUES (?, ?, ?)",
            (session_id, "Edit", f"/proj/a.py line {i}"),
        )
        conn.commit()
        conn.close()

    # First pass runs heuristic detection and records last_obs_count.
    first = run_session_end(config, session_id)
    assert "skipped" not in first

    # Second pass: SAME observation count, but a summary is supplied. Detection is
    # skipped (the gate fires) — yet the summary must still be persisted.
    second = run_session_end(config, session_id, summary="final summary text")
    assert second["skipped"] == "no_new_observations"

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT summary, ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["summary"] == "final summary text"  # not silently dropped
    assert row["ended_at"] is not None  # ended_at stamped on the skip path too


def test_redetect_threads_replaces_threads_and_refreshes_heuristic_summary(tmp_path):
    """Redetect: post-hoc add of UserPrompt obs should produce a new boundary
    landing on the prompt (now that prompts amplify gap signals), and replace
    the existing thread breakdown without touching ended_at or other summary
    slots."""
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import redetect_threads, run_session_end

    db_path = tmp_path / "redetect.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    base = datetime(2026, 1, 1, 12, 0)

    def _insert(ts_minutes, tool, content):
        ts = (base + timedelta(minutes=ts_minutes)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, tool, content),
        )
        conn.commit()
        conn.close()

    # Single contiguous flow — no gaps, all filesystem → 1 thread
    for i in range(6):
        _insert(i, "Edit", f"/proj/a.py line {i}")

    result = run_session_end(config, session_id, summary="done")
    assert result["threads"] == 1

    conn = connect(db_path)
    try:
        sess = conn.execute(
            "SELECT ended_at, summary, summary_llm, summary_heuristic FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        original_ended_at = sess["ended_at"]
        assert original_ended_at is not None
        assert sess["summary"] == "done"  # manual slot
        assert sess["summary_heuristic"] is None  # caller-passed summary, not heuristic
        # Pin an LLM title so we can confirm redetect doesn't clobber it.
        conn.execute(
            "UPDATE sessions SET summary_llm = ? WHERE id = ?",
            ("LLM-set title", session_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Late-arriving import: a UserPrompt after a 35-min gap.
    # That gap alone is gap_medium=0.6, plus user_prompt=0.3 = 0.9 → boundary.
    _insert(45, "UserPrompt", "switch topics — work on /proj/b.py now")
    for i in range(5):
        _insert(46 + i, "Edit", f"/proj/b.py line {i}")

    redetect = redetect_threads(config, session_id)
    assert redetect["threads"] == 2, redetect
    assert redetect["observations"] == 12

    conn = connect(db_path)
    try:
        threads = conn.execute(
            "SELECT thread_num, label FROM threads WHERE session_id=? ORDER BY thread_num",
            (session_id,),
        ).fetchall()
        assert len(threads) == 2

        sess = conn.execute(
            "SELECT ended_at, summary, summary_llm, summary_heuristic FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        # ended_at untouched
        assert sess["ended_at"] == original_ended_at
        # manual + LLM slots preserved
        assert sess["summary"] == "done"
        assert sess["summary_llm"] == "LLM-set title"
        # heuristic slot freshly written
        assert sess["summary_heuristic"]
        assert sess["summary_heuristic"] != "done"

        # At least one boundary involves the user_prompt signal
        boundaries = conn.execute(
            "SELECT signals_json FROM thread_boundaries WHERE session_id=? AND is_boundary=1",
            (session_id,),
        ).fetchall()
        assert any("user_prompt" in b["signals_json"] for b in boundaries)
    finally:
        conn.close()


def test_redetect_threads_no_sync_by_default(tmp_path):
    """Default --no-sync: the outbox stays empty. Iterative tuning runs
    must not flood the outbox with stale syncs."""
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import redetect_threads, run_session_end
    from techne.sync.outbox import pending_count

    db_path = tmp_path / "nosync.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    base = datetime(2026, 1, 1, 12, 0)
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Edit", f"/proj/a.py {i}"),
        )
        conn.commit()
        conn.close()
    run_session_end(config, session_id, summary="done")

    before = pending_count(db_path)
    result = redetect_threads(config, session_id)  # default sync=False
    assert result["queued"] == 0
    assert pending_count(db_path) == before


def test_session_end_noop_adapter_does_not_enqueue(tmp_path):
    """With the default noop adapter there is no consumer to drain the outbox,
    so session_end must not enqueue thread summaries — otherwise the queue
    grows an undrainable backlog (one row per thread, every session)."""
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end
    from techne.sync.outbox import pending_count

    db_path = tmp_path / "noop.db"
    init_db(db_path)
    config = Config()  # adapter defaults to "noop"
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"
    assert config.sync.adapter == "noop"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    base = datetime(2026, 1, 1, 12, 0)
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Edit", f"/proj/a.py {i}"),
        )
        conn.commit()
        conn.close()
    result = run_session_end(config, session_id, summary="done")
    assert result["threads"] >= 1  # threads were detected...
    assert result["queued"] == 0  # ...but nothing was enqueued
    assert pending_count(db_path) == 0


def test_redetect_threads_sync_enqueues_one_per_thread(tmp_path):
    """`sync=True`: one outbox enqueue call per detected thread. The outbox
    itself dedups by content_hash, so identical re-syncs no-op silently;
    `queued` reflects intent, not net-new outbox rows."""
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import redetect_threads, run_session_end
    from techne.sync.outbox import pending_count

    db_path = tmp_path / "sync.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"
    # A live consumer must exist for enqueue to fire (gated on a non-noop
    # adapter); the gate's value is exercised in its own test below.
    config.sync.adapter = "filesystem"
    config.sync.path = str(tmp_path / "sync-out")

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    base = datetime(2026, 1, 1, 12, 0)
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Edit", f"/proj/a.py {i}"),
        )
        conn.commit()
        conn.close()
    for i in range(4):
        ts = (base + timedelta(minutes=120 + i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Bash", f"echo {i}"),
        )
        conn.commit()
        conn.close()
    run_session_end(config, session_id, summary="done")
    after_first_run = pending_count(db_path)

    result = redetect_threads(config, session_id, sync=True)
    assert result["queued"] == result["threads"]
    # Identical thread structure → outbox dedup absorbs the re-enqueues
    # (this is the desired property: a stable redetect doesn't flood).
    assert pending_count(db_path) == after_first_run


def test_redetect_drops_orphan_threads_when_fewer_detected(tmp_path):
    """If redetect produces FEWER threads than were stored, the leftovers must
    not survive — save_threads is INSERT OR REPLACE on PK, so without an
    explicit DELETE the old thread_num=2 row would persist."""
    from datetime import datetime, timedelta

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import redetect_threads, run_session_end

    db_path = tmp_path / "orphans.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    base = datetime(2026, 1, 1, 12, 0)

    def _insert(idx, tool, content, ts_minutes):
        ts = (base + timedelta(minutes=ts_minutes)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, tool, content),
        )
        conn.commit()
        conn.close()

    # Two well-separated clusters → 2 threads
    for i in range(6):
        _insert(i, "Edit", f"/proj/a.py line {i}", i)
    for i in range(6):
        _insert(i, "Bash", f"echo {i}", 180 + i)

    run_session_end(config, session_id, summary="done")
    conn = connect(db_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM threads WHERE session_id=?", (session_id,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert before == 2

    # Wipe the gap-driving timestamps by collapsing all obs to a tight window
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM observations WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        ).fetchall()
        for i, r in enumerate(rows):
            new_ts = (base + timedelta(minutes=i)).isoformat()
            conn.execute(
                "UPDATE observations SET timestamp=? WHERE id=?",
                (new_ts, r["id"]),
            )
        conn.commit()
    finally:
        conn.close()

    redetect = redetect_threads(config, session_id)
    # Now everything is contiguous; expect 1 thread.
    assert redetect["threads"] == 1

    conn = connect(db_path)
    try:
        after = conn.execute(
            "SELECT thread_num FROM threads WHERE session_id=? ORDER BY thread_num",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [r["thread_num"] for r in after] == [1]


def test_session_end_with_failing_llm(tmp_path):
    """LLM failure: detect_and_summarize falls back gracefully, logs a warning."""
    from structlog.testing import capture_logs

    from techne.config import Config
    from techne.core.threads import detect_and_summarize

    config = Config()
    config.threads.min_observations = 5
    # Tests the LLM-path fallback behavior explicitly.
    config.threads.detector = "llm"
    config.threads.summarizer = "llm"

    observations = [
        {"tool_name": "Bash", "content": f"step {i}", "timestamp": ""}
        for i in range(20)
    ]

    def failing_generate(prompt):
        raise RuntimeError("simulated LLM failure")

    with capture_logs() as cap_logs:
        threads = detect_and_summarize(observations, config, generate_fn=failing_generate)

    # Fallback should produce at least one thread
    assert len(threads) >= 1
    # Either single-thread fallback or thread parsing fallback
    assert threads[0].summary  # non-empty summary

    # Error should have been logged via structlog (detector LLM path fails first)
    assert any(e["event"] == "thread_inference_llm_failed" for e in cap_logs)
    assert any("simulated LLM failure" in e.get("error", "") for e in cap_logs)


# ---------------------------------------------------------------------------
# External-id session binding (added in v0.6.0 for runtimes that have their
# own task / conversation ids — e.g., A2A Task IDs in Cabal).
# ---------------------------------------------------------------------------


def test_start_session_with_explicit_session_id(tmp_path):
    """Passing session_id= uses it verbatim instead of generating a UUID."""
    from techne.config import Config
    from techne.core.state import start_session

    config = Config()
    config.db_path = tmp_path / "external.db"

    result = start_session(
        config.db_path,
        project_path="/some/project",
        session_id="task-abc-123",
    )
    assert result == "task-abc-123"


def test_start_session_without_session_id_generates_uuid(tmp_path):
    """Default path still generates a UUID (no behavior change for existing callers)."""
    import re
    from techne.config import Config
    from techne.core.state import start_session

    config = Config()
    config.db_path = tmp_path / "uuid.db"

    result = start_session(config.db_path)
    # UUIDs match this shape; explicit ids like "task-abc-123" wouldn't.
    assert re.match(r"[0-9a-f-]{36}", result), f"expected UUID, got {result!r}"


def test_init_session_for_external_id_is_idempotent(tmp_path):
    """Calling init_session_for_external_id with the same id twice returns the
    same id, no PRIMARY KEY violation. This is the contract Cabal-style runtimes
    need when an agent restarts mid-Task and re-binds the same task_id."""
    from techne.config import Config
    from techne.lifecycle import init_session_for_external_id

    config = Config()
    config.db_path = tmp_path / "idempotent.db"

    first = init_session_for_external_id(config, "cabal-task-xyz")
    second = init_session_for_external_id(config, "cabal-task-xyz")

    assert first == "cabal-task-xyz"
    assert second == "cabal-task-xyz"


def test_init_session_for_external_id_respects_config_platform(tmp_path):
    """When config.platform='cabal', the session row's platform column carries
    'cabal' — not the previous claude-code default."""
    from techne.config import Config
    from techne.core.database import connect
    from techne.lifecycle import init_session_for_external_id

    config = Config()
    config.db_path = tmp_path / "platform.db"
    config.platform = "cabal"

    sid = init_session_for_external_id(config, "task-1")

    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT platform FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["platform"] == "cabal"


def test_init_session_for_external_id_carries_fields_single_row(tmp_path):
    """techne#170: the switch to ensure_session (atomic INSERT OR IGNORE, closing the
    SELECT→INSERT TOCTOU) must still persist project_path/agent_identity and leave
    exactly one row even when re-called."""
    from techne.config import Config
    from techne.core.database import connect
    from techne.lifecycle import init_session_for_external_id

    config = Config()
    config.db_path = tmp_path / "fields.db"

    init_session_for_external_id(config, "task-9", project_path="/repo", agent_identity="raven")
    # Re-bind (agent restart): must not duplicate or overwrite.
    init_session_for_external_id(config, "task-9", project_path="/other", agent_identity="rook")

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT project_path, agent_identity FROM sessions WHERE id = ?", ("task-9",)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "exactly one session row per external id"
    # INSERT OR IGNORE keeps the first write — the row is not modified on re-bind.
    assert rows[0]["project_path"] == "/repo"
    assert rows[0]["agent_identity"] == "raven"


def test_run_session_start_passes_session_id_through(tmp_path):
    """The lifecycle wrapper threads session_id down to start_session unchanged."""
    from techne.config import Config
    from techne.lifecycle import run_session_start

    config = Config()
    config.db_path = tmp_path / "rss.db"

    result = run_session_start(
        config,
        project_path="/proj",
        session_id="explicit-id-from-runtime",
    )
    assert result == "explicit-id-from-runtime"


def test_redetect_threads_never_stamps_ended_at_on_open_session(tmp_path):
    """redetect_threads must not set ended_at on a session that was never ended.

    Regression guard for issue #3: a future refactor consolidating the write
    paths could silently stamp ended_at. One observation is enough — the guard
    is about the write path, not thread detection quality.
    """
    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import redetect_threads

    db_path = tmp_path / "open_session.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO observations (session_id, timestamp, tool_name, content) "
        "VALUES (?, datetime('now'), 'Edit', 'x')",
        (session_id,),
    )
    conn.commit()
    conn.close()

    redetect_threads(config, session_id)

    conn = connect(db_path)
    row = conn.execute(
        "SELECT ended_at FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    conn.close()
    assert row["ended_at"] is None


# ---------------------------------------------------------------------------
# Atomicity: thread_boundaries + threads must be written in one transaction
# (techne#248).  This test bites against the pre-fix two-connection code
# because save_boundaries committed on its own connection before save_threads
# ran — a crash or raise between them left orphaned boundary rows.
# ---------------------------------------------------------------------------


def test_session_end_atomicity_boundaries_rolled_back_on_save_threads_failure(
    tmp_path, monkeypatch
):
    """If save_threads raises, thread_boundaries must NOT be left committed.

    Under the old two-connection code this test fails: save_boundaries
    committed before save_threads was called, so a failure in save_threads
    left orphaned boundary rows in the DB.

    Under the fixed single-transaction code both writes roll back together,
    so thread_boundaries is empty after the exception.
    """
    from datetime import datetime, timedelta

    import pytest

    from techne.config import Config
    from techne.core.database import connect, init_db
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    db_path = tmp_path / "atomicity.db"
    init_db(db_path)
    config = Config()
    config.db_path = db_path
    config.threads.detector = "heuristic"
    config.threads.summarizer = "heuristic"

    session_id = start_session(db_path, project_path="/proj", agent_identity="t")

    # Two clusters separated by a 2-hour gap → heuristic produces boundary
    # scores (scores is non-empty), so the if-scores branch executes.
    base = datetime(2026, 1, 1, 12, 0)
    for i in range(6):
        ts = (base + timedelta(minutes=i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Edit", f"/proj/a.py line {i}"),
        )
        conn.commit()
        conn.close()
    for i in range(6):
        ts = (base + timedelta(minutes=120 + i)).isoformat()
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO observations (session_id, timestamp, tool_name, content) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, "Bash", f"echo step {i}"),
        )
        conn.commit()
        conn.close()

    # Patch save_threads to raise AFTER the boundary rows would have been
    # written in the old two-connection code.  The import inside run_session_end
    # resolves at call time from techne.core.threads, so patching that module
    # attribute replaces the binding the function picks up.
    import techne.core.threads as _threads_mod

    original_save_threads = _threads_mod.save_threads

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated save_threads failure")

    monkeypatch.setattr(_threads_mod, "save_threads", _boom)

    with pytest.raises(RuntimeError, match="simulated save_threads failure"):
        run_session_end(config, session_id, summary="done")

    # The whole transaction must have rolled back — no orphaned boundary rows.
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM thread_boundaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert rows["c"] == 0, (
        f"Expected 0 thread_boundary rows after rolled-back save_threads, "
        f"got {rows['c']} — boundaries were committed before the rollback "
        f"(non-atomic two-connection bug, techne#248)"
    )


import pytest


@pytest.mark.parametrize("reflect_enabled", [False, True])
def test_archivist_push_failure_reports_its_own_error(tmp_path, monkeypatch, capsys, reflect_enabled):
    """A failing archivist push must surface ITS OWN error and let teardown finish.

    Regression for the `import sys` that sat inside the reflect except-handler in
    run_session_end. Because a function-local import binds the name for the WHOLE
    function, `sys` was unbound on every path where reflect did not raise — which
    includes the default config, where reflect is disabled and the handler never
    runs at all. The archivist handler below then died with UnboundLocalError,
    replacing the real egress error with a traceback about `sys` and taking
    session teardown down with it.

    Observed live 2026-08-03: hook:session_end:last_error recorded
    "UnboundLocalError: cannot access local variable 'sys'" while `techne doctor`
    still reported session_end as healthy.

    Both branches are pinned: reflect-disabled (the default) and reflect-enabled
    -and-succeeding. The old code passed neither.
    """
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.state import start_session, add_observation
    from techne.lifecycle import run_session_end
    import techne.core.archivist as archivist_mod

    db_path = tmp_path / "lifecycle.db"
    init_db(db_path)

    config = Config()
    config.db_path = db_path
    config.reflect.enabled = reflect_enabled

    if reflect_enabled:
        # Reflect must SUCCEED — that is the precondition that left `sys` unbound.
        import techne.core.reflect as reflect_mod

        monkeypatch.setattr(
            reflect_mod,
            "reflect_on_session",
            lambda *a, **k: {"threads_reflected": 0, "candidates_promoted": 0, "errors": 0},
        )

    def _explode(_config):
        raise RuntimeError("archivist unreachable: the REAL error")

    monkeypatch.setattr(archivist_mod, "push_knowledge_to_archivist", _explode)

    session_id = start_session(db_path, project_path="/tmp/test", agent_identity="test-agent")
    add_observation(db_path, session_id, "Bash", "some work")

    # Must not raise: egress failure may never break session teardown.
    result = run_session_end(config, session_id, summary="done")

    assert result["knowledge_pushed"] == 0

    stderr = capsys.readouterr().err
    assert "archivist unreachable: the REAL error" in stderr, (
        f"The real egress error must reach stderr, not be masked. Got: {stderr!r}"
    )
    assert "UnboundLocalError" not in stderr
    assert "'sys'" not in stderr
