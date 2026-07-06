"""Dream / recombine pass tests (techne#108 PR B).

Covers the group selector, parse/verdict, emit→ingest round-trip, anti-churn guard,
origin scoping (dream vs consolidate never cross-block), and MCP tool round-trip.
The agent's role (answering a prompt) is faked by canned JSON — no real LLM.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import store
from techne.core.sleepdream import (
    _already_proposed_dream,
    _cluster_hash,
    consolidate_ingest,
    dream_emit,
    dream_ingest,
    parse_dream_response,
    recombine_groups,
)
from techne.mcp_server import handle_request


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "dream.db"
    init_db(cfg.db_path)
    return cfg


# ---------------------------------------------------------------------------
# recombine_groups — selector
# ---------------------------------------------------------------------------


def test_recombine_groups_shared_tag_yields_group(config):
    """Rows sharing a tag should form a group."""
    db = config.db_path
    store(db, "Python uses indentation for blocks", type="fact", confidence="high",
          tags="lang:python,scope:syntax")
    store(db, "Python is dynamically typed", type="fact", confidence="high",
          tags="lang:python,scope:types")
    groups = recombine_groups(config)
    assert any(g["via"] == "tag:lang:python" for g in groups)


def test_recombine_groups_duplicate_tag_no_degenerate_group(config):
    """A single row carrying a duplicated tag token (tags='foo,foo') must NOT form a
    degenerate group of one distinct row repeated (Prudence nit — per-tag id dedup)."""
    db = config.db_path
    store(db, "lone fact with a doubled tag", type="fact", confidence="high", tags="dup,dup")
    groups = recombine_groups(config)
    # No group: the single row dedupes to one distinct id (< 2), so no recombination.
    assert all("dup" not in g["via"] for g in groups)
    for g in groups:
        assert len(set(g["group_ids"])) == len(g["group_ids"]) >= 2  # no repeated ids, real groups


def test_recombine_groups_no_shared_tag_yields_no_group(config):
    """Rows with entirely disjoint tags should not be grouped."""
    db = config.db_path
    store(db, "Rust has ownership semantics", type="fact", confidence="high",
          tags="lang:rust")
    store(db, "Go uses goroutines", type="fact", confidence="high",
          tags="lang:go")
    groups = recombine_groups(config)
    assert groups == []


def test_recombine_groups_excludes_meta_user_tag(config):
    """Rows tagged memory-type:user must not appear in any group."""
    db = config.db_path
    a = store(db, "Richard prefers terse answers", type="fact", confidence="high",
              tags="memory-type:user,style:communication")
    b = store(db, "Richard uses dev04", type="fact", confidence="high",
              tags="memory-type:user,style:communication")
    groups = recombine_groups(config)
    # No group should contain either row.
    for g in groups:
        assert a not in g["group_ids"]
        assert b not in g["group_ids"]


def test_recombine_groups_excludes_meta_feedback_tag(config):
    """Rows tagged memory-type:feedback must not appear in any group."""
    db = config.db_path
    a = store(db, "User said response was too verbose", type="fact", confidence="high",
              tags="memory-type:feedback,topic:style")
    b = store(db, "User prefers code over prose", type="fact", confidence="high",
              tags="memory-type:feedback,topic:style")
    groups = recombine_groups(config)
    for g in groups:
        assert a not in g["group_ids"]
        assert b not in g["group_ids"]


def test_recombine_groups_excludes_meta_project_tag(config):
    """Rows tagged memory-type:project must not appear in any group."""
    db = config.db_path
    a = store(db, "Project uses Python 3.12", type="fact", confidence="high",
              tags="memory-type:project,topic:env")
    b = store(db, "Project is deployed on K8s", type="fact", confidence="high",
              tags="memory-type:project,topic:env")
    groups = recombine_groups(config)
    for g in groups:
        assert a not in g["group_ids"]
        assert b not in g["group_ids"]


def test_recombine_groups_skips_giant_tag(config):
    """A tag shared by too many rows is a generic tag, not a specific signal — skip it."""
    db = config.db_path
    # group_size=2 → giant_threshold = min(0.6*n_eligible, 3*2=6).
    # Store 6 rows with tag 'shared' → len(row_ids)==6 which is NOT > 6 (threshold),
    # so they'd normally be included. Store 7 rows → len=7 > 6 → skipped.
    for i in range(7):
        store(db, f"generic fact {i}", type="fact", confidence="high", tags="shared")
    groups = recombine_groups(config, group_size=2)
    assert not any(g["via"] == "tag:shared" for g in groups)


def test_recombine_groups_group_size_cap(config):
    """Groups should contain at most group_size rows.

    Setup: 4 rows tagged 'algo:bfs' + 3 rows tagged 'algo:dfs' → n_eligible=7,
    group_size=3. giant_threshold = max(3, min(0.6*7, 9)) = max(3, 4.2) = 4.2.
    'algo:bfs' on 4 rows: 4 <= 4.2 → not giant → group forms (capped at 3).
    """
    db = config.db_path
    for i in range(4):
        store(db, f"bfs related fact {i}", type="fact", confidence="high",
              tags="algo:bfs")
    for i in range(3):
        store(db, f"dfs related fact {i}", type="fact", confidence="high",
              tags="algo:dfs")
    groups = recombine_groups(config, group_size=3)
    bfs_groups = [g for g in groups if g["via"] == "tag:algo:bfs"]
    assert len(bfs_groups) >= 1, "expected at least one algo:bfs group"
    for g in bfs_groups:
        assert len(g["group_ids"]) <= 3


def test_recombine_groups_max_groups_cap(config):
    """Total groups returned must not exceed max_groups."""
    db = config.db_path
    # Create many tags with 2 rows each.
    for i in range(20):
        store(db, f"row a for tag {i}", type="fact", confidence="high",
              tags=f"unique:tag{i}")
        store(db, f"row b for tag {i}", type="fact", confidence="high",
              tags=f"unique:tag{i}")
    groups = recombine_groups(config, max_groups=5)
    assert len(groups) <= 5


def test_recombine_groups_dedup_by_idset(config):
    """A group whose id-set is a subset of an already-added group should be skipped."""
    db = config.db_path
    # Two rows sharing both tag:A and tag:B. The tag:A group will be added first;
    # the tag:B group has the same or smaller id-set and should be deduped.
    a = store(db, "alpha fact", type="fact", confidence="high", tags="tag:A,tag:B")
    b = store(db, "beta fact", type="fact", confidence="high", tags="tag:A,tag:B")
    groups = recombine_groups(config)
    # There should be exactly one group containing {a, b}, not two.
    matching = [g for g in groups if frozenset(g["group_ids"]) == frozenset({a, b})]
    assert len(matching) == 1


def test_recombine_groups_skips_low_confidence_rows(config):
    """Only high-confidence rows qualify for recombination."""
    db = config.db_path
    store(db, "medium fact", type="fact", confidence="medium", tags="topic:shared")
    store(db, "low fact", type="fact", confidence="low", tags="topic:shared")
    groups = recombine_groups(config)
    assert groups == []


# ---------------------------------------------------------------------------
# parse_dream_response — parser
# ---------------------------------------------------------------------------

_CONNECT_VERDICT = json.dumps({
    "verdict": "connect",
    "content": "Both facts together imply that X leads to Y via Z.",
    "type": "fact",
    "basis": "Row 1 and Row 2 combine because ...",
})

_NONE_VERDICT = json.dumps({"verdict": "none"})


def test_parse_connect_valid():
    v = parse_dream_response(_CONNECT_VERDICT)
    assert v is not None
    assert v["verdict"] == "connect"
    assert v["content"] == "Both facts together imply that X leads to Y via Z."
    assert v["type"] == "fact"


def test_parse_none_returns_none():
    assert parse_dream_response(_NONE_VERDICT) is None


def test_parse_garbage_returns_none():
    assert parse_dream_response("not json at all") is None
    assert parse_dream_response("") is None


def test_parse_fenced_json():
    fenced = "```json\n" + _CONNECT_VERDICT + "\n```"
    v = parse_dream_response(fenced)
    assert v is not None
    assert v["verdict"] == "connect"


def test_parse_connect_missing_content_returns_none():
    raw = json.dumps({"verdict": "connect"})
    assert parse_dream_response(raw) is None


def test_parse_bad_type_defaults_to_fact():
    raw = json.dumps({
        "verdict": "connect",
        "content": "some insight",
        "type": "not-a-valid-type",
    })
    v = parse_dream_response(raw)
    assert v is not None
    assert v["type"] == "fact"


def test_parse_valid_type_preserved():
    raw = json.dumps({
        "verdict": "connect",
        "content": "we should do X",
        "type": "decision",
    })
    v = parse_dream_response(raw)
    assert v is not None
    assert v["type"] == "decision"


# ---------------------------------------------------------------------------
# dream_ingest — verdict persistence
# ---------------------------------------------------------------------------


def _store_pair(db, tag="topic:shared"):
    a = store(db, "fact alpha about shared topic", type="fact", confidence="high", tags=tag)
    b = store(db, "fact beta about shared topic", type="fact", confidence="high", tags=tag)
    return a, b


def test_dream_ingest_connect_yields_pending_candidate(config):
    """A 'connect' verdict must write a pending candidate with origin='dream',
    action='add', and confidence='low' (forced, regardless of model output)."""
    db = config.db_path
    a, b = _store_pair(db)
    group_ids = [a, b]
    gh = _cluster_hash(group_ids)
    counts = dream_ingest(config, group_ids, gh, _CONNECT_VERDICT)
    assert counts["proposed"] == 1 and counts["written"] == 1

    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT origin, action, confidence, status, target_ids "
            "FROM knowledge_candidates WHERE origin='dream' AND status='pending'"
        ).fetchone()
        assert row is not None
        assert row["origin"] == "dream"
        assert row["action"] == "add"
        assert row["confidence"] == "low"  # forced, not from model
        assert set(json.loads(row["target_ids"])) == {a, b}
    finally:
        conn.close()


def test_dream_ingest_none_yields_discarded_marker(config):
    """A 'none' verdict must write a discarded keep-memory marker."""
    db = config.db_path
    a, b = _store_pair(db)
    group_ids = [a, b]
    gh = _cluster_hash(group_ids)
    counts = dream_ingest(config, group_ids, gh, _NONE_VERDICT)
    assert counts["kept"] == 1 and counts["written"] == 0

    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT status, decided_by, origin FROM knowledge_candidates WHERE origin='dream'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "discarded"
        assert row["decided_by"] == "agent-triage"
        assert row["origin"] == "dream"
    finally:
        conn.close()


def test_dream_ingest_anti_churn(config):
    """Second ingest of the same group must be a no-op (anti-churn at ingest)."""
    db = config.db_path
    a, b = _store_pair(db)
    group_ids = [a, b]
    gh = _cluster_hash(group_ids)

    first = dream_ingest(config, group_ids, gh, _CONNECT_VERDICT)
    assert first["written"] == 1 and first["skipped"] == 0

    second = dream_ingest(config, group_ids, gh, _CONNECT_VERDICT)
    assert second["skipped"] == 1
    assert second["written"] == 0 and second["proposed"] == 0

    conn = connect(db)
    try:
        n = conn.execute(
            "SELECT count(*) FROM knowledge_candidates WHERE origin='dream'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "duplicate ingest must not create a second candidate"


def test_dream_ingest_recomputes_group_hash(config):
    """dream_ingest must store the hash derived from group_ids, not the caller-supplied one."""
    db = config.db_path
    a, b = _store_pair(db)
    group_ids = [a, b]
    dream_ingest(config, group_ids, "bogus-hash-from-caller", _CONNECT_VERDICT)
    conn = connect(db)
    try:
        ph = conn.execute(
            "SELECT prompt_hash FROM knowledge_candidates WHERE origin='dream'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ph == _cluster_hash(group_ids)


def test_dream_ingest_forced_low_confidence(config):
    """Confidence must be forced to 'low' even if the model claims 'high'."""
    db = config.db_path
    a, b = _store_pair(db)
    group_ids = [a, b]
    raw_high = json.dumps({
        "verdict": "connect", "content": "Some new insight", "type": "fact",
        "confidence": "high",  # model claims high — must be ignored
    })
    dream_ingest(config, group_ids, _cluster_hash(group_ids), raw_high)
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT confidence FROM knowledge_candidates WHERE origin='dream' AND status='pending'"
        ).fetchone()
        assert row["confidence"] == "low"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# dream_emit — skips already-proposed groups
# ---------------------------------------------------------------------------


def test_dream_emit_skips_already_proposed(config):
    """After a group is ingested, dream_emit must not re-surface it."""
    db = config.db_path
    a, b = _store_pair(db)
    batch1 = dream_emit(config)
    item = next((i for i in batch1 if set(i["group_ids"]) == {a, b}), None)
    assert item is not None, "pair should appear in first emit"

    dream_ingest(config, item["group_ids"], item["group_hash"], _NONE_VERDICT)

    batch2 = dream_emit(config)
    assert not any(set(i["group_ids"]) == {a, b} for i in batch2)


# ---------------------------------------------------------------------------
# Origin scoping — dream and consolidate must not cross-block each other
# ---------------------------------------------------------------------------


def test_dream_and_consolidate_origin_scoping(config):
    """A group judged by dream is NOT skipped by consolidate's check, and vice-versa.
    The two passes are origin-scoped and must be fully independent."""
    db = config.db_path
    a = store(db, "identical fact", type="fact", confidence="high",
              tags="topic:shared")
    b = store(db, "identical fact", type="fact", confidence="high",
              tags="topic:shared")
    group_ids = [a, b]
    gh = _cluster_hash(group_ids)

    # Ingest under dream origin.
    dream_ingest(config, group_ids, gh, _CONNECT_VERDICT)

    # consolidate's _already_proposed must NOT see the dream row.
    conn = connect(db)
    try:
        from techne.core.sleepdream import _already_proposed
        consolidate_blocked = _already_proposed(conn, gh)
        dream_blocked = _already_proposed_dream(conn, gh)
    finally:
        conn.close()

    assert not consolidate_blocked, (
        "consolidate should not be blocked by a dream proposal"
    )
    assert dream_blocked, (
        "dream should be blocked by its own prior proposal"
    )


def test_consolidate_and_dream_origin_scoping_reverse(config):
    """A group proposed by consolidate does NOT block dream for the same ids."""
    db = config.db_path
    a = store(db, "dup fact x", type="fact", confidence="high", tags="topic:shared")
    b = store(db, "dup fact x", type="fact", confidence="high", tags="topic:shared")
    group_ids = [a, b]
    gh = _cluster_hash(group_ids)

    # Ingest under consolidate (merge verdict).
    merge_verdict = json.dumps({
        "verdict": "merge", "content": "the merged fact", "type": "fact",
        "confidence": "high", "reason": "dup",
    })
    consolidate_ingest(config, group_ids, gh, merge_verdict)

    conn = connect(db)
    try:
        from techne.core.sleepdream import _already_proposed
        consolidate_blocked = _already_proposed(conn, gh)
        dream_blocked = _already_proposed_dream(conn, gh)
    finally:
        conn.close()

    assert consolidate_blocked, "consolidate should be blocked by its own prior proposal"
    assert not dream_blocked, "dream should not be blocked by a consolidate proposal"


# ---------------------------------------------------------------------------
# MCP round-trip — techne_dream_emit / techne_dream_ingest
# ---------------------------------------------------------------------------


class TestDreamMcpRoundTrip:
    def test_dream_emit_returns_batch(self, config):
        db = config.db_path
        store(db, "fact one shared topic", type="fact", confidence="high",
              tags="topic:mcp-test")
        store(db, "fact two shared topic", type="fact", confidence="high",
              tags="topic:mcp-test")
        resp = handle_request(
            {"id": 10, "method": "tools/call",
             "params": {"name": "techne_dream_emit", "arguments": {}}},
            config,
        )
        assert "error" not in resp
        result = json.loads(resp["result"]["content"][0]["text"])
        assert "count" in result and "batch" in result
        assert result["count"] >= 1

    def test_dream_ingest_via_mcp(self, config):
        db = config.db_path
        a = store(db, "mcp fact alpha", type="fact", confidence="high",
                  tags="topic:mcp-test2")
        b = store(db, "mcp fact beta", type="fact", confidence="high",
                  tags="topic:mcp-test2")
        emit_resp = handle_request(
            {"id": 11, "method": "tools/call",
             "params": {"name": "techne_dream_emit", "arguments": {}}},
            config,
        )
        batch = json.loads(emit_resp["result"]["content"][0]["text"])["batch"]
        item = next((i for i in batch if set(i["group_ids"]) == {a, b}), None)
        assert item is not None

        ingest_resp = handle_request(
            {"id": 12, "method": "tools/call",
             "params": {"name": "techne_dream_ingest", "arguments": {
                 "group_ids": item["group_ids"],
                 "group_hash": item["group_hash"],
                 "response": _CONNECT_VERDICT,
             }}},
            config,
        )
        assert "error" not in ingest_resp
        result = json.loads(ingest_resp["result"]["content"][0]["text"])
        assert result.get("written") == 1

        # Re-emit: the group must be gone.
        emit_resp2 = handle_request(
            {"id": 13, "method": "tools/call",
             "params": {"name": "techne_dream_emit", "arguments": {}}},
            config,
        )
        batch2 = json.loads(emit_resp2["result"]["content"][0]["text"])["batch"]
        assert not any(set(i["group_ids"]) == {a, b} for i in batch2)


# ---------------------------------------------------------------------------
# #246 — silent-failure logging: dream_ingest swallow must now log
# ---------------------------------------------------------------------------


def test_dream_ingest_logs_on_insert_failure(config, caplog):
    """#246: when _insert_dream_candidate raises inside the try block,
    log.exception fires with context AND the errors counter still increments."""
    db = config.db_path
    a = store(db, "insight A about neural nets", type="fact", confidence="high",
              tags="topic:ml")
    b = store(db, "insight B about backprop", type="fact", confidence="high",
              tags="topic:ml")

    with patch(
        "techne.core.sleepdream._insert_dream_candidate",
        side_effect=RuntimeError("disk full"),
    ):
        with caplog.at_level(logging.ERROR, logger="techne.core.sleepdream"):
            counts = dream_ingest(config, [a, b], "ignored-hash", _CONNECT_VERDICT)

    assert counts["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any(
        "dream_ingest" in r.message for r in caplog.records
    ), "log.exception must emit a record naming the function"
