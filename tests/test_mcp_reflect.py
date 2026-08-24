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


def test_emit_trims_batch_by_serialized_size(config, monkeypatch):
    """The by-size trim (not just the count cap) actually fires and keeps the
    returned batch within emit_max_chars (techne#268, Prudence #3)."""
    import techne.core.reflect as reflect_mod

    _seed(config, "sz", n=6)
    config.reflect.emit_max_chars = 400  # tiny budget forces the size trim
    big = "x" * 150
    fake = [
        {"thread_id": f"t{i}", "session_id": "sz", "prompt_hash": f"h{i}", "prompt": big}
        for i in range(6)
    ]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))

    out = _techne_reflect_emit({"session_id": "sz"}, config)
    assert 1 <= out["count"] < 6  # trimmed below the 6 available threads
    assert out["truncated"] is True
    assert len(json.dumps(out["batch"])) <= 400  # the returned payload actually fits
    conn = connect(config.db_path)
    try:
        assert get_state(conn, "reflect:emitted:sz") is not None  # delivered → marked
    finally:
        conn.close()


def test_emit_oversized_single_thread_is_flagged_and_unmarked(config, monkeypatch):
    """A lone thread that exceeds the size budget can't be delivered: it's flagged
    `oversized` and the session is NOT marked emitted, so the auto-trigger keeps
    surfacing it instead of silently skipping it (techne#268, Prudence #1 — the
    single-oversized-thread reappearance of footgun 3)."""
    import techne.core.reflect as reflect_mod

    _seed(config, "huge", n=6)
    config.reflect.emit_max_chars = 200
    fake = [{"thread_id": "t0", "session_id": "huge", "prompt_hash": "h0", "prompt": "y" * 5000}]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))

    out = _techne_reflect_emit({"session_id": "huge"}, config)
    assert out.get("oversized") is True
    conn = connect(config.db_path)
    try:
        assert get_state(conn, "reflect:emitted:huge") is None  # NOT marked → re-nudgeable
    finally:
        conn.close()
    assert [p["session_id"] for p in pending_reflection(config)] == ["huge"]


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


def test_ingest_empty_response_is_a_discarded_pass_not_a_noop(config):
    """An empty response is a reflection that DIDN'T HAPPEN, and must report as one.

    This surface used to short-circuit whitespace with {"note": "nothing to
    ingest"} and no `errors` key, while the core it wraps calls the identical
    input `parse_failures=1, errors=1` ("empty response"). That erased #465's
    whole distinction at the boundary agents actually use (techne#485).
    """
    counts = _techne_reflect_ingest(
        {"session_id": "s", "thread_id": "t", "response": "  "}, config
    )
    assert counts["parse_failures"] == 1
    assert counts["errors"] == 1
    assert counts["candidates_written"] == 0
    assert list_candidates(config.db_path, status="all") == []


def test_ingest_unparseable_response_surfaces_the_failure(config):
    """Prose instead of the JSON array — the realistic failure, not an exotic one."""
    counts = _techne_reflect_ingest(
        {"session_id": "s", "thread_id": "t", "response": "I didn't find anything worth saving."},
        config,
    )
    assert counts["parse_failures"] == 1
    assert counts["errors"] == 1
    assert counts["candidates_written"] == 0


def test_ingest_counts_shape_is_the_core_shape(config):
    """Every key of new_ingest_counts() reaches the MCP surface, on both paths.

    The anti-drift assertion. A fourth hand-written copy of this dict lived here;
    a key added upstream silently never arrived, which is exactly how the CLI copy
    came to crash with KeyError through 2,383 passing tests (techne#465/#485).
    """
    from techne.core.reflect import new_ingest_counts

    expected = set(new_ingest_counts())
    discarded = _techne_reflect_ingest(
        {"session_id": "s", "thread_id": "t", "response": ""}, config
    )
    assert expected <= set(discarded)

    _seed(config, "sess-shape")
    item = _techne_reflect_emit({"session_id": "sess-shape"}, config)["batch"][0]
    good = _techne_reflect_ingest(
        {
            "session_id": "sess-shape",
            "thread_id": item["thread_id"],
            "prompt_hash": item["prompt_hash"],
            "response": _CANNED,
        },
        config,
    )
    assert expected <= set(good)


def test_ingest_discarded_pass_does_not_drain_reflection_debt(config):
    """Draining on a discarded pass would stamp it paid permanently (drain is
    guarded on drained_at IS NULL), so a thrown-away reflection could never be
    retried."""
    counts = _techne_reflect_ingest(
        {"session_id": "s", "thread_id": "t", "response": "  "}, config
    )
    assert "reflection_debt_drained" not in counts


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


# ---- per-thread emit cursor (techne#269) ----------------------------------


