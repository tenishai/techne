"""Tests for the end-of-session reflection pass."""

import json
import logging
from unittest.mock import patch

import pytest

from techne.config import Config, PostureConfig
from techne.core.database import connect, init_db
from techne.core.reflect import (
    candidate_stats,
    discard_candidate,
    effective_write_to,
    ingest_reflection_response,
    list_candidates,
    parse_response,
    promote_candidate,
    reflect_on_session,
)
from techne.core.state import start_session
from techne.core.threads import Thread

# --- parse_response -------------------------------------------------------


def test_parse_response_clean_json():
    raw = json.dumps(
        [
            {
                "scope": "knowledge",
                "type": "decision",
                "content": "Use Postgres for orders",
                "tags": "storage",
                "confidence": "high",
            },
            {
                "scope": "identity",
                "type": "user.prefers.terse_responses",
                "content": "Operator wants brief replies",
                "tags": "style",
                "confidence": "medium",
            },
        ]
    )
    parsed = parse_response(raw, max_items=10).candidates
    assert len(parsed) == 2
    assert parsed[0].scope == "knowledge"
    assert parsed[0].type == "decision"
    assert parsed[1].scope == "identity"


def test_parse_response_tolerates_code_fence():
    raw = (
        "```json\n"
        + json.dumps([{"scope": "knowledge", "type": "fact", "content": "x", "confidence": "low"}])
        + "\n```"
    )
    parsed = parse_response(raw, max_items=10).candidates
    assert len(parsed) == 1


def test_parse_response_tolerates_leading_prose():
    raw = (
        "Here is the JSON you asked for:\n["
        + json.dumps(
            {"scope": "knowledge", "type": "decision", "content": "y", "confidence": "medium"}
        )
        + "]\nThank you."
    )
    parsed = parse_response(raw, max_items=10).candidates
    assert len(parsed) == 1
    assert parsed[0].type == "decision"


def test_parse_response_rejects_team_domain_pattern():
    """techne#137: reflect must not promote a `pattern` candidate into personal
    memory — patterns belong in architect.db, not the reflection pass."""
    raw = json.dumps(
        [{"scope": "knowledge", "type": "pattern", "content": "x", "confidence": "high"}]
    )
    assert parse_response(raw, max_items=10).candidates == []


def test_parse_response_rejects_invalid_scope():
    raw = json.dumps([{"scope": "lol", "type": "fact", "content": "x", "confidence": "high"}])
    assert parse_response(raw, max_items=10).candidates == []


def test_parse_response_rejects_invalid_knowledge_type():
    raw = json.dumps(
        [{"scope": "knowledge", "type": "not-a-real-type", "content": "x", "confidence": "high"}]
    )
    assert parse_response(raw, max_items=10).candidates == []


def test_parse_response_respects_max_items():
    raw = json.dumps(
        [
            {"scope": "knowledge", "type": "fact", "content": f"item {i}", "confidence": "medium"}
            for i in range(10)
        ]
    )
    parsed = parse_response(raw, max_items=3).candidates
    assert len(parsed) == 3


def test_parse_response_empty_list_is_valid():
    """A parsed-but-empty array is the model saying "nothing worth saving".

    It must NOT read as a failure — that direction matters as much as the other,
    or every quiet-but-correct pass starts reporting an error.
    """
    r = parse_response("[]", max_items=5)
    assert r.candidates == []
    assert r.failure is None
    assert r.dropped == 0


def test_parse_response_garbage_reports_a_failure_not_an_empty_pass():
    """techne#465: the whole point. Unparseable and "nothing to save" produced
    byte-identical results, so a discarded reflection was reported as success."""
    garbage = parse_response("not json at all", max_items=5)
    assert garbage.candidates == []
    assert garbage.failure == "no JSON array found"

    empty = parse_response("", max_items=5)
    assert empty.failure == "empty response", "an empty reply is a distinct failure"

    truncated = parse_response('[{"scope": "knowledge", "type": "fact",', max_items=5)
    assert truncated.failure, "a truncated array is the realistic trigger (output limit)"

    prose = parse_response("I reviewed the session and found nothing notable.", max_items=5)
    assert prose.failure, "a prose-only reply is a failure, not an empty result"


