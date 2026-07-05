"""Tests for contradiction detection → supersession (techne#213, D1).

Covers:
  - Candidate selection by shared tags + same type (not FTS/similarity)
  - event_at tiebreak (older loses)
  - Emit / ingest round-trip with canned LLM responses (no real network calls)
  - Anti-re-proposal guard
  - The sqlite-supersedes-postgres scenario (definition of done from the issue)
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.conflict import (
    VALID_VERDICTS,
    conflict_check_emit,
    conflict_check_ingest,
    parse_conflict_response,
    run_conflict_check,
    select_conflict_candidates,
)
from techne.core.database import connect, init_db
from techne.core.knowledge import get, store, supersede
from techne.core.reflect import promote_candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_hash(focus_id: int, comparison_ids: list[int]) -> str:
    """Build the stable group identifier (mirrors the private conflict._check_hash)."""
    from techne.core.sleepdream import _cluster_hash

    return _cluster_hash(sorted([focus_id] + list(comparison_ids)))


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "conflict_test.db"
    init_db(cfg.db_path)
    return cfg


def _status(db, kid):
    conn = connect(db)
    try:
        return conn.execute("SELECT status FROM knowledge WHERE id=?", (kid,)).fetchone()[0]
    finally:
        conn.close()


def _pending_conflict_candidates(db):
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE origin='conflict' AND status='pending'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _all_conflict_candidates(db):
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE origin='conflict'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Candidate selection tests
# ---------------------------------------------------------------------------


def test_select_candidates_by_shared_tags(config):
    """select_conflict_candidates returns live rows with ≥1 shared tag, same type."""
    db = config.db_path
    focus = dict(id=store(db, "use postgres", type="fact", tags="datastore,infra"))
    store(db, "we use redis too", type="fact", tags="datastore,cache")   # shares 'datastore'
    unrelated = store(db, "prefer dark mode", type="fact", tags="ui,editor")
    wrong_type = store(db, "we use postgres", type="decision", tags="datastore,infra")

    focus_row = dict(id=focus["id"], type="fact", tags="datastore,infra")
    focus_row["id"] = focus["id"]

    # Fetch the actual stored row
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
            (focus["id"],)
        ).fetchone()
    finally:
        conn.close()

    candidates = select_conflict_candidates(db, dict(row))
    cand_ids = {c["id"] for c in candidates}

    # Should include the redis row (shared 'datastore' tag, same 'fact' type)
    assert any("redis" in c.get("content", "") for c in candidates), \
        "Expected redis row (shares 'datastore' tag) in candidates"
    # Should NOT include unrelated (no shared tag) or wrong_type (different type)
    assert unrelated not in cand_ids
    assert wrong_type not in cand_ids


def test_select_candidates_same_type_only(config):
    """Different types are never offered as conflict candidates — apples vs oranges."""
    db = config.db_path
    fact_id = store(db, "use postgres", type="fact", tags="db")
    store(db, "switch to postgres", type="decision", tags="db")  # same tags, different type

    conn = connect(db)
    try:
        focus_row = dict(conn.execute(
            "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
            (fact_id,)
        ).fetchone())
    finally:
        conn.close()

    candidates = select_conflict_candidates(db, focus_row)
    # Only fact-typed rows should appear; the decision row must be excluded
    assert all(c["type"] == "fact" for c in candidates)


def test_select_candidates_excludes_superseded(config):
    """Superseded rows must never be offered as comparison candidates."""
    db = config.db_path
    old_id = store(db, "use postgres", type="fact", tags="datastore")
    supersede(db, old_id, "postgres is retired")  # old_id is now superseded

    focus_id = store(db, "we switched to sqlite", type="fact", tags="datastore")
    conn = connect(db)
    try:
        focus_row = dict(conn.execute(
            "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
            (focus_id,)
        ).fetchone())
    finally:
        conn.close()

    candidates = select_conflict_candidates(db, focus_row)
    # old_id is superseded → must not appear
    assert old_id not in {c["id"] for c in candidates}


def test_select_candidates_excludes_self(config):
    """A row is never returned as a conflict candidate for itself."""
    db = config.db_path
    rid = store(db, "use postgres", type="fact", tags="datastore")
    conn = connect(db)
    try:
        row = dict(conn.execute(
            "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
            (rid,)
        ).fetchone())
    finally:
        conn.close()

    candidates = select_conflict_candidates(db, row)
    assert rid not in {c["id"] for c in candidates}


def test_select_candidates_keyword_fallback_no_tags(config):
    """Fallback to keyword overlap when candidate has no tags."""
    db = config.db_path
    store(db, "we use postgres for storage", type="fact", tags="")
    focus_id = store(db, "switched from postgres to sqlite", type="fact", tags="")

    conn = connect(db)
    try:
        focus_row = dict(conn.execute(
            "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
            (focus_id,)
        ).fetchone())
    finally:
        conn.close()

    candidates = select_conflict_candidates(db, focus_row)
    # 'postgres' appears in both → should match via keyword fallback
    assert any("postgres" in c.get("content", "").lower() for c in candidates)


# ---------------------------------------------------------------------------
# run_conflict_check (prompt builder — pure function)
# ---------------------------------------------------------------------------


def test_run_conflict_check_builds_prompt(config):
    """run_conflict_check returns a prompt string mentioning key fields."""
    focus = {"id": 1, "type": "fact", "content": "we switched to sqlite", "event_at": "2025-06-01"}
    comparison = [
        {"id": 2, "type": "fact", "content": "use postgres for main db", "event_at": "2025-01-01"}
    ]
    prompt = run_conflict_check(focus, comparison)
    assert "sqlite" in prompt
    assert "postgres" in prompt
    assert "SUPERSEDE" in prompt
    assert "#1" in prompt or "1" in prompt
    assert "#2" in prompt or "2" in prompt


def test_run_conflict_check_raises_on_empty_comparison():
    with pytest.raises(ValueError, match="comparison_set must not be empty"):
        run_conflict_check({"id": 1, "type": "fact", "content": "x"}, [])


# ---------------------------------------------------------------------------
# parse_conflict_response
# ---------------------------------------------------------------------------


def test_parse_conflict_response_supersede():
    raw = json.dumps([
        {"id": 5, "verdict": "SUPERSEDE", "content": "we switched to sqlite", "reason": "contradiction"}
    ])
    decisions = parse_conflict_response(raw)
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == "SUPERSEDE"
    assert decisions[0]["id"] == 5
    assert "sqlite" in (decisions[0]["content"] or "")


def test_parse_conflict_response_noop():
    raw = json.dumps([{"id": 3, "verdict": "NOOP", "reason": "already known"}])
    decisions = parse_conflict_response(raw)
    assert decisions[0]["verdict"] == "NOOP"


def test_parse_conflict_response_distinct():
    raw = json.dumps([{"id": 7, "verdict": "DISTINCT", "reason": "different topic"}])
    decisions = parse_conflict_response(raw)
    assert decisions[0]["verdict"] == "DISTINCT"


def test_parse_conflict_response_unknown_verdict_ignored():
    raw = json.dumps([
        {"id": 1, "verdict": "DELETE", "reason": "bad"},  # unknown — should be ignored
        {"id": 2, "verdict": "NOOP", "reason": "fine"},
    ])
    decisions = parse_conflict_response(raw)
    assert len(decisions) == 1
    assert decisions[0]["id"] == 2


def test_parse_conflict_response_garbage_returns_empty():
    assert parse_conflict_response("not json at all") == []
    assert parse_conflict_response("") == []


def test_parse_conflict_response_tolerates_code_fence():
    raw = "```json\n" + json.dumps([{"id": 4, "verdict": "DISTINCT", "reason": "x"}]) + "\n```"
    decisions = parse_conflict_response(raw)
    assert decisions[0]["verdict"] == "DISTINCT"


def test_parse_conflict_response_all_verdicts():
    for v in VALID_VERDICTS:
        raw = json.dumps([{"id": 1, "verdict": v, "content": "x", "reason": "r"}])
        decisions = parse_conflict_response(raw)
        assert decisions[0]["verdict"] == v


# ---------------------------------------------------------------------------
# THE DEFINITION-OF-DONE SCENARIO: sqlite supersedes postgres (techne#213)
# ---------------------------------------------------------------------------


def test_sqlite_supersedes_postgres(config):
    """Storing 'we switched to sqlite' after 'use postgres' (same type, shared tag)
    must emit a SUPERSEDE candidate on the next conflict_check pass. The older
    event_at loses. This is the exact scenario from the issue definition of done."""
    db = config.db_path

    # Older fact: 'use postgres' — earlier event_at
    postgres_id = store(db, "use postgres", type="fact", tags="datastore,database")
    # Explicitly give it an older event_at by updating directly
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE knowledge SET event_at='2025-01-01 00:00:00' WHERE id=?", (postgres_id,)
        )
        conn.commit()
    finally:
        conn.close()

    # Newer fact: 'we switched to sqlite' — later event_at
    sqlite_id = store(db, "we switched to sqlite", type="fact", tags="datastore,database")
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE knowledge SET event_at='2025-06-01 00:00:00' WHERE id=?", (sqlite_id,)
        )
        conn.commit()
    finally:
        conn.close()

    # ---- Emit ----
    batch = conflict_check_emit(config)
    assert len(batch) > 0, "Expected at least one conflict group"

    # Find the group containing both entries
    target_group = None
    for item in batch:
        all_ids = {item["focus_id"]} | set(item["comparison_ids"])
        if postgres_id in all_ids and sqlite_id in all_ids:
            target_group = item
            break
    assert target_group is not None, (
        f"Expected a conflict group containing both postgres ({postgres_id}) "
        f"and sqlite ({sqlite_id}) entries; got batch: {batch}"
    )

    # ---- Ingest with SUPERSEDE verdict ----
    # The verdict must reference a comparison_id (not the focus_id). Determine
    # which of postgres/sqlite is actually the comparison entry in this batch item.
    comp_ids_set = set(target_group["comparison_ids"])
    # At least one of the two entries must be in the comparison set
    # (the other is the focus). Target whichever comparison entry is present.
    verdict_target = next(
        cid for cid in [postgres_id, sqlite_id] if cid in comp_ids_set
    )
    verdict_response = json.dumps([
        {
            "id": verdict_target,
            "verdict": "SUPERSEDE",
            "content": "we switched to sqlite",
            "reason": "new fact directly contradicts the old one",
        }
    ])
    counts = conflict_check_ingest(
        config,
        target_group["focus_id"],
        target_group["comparison_ids"],
        target_group["check_hash"],
        verdict_response,
    )
    assert counts["supersede"] == 1, f"Expected 1 supersede; got {counts}"
    assert counts["errors"] == 0

    # ---- Verify: a pending 'supersede' candidate was staged ----
    candidates = _pending_conflict_candidates(db)
    assert len(candidates) >= 1, "Expected at least one pending supersede candidate"
    cand = candidates[0]
    assert cand["action"] == "supersede"
    assert cand["origin"] == "conflict"

    # The TARGET should be the OLDER entry (postgres, earlier event_at)
    target_ids = json.loads(cand["target_ids"])
    assert postgres_id in target_ids, (
        f"Expected postgres entry ({postgres_id}) to be the supersede target (older event_at); "
        f"got {target_ids}"
    )

    # ---- Promote the candidate — verify postgres is now retired ----
    new_id = promote_candidate(db, cand["id"])
    assert new_id is not None
    postgres_row = get(db, postgres_id)
    assert postgres_row["superseded_by"] is not None, (
        "postgres entry should be superseded after promoting the conflict candidate"
    )


# ---------------------------------------------------------------------------
# event_at tiebreak — explicit
# ---------------------------------------------------------------------------


def test_event_at_tiebreak_older_loses(config):
    """The entry with the earlier event_at must be the supersede target."""
    db = config.db_path

    older_id = store(db, "old fact about db", type="fact", tags="db")
    newer_id = store(db, "new fact about db", type="fact", tags="db")

    conn = connect(db)
    try:
        conn.execute(
            "UPDATE knowledge SET event_at='2024-01-01' WHERE id=?", (older_id,)
        )
        conn.execute(
            "UPDATE knowledge SET event_at='2025-01-01' WHERE id=?", (newer_id,)
        )
        conn.commit()
    finally:
        conn.close()

    # Simulate ingest where focus=newer, comparison=[older], verdict=SUPERSEDE
    verdict_response = json.dumps([
        {"id": older_id, "verdict": "SUPERSEDE", "content": "new fact about db", "reason": "newer wins"}
    ])
    counts = conflict_check_ingest(
        config, newer_id, [older_id], "test-hash-tiebreak", verdict_response
    )
    assert counts["supersede"] == 1

    candidates = _pending_conflict_candidates(db)
    assert len(candidates) == 1
    target_ids = json.loads(candidates[0]["target_ids"])
    assert older_id in target_ids, "Older entry must be the supersede target"


def test_event_at_tiebreak_when_focus_is_older(config):
    """If focus has an earlier event_at than comparison, comparison wins → focus retired."""
    db = config.db_path

    # focus is OLDER
    focus_id = store(db, "old db fact", type="fact", tags="db")
    comp_id = store(db, "new db fact", type="fact", tags="db")

    conn = connect(db)
    try:
        conn.execute("UPDATE knowledge SET event_at='2024-01-01' WHERE id=?", (focus_id,))
        conn.execute("UPDATE knowledge SET event_at='2025-06-01' WHERE id=?", (comp_id,))
        conn.commit()
    finally:
        conn.close()

    # Verdict says focus SUPERSEDES comp — but focus is actually older.
    # The tiebreak in code should override and retire the focus (older event_at).
    verdict_response = json.dumps([
        {"id": comp_id, "verdict": "SUPERSEDE", "content": "new db fact", "reason": "test"}
    ])
    counts = conflict_check_ingest(config, focus_id, [comp_id], "test-hash-older-focus", verdict_response)
    assert counts["supersede"] == 1

    candidates = _pending_conflict_candidates(db)
    assert len(candidates) == 1
    target_ids = json.loads(candidates[0]["target_ids"])
    # focus_id is older → should be the supersede target
    assert focus_id in target_ids, f"focus (older event_at) should be the target; got {target_ids}"


# ---------------------------------------------------------------------------
# Anti-re-proposal guard
# ---------------------------------------------------------------------------


def test_conflict_emit_skips_already_proposed(config):
    """A pair with a pending/discarded conflict candidate must not be re-emitted."""
    db = config.db_path
    a_id = store(db, "use postgres", type="fact", tags="datastore")
    b_id = store(db, "we switched to sqlite", type="fact", tags="datastore")

    batch1 = conflict_check_emit(config)
    assert len(batch1) > 0

    # Ingest a DISTINCT verdict → discarded marker
    group = next(
        i for i in batch1
        if {i["focus_id"]} | set(i["comparison_ids"]) == {a_id, b_id}
    )
    verdict = json.dumps([{"id": (a_id if group["focus_id"] == b_id else b_id),
                            "verdict": "DISTINCT", "reason": "test"}])
    conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                          group["check_hash"], verdict)

    # Second emit: the pair must NOT reappear
    batch2 = conflict_check_emit(config)
    for item in batch2:
        all_ids = {item["focus_id"]} | set(item["comparison_ids"])
        assert {a_id, b_id} != all_ids, "Already-judged pair must not be re-emitted"


def test_conflict_ingest_deduplicates_on_reingest(config):
    """Re-ingesting the same check_hash is a no-op (skipped counter increments)."""
    db = config.db_path
    a_id = store(db, "postgres fact", type="fact", tags="db")
    b_id = store(db, "sqlite fact", type="fact", tags="db")

    batch = conflict_check_emit(config)
    group = next(
        i for i in batch
        if {i["focus_id"]} | set(i["comparison_ids"]) == {a_id, b_id}
    )
    verdict = json.dumps([
        {"id": b_id if group["focus_id"] == a_id else a_id, "verdict": "NOOP", "reason": "test"}
    ])

    first = conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                                   group["check_hash"], verdict)
    assert first["skipped"] == 0

    second = conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                                    group["check_hash"], verdict)
    assert second["skipped"] == 1


# ---------------------------------------------------------------------------
# NOOP and DISTINCT produce discarded markers (not pending candidates)
# ---------------------------------------------------------------------------


def test_noop_verdict_writes_discarded_marker(config):
    """NOOP should write a discarded marker, not a pending candidate."""
    db = config.db_path
    a_id = store(db, "postgres is the db", type="fact", tags="db")
    b_id = store(db, "use postgres", type="fact", tags="db")

    batch = conflict_check_emit(config)
    group = next(
        i for i in batch
        if {i["focus_id"]} | set(i["comparison_ids"]) == {a_id, b_id}
    )
    comp_id = group["comparison_ids"][0] if group["focus_id"] in {a_id, b_id} else a_id
    verdict = json.dumps([{"id": comp_id, "verdict": "NOOP", "reason": "same fact"}])
    counts = conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                                   group["check_hash"], verdict)
    assert counts["noop"] == 1

    # No pending candidates — only a discarded marker
    pending = _pending_conflict_candidates(db)
    assert len(pending) == 0

    all_cands = _all_conflict_candidates(db)
    assert any(c["status"] == "discarded" for c in all_cands)


def test_distinct_verdict_writes_discarded_marker(config):
    """DISTINCT should write a discarded marker."""
    db = config.db_path
    a_id = store(db, "use postgres for storage", type="fact", tags="db")
    b_id = store(db, "prefer sqlite", type="fact", tags="db")

    batch = conflict_check_emit(config)
    group = next(
        i for i in batch
        if {i["focus_id"]} | set(i["comparison_ids"]) == {a_id, b_id}
    )
    comp_id = group["comparison_ids"][0] if group["focus_id"] in {a_id, b_id} else a_id
    verdict = json.dumps([{"id": comp_id, "verdict": "DISTINCT", "reason": "different scope"}])
    counts = conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                                   group["check_hash"], verdict)
    assert counts["distinct"] == 1

    pending = _pending_conflict_candidates(db)
    assert len(pending) == 0


# ---------------------------------------------------------------------------
# UPDATE verdict
# ---------------------------------------------------------------------------


def test_update_verdict_stages_supersede_candidate(config):
    """UPDATE stages a pending supersede candidate with the merged content."""
    db = config.db_path
    a_id = store(db, "postgres version 13", type="fact", tags="db")
    b_id = store(db, "postgres v13 with extensions", type="fact", tags="db")

    batch = conflict_check_emit(config)
    group = next(
        i for i in batch
        if {i["focus_id"]} | set(i["comparison_ids"]) == {a_id, b_id}
    )
    comp_id = group["comparison_ids"][0]
    verdict = json.dumps([{
        "id": comp_id,
        "verdict": "UPDATE",
        "content": "we use postgres v13 with pgvector and hstore extensions",
        "reason": "augments without contradiction"
    }])
    counts = conflict_check_ingest(config, group["focus_id"], group["comparison_ids"],
                                   group["check_hash"], verdict)
    assert counts["update"] == 1
    assert counts["errors"] == 0

    pending = _pending_conflict_candidates(db)
    assert len(pending) >= 1
    cand = pending[0]
    assert cand["action"] == "supersede"
    assert "pgvector" in cand["content"]


# ---------------------------------------------------------------------------
# Promote supersede candidate applies supersession
# ---------------------------------------------------------------------------


def test_promote_conflict_candidate_applies_supersession(config):
    """Promoting a conflict supersede candidate calls supersede() on the older entry."""
    db = config.db_path
    postgres_id = store(db, "use postgres", type="fact", tags="datastore")
    sqlite_id = store(db, "we switched to sqlite", type="fact", tags="datastore")

    conn = connect(db)
    try:
        conn.execute("UPDATE knowledge SET event_at='2024-01-01' WHERE id=?", (postgres_id,))
        conn.execute("UPDATE knowledge SET event_at='2025-06-01' WHERE id=?", (sqlite_id,))
        conn.commit()
    finally:
        conn.close()

    verdict = json.dumps([
        {"id": postgres_id, "verdict": "SUPERSEDE",
         "content": "we switched to sqlite", "reason": "newer fact wins"}
    ])
    conflict_check_ingest(config, sqlite_id, [postgres_id], "test-promote", verdict)

    candidates = _pending_conflict_candidates(db)
    assert len(candidates) == 1

    new_id = promote_candidate(db, candidates[0]["id"])
    assert new_id is not None

    # postgres entry should now be retired
    postgres_row = get(db, postgres_id)
    assert postgres_row["superseded_by"] == new_id

    # new entry should carry the surviving content
    new_row = get(db, new_id)
    assert "sqlite" in new_row["content"]


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_conflict_config_defaults(config):
    """ConflictConfig defaults are sane."""
    from techne.config import ConflictConfig

    cfg = ConflictConfig()
    assert cfg.max_comparison_candidates > 0
    assert cfg.max_items_per_emit > 0
    assert cfg.log_raw_response is True
    assert cfg.model == ""  # falls back to Config.model


def test_config_conflict_field_accessible(config):
    """Config has a .conflict attribute after adding ConflictConfig."""
    assert hasattr(config, "conflict")
    assert hasattr(config.conflict, "max_comparison_candidates")
    assert hasattr(config.conflict, "max_items_per_emit")


# ---------------------------------------------------------------------------
# run_conflict_check is reusable (federation call site design check)
# ---------------------------------------------------------------------------


def test_run_conflict_check_is_pure():
    """run_conflict_check takes plain dicts and returns a string — no DB access,
    making it callable at the federation read-merge call site."""
    focus = {
        "id": 42,
        "type": "fact",
        "content": "we now use clickhouse",
        "event_at": "2025-10-01",
        "tags": "datastore,analytics",
    }
    comparison = [
        {
            "id": 7,
            "type": "fact",
            "content": "we use bigquery for analytics",
            "event_at": "2025-01-01",
            "tags": "datastore,analytics",
        }
    ]
    prompt = run_conflict_check(focus, comparison)
    assert isinstance(prompt, str)
    assert len(prompt) > 50
    # Both entries' content should appear in the prompt
    assert "clickhouse" in prompt.lower()
    assert "bigquery" in prompt.lower()
