"""Tests for the end-of-session reflection pass."""

import json

from techne.config import Config
from techne.core.database import init_db, connect
from techne.core.reflect import (
    Candidate,
    candidate_stats,
    discard_candidate,
    list_candidates,
    parse_response,
    promote_candidate,
    reflect_on_session,
)
from techne.core.state import start_session
from techne.core.threads import Thread


# --- parse_response -------------------------------------------------------


def test_parse_response_clean_json():
    raw = json.dumps([
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
    ])
    parsed = parse_response(raw, max_items=10)
    assert len(parsed) == 2
    assert parsed[0].scope == "knowledge"
    assert parsed[0].type == "decision"
    assert parsed[1].scope == "identity"


def test_parse_response_tolerates_code_fence():
    raw = "```json\n" + json.dumps([
        {"scope": "knowledge", "type": "fact", "content": "x", "confidence": "low"}
    ]) + "\n```"
    parsed = parse_response(raw, max_items=10)
    assert len(parsed) == 1


def test_parse_response_tolerates_leading_prose():
    raw = "Here is the JSON you asked for:\n[" + json.dumps(
        {"scope": "knowledge", "type": "pattern", "content": "y", "confidence": "medium"}
    ) + "]\nThank you."
    parsed = parse_response(raw, max_items=10)
    assert len(parsed) == 1
    assert parsed[0].type == "pattern"


def test_parse_response_rejects_invalid_scope():
    raw = json.dumps([{"scope": "lol", "type": "fact", "content": "x", "confidence": "high"}])
    assert parse_response(raw, max_items=10) == []


def test_parse_response_rejects_invalid_knowledge_type():
    raw = json.dumps([
        {"scope": "knowledge", "type": "not-a-real-type", "content": "x", "confidence": "high"}
    ])
    assert parse_response(raw, max_items=10) == []


def test_parse_response_respects_max_items():
    raw = json.dumps([
        {"scope": "knowledge", "type": "fact", "content": f"item {i}", "confidence": "medium"}
        for i in range(10)
    ])
    parsed = parse_response(raw, max_items=3)
    assert len(parsed) == 3


def test_parse_response_empty_list_is_valid():
    assert parse_response("[]", max_items=5) == []


def test_parse_response_garbage_returns_empty():
    assert parse_response("not json at all", max_items=5) == []
    assert parse_response("", max_items=5) == []


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
    session_id = start_session(
        config.db_path, project_path="/tmp/p", agent_identity="test-agent"
    )
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
        [thread], session_id, config.db_path,
        parent_links=[(f"{session_id}-{thread.thread_num}", None)],
    )


def test_reflect_disabled_is_noop(tmp_path):
    config = _mk_config(tmp_path, enabled=False)
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x",
                      observation_indices=list(range(5)))]

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
    threads = [Thread(thread_num=1, label="auth work", summary="auth work",
                      observation_indices=list(range(5)))]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps([
            {"scope": "knowledge", "type": "decision",
             "content": "Use JWT for service auth", "tags": "auth",
             "confidence": "high"},
            {"scope": "identity", "type": "user.team.name",
             "content": "User works solo", "tags": "team",
             "confidence": "medium"},
        ])

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
    threads = [Thread(thread_num=1, label="x", summary="x",
                      observation_indices=list(range(5)))]

    calls = []

    def fake_llm(p):
        calls.append(p)
        return "[]"

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert calls == []
    assert result["threads_reflected"] == 0


def test_reflect_min_confidence_filter(tmp_path):
    """Candidates below config.reflect.min_confidence are dropped silently."""
    config = _mk_config(tmp_path, enabled=True)
    config.reflect.min_confidence = "high"
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x",
                      observation_indices=list(range(5)))]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps([
            {"scope": "knowledge", "type": "fact", "content": "low-conf",
             "confidence": "low"},
            {"scope": "knowledge", "type": "fact", "content": "med-conf",
             "confidence": "medium"},
            {"scope": "knowledge", "type": "fact", "content": "high-conf",
             "confidence": "high"},
        ])

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert result["candidates_written"] == 1
    cand = list_candidates(config.db_path, status="pending")[0]
    assert cand["content"] == "high-conf"


def test_reflect_llm_error_counted_not_raised(tmp_path):
    """Hook contract: an LLM failure must not propagate out of reflect_on_session."""
    config = _mk_config(tmp_path, enabled=True)
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x",
                      observation_indices=list(range(5)))]

    def angry_llm(_):
        raise RuntimeError("provider exploded")

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=angry_llm)
    assert result["errors"] == 1
    assert result["candidates_written"] == 0


def test_reflect_write_to_live_writes_knowledge(tmp_path):
    """write_to='live' should land entries in the knowledge table directly."""
    config = _mk_config(tmp_path, enabled=True, write_to="live")
    session_id, obs = _mk_session_with_obs(config, 5)
    threads = [Thread(thread_num=1, label="x", summary="x",
                      observation_indices=list(range(5)))]
    _save_thread_row(config, session_id, threads[0])

    def fake_llm(_):
        return json.dumps([
            {"scope": "knowledge", "type": "fact",
             "content": "live-write test", "confidence": "high"},
        ])

    result = reflect_on_session(config, session_id, threads, obs, generate_fn=fake_llm)
    assert result["candidates_promoted"] == 1

    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT content FROM knowledge WHERE content = ?", ("live-write test",)
        ).fetchone()
        assert row is not None
        cand = conn.execute(
            "SELECT status, promoted_to_id FROM knowledge_candidates"
        ).fetchone()
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
    thread = Thread(thread_num=1, label=f"seed-{content}", summary=f"seed {content}",
                    observation_indices=list(range(3)))
    _save_thread_row(config, session_id, thread)

    def fake_llm(_):
        return json.dumps([
            {"scope": scope, "type": type_, "content": content, "confidence": "high"}
        ])

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
        live = conn.execute(
            "SELECT content FROM knowledge WHERE id = ?", (new_id,)
        ).fetchone()
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
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version >= 6
    finally:
        conn.close()