def test_parse_response_counts_rejected_items_as_dropped():
    """The third outcome: the array parsed and every item was thrown away.

    A prompt drifting out of contract shows up here first, and before this it was
    as silent as a parse failure.
    """
    raw = '[{"scope": "nonsense", "type": "fact", "content": "x"},' \
          ' {"scope": "knowledge", "type": "not-a-type", "content": "y"}]'
    r = parse_response(raw, max_items=5)
    assert r.candidates == []
    assert r.failure is None, "the array DID parse — this is not a parse failure"
    assert r.dropped == 2


# --- reflect_on_session ---------------------------------------------------


def _mk_config(tmp_path, *, enabled: bool, write_to: str = "candidates"):
    config = Config()
    config.db_path = tmp_path / "reflect.db"
    config.reflect.enabled = enabled
    config.reflect.min_observations_per_thread = 3
    config.reflect.min_confidence = "low"
    config.reflect.max_candidates_per_thread = 5
    config.reflect.write_to = write_to
    config.reflect.include_existing_knowledge = False  # skip FTS in tests
    init_db(config.db_path)
    return config


def _mk_session_with_obs(config, n_obs: int) -> tuple[str, list[dict]]:
    session_id = start_session(config.db_path, project_path="/tmp/p", agent_identity="test-agent")
    obs = [
        {"tool_name": "Bash", "content": f"obs {i}", "timestamp": "2026-06-15"}
        for i in range(n_obs)
    ]
    return session_id, obs


def _save_thread_row(config, session_id: str, thread: Thread) -> None:
    """Persist a Thread row so reflect's FK to threads(id) is satisfied.
    Production calls save_threads before reflect_on_session — this mirrors that.
    """
    from techne.core.threads import save_threads

    save_threads(
        [thread],
        session_id,
        config.db_path,
        parent_links=[(f"{session_id}-{thread.thread_num}", None)],
    )


def test_persisted_thread_id_recovers_from_drift(tmp_path):
    """#106: when the recomputed id doesn't match the stored row (boundary_signal
    drift on reload), resolve via (session_id, thread_num) instead of failing."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 3)
    # A thread persisted under the content-addressed scheme: thread_num=1's stored
    # id is a t-hash, not "{session}-1".
    conn = connect(config.db_path)
    conn.execute(
        "INSERT INTO threads (id, session_id, thread_num, label, summary) VALUES (?,?,?,?,?)",
        ("t-deadbeef00", session_id, 1, "L", "S"),
    )
    conn.commit()
    conn.close()

    from techne.core.thread_orchestrator import persisted_thread_id, thread_row_id

    # A reloaded Thread whose recomputed id drifts to the legacy form.
    t = Thread(thread_num=1, label="L", summary="S", observation_indices=[0, 1, 2])
    assert thread_row_id(session_id, t) == f"{session_id}-1"  # the drifted (wrong) id
    assert persisted_thread_id(config.db_path, session_id, t) == "t-deadbeef00"  # recovered


def test_persisted_thread_id_none_when_unpersisted(tmp_path):
    """No persisted row for the thread → None (caller stores a NULL FK)."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 3)
    from techne.core.thread_orchestrator import persisted_thread_id

    t = Thread(thread_num=9, label="L", summary="S", observation_indices=[0, 1, 2])
    assert persisted_thread_id(config.db_path, session_id, t) is None


