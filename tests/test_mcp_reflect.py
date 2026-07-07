"""Resident-model reflect extraction over MCP (techne#19 Part B).

The reflect engine already exists; these tests cover the new MCP surface
(techne_reflect_emit / techne_reflect_ingest), the kind-aware prompt, and the
SessionStart auto-trigger (pending_reflection). The agent's role — answering each
emitted prompt with its own model — is faked by a canned JSON candidate array, so
no real LLM is needed (same contract as the CLI emit/ingest path).
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.extension_state import get_state
from techne.core.reflect import list_candidates, pending_reflection
from techne.core.state import record_observation
from techne.mcp_server import _techne_reflect_emit, _techne_reflect_ingest


@pytest.fixture()
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "reflect_mcp.db"
    cfg.reflect.min_observations_per_thread = 3
    cfg.reflect.min_confidence = "low"
    cfg.reflect.include_existing_knowledge = False  # skip FTS in tests
    cfg.reflect.write_to = "candidates"
    init_db(cfg.db_path)
    return cfg


def _seed(config, session_id: str, n: int = 10) -> None:
    for i in range(n):
        record_observation(
            config, session_id, f"Decided to use JWT for service auth, step {i}", tool_name="Edit"
        )


# ---- emit -----------------------------------------------------------------


def test_emit_returns_batch_and_marks_session(config):
    _seed(config, "sess-1")
    out = _techne_reflect_emit({"session_id": "sess-1"}, config)
    assert out["count"] >= 1
    assert out["batch"], "expected at least one thread prompt"
    item = out["batch"][0]
    assert {"thread_id", "session_id", "prompt_hash", "prompt"} <= set(item)
    # The session is marked emitted so the auto-trigger won't re-nudge it.
    conn = connect(config.db_path)
    try:
        assert get_state(conn, "reflect:emitted:sess-1") is not None
    finally:
        conn.close()


def test_emit_defaults_to_most_recent_session(config):
    _seed(config, "older")
    _seed(config, "newer")
    # started_at defaults to CURRENT_TIMESTAMP (1s granularity) — both seed in the
    # same second, so pin them apart to test the most-recent default deterministically.
    conn = connect(config.db_path)
    conn.execute("UPDATE sessions SET started_at = '2026-01-01 00:00:00' WHERE id = 'older'")
    conn.execute("UPDATE sessions SET started_at = '2026-06-01 00:00:00' WHERE id = 'newer'")
    conn.commit()
    conn.close()
    out = _techne_reflect_emit({}, config)  # no session_id → most recent
    assert out["session_id"] == "newer"


def test_emit_no_session_is_clean(config):
    out = _techne_reflect_emit({}, config)  # empty db
    assert out["batch"] == [] and out["count"] == 0


def test_emit_default_skips_thin_newest_session(config):
    """Reflect-before-restart regression (techne#267): the newest session is a fresh
    (thin) one, so the default must fall through to the real work session, not the
    empty newest one."""
    _seed(config, "work", n=6)
    _seed(config, "thin-newer", n=1)  # newest, but below min_observations → not reflectable
    conn = connect(config.db_path)
    conn.execute("UPDATE sessions SET started_at = '2026-01-01 00:00:00' WHERE id = 'work'")
    conn.execute("UPDATE sessions SET started_at = '2026-06-01 00:00:00' WHERE id = 'thin-newer'")
    conn.commit()
    conn.close()
    out = _techne_reflect_emit({}, config)  # no session_id
    assert out["session_id"] == "work"


def test_emit_caps_large_session_and_flags_truncation(config, monkeypatch):
    """A large session must not emit an oversized batch: it's capped, the drop is
    surfaced loudly, and the marker records the shortfall (techne#267)."""
    import techne.core.reflect as reflect_mod

    _seed(config, "big", n=6)
    fake = [
        {"thread_id": f"t{i}", "session_id": "big", "prompt_hash": f"h{i}", "prompt": "p"}
        for i in range(30)
    ]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))
    config.reflect.emit_max_threads = 25

    out = _techne_reflect_emit({"session_id": "big"}, config)
    assert out["count"] == 25
    assert out["truncated"] is True
    assert out["remaining"] == 5
    assert "re-emit" in out["note"]

    conn = connect(config.db_path)
    try:
        marker = get_state(conn, "reflect:emitted:big")
    finally:
        conn.close()
    assert marker["total"] == 30 and marker["remaining"] == 5