def test_emit_cursor_drains_large_session_across_passes(config, monkeypatch):
    """A session with more reflectable threads than the emit cap drains losslessly
    across passes: each pass emits the NEXT slice (no overlap, no loss), and
    pending_reflection keeps surfacing it until every thread is drained, then stops
    (techne#269)."""
    import techne.core.reflect as reflect_mod

    _seed(config, "multi", n=6)  # real observations so pending_reflection surfaces it
    fake = [
        {"thread_id": f"t{i}", "session_id": "multi", "prompt_hash": f"h{i}", "prompt": "p"}
        for i in range(7)
    ]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))
    config.reflect.emit_max_threads = 3

    seen: list[str] = []
    passes = 0
    while pending_reflection(config):  # cursor must keep surfacing until drained
        out = _techne_reflect_emit({}, config)  # no session_id → most-recent pending
        assert out["session_id"] == "multi"
        hashes = [item["prompt_hash"] for item in out["batch"]]
        assert not (set(hashes) & set(seen)), "passes must not overlap"
        seen.extend(hashes)
        passes += 1
        assert passes <= 5, "cursor failed to terminate"

    assert sorted(seen) == [f"h{i}" for i in range(7)]  # every thread emitted exactly once
    assert passes == 3  # ceil(7 / cap=3)
    assert pending_reflection(config) == []  # drained → no longer pending


def test_emit_cursor_marker_records_emitted_threads(config, monkeypatch):
    """The cursor marker carries the cumulative per-thread 'emitted' list + total, and
    a re-emit skips already-delivered threads (techne#269/#236).

    KEYED ON thread_id SINCE techne#733 — this test previously asserted prompt_hashes,
    which is exactly the key that drifted. The BEHAVIOURAL assertion below (pass 2
    delivers h2/h3, not h0/h1) is unchanged and is what actually guards the cursor.
    """
    import techne.core.reflect as reflect_mod

    _seed(config, "cur", n=6)
    fake = [
        {"thread_id": f"t{i}", "session_id": "cur", "prompt_hash": f"h{i}", "prompt": "p"}
        for i in range(5)
    ]
    monkeypatch.setattr(reflect_mod, "emit_reflection_batch", lambda *a, **k: list(fake))
    config.reflect.emit_max_threads = 2

    _techne_reflect_emit({"session_id": "cur"}, config)  # pass 1 → h0,h1
    out2 = _techne_reflect_emit({"session_id": "cur"}, config)  # pass 2 → h2,h3
    assert sorted(item["prompt_hash"] for item in out2["batch"]) == ["h2", "h3"]

    conn = connect(config.db_path)
    try:
        marker = get_state(conn, "reflect:emitted:cur")
    finally:
        conn.close()
    assert sorted(marker["emitted"]) == ["t0", "t1", "t2", "t3"]
    assert marker["key"] == "thread_id", "a new marker must name its key (#733)"
    assert marker["total"] == 5 and marker["remaining"] == 1


def test_pending_reflection_ignores_old_shape_marker(config):
    """Backward-compat (techne#269): a #268-era marker ({count,total,remaining} with NO
    per-thread 'emitted' list) is treated as fully emitted — NOT re-surfaced — even
    though it recorded a nonzero `remaining`."""
    from techne.core.extension_state import set_state

    _seed(config, "legacy", n=6)
    conn = connect(config.db_path)
    try:
        set_state(conn, "reflect:emitted:legacy", {"count": 25, "total": 30, "remaining": 5})
    finally:
        conn.close()
    assert pending_reflection(config) == []


def test_the_mcp_cursor_survives_a_knowledge_write(config):
    """techne#733/#734 — THE INTEGRATION-LEVEL GAP Prudence named.

    Every cursor test in this file runs on the module fixture, which sets
    `include_existing_knowledge = False` ("skip FTS in tests") — and that block IS the
    #733 defect, so this file was structurally unable to observe the drift it is the
    integration surface for. The same fixture default is what let a root-cause test pass
    against broken code during the #734 work.

    WHAT THIS LAYER CAN ACTUALLY OBSERVE, stated plainly because it is not obvious:
    `_seed` records observations only, so no thread is persisted and
    `persisted_thread_id` returns None for every one of them. Every item here therefore
    takes the `prompt_hash` FALLBACK — this file exercises the unstable key exclusively.
    That makes re-delivery expected (re-offering a thread with no stable identity is the
    only safe answer) and makes the BOUND the property worth pinning: with the knowledge
    block on, each pass moves every hash, and before the write-time reconciliation that
    grew the cursor by one entry per thread per pass, without limit, forever.
    """
    from techne.core.knowledge import store

    config.reflect.include_existing_knowledge = True
    _seed(config, "kw", n=6)

    sizes = []
    for i in range(4):
        out = _techne_reflect_emit({"session_id": "kw"}, config)
        assert out["batch"] or i > 0, "precondition: the session emitted something"
        store(config.db_path, f"an unrelated durable fact about session work {i}", type="fact")
        conn = connect(config.db_path)
        try:
            marker = get_state(conn, "reflect:emitted:kw")
        finally:
            conn.close()
        sizes.append(len(marker["emitted"]))
        assert len(marker["emitted"]) <= marker["total"], (
            f"pass {i}: cursor holds {len(marker['emitted'])} keys for "
            f"{marker['total']} renderable threads — the unstable key is accumulating"
        )
        assert marker.get("remaining", 0) >= 0

    assert max(sizes) <= marker["total"], f"cursor grew unbounded across passes: {sizes}"