def test_insert_candidate_nulls_missing_thread_fk(tmp_path):
    """#106 safety net: a stale/missing thread FK must NOT drop the candidate — it
    lands with a NULL thread id instead of raising an FK error."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 3)
    from techne.core.reflect import Candidate, _insert_candidate

    cid = _insert_candidate(
        config.db_path,
        Candidate(
            scope="knowledge", type="fact", content="a durable fact", tags="t", confidence="high"
        ),
        session_id=session_id,
        thread_id="t-does-not-exist",  # no such threads row
        model_used="agent-assisted",
        prompt_hash="abc123",
        raw_response="[]",
    )
    assert cid is not None
    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT source_thread_id, source_session_id FROM knowledge_candidates WHERE id=?",
            (cid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["source_thread_id"] is None  # FK nulled, not rejected
    assert row["source_session_id"] == session_id  # session provenance preserved


def test_reflect_disabled_is_noop(tmp_path):
    config = _mk_config(tmp_path, enabled=False)
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))]

    def boom(_):
        raise AssertionError("generate_fn should not be called when reflect is disabled")

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=boom)
    assert result["threads_reflected"] == 0
    assert result["candidates_proposed"] == 0
    assert list_candidates(config.db_path, status="all") == []


def test_reflect_writes_candidates_not_live(tmp_path):
    """write_to='candidates' must NOT touch the live knowledge / identity tables."""
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [
        Thread(
            thread_num=1, label="auth work", summary="auth work", observation_indices=list(range(5))
        )
    ]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps(
            [
                {
                    "scope": "knowledge",
                    "type": "decision",
                    "content": "Use JWT for service auth",
                    "tags": "auth",
                    "confidence": "high",
                },
                {
                    "scope": "identity",
                    "type": "user.team.name",
                    "content": "User works solo",
                    "tags": "team",
                    "confidence": "medium",
                },
            ]
        )

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert result["threads_reflected"] == 1
    assert result["candidates_written"] == 2

    cands = list_candidates(config.db_path, status="pending")
    assert len(cands) == 2
    assert {c["scope"] for c in cands} == {"knowledge", "identity"}

    # Crucial invariant: live tables are untouched
    conn = connect(config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 0
    finally:
        conn.close()


def test_reflect_skips_thin_threads(tmp_path):
    """Threads below min_observations_per_thread must not trigger an LLM call."""
    config = _mk_config(tmp_path, enabled=True)
    config.reflect.min_observations_per_thread = 10
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))]

    calls = []

    def fake_llm(p):
        calls.append(p)
        return "[]"

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert calls == []
    assert result["threads_reflected"] == 0


def test_reflect_min_confidence_filter(tmp_path):
    """Candidates below config.reflect.min_confidence are dropped.

    NO LONGER SILENTLY — they are counted in `candidates_below_confidence` and
    logged (techne#465). This test still pins the FILTER; the counting is pinned
    by test_low_confidence_candidates_are_not_silently_dropped.
    """
    config = _mk_config(tmp_path, enabled=True)
    config.reflect.min_confidence = "high"
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps(
            [
                {"scope": "knowledge", "type": "fact", "content": "low-conf", "confidence": "low"},
                {
                    "scope": "knowledge",
                    "type": "fact",
                    "content": "med-conf",
                    "confidence": "medium",
                },
                {
                    "scope": "knowledge",
                    "type": "fact",
                    "content": "high-conf",
                    "confidence": "high",
                },
            ]
        )

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert result["candidates_written"] == 1
    cand = list_candidates(config.db_path, status="pending")[0]
    assert cand["content"] == "high-conf"


def test_reflect_llm_error_counted_not_raised(tmp_path):
    """Hook contract: an LLM failure must not propagate out of reflect_on_session."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))]

    def angry_llm(_):
        raise RuntimeError("provider exploded")

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=angry_llm)
    assert result["errors"] == 1
    assert result["candidates_written"] == 0


def test_reflect_write_to_live_writes_knowledge(tmp_path):
    """write_to='live' should land entries in the knowledge table directly."""
    config = _mk_config(tmp_path, enabled=True, write_to="live")
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps(
            [
                {
                    "scope": "knowledge",
                    "type": "fact",
                    "content": "live-write test",
                    "confidence": "high",
                },
            ]
        )

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert result["candidates_promoted"] == 1

    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT content FROM knowledge WHERE content = ?", ("live-write test",)
        ).fetchone()
        assert row is not None
        cand = conn.execute("SELECT status, promoted_to_id FROM knowledge_candidates").fetchone()
        assert cand["status"] == "promoted"
        assert cand["promoted_to_id"] is not None
    finally:
        conn.close()


# --- review actions -------------------------------------------------------


def _seed_candidate(config, content="seed", scope="knowledge", type_="fact") -> int:
    """Helper: insert one pending candidate by running reflect with a stub LLM.
    Threads are unique per call so multiple seeds don't share a thread row."""
    # Each seed call gets its own session + thread_num for FK uniqueness
    session_id, obs = _mk_session_with_obs(config, 3)
    thread = Thread(
        thread_num=1,
        label=f"seed-{content}",
        summary=f"seed {content}",
        observation_indices=list(range(3)),
    )
    _save_thread_row(config, session_id, thread)

    def fake_llm(_):
        return json.dumps(
            [{"scope": scope, "type": type_, "content": content, "confidence": "high"}]
        )

    config.reflect.enabled = True
    reflect_on_session(config, session_id, [thread], obs, generate_fn=fake_llm)
    # Return the actual max id; created_at has 1s resolution so seeds tie.
    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM knowledge_candidates WHERE source_session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def test_promote_writes_to_live_and_marks_promoted(tmp_path):
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    cand_id = _seed_candidate(config, content="promote me")

    new_id = promote_candidate(config.db_path, cand_id)
    assert new_id is not None

    conn = connect(config.db_path)
    try:
        live = conn.execute("SELECT content FROM knowledge WHERE id = ?", (new_id,)).fetchone()
        assert live["content"] == "promote me"
        cand = conn.execute(
            "SELECT status, promoted_to_id FROM knowledge_candidates WHERE id = ?",
            (cand_id,),
        ).fetchone()
        assert cand["status"] == "promoted"
        assert cand["promoted_to_id"] == new_id
    finally:
        conn.close()