def test_emit_explicit_limit_overrides_default_cap(config, monkeypatch):
    import techne.core.reflect as reflect_mod

    _seed(config, "lim", n=6)
    fake = [
        {"thread_id": f"t{i}", "session_id": "lim", "prompt_hash": f"h{i}", "prompt": "p"}
        for i in range(10)
    ]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))
    out = _techne_reflect_emit({"session_id": "lim", "limit": 3}, config)
    assert out["count"] == 3 and out["truncated"] is True and out["remaining"] == 7


def test_emit_prompt_is_kind_aware(config):
    _seed(config, "sess-k")
    out = _techne_reflect_emit({"session_id": "sess-k"}, config)
    prompt = out["batch"][0]["prompt"]
    assert "[Edit|" in prompt, "observation lines should carry the inferred kind"
    assert "decision-shaped" in prompt and "fact-shaped" in prompt  # the weighting hint


# ---- ingest ---------------------------------------------------------------


_CANNED = json.dumps(
    [{"scope": "knowledge", "type": "decision",
      "content": "Use JWT for service auth", "tags": "auth", "confidence": "high"}]
)


def test_emit_ingest_round_trip(config):
    _seed(config, "sess-rt")
    emitted = _techne_reflect_emit({"session_id": "sess-rt"}, config)
    item = emitted["batch"][0]

    counts = _techne_reflect_ingest(
        {
            "session_id": "sess-rt",
            "thread_id": item["thread_id"],
            "prompt_hash": item["prompt_hash"],
            "response": _CANNED,
        },
        config,
    )
    assert counts["candidates_written"] == 1
    cands = list_candidates(config.db_path, status="pending")
    assert len(cands) == 1
    assert cands[0]["content"] == "Use JWT for service auth"


def test_ingest_empty_response_is_noop(config):
    counts = _techne_reflect_ingest(
        {"session_id": "s", "thread_id": "t", "response": "  "}, config
    )
    assert counts["candidates_written"] == 0
    assert list_candidates(config.db_path, status="all") == []


def test_ingest_tolerates_inlined_array(config):
    """An agent that passes the candidate array as JSON (not a string) still ingests."""
    _seed(config, "sess-inl")
    item = _techne_reflect_emit({"session_id": "sess-inl"}, config)["batch"][0]
    counts = _techne_reflect_ingest(
        {
            "session_id": "sess-inl",
            "thread_id": item["thread_id"],
            "prompt_hash": item["prompt_hash"],
            "response": json.loads(_CANNED),  # a list, not a string
        },
        config,
    )
    assert counts["candidates_written"] == 1


# ---- auto-trigger (pending_reflection) ------------------------------------


def test_pending_reflection_flags_unemitted_session(config):
    _seed(config, "sess-pend", n=6)
    pend = pending_reflection(config)
    assert [p["session_id"] for p in pend] == ["sess-pend"]
    assert pend[0]["observations"] == 6


def test_pending_reflection_skips_after_emit(config):
    _seed(config, "sess-once", n=6)
    assert pending_reflection(config)  # pending before emit
    _techne_reflect_emit({"session_id": "sess-once"}, config)  # sets the marker
    assert pending_reflection(config) == []  # not re-nudged, even if no candidates


def test_pending_reflection_ignores_thin_sessions(config):
    _seed(config, "sess-thin", n=2)  # below min_observations_per_thread (3)
    assert pending_reflection(config) == []
