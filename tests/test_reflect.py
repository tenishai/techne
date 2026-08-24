"""Tests for the end-of-session reflection pass."""

import json
import logging
import sqlite3
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
    raw = (
        '[{"scope": "nonsense", "type": "fact", "content": "x"},'
        ' {"scope": "knowledge", "type": "not-a-type", "content": "y"}]'
    )
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


def _enqueue_as_production_does(config, session_id, threads, observations) -> int:
    """Seed the reflection queue by the route both production callers take.

    #527 r5: these tests used to call `enqueue_reflection_debt(db_path,
    session_id)` and lean on a "queue every thread" default. That default is gone
    — it was the r4 bug wearing a docstring — but the deeper problem was that the
    regression tests for r1/r2/r3 were seeding by a route production could not
    take, so they no longer pinned the shape that ships. Going through
    `emittable_thread_ids` here means a change to the predicate breaks these
    tests, which is the point of having them.
    """
    from techne.core.reflect import emittable_thread_ids, enqueue_reflection_debt

    return enqueue_reflection_debt(
        config.db_path,
        session_id,
        thread_ids=emittable_thread_ids(config, session_id, threads, observations),
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

    first = ingest_reflection_response(
        config, session_id, thread_id, "deadbeef", _candidate_array()
    )
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
        [
            {
                "scope": "knowledge",
                "type": "fact",
                "content": content,
                "tags": "test",
                "confidence": "high",
            }
        ]
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
    assert any("ingest_reflection_response" in r.message for r in caplog.records), (
        "log.exception must emit a record naming the function"
    )


def test_reflect_on_session_logs_on_generate_fn_failure(tmp_path, caplog):
    """#246: when the per-thread LLM call raises, log.exception fires AND errors
    counter increments.  Uses an injectable generate_fn that raises."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(
        thread_num=1, label="crash thread", summary="s", observation_indices=list(range(5))
    )
    _save_thread_row(config, session_id, thread)

    def exploding_generate_fn(_prompt):
        raise RuntimeError("LLM unavailable")

    with caplog.at_level(logging.ERROR, logger="techne.core.reflect"):
        result = reflect_on_session(
            config, session_id, [thread], obs, generate_fn=exploding_generate_fn
        )

    assert result["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any("reflect_on_session" in r.message for r in caplog.records), (
        "log.exception must emit a record naming the function"
    )


def test_reflect_on_session_logs_on_make_generate_fn_failure(tmp_path, caplog):
    """#246: when _make_generate_fn raises, log.exception fires AND errors counter
    increments (generate_fn=None path so the real _make_generate_fn is called)."""
    config = _mk_config(tmp_path, enabled=True)
    # A BOUND model is what puts us on the _make_generate_fn path at all (techne#555):
    # unbound skips the automatic pass entirely, so without this the test would assert
    # the error counter on a pass that never tried.
    config.reflect.model = "ollama/llama3"
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="x", summary="x", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    with patch("techne.core.reflect._make_generate_fn", side_effect=RuntimeError("no model")):
        with caplog.at_level(logging.ERROR, logger="techne.core.reflect"):
            result = reflect_on_session(config, session_id, [thread], obs)

    assert result["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any("reflect_on_session" in r.message for r in caplog.records), (
        "log.exception must emit a record naming the function"
    )


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

    # enabled=True IS LOAD-BEARING. With enabled=False, reflect_on_session returns
    # at its guard BEFORE the `result[key] += val` loop this test is named for —
    # so leg 1 was hollow: popping a key from the session seed PASSED it while
    # failing nine other tests. Caught by Prudence mutating exactly that.
    config = _mk_config(tmp_path, enabled=True, write_to="candidates")
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="a", summary="a", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    # 1. the session aggregator — driven with a stub model so the loop is reached
    #    without an LLM call.
    reflect_on_session(
        config, session_id, [thread], obs, generate_fn=lambda *a, **kw: _candidate_array()
    )

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


def test_candidates_beyond_max_items_are_counted_as_dropped(tmp_path):
    """The fourth silent drop: `max_items` truncation.

    `dropped` was computed after the slice, so a model producing more candidates
    than `max_candidates_per_thread` had the excess vanish with every counter at
    zero. Not byte-identical to honest-empty — the operator is not misled — but
    the cap is CONFIG, and tuning it blind is the thing this issue is about.
    """
    from techne.core.reflect import parse_response

    items = ", ".join(
        f'{{"scope": "knowledge", "type": "fact", "content": "fact {i}"}}' for i in range(8)
    )
    r = parse_response(f"[{items}]", max_items=5)
    assert len(r.candidates) == 5
    assert r.failure is None
    assert r.dropped == 3, f"the 3 truncated candidates must be counted, got {r.dropped}"


# --- reflection debt queue (techne#514) -----------------------------------


class TestReflectionQueue:
    """session_end must RECORD the debt, not just count it.

    The bug: session_end logged "403 obs, 19 threads" and enqueued nothing, so
    reflection survived exactly one exit path — the polite one. Two sessions
    ended four minutes apart on 2026-08-07 with 297 and 403 observations and
    produced ZERO candidates between them; the only reason one was ever reflected
    is that a peer noticed by hand.
    """

    def test_enqueue_records_one_row_per_thread(self, tmp_path):
        from techne.core.reflect import pending_reflection_queue

        config = _mk_config(tmp_path, enabled=True)
        session_id, obs = _mk_session_with_obs(config, 5)
        threads = [
            Thread(thread_num=n, label=f"t{n}", summary="s", observation_indices=[0, 1, 2])
            for n in (1, 2, 3)
        ]
        for t in threads:
            _save_thread_row(config, session_id, t)

        assert _enqueue_as_production_does(config, session_id, threads, obs) == 3
        pending = pending_reflection_queue(config.db_path)
        assert len(pending) == 3
        assert {p["session_id"] for p in pending} == {session_id}

    def test_enqueue_is_idempotent(self, tmp_path):
        """session_end fires once per TURN, not once per session — so this runs
        many times against the same threads. Idempotency is enforced by the
        UNIQUE(thread_id) schema constraint, not by a check-then-insert, so two
        sessions racing cannot double-queue either.
        """
        from techne.core.reflect import pending_reflection_queue

        config = _mk_config(tmp_path, enabled=True)
        session_id, obs = _mk_session_with_obs(config, 5)
        thread = Thread(thread_num=1, label="t", summary="s", observation_indices=[0, 1, 2])
        _save_thread_row(config, session_id, thread)

        assert _enqueue_as_production_does(config, session_id, [thread], obs) == 1
        assert _enqueue_as_production_does(config, session_id, [thread], obs) == 0
        assert _enqueue_as_production_does(config, session_id, [thread], obs) == 0
        assert len(pending_reflection_queue(config.db_path)) == 1

    def test_queue_surfaces_debt_that_the_recency_heuristic_loses(self, tmp_path):
        """THE REGRESSION TEST FOR THE ACTUAL BUG, and the reason the queue
        exists rather than a bigger limit on the old path.

        `pending_reflection` scans the N most-recent sessions and filters AFTER
        the LIMIT — by its own docstring, "a genuinely-old pending session past
        that window is intentionally not surfaced." So debt does not merely go
        unnoticed, it becomes PERMANENTLY invisible once enough sessions follow
        it. The queue is a ledger of what was owed; it does not care how many
        sessions came later.
        """
        from techne.core.reflect import (
            pending_reflection,
            pending_reflection_queue,
        )

        config = _mk_config(tmp_path, enabled=True)
        old_session, obs = _mk_session_with_obs(config, 5)
        thread = Thread(thread_num=1, label="old", summary="s", observation_indices=[0, 1, 2])
        _save_thread_row(config, old_session, thread)
        _enqueue_as_production_does(config, old_session, [thread], obs)

        # Bury it under newer sessions, past the heuristic's window.
        for _ in range(8):
            _mk_session_with_obs(config, 5)

        recent_ids = {p["session_id"] for p in pending_reflection(config, limit=5)}
        assert old_session not in recent_ids, "precondition: the heuristic has lost it"

        queued_ids = {p["session_id"] for p in pending_reflection_queue(config.db_path)}
        assert old_session in queued_ids, "the queue must still owe it"

    def test_drain_stamps_once_and_only_once(self, tmp_path):
        """Drained rows are STAMPED, not deleted — the queue doubles as the
        record that the debt existed and was paid, which is the evidence #514
        was filed on. A second drain must not rewrite the first payment.
        """
        from techne.core.database import connect
        from techne.core.reflect import (
            drain_reflection_queue,
            pending_reflection_queue,
        )

        config = _mk_config(tmp_path, enabled=True)
        session_id, obs = _mk_session_with_obs(config, 5)
        thread = Thread(thread_num=1, label="t", summary="s", observation_indices=[0, 1, 2])
        _save_thread_row(config, session_id, thread)
        _enqueue_as_production_does(config, session_id, [thread], obs)
        thread_id = pending_reflection_queue(config.db_path)[0]["thread_id"]

        assert drain_reflection_queue(config.db_path, [thread_id]) == 1
        assert pending_reflection_queue(config.db_path) == []

        conn = connect(config.db_path)
        try:
            first = conn.execute(
                "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        assert drain_reflection_queue(config.db_path, [thread_id]) == 0, "second drain is a no-op"

        conn = connect(config.db_path)
        try:
            still = conn.execute(
                "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert still == first, "the first payment is the true one"

    def test_drain_of_nothing_is_not_a_query(self, tmp_path):
        from techne.core.reflect import drain_reflection_queue

        config = _mk_config(tmp_path, enabled=True)
        assert drain_reflection_queue(config.db_path, []) == 0


def test_redetect_preserves_reflection_debt(tmp_path):
    """#527 review, CRITICAL: `techne redetect` deletes a session's threads, the
    CASCADE takes reflection_queue with them, and session_end does not re-run —
    so the debt #514 exists to protect was silently destroyed by an ordinary
    operator command. Every caller that deletes threads must re-enqueue.
    """
    from techne.core.reflect import pending_reflection_queue
    from techne.lifecycle import redetect_threads

    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    from techne.core.state import add_observation

    for i in range(12):
        add_observation(config.db_path, session_id, "Edit", f"edit {i}")
    thread = Thread(thread_num=1, label="t", summary="s", observation_indices=[0, 1, 2])
    _save_thread_row(config, session_id, thread)
    _enqueue_as_production_does(config, session_id, [thread], obs)
    assert len(pending_reflection_queue(config.db_path)) == 1

    redetect_threads(config, session_id)

    assert pending_reflection_queue(config.db_path), (
        "redetect destroyed the queued reflection debt — CASCADE fired and nothing "
        "re-enqueued (#527 review)"
    )


def test_ingest_drains_the_queue_so_the_banner_can_clear(tmp_path):
    """#527 r1: drain_reflection_queue had NO production caller, so the queued-debt
    banner grew forever and the action it named ("run techne_reflect_emit")
    returned count:0 on an already-reflected session. The drain half of #504's
    shape, inside the PR that refused to ship it.
    """
    from techne.core.reflect import (
        emit_reflection_batch,
        pending_reflection_queue,
    )
    from techne.mcp_server import _techne_reflect_ingest

    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="t", summary="s", observation_indices=[0, 1, 2, 3, 4])
    _save_thread_row(config, session_id, thread)
    _enqueue_as_production_does(config, session_id, [thread], obs)
    assert len(pending_reflection_queue(config.db_path)) == 1

    batch = emit_reflection_batch(config, session_id, [thread], obs)
    assert batch, "precondition: emit produced a prompt to answer"
    item = batch[0]
    _techne_reflect_ingest(
        {
            "session_id": session_id,
            "thread_id": item["thread_id"],
            "prompt_hash": item["prompt_hash"],
            "response": _candidate_array("Drain fact"),
        },
        config,
    )

    assert pending_reflection_queue(config.db_path) == [], (
        "ingest must drain the queue — otherwise the banner never clears and "
        "saturates into unconditional noise"
    )


def test_redetect_preserves_a_paid_drain_stamp(tmp_path):
    """#527 r2: re-enqueueing after redetect resurrected PAID debt as pending.

    Drives redetect TWICE. thread_row_id is content-addressed for
    heuristic-detected threads, so an unchanged segmentation yields the SAME id
    on the second pass — which is the case where the drain stamp must survive.
    (A changed segmentation is a genuinely new thread and SHOULD owe reflection.)
    """
    from techne.core.reflect import (
        drain_reflection_queue,
        pending_reflection_queue,
    )
    from techne.core.state import add_observation
    from techne.lifecycle import redetect_threads

    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 5)
    for i in range(12):
        add_observation(config.db_path, session_id, "Edit", f"edit {i}")

    redetect_threads(config, session_id)
    queued = pending_reflection_queue(config.db_path)
    assert queued, "precondition: the first redetect enqueued debt"
    before = {q["thread_id"] for q in queued}
    drain_reflection_queue(config.db_path, [q["thread_id"] for q in queued])
    assert pending_reflection_queue(config.db_path) == []

    redetect_threads(config, session_id)

    after = pending_reflection_queue(config.db_path)
    resurrected = {q["thread_id"] for q in after} & before
    assert not resurrected, (
        f"redetect resurrected already-reflected debt as pending: {resurrected} (#527 r2)"
    )


def test_the_cascade_fires_on_or_replace_not_only_on_delete(tmp_path):
    """`INSERT OR REPLACE` on threads.id removes the row and fires the
    reflection_queue CASCADE; `INSERT OR IGNORE` does not.

    database.py's schema comment tells the next implementer that a caller which
    removes a threads row must re-enqueue — and "removes" is not only DELETE.
    OR REPLACE resolves the conflict by deleting the existing row first, and
    save_threads picks its verb by whether the thread has a boundary_signal, so
    the two paths differ. That comment used to carry these numbers as prose; a
    measured number in a comment is a cached result with no invalidation
    (techne#541), so it is pinned here instead and the comment points at it.
    """
    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 5)

    def _seed_one_queued_thread(conn):
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('tid', ?, 1, 'l', 's', 'active')",
            (session_id,),
        )
        conn.execute(
            "INSERT INTO reflection_queue (session_id, thread_id) VALUES (?, 'tid')",
            (session_id,),
        )
        conn.commit()

    def _queue_rows_after(verb: str) -> int:
        conn = connect(config.db_path)
        try:
            conn.execute("DELETE FROM reflection_queue")
            conn.execute("DELETE FROM threads WHERE session_id = ?", (session_id,))
            conn.commit()
            _seed_one_queued_thread(conn)
            assert conn.execute("SELECT COUNT(*) FROM reflection_queue").fetchone()[0] == 1
            conn.execute(
                f"{verb} INTO threads (id, session_id, thread_num, label, summary, status) "
                "VALUES ('tid', ?, 1, 'l2', 's2', 'active')",
                (session_id,),
            )
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM reflection_queue").fetchone()[0]
        finally:
            conn.close()

    assert _queue_rows_after("INSERT OR REPLACE") == 0, (
        "OR REPLACE must be treated as a row removal — it fires the CASCADE, so a "
        "caller using it has to re-enqueue exactly as a DELETE caller does"
    )
    assert _queue_rows_after("INSERT OR IGNORE") == 1, (
        "OR IGNORE leaves the row in place, so the queued debt survives"
    )


def test_a_failed_enqueue_rolls_the_whole_redetect_back(tmp_path, monkeypatch):
    """#527 r5: the enqueue is INSIDE redetect's transaction, so a failure there
    leaves NO half-applied redetect.

    While the enqueue sat after the commit, a raise from it (a locked database is
    plausible — this repo deliberately supports concurrent peer sessions) left
    the threads committed, the debt unqueued, any paid drain stamps unrestored
    and set_session_summary never reached. That state is unrecoverable in
    practice: nothing anywhere re-derives debt for a thread that is emittable but
    has no queue row, so the loss is permanent unless an operator happens to run
    redetect again.

    The discriminator is the QUEUE, not the exception. Under the old ordering the
    DELETE had already committed, so the CASCADE had taken the queue with it and
    nothing put it back; under this one the rollback restores it.
    """
    import techne.core.reflect as reflect_mod
    from techne.core.reflect import pending_reflection_queue
    from techne.core.state import add_observation
    from techne.lifecycle import redetect_threads

    config = _mk_config(tmp_path, enabled=True)
    session_id, _ = _mk_session_with_obs(config, 5)
    for i in range(12):
        add_observation(config.db_path, session_id, "Edit", f"edit {i}")

    redetect_threads(config, session_id)
    before = {q["thread_id"] for q in pending_reflection_queue(config.db_path)}
    assert before, "precondition: the first redetect queued debt"

    def _explode(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reflect_mod, "enqueue_reflection_debt", _explode)
    with pytest.raises(sqlite3.OperationalError):
        redetect_threads(config, session_id)

    after = {q["thread_id"] for q in pending_reflection_queue(config.db_path)}
    assert after == before, (
        "a failed enqueue left the redetect half-applied: the DELETE committed and "
        "the CASCADE took the debt with it (#527 r5)"
    )


def test_a_parse_failure_does_not_stamp_the_debt_paid(tmp_path):
    """#527 r3, CRITICAL: the drain fired whenever thread_id was truthy —
    including the branch whose own comment reads "THE PASS DID NOT HAPPEN"
    (reflect.py:645). A discarded reflection was stamped paid, permanently,
    because drain is guarded on drained_at IS NULL.
    """
    from techne.core.reflect import (
        emit_reflection_batch,
        pending_reflection_queue,
    )
    from techne.mcp_server import _techne_reflect_ingest

    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    thread = Thread(thread_num=1, label="t", summary="s", observation_indices=[0, 1, 2, 3, 4])
    _save_thread_row(config, session_id, thread)
    _enqueue_as_production_does(config, session_id, [thread], obs)
    item = emit_reflection_batch(config, session_id, [thread], obs)[0]

    out = _techne_reflect_ingest(
        {
            "session_id": session_id,
            "thread_id": item["thread_id"],
            "prompt_hash": item["prompt_hash"],
            "response": "I am sorry, I cannot produce that JSON.",
        },
        config,
    )

    assert out.get("parse_failures") == 1, "precondition: the response did not parse"
    assert pending_reflection_queue(config.db_path), (
        "a DISCARDED reflection was stamped paid — the debt is now unrecoverable "
        "because drain is guarded on drained_at IS NULL (#527 r3)"
    )


def test_sub_threshold_threads_are_not_queued(tmp_path):
    """#527 r4: enqueue queued EVERY thread while emit skips any under
    min_observations_per_thread, so sub-threshold threads were queued, never
    emittable, and therefore never drainable — the banner accumulated rows
    nothing could clear.

    Uses the PRODUCTION default (8), not _mk_config's 3. The helper's lower value
    is why the suite could not see this.
    """
    from techne.core.reflect import (
        emit_reflection_batch,
        emittable_thread_ids,
        enqueue_reflection_debt,
        pending_reflection_queue,
    )

    config = _mk_config(tmp_path, enabled=True)
    config.reflect.min_observations_per_thread = 8  # production default
    session_id, obs = _mk_session_with_obs(config, 5)  # only 5 — below it
    thread = Thread(thread_num=1, label="t", summary="s", observation_indices=list(range(5)))
    _save_thread_row(config, session_id, thread)

    assert emit_reflection_batch(config, session_id, [thread], obs) == [], (
        "precondition: emit produces no prompt for a sub-threshold thread"
    )

    enqueue_reflection_debt(
        config.db_path,
        session_id,
        thread_ids=emittable_thread_ids(config, session_id, [thread], obs),
    )

    assert pending_reflection_queue(config.db_path) == [], (
        "a thread emit will never produce a prompt for must not be queued — it "
        "could never be drained (#527 r4)"
    )


# ---------------------------------------------------------------------------
# The CLI promotion path — the one the documented /techne-promote skill runs.
# tenishai/techne#642 umbrella: #644 (no drain), #645 (no cursor), #646 (stuck rows).
# ---------------------------------------------------------------------------


def _mk_db_backed_session(config, n_obs: int = 14) -> str:
    """A session whose observations and threads actually exist in the DB.

    emit_reflection_pass re-derives threads from the store (exactly as the MCP tool
    does), so these tests cannot hand it in-memory Thread objects the way the
    older emit_reflection_batch tests do. Going through add_observation +
    redetect_threads means these tests exercise the route production takes,
    including the enqueue.
    """
    from techne.core.state import add_observation
    from techne.lifecycle import redetect_threads

    session_id = start_session(config.db_path, project_path="/tmp/p", agent_identity="test-agent")
    for i in range(n_obs):
        add_observation(config.db_path, session_id, "Edit", f"edit {i}")
    redetect_threads(config, session_id)
    return session_id


def _fill_raw_responses(batch, content="CLI round-trip fact"):
    """Stand in for the agent: answer every emitted prompt."""
    for item in batch:
        item["raw_response"] = _candidate_array(content)
    return batch


def test_cli_round_trip_drains_the_queue_and_advances_the_cursor(tmp_path):
    """#644 + #645, together, because they are one broken path.

    `techne reflect emit` -> agent fills raw_response -> `techne reflect ingest`
    is the documented no-API-key promotion path. Before this, ingest wrote
    candidates and drained nothing (drain_reflection_queue had exactly one
    production caller, in mcp_server), and emit never wrote the
    `reflect:emitted:` cursor that pending_reflection's own docstring assumes.
    So the debt banner and the pending-reflection nudge could BOTH be cleared
    only by the MCP path, and the skill left no trace of having run.
    """
    from techne.core.database import connect
    from techne.core.extension_state import get_state
    from techne.core.reflect import (
        emit_reflection_pass,
        ingest_reflection_batch,
        pending_reflection_queue,
    )

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    owed_before = len(pending_reflection_queue(config.db_path))
    assert owed_before >= 1, "precondition: the session owes reflection"

    result = emit_reflection_pass(config, session_id)
    batch = result["batch"]
    assert batch, "precondition: emit produced a prompt"

    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    assert marker and marker.get("emitted"), (
        "emit must advance the reflect:emitted: cursor — without it "
        "pending_reflection re-nominates this session on every SessionStart (#645)"
    )

    totals = ingest_reflection_batch(config, _fill_raw_responses(batch))
    assert totals["candidates_written"] >= 1
    assert totals["drained"] == len(batch), "ingest must report the debt it paid (#644)"
    assert pending_reflection_queue(config.db_path) == [], (
        "the CLI ingest path must drain the queue, not just the MCP path (#644)"
    )


def test_cli_ingest_does_not_drain_an_unanswered_thread(tmp_path):
    """A blank raw_response is a deliberate skip, not work done.

    Draining it would stamp unpaid debt as paid — permanently, since drain is
    guarded on `drained_at IS NULL`.
    """
    from techne.core.reflect import (
        emit_reflection_pass,
        ingest_reflection_batch,
        pending_reflection_queue,
    )

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    owed = len(pending_reflection_queue(config.db_path))

    batch = emit_reflection_pass(config, session_id)["batch"]
    assert batch
    for item in batch:
        item["raw_response"] = ""  # the agent declined this thread

    totals = ingest_reflection_batch(config, batch)
    assert totals["skipped"] == len(batch)
    assert totals["drained"] == 0
    assert len(pending_reflection_queue(config.db_path)) == owed, (
        "an unanswered thread still owes reflection"
    )


def test_cli_ingest_does_not_drain_on_a_parse_failure(tmp_path):
    """A DISCARDED reflection is not a completed one (techne#465, drain half).

    Mirrors the MCP guard: drain only when parse_failures and errors are both 0.
    """
    from techne.core.reflect import (
        emit_reflection_pass,
        ingest_reflection_batch,
        pending_reflection_queue,
    )

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    owed = len(pending_reflection_queue(config.db_path))

    batch = emit_reflection_pass(config, session_id)["batch"]
    assert batch
    for item in batch:
        item["raw_response"] = "this is not JSON at all"

    totals = ingest_reflection_batch(config, batch)
    assert totals["parse_failures"] == len(batch)
    assert totals["drained"] == 0
    assert len(pending_reflection_queue(config.db_path)) == owed, (
        "a parse failure means the pass did not happen; the debt is still owed"
    )


def test_unemittable_queue_rows_are_reported_and_never_drained(tmp_path):
    """#646: the min_observations gate is evaluated twice, against different buckets.

    Enqueue threads that ARE emittable at session_end, then make them
    sub-threshold at emit time by raising the floor. Those rows are now real debt
    no prompt can ever pay. They must be REPORTED, not swept — draining them
    would restore a clean banner and destroy the only evidence the class exists,
    which is exactly how it survived #527 r4.
    """
    from techne.core.reflect import (
        emit_reflection_pass,
        pending_reflection_queue,
        unemittable_queue_rows,
    )

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    owed = len(pending_reflection_queue(config.db_path))
    assert owed >= 1

    # The second evaluation disagrees with the first — the divergence #646 measures.
    config.reflect.min_observations_per_thread = 9999

    stuck = unemittable_queue_rows(config, session_id)
    assert len(stuck) == owed, "every queued row is now unemittable and must be visible"

    result = emit_reflection_pass(config, session_id)
    assert result["count"] == 0
    assert result["unemittable_queued"] == owed
    assert "646" in result["unemittable_note"]
    assert len(pending_reflection_queue(config.db_path)) == owed, (
        "an unemittable row must stay queued — reporting it is the fix, draining it is the bug"
    )


def test_reflection_debt_stats_counts_the_ledger_not_the_candidates(tmp_path):
    """The debt ledger and the candidate counts answer different questions."""
    from techne.core.reflect import (
        drain_reflection_queue,
        pending_reflection_queue,
        reflection_debt_stats,
    )

    config = _mk_config(tmp_path, enabled=True)
    _mk_db_backed_session(config)
    queued = pending_reflection_queue(config.db_path)
    assert queued

    d = reflection_debt_stats(config)
    assert d["pending"] == len(queued)
    assert d["drained"] == 0
    assert d["sessions_pending"] == 1
    assert d["oldest_pending"] is not None

    drain_reflection_queue(config.db_path, [q["thread_id"] for q in queued])
    d = reflection_debt_stats(config)
    assert d["pending"] == 0
    assert d["drained"] == len(queued)
    assert d["oldest_pending"] is None


def test_promote_refuses_to_mint_an_unbacked_document_card(tmp_path):
    """#633: `fsck` dangling_cards has been red since 2026-06-21.

    reflect-promote and consolidate-merge could both mint a `document_card` with
    no `doc:<id>` pointer — four such rows exist (knowledge 361, 366, 624, 625).
    The content is real, so demote it to `fact` rather than dropping it.
    """
    from techne.core.database import connect
    from techne.core.reflect import (
        emit_reflection_pass,
        ingest_reflection_response,
        list_candidates,
        promote_candidate,
    )

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    item = emit_reflection_pass(config, session_id)["batch"][0]

    unbacked = json.dumps(
        [
            {
                "scope": "knowledge",
                "type": "document_card",
                "content": "A card with nothing behind it.",
                "tags": "document_card,backing:summary",  # no doc:<id>
                "confidence": "high",
            }
        ]
    )
    ingest_reflection_response(
        config, session_id, item["thread_id"], item["prompt_hash"], unbacked, model_used="test"
    )
    cand = [c for c in list_candidates(config.db_path, status="pending")][0]
    new_id = promote_candidate(config.db_path, cand["id"])

    conn = connect(config.db_path)
    try:
        row = conn.execute("SELECT type FROM knowledge WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    assert row["type"] == "fact", (
        "a document_card with no doc: pointer is not a card — it must not be minted "
        "as one, or fsck goes red forever (#633)"
    )


# ---------------------------------------------------------------------------
# techne#733 — the emit cursor was keyed on an UNSTABLE value
# ---------------------------------------------------------------------------


def _cursor(config, session_id):
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    conn = connect(config.db_path)
    try:
        return get_state(conn, f"reflect:emitted:{session_id}") or {}
    finally:
        conn.close()


def test_prompt_hash_moves_when_live_knowledge_changes(tmp_path):
    """THE ROOT CAUSE, pinned at its source. The old cursor's docstring called
    prompt_hash "the stable, always-present per-thread id" and scoped the drift hazard
    to write_to='live'. In fact _build_prompt embeds a freshly-retrieved existing-
    knowledge block, so ANY write to live knowledge — from any session, at any time —
    moves the hash of a thread whose own observations never changed."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_batch, load_session_for_reflection

    config = _mk_config(tmp_path, enabled=True)
    # THE FIXTURE DEFAULT DISABLES THIS BLOCK ("skip FTS in tests"), and the block IS the
    # defect — a suite that leaves it off cannot observe the drift at all. Production runs
    # with include_existing_knowledge: true.
    config.reflect.include_existing_knowledge = True
    session_id = _mk_db_backed_session(config)
    threads, observations = load_session_for_reflection(config, session_id)

    render = lambda: [  # noqa: E731
        b["prompt_hash"]
        for b in emit_reflection_batch(config, session_id, threads, observations)
    ]
    before = render()
    assert before, "precondition: the thread must render"
    assert before == render(), (
        "back-to-back renders must agree — the hash is not nondeterministic, it DRIFTS"
    )

    store(config.db_path, "Session work Filesystem observations tools filesystem", type="fact")
    after = render()

    assert after != before, (
        "prompt_hash did NOT move after a live-knowledge write — if this ever passes, "
        "the #733 premise is gone and the cursor could key on prompt_hash again"
    )


def test_a_drifting_hash_does_not_re_emit_or_inflate_the_cursor(tmp_path):
    """THE SYMPTOM. Under the old key a drifted hash matched nothing, so the thread was
    re-emitted AND a second entry appended — accumulating without bound. Measured on the
    operator's store: 51 entries for 23 threads, remaining=-27."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_pass

    config = _mk_config(tmp_path, enabled=True)
    config.reflect.include_existing_knowledge = True  # the drift needs the block on
    session_id = _mk_db_backed_session(config)

    first = emit_reflection_pass(config, session_id)
    assert first["count"] >= 1, "precondition: at least one thread emitted"

    store(config.db_path, "Session work Filesystem observations tools filesystem", type="fact")

    second = emit_reflection_pass(config, session_id)
    assert second["count"] == 0, "a delivered thread must stay delivered across a knowledge write"

    marker = _cursor(config, session_id)
    assert len(marker["emitted"]) <= marker["total"], "cursor must never exceed the thread count"
    assert marker.get("remaining", 0) >= 0, "remaining must never go negative"


def test_a_corrupt_cursor_reads_as_pending_not_done(tmp_path):
    """FAIL-CLOSED. An over-full cursor is not evidence of completion, and treating it as
    completion is what dropped six sessions out of the SessionStart nudge — the reason
    126 owed queue rows were reported to the operator as 50."""
    from techne.core.reflect import _marker_has_unemitted_threads

    assert _marker_has_unemitted_threads({"emitted": ["a"] * 51, "total": 24}) is True
    assert _marker_has_unemitted_threads({"emitted": ["a", "b"], "total": 5}) is True
    assert _marker_has_unemitted_threads({"emitted": ["a", "b"], "total": 2}) is False


def test_a_legacy_cursor_is_rebuilt_from_the_drained_queue(tmp_path):
    """A pre-#733 marker holds prompt_hashes that are unrecoverable, so the delivered set
    comes from the ledger that was right all along — the queue rows already drained."""
    from techne.core.database import connect
    from techne.core.extension_state import set_state
    from techne.core.reflect import _emitted_keys

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)

    conn = connect(config.db_path)
    tid = conn.execute("SELECT id FROM threads WHERE session_id = ?", (session_id,)).fetchone()[0]
    conn.execute("DELETE FROM reflection_queue WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO reflection_queue (session_id, thread_id, drained_at) VALUES (?,?,?)",
        (session_id, tid, "2026-08-20 00:00:00"),
    )
    conn.commit()
    set_state(conn, f"reflect:emitted:{session_id}", {"emitted": ["deadbeef"], "total": 1})
    legacy = {"emitted": ["deadbeef"], "total": 1}
    conn.close()

    assert _emitted_keys(config, session_id, legacy) == {tid}, (
        "a legacy marker's prompt_hashes must be discarded for the drained-queue record"
    )


def test_owed_threads_are_ordered_ahead_of_paid_ones(tmp_path):
    """THE REPORTED SYMPTOM. The char budget is spent in order, so a session whose early
    threads are already paid spent its whole budget re-rendering them and never reached
    the owed rows — measured: 11 prompts emitted, ZERO of them owed."""
    from techne.core.database import connect
    from techne.core.reflect import _owed_first

    config = _mk_config(tmp_path, enabled=True)
    session_id = _mk_db_backed_session(config)
    conn = connect(config.db_path)
    conn.execute("DELETE FROM reflection_queue WHERE session_id = ?", (session_id,))
    for tid, drained in (("t-paid", "2026-08-20 00:00:00"), ("t-owed", None)):
        conn.execute(
            "INSERT OR IGNORE INTO threads (id, session_id, thread_num, label, summary) "
            "VALUES (?,?,?,?,?)",
            (tid, session_id, 1, tid, tid),
        )
        conn.execute(
            "INSERT INTO reflection_queue (session_id, thread_id, drained_at) VALUES (?,?,?)",
            (session_id, tid, drained),
        )
    conn.commit()
    conn.close()

    batch = [{"thread_id": "t-paid"}, {"thread_id": "t-owed"}]
    assert [i["thread_id"] for i in _owed_first(config, session_id, batch)] == ["t-owed", "t-paid"]