def test_promote_rejects_double_promotion(tmp_path):
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    cand_id = _seed_candidate(config)
    promote_candidate(config.db_path, cand_id)
    try:
        promote_candidate(config.db_path, cand_id)
    except ValueError as e:
        assert "already" in str(e)
    else:
        raise AssertionError("expected ValueError on second promotion")


def test_promote_no_duplicate_knowledge_row(tmp_path):
    """Second promote call must not insert a second knowledge row (TOCTOU guard)."""
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    cand_id = _seed_candidate(config, content="atomic promote test")

    first_id = promote_candidate(config.db_path, cand_id)
    assert first_id is not None

    with pytest.raises(ValueError, match="already"):
        promote_candidate(config.db_path, cand_id)

    conn = connect(config.db_path)
    try:
        knowledge_rows = conn.execute(
            "SELECT id FROM knowledge WHERE content = 'atomic promote test'"
        ).fetchall()
        assert len(knowledge_rows) == 1, f"Expected 1 knowledge row, got {len(knowledge_rows)}"
        cand = conn.execute(
            "SELECT status, promoted_to_id FROM knowledge_candidates WHERE id = ?",
            (cand_id,),
        ).fetchone()
        assert cand["status"] == "promoted"
        assert cand["promoted_to_id"] == first_id
    finally:
        conn.close()


def test_discard_flips_status(tmp_path):
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    cand_id = _seed_candidate(config)
    discard_candidate(config.db_path, cand_id)
    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM knowledge_candidates WHERE id = ?", (cand_id,)
        ).fetchone()
        assert row["status"] == "discarded"
    finally:
        conn.close()


def test_candidate_stats_counts_buckets(tmp_path):
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    a = _seed_candidate(config, content="a")
    b = _seed_candidate(config, content="b")
    _seed_candidate(config, content="c")
    promote_candidate(config.db_path, a)
    discard_candidate(config.db_path, b)
    s = candidate_stats(config.db_path)
    assert s["pending"] == 1
    assert s["promoted"] == 1
    assert s["discarded"] == 1


# --- config + schema integration ------------------------------------------


def test_config_round_trip_reflect_block(tmp_path):
    """ReflectConfig must survive save() → load()."""
    from techne.config import Config

    cfg_path = tmp_path / "config.yaml"
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    cfg.reflect.enabled = True
    cfg.reflect.min_observations_per_thread = 12
    cfg.reflect.write_to = "live"
    cfg.reflect.model = "claude-haiku-4-5-20251001"
    cfg.save(cfg_path)

    reloaded = Config.load(cfg_path)
    assert reloaded.reflect.enabled is True
    assert reloaded.reflect.min_observations_per_thread == 12
    assert reloaded.reflect.write_to == "live"
    assert reloaded.reflect.model == "claude-haiku-4-5-20251001"


def test_schema_v6_creates_candidates_table(tmp_path):
    """init_db on a fresh DB should produce the v6 schema with the new table."""
    db_path = tmp_path / "fresh.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_candidates'"
        ).fetchone()
        assert row is not None
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version >= 6
    finally:
        conn.close()


# --- agent-assisted emit / ingest (techne#92 promotion path) --------------


def _candidate_array(content="Use JWT for service auth"):
    return json.dumps(
        [
            {
                "scope": "knowledge",
                "type": "decision",
                "content": content,
                "tags": "auth",
                "confidence": "high",
            }
        ]
    )


def test_emit_reflection_batch_builds_prompts(tmp_path):
    from techne.core.reflect import emit_reflection_batch

    config = _mk_config(tmp_path, enabled=False)  # emit must NOT require enabled
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="auth", summary="auth", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)  # production saves threads before reflect
    batch = emit_reflection_batch(config, session_id, [thread], obs)
    assert len(batch) == 1
    item = batch[0]
    assert item["session_id"] == session_id
    assert item["thread_num"] == 1
    # The resolved, persisted FK id (techne#106) — matches the saved threads row.
    assert item["thread_id"] == f"{session_id}-1"
    assert item["prompt_hash"] and item["prompt"]
    assert "JSON array" in item["prompt"]  # the real reflect prompt


def test_emit_respects_min_observations(tmp_path):
    from techne.core.reflect import emit_reflection_batch

    config = _mk_config(tmp_path, enabled=False)  # min_observations_per_thread = 3
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [
        Thread(thread_num=1, label="big", summary="big", observation_indices=list(range(5))),
        Thread(thread_num=2, label="tiny", summary="tiny", observation_indices=[0, 1]),  # < 3
    ]
    batch = emit_reflection_batch(config, session_id, threads, obs)
    assert [b["thread_num"] for b in batch] == [1]


def test_ingest_writes_candidates(tmp_path):
    from techne.core.reflect import ingest_reflection_response

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    thread_id = f"{session_id}-1"
    counts = ingest_reflection_response(
        config, session_id, thread_id, "deadbeef", _candidate_array()
    )
    assert counts["candidates_written"] == 1
    rows = list_candidates(config.db_path, status="all")
    assert len(rows) == 1
    assert rows[0]["model_used"] == "agent-assisted"
    assert rows[0]["source_thread_id"] == thread_id


def test_ingest_is_idempotent_on_replay(tmp_path):
    """Exactly-once ingest (techne#236 Gap 1): re-ingesting the SAME thread's answer
    (same prompt_hash + candidate content) must NOT insert a duplicate candidate — the
    partial UNIQUE(prompt_hash, content) index + ON CONFLICT DO NOTHING collapse it to a
    single row, and the second pass reports zero written."""
    from techne.core.reflect import ingest_reflection_response

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    thread_id = f"{session_id}-1"

    first = ingest_reflection_response(config, session_id, thread_id, "deadbeef", _candidate_array())
    assert first["candidates_written"] == 1
    second = ingest_reflection_response(
        config, session_id, thread_id, "deadbeef", _candidate_array()
    )
    assert second["candidates_written"] == 0  # duplicate skipped, not re-inserted

    rows = list_candidates(config.db_path, status="all")
    assert len(rows) == 1, "re-ingest must not create a second candidate row"


def test_ingest_write_to_live_promotes(tmp_path):
    from techne.core.knowledge import list_entries
    from techne.core.reflect import ingest_reflection_response

    config = _mk_config(tmp_path, enabled=False, write_to="live")
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    counts = ingest_reflection_response(
        config, session_id, f"{session_id}-1", "deadbeef", _candidate_array()
    )
    assert counts["candidates_promoted"] == 1
    live = list_entries(config.db_path, limit=10)
    assert any("JWT for service auth" in r["content"] for r in live)


def test_emit_ingest_heuristic_thread_fk_resolves(tmp_path):
    """Regression for the silent #92 bug: a heuristic thread (boundary_signal set)
    gets a content-addressed id, NOT "{session}-{num}". emit must surface that real
    id and ingest must use it, or the candidates FK fails and inserts vanish."""
    from techne.core.reflect import emit_reflection_batch, ingest_reflection_response
    from techne.core.threads import save_threads

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, obs = _mk_session_with_obs(config, 5)
    t = Thread(
        thread_num=1,
        label="h",
        summary="h",
        observation_indices=list(range(5)),
        boundary_signal="0.9",  # heuristic → content-addressed id
    )
    save_threads([t], session_id, config.db_path, parent_links=None)
    batch = emit_reflection_batch(config, session_id, [t], obs)
    assert batch[0]["thread_id"] != f"{session_id}-1"  # the bug's tell
    counts = ingest_reflection_response(
        config, session_id, batch[0]["thread_id"], batch[0]["prompt_hash"], _candidate_array()
    )
    assert counts["candidates_written"] == 1 and counts["errors"] == 0


def test_emit_ingest_roundtrip_equals_reflect_on_session(tmp_path):
    """emit -> agent-answers -> ingest must produce the same candidates as the
    automated reflect_on_session with the equivalent generate_fn."""
    from techne.core.reflect import (
        emit_reflection_batch,
        ingest_reflection_response,
        reflect_on_session,
    )

    raw = _candidate_array("Roundtrip fact")
    threads = [Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5)))]

    # Automated path
    cfg_auto = _mk_config(tmp_path / "auto", enabled=True, write_to="candidates")
    sid_a, obs_a = _mk_session_with_obs(cfg_auto, 5)
    _save_thread_row(cfg_auto, sid_a, threads[0])
    res_auto = reflect_on_session(cfg_auto, sid_a, threads, obs_a, generate_fn=lambda _p: raw)

    # Agent-assisted path: emit (no LLM) then ingest the same raw answer
    cfg_manual = _mk_config(tmp_path / "manual", enabled=False, write_to="candidates")
    sid_m, obs_m = _mk_session_with_obs(cfg_manual, 5)
    _save_thread_row(cfg_manual, sid_m, threads[0])
    batch = emit_reflection_batch(cfg_manual, sid_m, threads, obs_m)
    assert len(batch) == 1
    res_manual = ingest_reflection_response(
        cfg_manual, sid_m, batch[0]["thread_id"], batch[0]["prompt_hash"], raw
    )

    assert res_auto["candidates_written"] == res_manual["candidates_written"] == 1
    auto_rows = list_candidates(cfg_auto.db_path, status="all")
    manual_rows = list_candidates(cfg_manual.db_path, status="all")
    assert len(auto_rows) == len(manual_rows) == 1
    assert auto_rows[0]["content"] == manual_rows[0]["content"] == "Roundtrip fact"


def test_promote_identity_candidate_no_deadlock(tmp_path):
    """Identity promotion must reuse the caller's open connection — a second
    connection self-deadlocks against the candidate UPDATE's write lock, which
    made identity candidates impossible to promote (identity table stuck empty)."""
    from techne.core.identity import get as identity_get
    from techne.core.reflect import ingest_reflection_response, promote_candidate

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    raw = json.dumps(
        [
            {
                "scope": "identity",
                "type": "user.prefers.terse",
                "content": "Operator wants terse replies",
                "tags": "style",
                "confidence": "high",
            }
        ]
    )
    ingest_reflection_response(config, session_id, f"{session_id}-1", "h", raw)
    cand = list_candidates(config.db_path, status="pending")[0]
    promote_candidate(config.db_path, cand["id"])  # must not deadlock
    assert identity_get(config.db_path, "user.prefers.terse") == "Operator wants terse replies"


# --- effective_write_to (techne#107 PR4) -----------------------------------


def test_effective_write_to_observe_honours_reflect_write_to():
    """Under observe posture (default), effective_write_to == reflect.write_to."""
    cfg = Config()
    cfg.reflect.write_to = "candidates"
    assert effective_write_to(cfg) == "candidates"

    cfg.reflect.write_to = "live"
    assert effective_write_to(cfg) == "live"


def test_effective_write_to_spool_primary_overrides_to_candidates():
    """spool-primary forces candidates regardless of reflect.write_to."""
    cfg = Config()
    cfg.posture = PostureConfig(mode="spool-primary", cc_memory_detected=True)
    cfg.reflect.write_to = "live"
    assert effective_write_to(cfg) == "candidates"

    # Also holds when write_to is already candidates
    cfg.reflect.write_to = "candidates"
    assert effective_write_to(cfg) == "candidates"


def test_effective_write_to_defensive_no_posture_attr():
    """A Config object without a posture attr (old code) falls through to reflect.write_to."""
    cfg = Config()
    cfg.reflect.write_to = "live"
    del cfg.posture  # simulate old config
    assert effective_write_to(cfg) == "live"


def test_spool_primary_blocks_live_write_via_ingest(tmp_path):
    """Integration: posture=spool-primary + reflect.write_to='live' must write a
    candidate but NOT touch the live knowledge table (effective_write_to gate)."""
    config = _mk_config(tmp_path, enabled=False, write_to="live")
    config.posture = PostureConfig(mode="spool-primary", cc_memory_detected=True)
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    counts = ingest_reflection_response(
        config,
        session_id,
        f"{session_id}-1",
        "deadbeef",
        _candidate_array("spool-primary live-gate test"),
    )
    assert counts["candidates_written"] == 1
    assert counts["candidates_promoted"] == 0  # gate blocked

    conn = connect(config.db_path)
    try:
        knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        assert knowledge_count == 0  # live table untouched
        rows = list_candidates(config.db_path, status="all")
        assert len(rows) == 1
        assert rows[0]["content"] == "spool-primary live-gate test"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #246 — silent-failure logging: swallowed exceptions must now log
# ---------------------------------------------------------------------------


def _candidate_json(content: str = "a durable fact") -> str:
    return json.dumps(
        [{"scope": "knowledge", "type": "fact", "content": content,
          "tags": "test", "confidence": "high"}]
    )


def test_ingest_reflection_response_logs_on_insert_failure(tmp_path, caplog):
    """#246: when _insert_candidate raises, log.exception fires (not silent swallow)
    AND errors counter still increments (behavior preserved)."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 3)

    with patch("techne.core.reflect._insert_candidate", side_effect=RuntimeError("db gone")):
        with caplog.at_level(logging.ERROR, logger="techne.core.reflect"):
            counts = ingest_reflection_response(
                config, session_id, "t-test", "deadbeef", _candidate_json()
            )

    assert counts["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any(
        "ingest_reflection_response" in r.message for r in caplog.records
    ), "log.exception must emit a record naming the function"


def test_reflect_on_session_logs_on_generate_fn_failure(tmp_path, caplog):
    """#246: when the per-thread LLM call raises, log.exception fires AND errors
    counter increments.  Uses an injectable generate_fn that raises."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="crash thread", summary="s", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    def exploding_generate_fn(_prompt):
        raise RuntimeError("LLM unavailable")

    with caplog.at_level(logging.ERROR, logger="techne.core.reflect"):
        result = reflect_on_session(
            config, session_id, [thread], obs, generate_fn=exploding_generate_fn
        )

    assert result["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any(
        "reflect_on_session" in r.message for r in caplog.records
    ), "log.exception must emit a record naming the function"


def test_reflect_on_session_logs_on_make_generate_fn_failure(tmp_path, caplog):
    """#246: when _make_generate_fn raises, log.exception fires AND errors counter
    increments (generate_fn=None path so the real _make_generate_fn is called)."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    with patch("techne.core.reflect._make_generate_fn", side_effect=RuntimeError("no model")):
        with caplog.at_level(logging.ERROR, logger="techne.core.reflect"):
            result = reflect_on_session(config, session_id, [thread], obs)

    assert result["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any(
        "reflect_on_session" in r.message for r in caplog.records
    ), "log.exception must emit a record naming the function"


# ---------------------------------------------------------------------------
# techne#465 — a discarded reflection must not report as a successful empty one
# ---------------------------------------------------------------------------


def _ingest(config, session_id, raw):
    from techne.core.reflect import ingest_reflection_response

    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    return ingest_reflection_response(config, session_id, f"{session_id}-1", "deadbeef", raw)


def test_a_discarded_reflection_is_not_reported_as_an_empty_one(tmp_path):
    """THE BUG, at the level the operator actually reads.

    `parse_response` returned [] on failure and the caller never surfaced it, so
    ingest returned `{..., "errors": 0}` for BOTH an unparseable response and a
    correct pass where the model had nothing to save. A whole session's
    reflection could be thrown away and reported as success.

    Asserted as a DIFFERENCE between the two, not as an absolute — an assertion
    on the failure alone would still pass if the honest-empty case started
    reporting an error too, which would be a different bug of the same family.
    """
    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)

    broken = _ingest(config, session_id, "the model explained itself in prose")
    honest = _ingest(config, session_id, "[]")

    assert broken["errors"] == 1, broken
    assert broken["parse_failures"] == 1, broken
    assert honest["errors"] == 0, honest
    assert honest["parse_failures"] == 0, honest

    moving = ("errors", "parse_failures")
    assert {k: broken[k] for k in moving} != {k: honest[k] for k in moving}, (
        "a discarded reflection is still indistinguishable from an empty one"
    )


def test_a_truncated_response_is_the_realistic_trigger(tmp_path):
    """Not an exotic case: a long reflection hitting an output limit yields an
    unterminated array, and that is the most likely way this fires in practice."""
    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    counts = _ingest(config, session_id, '[{"scope": "knowledge", "type": "fact", "conte')
    assert counts["parse_failures"] == 1 and counts["errors"] == 1


def test_a_parse_failure_says_so_in_the_log(tmp_path, caplog):
    """The counter is machine-readable; the WARNING is what a human sees.

    It must say the reflection was discarded rather than empty, because the whole
    defect was an operator reasonably concluding "the model found nothing".
    """
    import logging

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)

    # The escape-hatch hint is for operators who do NOT already capture the raw
    # response — pointing at a setting that is already on would be noise.
    config.reflect.log_raw_response = False
    with caplog.at_level(logging.WARNING):
        _ingest(config, session_id, "prose, no array")
    text = caplog.text
    assert "DISCARDED" in text, text
    assert "log_raw_response" in text, "must point at the escape hatch for diagnosis"

    caplog.clear()
    config.reflect.log_raw_response = True
    with caplog.at_level(logging.WARNING):
        _ingest(config, session_id, "prose, no array")
    assert "DISCARDED" in caplog.text
    assert "log_raw_response" not in caplog.text, (
        "do not advise turning on a setting that is already on"
    )


def test_all_candidates_rejected_is_reported_separately(tmp_path):
    """The array parsed and every item was thrown away by validation — distinct
    from a parse failure, and equally silent before this."""
    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    counts = _ingest(
        config,
        session_id,
        '[{"scope": "bogus", "type": "fact", "content": "x"},'
        ' {"scope": "knowledge", "type": "nope", "content": "y"}]',
    )
    assert counts["candidates_dropped"] == 2, counts
    assert counts["parse_failures"] == 0, "the array DID parse"
    assert counts["errors"] == 0, "a rejected candidate is not an insert error"


def test_every_aggregator_survives_a_new_counts_key(tmp_path):
    """DRIVE the aggregators; do not read them.

    This replaces an `inspect.getsource` check that asserted the key names
    appeared in each function's text. Prudence showed it was a false-negative
    machine: deleting the keys while leaving a COMMENT that merely named them in
    quotes made it pass, while nine behavioural tests caught the same mutation.
    Worse, being anchored to one function by name it structurally could not have
    caught the third aggregator — `techne reflect ingest` in cli.py — which
    crashed with KeyError on every non-empty response through 2381 passing tests.

    So: call each path for real with a response that populates a new key, and let
    the `+=` blow up if a dict is missing it.
    """
    import json as _json

    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config
    from techne.core.reflect import reflect_on_session

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    # 1. the session aggregator
    reflect_on_session(config, session_id, [thread], obs)

    # 2. the CLI aggregator — the one nothing exercised. A NON-EMPTY response is
    #    required: the crash was on the `totals[k] += v` loop, which an empty
    #    input never reaches.
    responses = tmp_path / "responses.json"
    responses.write_text(
        _json.dumps(
            [
                {
                    "session_id": session_id,
                    "thread_id": f"{session_id}-1",
                    "prompt_hash": "deadbeef",
                    "raw_response": _candidate_array(),
                }
            ]
        )
    )
    import unittest.mock as _mock

    with _mock.patch.object(Config, "load", classmethod(lambda cls: config)):
        result = CliRunner().invoke(main, ["reflect", "ingest", str(responses)])
    assert result.exit_code == 0, (
        f"`techne reflect ingest` crashed: {result.output}\n{result.exception}"
    )


def test_cli_ingest_reports_a_parse_failure(tmp_path):
    """The CLI must also SURFACE the new signal, not merely survive it."""
    import json as _json
    import unittest.mock as _mock

    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    session_id, _ = _mk_session_with_obs(config, 5)
    _save_thread_row(
        config,
        session_id,
        Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5))),
    )
    responses = tmp_path / "r.json"
    responses.write_text(
        _json.dumps(
            [
                {
                    "session_id": session_id,
                    "thread_id": f"{session_id}-1",
                    "prompt_hash": "d",
                    "raw_response": "the model wrote prose instead of an array",
                }
            ]
        )
    )
    with _mock.patch.object(Config, "load", classmethod(lambda cls: config)):
        result = CliRunner().invoke(main, ["reflect", "ingest", str(responses)])
    assert result.exit_code == 0, result.output


def test_low_confidence_candidates_are_not_silently_dropped(tmp_path):
    """The same silence one line past validation, on the DEFAULT config.

    min_confidence defaults to "medium" while the prompt invites "low", so valid
    low-confidence candidates are discarded here — and before this produced a
    counts dict byte-identical to honest-empty. (Prudence, #483 review.)
    """
    config = _mk_config(tmp_path, enabled=False, write_to="candidates")
    # The fixture relaxes this to "low"; the DEFAULT is "medium" and that is the
    # config the silence actually bites on, so set it back explicitly.
    config.reflect.min_confidence = "medium"
    session_id, _ = _mk_session_with_obs(config, 5)
    raw = (
        '[{"scope": "knowledge", "type": "fact", "content": "a hunch", "confidence": "low"},'
        ' {"scope": "knowledge", "type": "fact", "content": "another hunch",'
        ' "confidence": "low"}]'
    )
    counts = _ingest(config, session_id, raw)
    assert counts["candidates_below_confidence"] == 2, counts
    assert counts["candidates_proposed"] == 0
    assert counts["parse_failures"] == 0, "the array parsed and the items were valid"
