"""Tests for federation read-merge (techne#222).

Mirrors test_conflict.py patterns. Six test groups:
  1. local_only_unchanged        — no connections → recall returns local-only, identical shape
  2. merges_team_results         — team connection → merged results include rows from both stores
  3. detects_cross_store_contradiction — sqlite-vs-postgres scenario surfaces correctly
  4. ingest_stages_pending_candidate  — SUPERSEDE → pending supersede; local-newer → discarded
  5. read_only_remote_under_WAL  — WAL-mode team db opens read-only; no schema write
  6. foreign_id_never_fetched_locally — namespaced focus id never _fetch_row'd from local db

Sovereignty invariants verified throughout:
  1. ?mode=ro  2. recall read-only  3. no auto-apply  4. raw never federates
  5. opt-in  6. no cabal import
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from techne.config import Config, FederationConnectionSpec
from techne.core.database import connect, init_db
from techne.core.knowledge import store

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _init_team_db(path):
    """Create a fully-initialised techne db at path. Returns the path."""
    conn = init_db(path)
    conn.close()
    return path


def _pending_federation_candidates(db):
    """Return all pending federation-conflict candidates from local db."""
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_candidates "
            "WHERE origin='federation-conflict' AND status='pending'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _all_federation_candidates(db):
    """Return ALL federation-conflict candidates (any status) from local db."""
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE origin='federation-conflict'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _knowledge_status(db, kid):
    conn = connect(db)
    try:
        row = conn.execute("SELECT status FROM knowledge WHERE id=?", (kid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _knowledge_superseded_by(db, kid):
    conn = connect(db)
    try:
        row = conn.execute("SELECT superseded_by FROM knowledge WHERE id=?", (kid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. local_only_unchanged — noop gate
# ---------------------------------------------------------------------------


class TestLocalOnlyUnchanged:
    """No connections → recall returns local-only, identical shape (DoD #5 opt-in)."""

    def test_no_connections_returns_local_shape(self, tmp_path):
        """The noop gate: empty connections → search_results + identity_matches only."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "postgres is the main database", type="fact", tags="datastore")

        # fan_out_search with empty connections — should return local rows, no remotes
        local_results, remote_results = fan_out_search(
            local_db,
            connections={},
            query="postgres database",
            limit=10,
            half_life_days=30.0,
        )

        assert len(local_results) >= 1
        assert remote_results == []

    def test_mcp_recall_no_connections_identical_shape(self, tmp_path):
        """techne_recall with no connections returns the pre-federation shape unchanged."""
        from techne.mcp_server import handle_request

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "we use redis for caching", type="fact", tags="cache")

        cfg = Config()
        cfg.db_path = local_db
        # connections defaults to empty dict

        resp = handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "techne_recall", "arguments": {"topic": "redis caching"}}},
            cfg,
        )

        result = resp["result"]["content"][0]["text"]
        data = json.loads(result)
        # Must have these two keys and nothing else federation-related
        assert "search_results" in data
        assert "identity_matches" in data
        assert "federation_conflicts" not in data
        assert "_federation_note" not in data

    def test_local_rows_tagged_local(self, tmp_path):
        """Local results are tagged _store=local, _role=local."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "always use type hints in Python", type="procedure", tags="python")

        local_results, _ = fan_out_search(
            local_db, {}, "python type hints", limit=10, half_life_days=30.0
        )

        for r in local_results:
            assert r.get("_store") == "local"
            assert r.get("_role") == "local"


# ---------------------------------------------------------------------------
# 2. merges_team_results — fan-out DoD
# ---------------------------------------------------------------------------


class TestMergesTeamResults:
    """Team connection → merged results include rows from both stores (DoD #1)."""

    def test_merged_results_include_team_rows(self, tmp_path):
        """With a team connection, recall returns rows from both local and team store."""
        from techne.core.federation import _cross_store_dedup, fan_out_search, merge_and_rank

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        store(local_db, "local decision: use FastAPI", type="decision", tags="framework")
        store(team_db, "team decision: use asyncio for concurrency", type="decision", tags="async")

        connections = {
            "team-shared": FederationConnectionSpec(role="team", db_path=str(team_db)),
        }

        local_results, remote_results = fan_out_search(
            local_db, connections, "decision framework async", limit=10, half_life_days=30.0
        )

        assert any("local" in (r.get("content") or "") for r in local_results), (
            "Local row should appear in local_results"
        )
        assert any("asyncio" in (r.get("content") or "") for r in remote_results), (
            "Team row should appear in remote_results"
        )

        remote_deduped = _cross_store_dedup(local_results, remote_results)
        merged = merge_and_rank(
            local_results, remote_deduped, limit=20, recency_weight=0.3, bm25_weight=0.7,
            half_life_days=30.0
        )

        contents = [r.get("content") or "" for r in merged]
        assert any("asyncio" in c for c in contents), "Team row must be in merged result"

    def test_merge_and_rank_handles_vec_first_rows(self):
        """Regression (techne#401 / PR #402): merge_and_rank must not crash on the
        rows a vec-enabled search() now emits.

        The vec-first union (search.py) returns vector-channel rows: a pure
        ``vec`` row carries ``distance`` but NO BM25 ``rank``, and an ``fts+vec``
        overlap row carries both. _rerank subscripts ``rank`` hard, so before the
        fix the pooled rerank raised KeyError on the federation merge path. The
        existing federation tests missed this because their env has no embedding
        backend → the vec channel is empty → only rank-bearing FTS rows flow.
        Here we construct the vec-first rows directly, no backend needed.
        """
        from techne.core.federation import merge_and_rank

        local_results = [
            # fts+vec overlap — Layer 1 restores `rank` from the FTS row.
            {"source_table": "knowledge", "id": 1, "content": "overlap hit",
             "rank": -2.0, "distance": 0.10, "match_source": "fts+vec",
             "fusion": "vec_first"},
            # pure vector-only hit — NO `rank`; Layer 2 gives it a neutral norm.
            {"source_table": "knowledge", "id": 2, "content": "vector-only hit",
             "distance": 0.15, "match_source": "vec", "fusion": "vec_first"},
            # plain FTS-only hit — has `rank`, unchanged path.
            {"source_table": "knowledge", "id": 3, "content": "lexical hit",
             "rank": -1.0, "match_source": "fts", "fusion": "vec_first"},
        ]

        # Must not raise, and must preserve every row (ranking only, no drops).
        merged = merge_and_rank(
            local_results, [], limit=20, recency_weight=0.3, bm25_weight=0.7,
            half_life_days=30.0,
        )
        assert {r["id"] for r in merged} == {1, 2, 3}
        # Scratch keys _rerank uses must be cleaned off the returned rows.
        for r in merged:
            assert "_norm_bm25" not in r and "_combined" not in r

    def test_team_rows_tagged_with_store_and_role(self, tmp_path):
        """Remote rows carry _store (connection name) and _role ('team')."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        store(team_db, "use mypy for type checking", type="procedure", tags="python,typing")

        connections = {
            "archivist": FederationConnectionSpec(role="team", db_path=str(team_db)),
        }

        _, remote_results = fan_out_search(
            local_db, connections, "mypy type checking", limit=10, half_life_days=30.0
        )

        assert len(remote_results) >= 1
        for r in remote_results:
            assert r.get("_store") == "archivist"
            assert r.get("_role") == "team"

    def test_raw_never_federates(self, tmp_path):
        """Remote search only returns knowledge rows, never threads/observations.

        This tests sovereignty invariant #4: raw never federates.
        """
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        # Store a knowledge row on the team db
        store(team_db, "knowledge about deployments", type="fact", tags="deploy")

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}
        _, remote_results = fan_out_search(
            local_db, connections, "deploy knowledge", limit=10, half_life_days=30.0
        )

        # All remote results must be type='knowledge' (never thread or observation)
        for r in remote_results:
            assert r.get("source_table") == "knowledge", (
                f"Remote result type must be 'knowledge', got {r.get('type')!r}"
            )

    def test_content_hash_dedup_removes_exact_copy(self, tmp_path):
        """Exact-copy content in both stores is deduplicated before merge."""
        from techne.core.federation import _cross_store_dedup, fan_out_search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        shared_content = "always use git commit --signoff"
        store(local_db, shared_content, type="procedure", tags="git")
        store(team_db, shared_content, type="procedure", tags="git")

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}
        local_results, remote_results = fan_out_search(
            local_db, connections, "git commit signoff", limit=10, half_life_days=30.0
        )

        remote_deduped = _cross_store_dedup(local_results, remote_results)
        # The exact-copy team row should have been removed
        assert all(shared_content not in (r.get("content") or "") for r in remote_deduped), (
            "Exact-copy team row should be dropped by content-hash dedup"
        )


# ---------------------------------------------------------------------------
# 3. detects_cross_store_contradiction — the sqlite-vs-postgres scenario
# ---------------------------------------------------------------------------


class TestDetectsCrossStoreContradiction:
    """The sqlite-vs-postgres scenario surfaces correctly (DoD #2). Make it bite."""

    def test_sqlite_vs_postgres_surfaces_conflict(self, tmp_path):
        """Local: 'use postgres' (older); Team: 'switched to sqlite' (newer, same tags/type).

        This is the canonical contradiction scenario from the proposal — two facts about
        the same entity (database technology) with contradictory content. The detection
        must surface a conflict prompt, not silently return both as if they were distinct.
        """
        from techne.core.federation import (
            _cross_store_dedup,
            detect_cross_store_contradictions,
            fan_out_search,
        )

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        # Local: older, uses postgres
        local_id = store(
            local_db,
            "use postgres for the main application database",
            type="fact",
            tags="datastore,database",
        )
        # Team: newer, switched to sqlite
        store(
            team_db,
            "we switched to sqlite — postgres was overkill for this workload",
            type="fact",
            tags="datastore,database",
        )

        # Set a recent event_at on the team row to make it clearly newer
        team_conn = connect(team_db)
        try:
            team_conn.execute(
                "UPDATE knowledge SET event_at = '2026-07-04 12:00:00' WHERE 1=1"
            )
            team_conn.commit()
        finally:
            team_conn.close()

        # Local row gets an older event_at
        local_conn = connect(local_db)
        try:
            local_conn.execute(
                "UPDATE knowledge SET event_at = '2026-01-01 00:00:00' WHERE id=?", (local_id,)
            )
            local_conn.commit()
        finally:
            local_conn.close()

        connections = {
            "team-archivist": FederationConnectionSpec(role="team", db_path=str(team_db))
        }
        local_results, remote_results = fan_out_search(
            local_db, connections, "database postgres sqlite", limit=10, half_life_days=30.0
        )

        remote_deduped = _cross_store_dedup(local_results, remote_results)
        # The team row must survive dedup (different content from local)
        assert any("sqlite" in (r.get("content") or "") for r in remote_deduped), (
            "Team 'switched to sqlite' row must survive content-hash dedup"
        )

        conflicts = detect_cross_store_contradictions(
            local_db, local_results, remote_deduped, max_candidates=20
        )

        assert len(conflicts) >= 1, (
            "Expected at least one cross-store contradiction to be detected. "
            "The sqlite-vs-postgres scenario must surface, not silently pass."
        )

        # The conflict must reference the local row
        all_comparison_ids = {cid for c in conflicts for cid in c["comparison_ids"]}
        assert local_id in all_comparison_ids, (
            f"Local row id={local_id} must appear in comparison_ids of a detected conflict"
        )

        # The focus_id must be namespaced (not a plain integer)
        for conflict in conflicts:
            assert ":" in conflict["focus_id"], (
                f"focus_id must be namespaced (store:id), got {conflict['focus_id']!r}"
            )
            assert conflict["focus_store"] == "team-archivist"
            assert "prompt" in conflict
            assert "check_hash" in conflict
            # Prompt must reference both the foreign and the local entries
            assert "sqlite" in conflict["prompt"].lower() or "team-archivist" in conflict["prompt"]

    def test_no_conflict_for_unrelated_content(self, tmp_path):
        """Content without shared tags does not trigger a contradiction check."""
        from techne.core.federation import (
            _cross_store_dedup,
            detect_cross_store_contradictions,
            fan_out_search,
        )

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        store(local_db, "prefer dark mode in all editors", type="fact", tags="ui,editor")
        store(team_db, "use kubernetes for orchestration", type="fact", tags="infra,k8s")

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}
        local_results, remote_results = fan_out_search(
            local_db, connections, "editor kubernetes", limit=10, half_life_days=30.0
        )
        remote_deduped = _cross_store_dedup(local_results, remote_results)
        conflicts = detect_cross_store_contradictions(
            local_db, local_results, remote_deduped
        )

        # No shared tags → no contradiction detected
        assert conflicts == [], (
            "Unrelated content (no shared tags) must not produce a conflict"
        )

    def test_anti_re_proposal_guard(self, tmp_path):
        """The same cross-store conflict pair is not re-emitted on a second recall."""
        from techne.core.federation import (
            _cross_store_dedup,
            detect_cross_store_contradictions,
            fan_out_search,
            federation_conflict_ingest,
        )

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        local_id = store(local_db, "use postgres", type="fact", tags="datastore")
        store(team_db, "switched to sqlite", type="fact", tags="datastore")

        cfg = Config()
        cfg.db_path = local_db

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}

        local_results, remote_results = fan_out_search(
            local_db, connections, "database postgres sqlite", limit=10, half_life_days=30.0
        )
        remote_deduped = _cross_store_dedup(local_results, remote_results)
        conflicts1 = detect_cross_store_contradictions(local_db, local_results, remote_deduped)

        assert len(conflicts1) >= 1, "First call must detect the conflict"

        # Ingest a verdict to register the check_hash in the local db
        conflict = conflicts1[0]
        federation_conflict_ingest(
            cfg,
            focus_content=conflict["focus_content"],
            focus_store=conflict["focus_store"],
            focus_event_at=conflict.get("focus_event_at"),
            comparison_ids=conflict["comparison_ids"],
            check_hash=conflict["check_hash"],
            response=json.dumps([
                {"id": local_id, "verdict": "DISTINCT", "reason": "test dedup guard"}
            ]),
        )

        # Second call — the guard must suppress the same conflict
        local_results2, remote_results2 = fan_out_search(
            local_db, connections, "database postgres sqlite", limit=10, half_life_days=30.0
        )
        remote_deduped2 = _cross_store_dedup(local_results2, remote_results2)
        conflicts2 = detect_cross_store_contradictions(
            local_db, local_results2, remote_deduped2
        )

        assert conflicts2 == [], (
            "Anti-re-proposal guard must suppress already-ingested conflict on second recall"
        )


# ---------------------------------------------------------------------------
# 4. ingest_stages_pending_candidate
# ---------------------------------------------------------------------------


class TestIngestStagesPendingCandidate:
    """Ingest paths: SUPERSEDE + team-newer → pending supersede; local-newer → discarded.

    The local knowledge row must NOT be auto-applied in any case.
    """

    def test_team_newer_supersede_stages_pending_supersede(self, tmp_path):
        """SUPERSEDE verdict + team newer → pending 'supersede' candidate in LOCAL db.

        The local knowledge row is still live (not auto-applied).
        """
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        local_id = store(local_db, "use postgres", type="fact", tags="datastore")

        cfg = Config()
        cfg.db_path = local_db

        # Team is newer (future event_at — unambiguously newer than any created_at)
        counts = federation_conflict_ingest(
            cfg,
            focus_content="we switched to sqlite — much simpler",
            focus_store="team-archivist",
            focus_event_at="2030-01-01T00:00:00",  # unambiguously newer than local created_at
            comparison_ids=[local_id],
            check_hash="",  # will be recomputed
            response=json.dumps([{
                "id": local_id,
                "verdict": "SUPERSEDE",
                "content": "we switched to sqlite — much simpler",
                "reason": "team adopted sqlite in 2026",
            }]),
        )

        assert counts["errors"] == 0
        assert counts["supersede"] == 1

        # A pending 'supersede' candidate must exist in the LOCAL db.
        # The action must be 'supersede' (not 'retire') because the foreign survivor
        # has no local id — _apply_retire requires two LOCAL ids [old, survivor] and
        # would ValueError with only one. _apply_supersede mints a new local row from
        # the content and points the stale local row's superseded_by at it. (techne#245)
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 1, f"Expected 1 pending candidate, got {pending}"
        cand = pending[0]
        assert cand["action"] == "supersede"
        assert cand["status"] == "pending"
        assert cand["origin"] == "federation-conflict"

        target_ids = json.loads(cand["target_ids"])
        assert local_id in target_ids, "Pending supersede must target the LOCAL row id"

        # CRITICAL: the local knowledge row must NOT be auto-applied (still live)
        assert _knowledge_status(local_db, local_id) != "superseded", (
            "Local row must NOT be auto-applied — it is still live pending operator review"
        )
        assert _knowledge_superseded_by(local_db, local_id) is None, (
            "superseded_by must not be set — the candidate is pending, not promoted"
        )

    def test_team_newer_supersede_promote_succeeds(self, tmp_path):
        """Promoting the federation SUPERSEDE candidate succeeds end-to-end (techne#245).

        This is the bug-bite test: before the fix, action='retire' with one target_id
        raised ValueError('retire candidate needs exactly two target_ids'). After the
        fix, action='supersede' mints a new local row from the team content and sets
        superseded_by on the stale local row.

        Assertions:
          - promote_candidate does NOT raise
          - the stale local row's superseded_by is now non-NULL
          - a new live knowledge row exists carrying the team's content
        """
        from techne.core.knowledge import get
        from techne.core.reflect import promote_candidate

        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        team_content = "we switched to sqlite — much simpler for this workload"
        local_id = store(local_db, "use postgres for all services", type="fact", tags="datastore")

        cfg = Config()
        cfg.db_path = local_db

        counts = federation_conflict_ingest(
            cfg,
            focus_content=team_content,
            focus_store="team-archivist",
            focus_event_at="2030-01-01T00:00:00",  # unambiguously newer
            comparison_ids=[local_id],
            check_hash="",
            response=json.dumps([{
                "id": local_id,
                "verdict": "SUPERSEDE",
                "content": team_content,
                "reason": "team adopted sqlite in 2026",
            }]),
        )
        assert counts["errors"] == 0
        assert counts["supersede"] == 1

        # Exactly one pending supersede candidate
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 1
        assert pending[0]["action"] == "supersede", (
            "Staged action must be 'supersede', not 'retire' — before fix this was 'retire' "
            "which would raise ValueError on promote (two target_ids required but only one "
            "provided). (techne#245)"
        )

        # --- Promote: must NOT raise ---
        cand_id = pending[0]["id"]
        new_id = promote_candidate(local_db, cand_id, decided_by="test", decision_reason="fix#245")

        # --- Post-promote assertions ---
        assert new_id is not None, "promote_candidate must return a new knowledge.id"

        # The stale local row must now be retired (superseded_by set)
        stale_row = get(local_db, local_id)
        assert stale_row is not None
        assert stale_row.get("superseded_by") is not None, (
            "The stale local row must have superseded_by set after promote (was None = bug)"
        )
        assert stale_row["superseded_by"] == new_id, (
            f"superseded_by must point at the newly minted row ({new_id})"
        )

        # A new live row must exist carrying the team's content
        new_row = get(local_db, new_id)
        assert new_row is not None, f"New knowledge row id={new_id} must exist after promote"
        assert new_row.get("superseded_by") is None, "New row must be live (not superseded)"
        assert team_content in (new_row.get("content") or ""), (
            f"New row must carry the team's content. Got: {new_row.get('content')!r}"
        )

    def test_temporal_tiebreak_iso_t_separator_normalized(self, tmp_path):
        """ISO 'T' separator in focus_event_at must compare correctly vs SQLite space separator.

        Bug (techne#245): 'T' (0x54) > ' ' (0x20) in ASCII, so without normalization an ISO
        timestamp '2026-01-01T00:00:00' compares as GREATER than '2026-01-01 01:00:00'
        (space-sep, one hour later) because the comparison short-circuits at position 10.
        This flips the tiebreak: the EARLIER team fact would be deemed 'newer', causing
        an incorrect pending supersede instead of a discarded marker.

        After the fix: both sides are normalized (T→space) so '2026-01-01 00:00:00' <
        '2026-01-01 01:00:00' → local is newer → discarded marker, no pending candidate.
        """
        from techne.core.federation import federation_conflict_ingest
        from techne.core.database import connect

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        local_id = store(local_db, "use postgres now", type="fact", tags="datastore")

        # Set local event_at to 2026-01-01 01:00:00 (space-separated, SQLite format)
        # so it is 1 hour NEWER than the team's 2026-01-01T00:00:00 (ISO format).
        local_conn = connect(local_db)
        try:
            local_conn.execute(
                "UPDATE knowledge SET event_at='2026-01-01 01:00:00' WHERE id=?", (local_id,)
            )
            local_conn.commit()
        finally:
            local_conn.close()

        cfg = Config()
        cfg.db_path = local_db

        # Team event_at is ISO "T" format, and is 1 hour EARLIER than local.
        # Without the fix: '2026-01-01T00:00:00' >= '2026-01-01 01:00:00' is True
        # (because T > space at char 10) → incorrectly stages a pending supersede.
        # With the fix: '2026-01-01 00:00:00' >= '2026-01-01 01:00:00' is False
        # (correct, team is earlier) → discarded marker, zero pending candidates.
        counts = federation_conflict_ingest(
            cfg,
            focus_content="use sqlite instead",
            focus_store="team-archivist",
            focus_event_at="2026-01-01T00:00:00",  # ISO format, 1 hour BEFORE local
            comparison_ids=[local_id],
            check_hash="",
            response=json.dumps([{
                "id": local_id,
                "verdict": "SUPERSEDE",
                "content": "use sqlite instead",
                "reason": "team prefers sqlite",
            }]),
        )

        assert counts["errors"] == 0
        assert counts["supersede"] == 1

        # Local is newer → must produce a DISCARDED marker, not a pending supersede
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 0, (
            f"Local is newer (2026-01-01 01:00:00 > 2026-01-01T00:00:00 after normalization) "
            f"so no pending candidate should be staged. Got {pending}. "
            "Without the T→space normalization fix this would be 1 (incorrect team-newer path)."
        )

        all_cands = _all_federation_candidates(local_db)
        assert len(all_cands) == 1
        assert all_cands[0]["status"] == "discarded", (
            "The candidate must be a discarded informational marker (local is newer)"
        )

    def test_foreign_credential_redacted_in_decision_reason(self, tmp_path):
        """A credential in the FOREIGN fact must be redacted in the stored decision_reason.

        The team-newer branch interpolates focus_content into decision_reason for operator
        display. _insert_federation_candidate redacts content/raw AND decision_reason, so a
        credential-shaped token in the foreign fact is scrubbed everywhere it lands.
        """
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        local_id = store(local_db, "use the old deploy key", type="fact", tags="deploy")

        cfg = Config()
        cfg.db_path = local_db

        secret = "AKIAIOSFODNN7EXAMPLE"  # AWS access key id shape → redacted
        counts = federation_conflict_ingest(
            cfg,
            focus_content=f"new deploy uses credential {secret} for the pipeline",
            focus_store="team",
            focus_event_at="2030-01-01T00:00:00",  # team newer → team-newer branch
            comparison_ids=[local_id],
            check_hash="",
            response=json.dumps([{
                "id": local_id,
                "verdict": "SUPERSEDE",
                "content": f"new deploy uses credential {secret}",
                "reason": "rotated key",
            }]),
        )
        assert counts["errors"] == 0

        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 1
        cand = pending[0]
        # The raw secret must NOT appear in ANY persisted text column
        assert secret not in (cand["decision_reason"] or ""), (
            "Credential must be redacted in decision_reason (foreign content leak)"
        )
        assert secret not in (cand["content"] or "")
        assert "<redacted:" in (cand["decision_reason"] or ""), (
            "decision_reason should carry the redaction marker where the secret was"
        )

    def test_local_newer_supersede_stages_discarded_marker(self, tmp_path):
        """SUPERSEDE verdict + local newer → discarded informational marker; no team write."""
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        local_id = store(local_db, "we use sqlite now", type="fact", tags="datastore")

        cfg = Config()
        cfg.db_path = local_db

        # Local is newer → team is stale; should produce a discarded marker.
        # Use a very old team event_at — unambiguously older than the local row's created_at.
        counts = federation_conflict_ingest(
            cfg,
            focus_content="use postgres for the main database",
            focus_store="team-archivist",
            focus_event_at="2020-01-01T00:00:00",  # unambiguously older than local created_at
            comparison_ids=[local_id],
            check_hash="",
            response=json.dumps([{
                "id": local_id,
                "verdict": "SUPERSEDE",
                "content": "use postgres for the main database",
                "reason": "team believes postgres is canonical",
            }]),
        )

        assert counts["errors"] == 0
        assert counts["supersede"] == 1

        # No pending candidates — the local is newer, so no action on local
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 0, (
            "Local-newer case must NOT stage a pending retire on the local row"
        )

        # A discarded marker must exist (informational — do not re-nag)
        all_cands = _all_federation_candidates(local_db)
        assert len(all_cands) == 1
        assert all_cands[0]["status"] == "discarded"
        assert all_cands[0]["origin"] == "federation-conflict"

        # Local row must still be live
        assert _knowledge_status(local_db, local_id) != "superseded"

    def test_noop_distinct_stage_discarded_markers(self, tmp_path):
        """NOOP and DISTINCT verdicts produce discarded markers; nothing is pending."""
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        local_id = store(local_db, "use git flow branching", type="procedure", tags="git")

        cfg = Config()
        cfg.db_path = local_db

        for verdict in ("NOOP", "DISTINCT"):
            # Reset db
            conn = connect(local_db)
            try:
                conn.execute("DELETE FROM knowledge_candidates WHERE origin='federation-conflict'")
                conn.commit()
            finally:
                conn.close()

            counts = federation_conflict_ingest(
                cfg,
                focus_content="team uses trunk-based development",
                focus_store="team",
                focus_event_at=None,
                comparison_ids=[local_id],
                check_hash="",
                response=json.dumps([{
                    "id": local_id,
                    "verdict": verdict,
                    "reason": "test",
                }]),
            )

            assert counts["errors"] == 0
            all_cands = _all_federation_candidates(local_db)
            assert all_cands, f"Expected a candidate for {verdict}"
            assert all(c["status"] == "discarded" for c in all_cands), (
                f"{verdict} must produce only discarded markers"
            )
            assert _pending_federation_candidates(local_db) == [], (
                f"{verdict} must NOT produce any pending candidates"
            )

    def test_ingest_idempotent_on_same_check_hash(self, tmp_path):
        """Ingesting the same conflict twice is a no-op (check_hash guard)."""
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        local_id = store(local_db, "use postgres", type="fact", tags="datastore")
        cfg = Config()
        cfg.db_path = local_db

        kwargs = dict(
            focus_content="switched to sqlite",
            focus_store="team",
            focus_event_at="2030-01-01T00:00:00",  # unambiguously newer than local created_at
            comparison_ids=[local_id],
            check_hash="",
            response=json.dumps([{"id": local_id, "verdict": "SUPERSEDE", "reason": "x"}]),
        )

        c1 = federation_conflict_ingest(cfg, **kwargs)
        c2 = federation_conflict_ingest(cfg, **kwargs)

        assert c1["errors"] == 0
        assert c2["skipped"] == 1, "Second ingest with same check_hash must be skipped"

        # Only one candidate in the db
        all_cands = _all_federation_candidates(local_db)
        assert len(all_cands) == 1


# ---------------------------------------------------------------------------
# 5. read_only_remote_under_WAL
# ---------------------------------------------------------------------------


class TestReadOnlyRemoteUnderWAL:
    """WAL-mode team db opens read-only; no schema write to foreign dir."""

    def test_wal_mode_open_succeeds_and_reads(self, tmp_path):
        """A WAL-mode team db can be opened read-only and queried."""
        from techne.core.federation import _open_remote_readonly

        team_dir = tmp_path / "team"
        team_dir.mkdir()
        team_db = team_dir / "techne.db"

        # Create the db in WAL mode with a simple table
        conn = sqlite3.connect(str(team_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO probe VALUES (1, 'hello-wal')")
        conn.commit()
        conn.close()

        # Open read-only via our sovereignty guard
        ro_conn = _open_remote_readonly(team_db)
        try:
            row = ro_conn.execute("SELECT val FROM probe WHERE id=1").fetchone()
            assert row is not None
            assert row["val"] == "hello-wal"
        finally:
            ro_conn.close()

    def test_write_on_readonly_connection_raises(self, tmp_path):
        """A write attempt on a ?mode=ro connection raises OperationalError."""
        from techne.core.federation import _open_remote_readonly

        team_db = tmp_path / "team.db"
        conn = sqlite3.connect(str(team_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.commit()
        conn.close()

        ro_conn = _open_remote_readonly(team_db)
        try:
            with pytest.raises(sqlite3.OperationalError, match="[Rr]ead[Oo]nly|readonly|read-only"):
                ro_conn.execute("INSERT INTO t VALUES ('forbidden')")
                ro_conn.commit()
        finally:
            ro_conn.close()

    def test_no_schema_write_in_team_dir(self, tmp_path):
        """Read-only open of a WAL-mode store: the team's MEMORY (db content + schema)
        is never modified, and no new .db file is created.

        database.connect() calls _ensure_initialized() which would write schema into the
        foreign dir. _open_remote_readonly() must not do this.

        SIDECAR FINDING (documented in _open_remote_readonly): reading a WAL-mode store
        under ?mode=ro DOES materialize transient `<db>-wal` / `<db>-shm` coordination
        files (SQLite's required read mechanism for WAL), and they persist after close
        because a read-only connection cannot checkpoint. This is NOT a memory write — the
        main techne.db is byte-identical before/after. This test asserts exactly that
        boundary: main db unchanged + no new .db file; sidecars, if any, are ONLY the
        transient -wal/-shm (never a -journal, never a second .db).
        """
        import hashlib

        from techne.core.federation import _open_remote_readonly

        team_dir = tmp_path / "team"
        team_dir.mkdir()
        team_db = team_dir / "techne.db"

        # Init a full techne db so the FTS tables are already present
        init_conn = init_db(team_db)
        # Force WAL mode — all real techne dbs run in WAL mode
        init_conn.execute("PRAGMA journal_mode=WAL")
        init_conn.commit()
        init_conn.close()
        store(team_db, "a fact to read back", type="fact", tags="probe")

        def _hash(p):
            return hashlib.sha256(p.read_bytes()).hexdigest()

        # Snapshot .db files + the main db content hash before
        db_files_before = {p.name for p in team_dir.iterdir() if p.suffix == ".db"}
        assert "techne.db" in db_files_before
        main_hash_before = _hash(team_db)

        ro_conn = _open_remote_readonly(team_db)
        try:
            # Exercise the connection with a real content read (touches the WAL)
            ro_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            ro_conn.execute("SELECT count(*) FROM knowledge").fetchall()
        finally:
            ro_conn.close()

        # 1. The team's MEMORY is untouched — main db byte-identical.
        assert _hash(team_db) == main_hash_before, (
            "read-only open must NOT modify the team's db content/schema"
        )

        # 2. No NEW .db file appeared (no schema write into a second store).
        db_files_after = {p.name for p in team_dir.iterdir() if p.suffix == ".db"}
        assert not (db_files_after - db_files_before), (
            f"read-only open must not create new .db files. "
            f"New: {db_files_after - db_files_before}"
        )

        # 3. Any sidecars that DID appear are ONLY the transient WAL coordination files
        #    — never a rollback -journal (which would signal a non-WAL write attempt) and
        #    never anything outside the -wal/-shm set.
        all_after = {p.name for p in team_dir.iterdir()}
        new_files = all_after - {p.name for p in team_dir.iterdir() if p.suffix == ".db"}
        allowed_sidecars = {"techne.db-wal", "techne.db-shm"}
        assert new_files <= allowed_sidecars, (
            f"Only transient -wal/-shm sidecars are acceptable; got unexpected: "
            f"{new_files - allowed_sidecars}"
        )
        assert "techne.db-journal" not in all_after, (
            "a rollback -journal would indicate a non-WAL write attempt on the foreign store"
        )

    def test_team_fts_search_works_over_wal_db(self, tmp_path):
        """The full search path (fan_out_search) succeeds against a WAL-mode team db."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        team_conn = init_db(team_db)
        team_conn.execute("PRAGMA journal_mode=WAL")
        team_conn.commit()
        team_conn.close()

        store(team_db, "WAL mode test: parallel reads work", type="fact", tags="sqlite,wal")

        connections = {"team-wal": FederationConnectionSpec(role="team", db_path=str(team_db))}
        local_results, remote_results = fan_out_search(
            local_db, connections, "WAL mode parallel", limit=10, half_life_days=30.0
        )

        assert any("WAL" in (r.get("content") or "") for r in remote_results), (
            "Should return the WAL-mode team row via read-only FTS search"
        )


# ---------------------------------------------------------------------------
# 6. foreign_id_never_fetched_locally
# ---------------------------------------------------------------------------


class TestForeignIdNeverFetchedLocally:
    """federation_conflict_ingest handles the namespaced focus id without a local _fetch_row."""

    def test_namespaced_focus_id_not_fetched_from_local_db(self, tmp_path):
        """The foreign namespaced id ("team-archivist:42") must never be looked up in
        the local db via _fetch_row. This verifies the id-namespace gotcha is handled.

        If _fetch_row were called with the string "team-archivist:42" as id, it would
        try SELECT WHERE id='team-archivist:42' which returns None (no such local row).
        This test verifies the function succeeds (no error) and produces a candidate
        WITHOUT any local _fetch_row of the namespaced id.
        """
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        # Store a local row that serves as the comparison target
        local_id = store(local_db, "use postgres for all services", type="fact", tags="datastore")

        cfg = Config()
        cfg.db_path = local_db

        # Call with a namespaced focus_id content and comparison to the local row.
        # The function must NOT try to _fetch_row("team-archivist:42") from local db.
        # Use a future event_at so the team is unambiguously newer → pending retire path.
        counts = federation_conflict_ingest(
            cfg,
            focus_content="switched to sqlite across all services",
            focus_store="team-archivist",
            focus_event_at="2030-01-01T00:00:00",  # unambiguously newer than local created_at
            comparison_ids=[local_id],
            check_hash="",  # will be recomputed from content+ids
            response=json.dumps([{
                "id": local_id,  # LOCAL id, not the namespaced focus id
                "verdict": "SUPERSEDE",
                "content": "switched to sqlite across all services",
                "reason": "simpler stack",
            }]),
        )

        # Must succeed without KeyError or TypeError from id coercion
        assert counts["errors"] == 0, f"errors={counts['errors']}; expected 0"
        assert counts["supersede"] == 1

        # Candidate must have the LOCAL id in target_ids (not the namespaced string)
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 1
        target_ids = json.loads(pending[0]["target_ids"])
        assert all(isinstance(tid, int) for tid in target_ids), (
            "target_ids must contain only LOCAL integer ids, never namespaced strings"
        )
        assert local_id in target_ids

    def test_ingest_with_no_matching_comparison_row_skips_gracefully(self, tmp_path):
        """If a comparison_id doesn't exist locally, ingest skips it without error.

        This covers the case where a local row was superseded between detect and ingest.
        """
        from techne.core.federation import federation_conflict_ingest

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)

        cfg = Config()
        cfg.db_path = local_db

        # comparison_ids=[9999] — no such local row
        counts = federation_conflict_ingest(
            cfg,
            focus_content="switched to sqlite",
            focus_store="team",
            focus_event_at=None,
            comparison_ids=[9999],
            check_hash="",
            response=json.dumps([{
                "id": 9999,
                "verdict": "SUPERSEDE",
                "reason": "ghost row test",
            }]),
        )

        assert counts["errors"] == 0, "Missing comparison row must not cause an error"
        # No pending candidates since the comparison row wasn't found
        assert _pending_federation_candidates(local_db) == []


# ---------------------------------------------------------------------------
# 7. Broken remote degrades to local-only (availability — Prudence Important 1)
# ---------------------------------------------------------------------------


def _recall(cfg, topic):
    """Call techne_recall via the MCP handler and return the parsed result dict."""
    from techne.mcp_server import handle_request

    resp = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "techne_recall", "arguments": {"topic": topic}}},
        cfg,
    )
    # tools/call wraps the handler result in content[0].text as JSON
    assert "error" not in resp, f"recall must not error: {resp.get('error')}"
    return json.loads(resp["result"]["content"][0]["text"])


class TestBrokenRemoteDegradesToLocal:
    """One bad remote must NOT break the operator's entire recall (Important 1).

    ?mode=ro opens LAZILY — it succeeds on any existing file and only fails at first
    execute(). So a wrong-schema / corrupt / unreachable remote raises inside the QUERY
    block, which must be caught and degraded to local-only. These tests bite against the
    pre-fix code (which had try/finally with no except on the query block).
    """

    def test_wrong_schema_remote_degrades_to_local(self, tmp_path):
        """A team db that is a valid sqlite file but has no knowledge_fts (older techne)
        raises 'no such table' at query time — recall must still return local results."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "local fact: use postgres", type="fact", tags="datastore")

        # A plain sqlite file with NO knowledge_fts table — ?mode=ro opens fine, the
        # FTS query raises 'no such table: knowledge_fts'.
        bad_db = tmp_path / "wrong_schema.db"
        c = sqlite3.connect(str(bad_db))
        c.execute("CREATE TABLE unrelated (x TEXT)")
        c.commit()
        c.close()

        connections = {"bad-team": FederationConnectionSpec(role="team", db_path=str(bad_db))}

        # Must NOT raise — degrades to local-only
        local_results, remote_results = fan_out_search(
            local_db, connections, "postgres datastore", limit=10, half_life_days=30.0
        )
        assert any("postgres" in (r.get("content") or "") for r in local_results), (
            "Local results must survive a broken remote"
        )
        assert remote_results == [], "Broken remote must contribute no rows"

    def test_nonexistent_remote_degrades_to_local(self, tmp_path):
        """A nonexistent remote path (open fails) must degrade to local-only, not raise."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "local fact: use redis", type="fact", tags="cache")

        connections = {
            "ghost": FederationConnectionSpec(
                role="team", db_path=str(tmp_path / "does_not_exist.db")
            )
        }
        local_results, remote_results = fan_out_search(
            local_db, connections, "redis cache", limit=10, half_life_days=30.0
        )
        assert any("redis" in (r.get("content") or "") for r in local_results)
        assert remote_results == []

    def test_corrupt_remote_degrades_to_local(self, tmp_path):
        """A corrupt db file (garbage bytes) raises at query time — degrade to local-only."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "local fact: use kafka", type="fact", tags="queue")

        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database at all \x00\x01\x02" * 20)

        connections = {"corrupt": FederationConnectionSpec(role="team", db_path=str(corrupt))}
        local_results, remote_results = fan_out_search(
            local_db, connections, "kafka queue", limit=10, half_life_days=30.0
        )
        assert any("kafka" in (r.get("content") or "") for r in local_results)
        assert remote_results == []

    def test_recall_full_path_degrades_on_broken_remote(self, tmp_path):
        """The full techne_recall MCP path returns local results even when a remote is
        broken — local recall must never be lost to a federation failure."""
        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "local decision: adopt FastAPI", type="decision", tags="framework")

        bad_db = tmp_path / "wrong.db"
        c = sqlite3.connect(str(bad_db))
        c.execute("CREATE TABLE nope (x TEXT)")
        c.commit()
        c.close()

        cfg = Config()
        cfg.db_path = local_db
        cfg.connections = {
            "bad": FederationConnectionSpec(role="team", db_path=str(bad_db)),
        }

        data = _recall(cfg, "FastAPI framework decision")
        # Local results must be present despite the broken remote
        assert any(
            "FastAPI" in (r.get("content") or "") for r in data["search_results"]
        ), "Local results must survive a broken remote in the full recall path"


# ---------------------------------------------------------------------------
# 8. Annotation must not cross the store boundary (Prudence Important 2)
# ---------------------------------------------------------------------------


class TestAnnotationStoreBoundary:
    """Remote and local knowledge ids collide (both small sequential ints). Annotating a
    remote row against the LOCAL db by its (colliding) id would stamp a team fact with
    YOUR file provenance. annotate_freshness/uncertain must run on LOCAL rows only.
    """

    def test_colliding_remote_id_not_annotated_with_local_provenance(self, tmp_path):
        """Local row id=1 is anchored; team row id=1 is not. After recall, the team row
        must NOT carry the local row's anchor_path/freshness/uncertain (id-collision)."""
        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        # Local id=1, ANCHORED (so annotate_freshness would stamp anchor_path)
        local_id = store(
            local_db,
            "local anchored fact about the database layer",
            type="fact",
            tags="datastore",
            anchor="techne/core/database.py",
        )
        # Team id=1 (same int), NOT anchored, different content + tags (no contradiction)
        team_id = store(
            team_db,
            "team fact about the caching layer using redis",
            type="fact",
            tags="cache",
        )
        assert local_id == team_id == 1, "Test needs the id collision to exist"

        cfg = Config()
        cfg.db_path = local_db
        cfg.connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}

        data = _recall(cfg, "database caching layer fact")

        results = data["search_results"]
        team_rows = [r for r in results if r.get("_store") == "team"]
        assert team_rows, "Team row must be present in merged results"

        for tr in team_rows:
            assert "anchor_path" not in tr, (
                "Team row must NOT inherit the local row's anchor_path (id-collision bug)"
            )
            assert "freshness" not in tr, (
                "Team row must NOT inherit the local row's freshness annotation"
            )
            assert "uncertain" not in tr, (
                "Team row must NOT inherit a local uncertain marker"
            )

        # And the LOCAL anchored row SHOULD still be annotated — proves annotation runs,
        # just scoped to local rows.
        local_rows = [r for r in results if r.get("_store") == "local"]
        anchored_local = [r for r in local_rows if r.get("anchor_path")]
        assert anchored_local, (
            "The local anchored row must still receive its anchor_path annotation"
        )


# ---------------------------------------------------------------------------
# Config round-trip test
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """connections block: parse → save → reload round-trip without mutation."""

    def test_connections_absent_defaults_to_empty_dict(self, tmp_path):
        """An existing config with no 'connections' key parses cleanly (no migration)."""
        from techne.config import Config

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("db_path: /tmp/test.db\ndata_dir: /tmp\n")

        cfg = Config.load(cfg_path)
        assert cfg.connections == {}, "Absent connections key must default to empty dict"

    def test_connections_round_trip(self, tmp_path):
        """connections: parse → save → reload preserves role, db_path, label."""
        from techne.config import Config, FederationConnectionSpec

        cfg = Config()
        cfg.db_path = tmp_path / "techne.db"
        cfg.data_dir = tmp_path
        cfg.connections = {
            "team-archivist": FederationConnectionSpec(
                role="team",
                db_path="/shared/archivist/techne.db",
                label="Team Archivist",
            )
        }

        cfg_path = tmp_path / "config.yaml"
        cfg.save(cfg_path)

        reloaded = Config.load(cfg_path)
        assert "team-archivist" in reloaded.connections

        spec = reloaded.connections["team-archivist"]
        assert spec.role == "team"
        assert spec.db_path == "/shared/archivist/techne.db"
        assert spec.label == "Team Archivist"

    def test_invalid_role_raises(self, tmp_path):
        """An invalid federation role raises ValueError on parse."""
        import yaml

        from techne.config import Config

        bad_config = {
            "db_path": str(tmp_path / "techne.db"),
            "data_dir": str(tmp_path),
            "connections": {
                "bad": {"role": "invalid-role", "db_path": "/tmp/x.db"}
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(bad_config))

        with pytest.raises(ValueError, match="Invalid connections"):
            Config.load(cfg_path)

    def test_connections_empty_dict_not_serialized(self, tmp_path):
        """When connections is empty, the key is not written to config.yaml."""
        import yaml

        from techne.config import Config

        cfg = Config()
        cfg.db_path = tmp_path / "techne.db"
        cfg.data_dir = tmp_path

        cfg_path = tmp_path / "config.yaml"
        cfg.save(cfg_path)

        raw = yaml.safe_load(cfg_path.read_text())
        assert "connections" not in raw, (
            "Empty connections must not be serialized (mirrors stores block behavior)"
        )


# ---------------------------------------------------------------------------
# 9. #254 — fan_out_search does NOT double-compute local when caller passes
#           precomputed local_results
# ---------------------------------------------------------------------------


class TestNoDoubleLocalSearch:
    """techne#254: fan_out_search must NOT re-run the local search when
    local_results is provided by the caller."""

    def test_fan_out_skips_local_search_when_precomputed(self, tmp_path, monkeypatch):
        """When local_results is supplied, the local search() call inside
        fan_out_search must NOT fire.

        This bites against the pre-fix code where fan_out_search always ran
        search() internally regardless of what the caller passed, causing the
        local db to be searched twice per federated recall.

        Spy approach: patch search in the search module itself (where federation.py
        imports it lazily via ``from techne.core.search import search``).
        """
        from techne.core import search as search_mod
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "redis is our caching layer", type="fact", tags="cache")

        # Precompute local results as _techne_recall would — before patching
        precomputed = search_mod.search(
            local_db, "redis cache", table="all", limit=10,
            half_life_days=30.0, include_archived=False,
        )
        for r in precomputed:
            r["_store"] = "local"
            r["_role"] = "local"

        # Now patch search in the search module with a sentinel that raises if called.
        # fan_out_search uses lazy ``from techne.core.search import search`` inside
        # the ``if local_results is None`` branch — patching the module attribute
        # intercepts it because Python resolves the name at call time within
        # that branch.
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "fan_out_search called search() internally despite local_results being provided — "
                "this is the techne#254 double-compute bug."
            )

        monkeypatch.setattr(search_mod, "search", _must_not_be_called)

        # fan_out_search with precomputed local_results — must NOT enter the
        # ``if local_results is None`` branch and therefore must never call search()
        returned_local, _ = fan_out_search(
            local_db,
            connections={},
            query="redis cache",
            limit=10,
            half_life_days=30.0,
            local_results=precomputed,
        )

        # The returned local results must be our precomputed set (by identity)
        assert returned_local is precomputed, (
            "fan_out_search must return the passed-in local_results unchanged"
        )

    def test_fan_out_without_precomputed_still_runs_local_search(self, tmp_path, monkeypatch):
        """Backward-compat: when local_results is NOT supplied (None),
        fan_out_search still computes local results internally."""
        from techne.core import search as search_mod
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "kafka is our message queue", type="fact", tags="queue")

        call_count = []
        original = search_mod.search

        def counting(db_path, query, **kwargs):
            call_count.append(1)
            return original(db_path, query, **kwargs)

        monkeypatch.setattr(search_mod, "search", counting)

        local_results, _ = fan_out_search(
            local_db, connections={}, query="kafka queue", limit=10, half_life_days=30.0
        )

        assert len(call_count) >= 1, (
            "Without precomputed local_results, fan_out_search must call search() internally"
        )
        assert any("kafka" in (r.get("content") or "") for r in local_results)

    def test_merged_results_identical_with_precomputed_vs_without(self, tmp_path):
        """End-to-end: the merged result set must be the same whether fan_out_search
        computes local internally or uses a precomputed set from the caller.

        This is the invariant that ensures the #254 fix doesn't change query output.
        """
        from techne.core.federation import (
            _cross_store_dedup,
            fan_out_search,
            merge_and_rank,
        )
        from techne.core.search import search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        store(local_db, "local: we use postgres for persistence", type="fact", tags="db")
        store(team_db, "team: we switched to sqlite for simplicity", type="fact", tags="db")

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}

        # Path A: fan_out_search computes local internally (old behaviour)
        local_a, remote_a = fan_out_search(
            local_db, connections, "database postgres sqlite", limit=10, half_life_days=30.0
        )
        deduped_a = _cross_store_dedup(local_a, remote_a)
        merged_a = merge_and_rank(
            local_a, deduped_a, limit=10, recency_weight=0.3, bm25_weight=0.7, half_life_days=30.0
        )

        # Path B: caller precomputes local and passes it in (#254 fix path)
        precomputed = search(
            local_db, "database postgres sqlite", table="all", limit=10,
            half_life_days=30.0, include_archived=False,
        )
        for r in precomputed:
            r["_store"] = "local"
            r["_role"] = "local"
        local_b, remote_b = fan_out_search(
            local_db, connections, "database postgres sqlite", limit=10, half_life_days=30.0,
            local_results=precomputed,
        )
        deduped_b = _cross_store_dedup(local_b, remote_b)
        merged_b = merge_and_rank(
            local_b, deduped_b, limit=10, recency_weight=0.3, bm25_weight=0.7, half_life_days=30.0
        )

        # The content sets must be identical (order may differ due to independent sorts)
        contents_a = sorted(r.get("content", "") for r in merged_a)
        contents_b = sorted(r.get("content", "") for r in merged_b)
        assert contents_a == contents_b, (
            "Merged results must be identical whether local search is internal or precomputed. "
            f"Path A: {contents_a}  Path B: {contents_b}"
        )


# ---------------------------------------------------------------------------
# 10. #253 — schema-compat check and stable public entrypoint
# ---------------------------------------------------------------------------


class TestSchemaCompatCheck:
    """techne#253: remotes with drifted/incompatible knowledge schemas must be
    skipped with a warning; local recall must still return."""

    def test_drifted_remote_missing_column_is_skipped(self, tmp_path, caplog):
        """A remote whose knowledge table is missing a required column (e.g.
        event_at or confidence_numeric) must be skipped with a log.warning —
        NOT raise, NOT silently mis-rank.

        This bites against the pre-253 code which only caught missing TABLES
        (no knowledge_fts) but let drifted-column remotes through to the query
        where they might fail silently or raise cryptically.
        """
        import logging

        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        _init_team_db(local_db)
        store(local_db, "local recall survives drifted remote", type="fact", tags="test")

        # Build a remote db that has knowledge_fts but whose knowledge table
        # is MISSING the event_at column — this is the "drifted schema" case.
        drifted_db = tmp_path / "drifted.db"
        conn = sqlite3.connect(str(drifted_db))
        conn.execute("""
            CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                confidence TEXT DEFAULT 'medium',
                confidence_numeric REAL DEFAULT 0.6,
                last_confirmed_at TIMESTAMP,
                status TEXT DEFAULT 'current',
                source TEXT,
                updated_at TIMESTAMP,
                superseded_by INTEGER
                -- NOTE: event_at column is intentionally ABSENT (drifted schema)
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                content, tags,
                content=knowledge, content_rowid=id,
                tokenize='porter unicode61'
            )
        """)
        conn.execute(
            "INSERT INTO knowledge (type, content, tags) VALUES ('fact', 'drifted remote fact', 'test')"
        )
        conn.execute(
            "INSERT INTO knowledge_fts(rowid, content, tags) SELECT id, content, COALESCE(tags,'') FROM knowledge"
        )
        conn.commit()
        conn.close()

        connections = {
            "drifted": FederationConnectionSpec(role="team", db_path=str(drifted_db))
        }

        with caplog.at_level(logging.WARNING, logger="techne.core.federation"):
            local_results, remote_results = fan_out_search(
                local_db, connections, "drifted remote recall", limit=10, half_life_days=30.0
            )

        # Local recall must survive
        assert any("local" in (r.get("content") or "").lower() for r in local_results), (
            "Local results must survive when a drifted-schema remote is skipped"
        )
        # Drifted remote must contribute NO rows
        assert remote_results == [], (
            "Drifted-schema remote must contribute no rows (skipped at schema-compat check)"
        )
        # A warning must have been emitted naming the remote
        assert any(
            "drifted" in rec.message and "incompatible" in rec.message.lower()
            for rec in caplog.records
            if rec.levelno >= logging.WARNING
        ), (
            "A warning naming 'drifted' and 'incompatible' must be logged when the remote is skipped. "
            f"Logged warnings: {[r.message for r in caplog.records if r.levelno >= logging.WARNING]}"
        )

    def test_schema_compat_check_passes_for_full_schema(self, tmp_path):
        """A fully-initialised techne db passes the schema-compat check."""
        from techne.core.federation import _check_remote_schema_compat, _open_remote_readonly

        db = tmp_path / "full.db"
        _init_team_db(db)
        store(db, "full schema fact", type="fact", tags="test")

        conn = _open_remote_readonly(db)
        try:
            compat, reason = _check_remote_schema_compat(conn)
        finally:
            conn.close()

        assert compat is True, f"Full techne db must pass compat check. reason={reason!r}"
        assert reason == ""

    def test_schema_compat_check_fails_for_missing_fts(self, tmp_path):
        """A db without knowledge_fts fails the compat check with an informative reason."""
        from techne.core.federation import _check_remote_schema_compat, _open_remote_readonly

        no_fts = tmp_path / "no_fts.db"
        c = sqlite3.connect(str(no_fts))
        c.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT)")
        c.commit()
        c.close()

        conn = _open_remote_readonly(no_fts)
        try:
            compat, reason = _check_remote_schema_compat(conn)
        finally:
            conn.close()

        assert compat is False
        assert "knowledge_fts" in reason

    def test_schema_compat_check_fails_for_missing_columns(self, tmp_path):
        """A db with knowledge_fts but missing required columns fails the check."""
        from techne.core.federation import _check_remote_schema_compat, _open_remote_readonly

        partial = tmp_path / "partial.db"
        c = sqlite3.connect(str(partial))
        # knowledge_fts exists but knowledge table is missing event_at and confidence_numeric
        c.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, type TEXT)")
        c.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, content=knowledge, content_rowid=id)"
        )
        c.commit()
        c.close()

        conn = _open_remote_readonly(partial)
        try:
            compat, reason = _check_remote_schema_compat(conn)
        finally:
            conn.close()

        assert compat is False
        assert "missing" in reason.lower()
        # The reason must name at least one of the missing columns
        assert any(col in reason for col in ("event_at", "confidence_numeric", "status")), (
            f"Reason must name missing columns; got: {reason!r}"
        )

    def test_drifted_schema_degrade_preserves_good_remotes(self, tmp_path):
        """When one remote has a drifted schema and another is good, only the
        good remote contributes rows — local + good remote are unchanged."""
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        good_db = tmp_path / "good.db"
        drifted_db = tmp_path / "drifted.db"
        _init_team_db(local_db)
        _init_team_db(good_db)

        store(local_db, "local postgres fact", type="fact", tags="db")
        store(good_db, "good remote sqlite fact", type="fact", tags="db")

        # Drifted remote: has knowledge_fts but missing event_at
        c = sqlite3.connect(str(drifted_db))
        c.execute("""CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
            confidence TEXT, confidence_numeric REAL, last_confirmed_at TIMESTAMP,
            status TEXT, source TEXT, updated_at TIMESTAMP, superseded_by INTEGER
        )""")
        c.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, tags, content=knowledge, content_rowid=id)")
        c.execute("INSERT INTO knowledge(type,content,tags) VALUES('fact','drifted content','db')")
        c.execute("INSERT INTO knowledge_fts(rowid,content,tags) SELECT id,content,COALESCE(tags,'') FROM knowledge")
        c.commit()
        c.close()

        connections = {
            "good": FederationConnectionSpec(role="team", db_path=str(good_db)),
            "drifted": FederationConnectionSpec(role="team", db_path=str(drifted_db)),
        }

        local_results, remote_results = fan_out_search(
            local_db, connections, "postgres sqlite fact", limit=10, half_life_days=30.0
        )

        # Good remote must contribute
        assert any("good remote" in (r.get("content") or "") for r in remote_results), (
            "Good remote must still contribute rows when another remote has a drifted schema"
        )
        # Drifted remote must NOT contribute
        assert not any("drifted content" in (r.get("content") or "") for r in remote_results), (
            "Drifted-schema remote must not contribute any rows"
        )
        # Local must survive
        assert any("postgres" in (r.get("content") or "") for r in local_results)


class TestPublicKnowledgeConnEntrypoint:
    """techne#253: search_knowledge_conn is a stable public entrypoint that
    returns event_at so federation no longer hand-backfills it."""

    def test_search_knowledge_conn_returns_event_at(self, tmp_path):
        """search_knowledge_conn must return rows WITH event_at populated
        (not None/absent) when the knowledge table has event_at set."""
        import sqlite3 as _sqlite3

        from techne.core.search import search_knowledge_conn

        db = tmp_path / "test.db"
        _init_team_db(db)

        kid = store(db, "event_at test: production switched to postgres", type="fact", tags="db")

        # Set a known event_at on the row
        from techne.core.database import connect as _connect
        c = _connect(db)
        try:
            c.execute("UPDATE knowledge SET event_at='2026-06-01 12:00:00' WHERE id=?", (kid,))
            c.commit()
        finally:
            c.close()

        conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        try:
            results = search_knowledge_conn(
                conn, "postgres production", limit=10, half_life_days=30.0
            )
        finally:
            conn.close()

        assert results, "search_knowledge_conn must return at least one result"
        row = results[0]
        assert "event_at" in row, (
            "search_knowledge_conn must include event_at in returned rows "
            "(this is the stable-seam guarantee from techne#253)"
        )
        assert row["event_at"] == "2026-06-01 12:00:00", (
            f"event_at must match what was stored; got {row['event_at']!r}"
        )

    def test_search_knowledge_conn_returns_empty_on_no_match(self, tmp_path):
        """search_knowledge_conn returns [] when nothing matches the query."""
        import sqlite3 as _sqlite3

        from techne.core.search import search_knowledge_conn

        db = tmp_path / "empty.db"
        _init_team_db(db)
        store(db, "something completely different about bananas", type="fact", tags="food")

        conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        try:
            results = search_knowledge_conn(conn, "quantum entanglement physics", limit=5,
                                            half_life_days=30.0)
        finally:
            conn.close()

        assert results == []

    def test_federation_remote_search_uses_event_at_from_entrypoint(self, tmp_path):
        """End-to-end: remote results returned by fan_out_search carry event_at
        (no longer hand-backfilled in _search_remote_knowledge — owned by the
        public entrypoint search_knowledge_conn instead).

        This verifies the stable-seam guarantee: if someone refactors
        _fts_match_rows to remove event_at, the public entrypoint test above
        catches it; this test verifies the federation path sees the same field.
        """
        from techne.core.federation import fan_out_search

        local_db = tmp_path / "local.db"
        team_db = tmp_path / "team.db"
        _init_team_db(local_db)
        _init_team_db(team_db)

        kid = store(team_db, "team fact about deployments", type="fact", tags="deploy")
        from techne.core.database import connect as _connect
        c = _connect(team_db)
        try:
            c.execute(
                "UPDATE knowledge SET event_at='2026-05-15 09:00:00' WHERE id=?", (kid,)
            )
            c.commit()
        finally:
            c.close()

        connections = {"team": FederationConnectionSpec(role="team", db_path=str(team_db))}
        _, remote_results = fan_out_search(
            local_db, connections, "team deployments", limit=10, half_life_days=30.0
        )

        assert remote_results, "Remote must return at least one result"
        team_row = next(
            (r for r in remote_results if "deployments" in (r.get("content") or "")), None
        )
        assert team_row is not None
        assert "event_at" in team_row, (
            "Remote rows must carry event_at (populated by search_knowledge_conn, "
            "not hand-backfilled in _search_remote_knowledge)"
        )
        assert team_row["event_at"] == "2026-05-15 09:00:00"


# ---------------------------------------------------------------------------
# Relative connection db_path resolution (techne#332)
# ---------------------------------------------------------------------------


class TestRelativeConnDbPath:
    """A connection db_path may be relative — resolved against TECHNE_HOME so a
    committed config.yaml stays portable across machines (techne#332). Resolution
    happens at open time, not config load, so the stored spec keeps the portable
    string for save round-trips."""

    def test_relative_resolves_against_techne_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.core.federation import _resolve_conn_db_path

        assert _resolve_conn_db_path("team-mirror.db") == tmp_path / "team-mirror.db"

    def test_nested_relative_resolves_against_techne_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.core.federation import _resolve_conn_db_path

        assert _resolve_conn_db_path("mirrors/team.db") == tmp_path / "mirrors" / "team.db"

    def test_absolute_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.core.federation import _resolve_conn_db_path

        abs_path = tmp_path / "elsewhere" / "team.db"
        assert _resolve_conn_db_path(str(abs_path)) == abs_path

    def test_tilde_expands_to_home_not_techne_home(self, tmp_path, monkeypatch):
        # ~ is the user's home, independent of TECHNE_HOME — expanduser wins and
        # the result is already absolute, so no techne_home join happens.
        import pathlib

        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.core.federation import _resolve_conn_db_path

        assert _resolve_conn_db_path("~/team.db") == pathlib.Path.home() / "team.db"

    def test_fan_out_opens_relative_remote(self, tmp_path, monkeypatch):
        """End-to-end: a connection whose db_path is RELATIVE resolves against
        TECHNE_HOME and federates — the portable-config path the issue exists for."""
        from techne.core.federation import fan_out_search

        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        local_db = _init_team_db(tmp_path / "techne.db")
        # Team store lives at <TECHNE_HOME>/team-mirror.db, referenced RELATIVELY.
        team_db = _init_team_db(tmp_path / "team-mirror.db")
        store(team_db, "team deploys via blue-green on Fridays", type="fact", tags="deploy")

        connections = {
            "team": FederationConnectionSpec(role="team", db_path="team-mirror.db")
        }
        _, remote_results = fan_out_search(
            local_db, connections, "deploy blue-green Fridays", limit=10, half_life_days=30.0
        )
        assert remote_results, "relative-path remote must resolve + return results"

    def test_relative_db_path_survives_save_round_trip(self, tmp_path):
        """The portable relative string must survive load → save → reload and NOT
        be rewritten to absolute — the exact property the resolve-at-open (vs
        resolve-at-config-load) design protects. A future refactor moving
        resolution to load would pass every other test but silently break this
        (Prudence, PR #365; techne#332)."""
        from techne.config import Config

        src = tmp_path / "config.yaml"
        src.write_text(
            "connections:\n"
            "  team:\n"
            "    role: team\n"
            "    db_path: team-mirror.db\n"
        )
        cfg = Config.load(src)
        assert cfg.connections["team"].db_path == "team-mirror.db"  # stored as-is

        out = tmp_path / "out.yaml"
        cfg.save(out)
        assert "db_path: team-mirror.db" in out.read_text()  # relative, not absolutized
        reloaded = Config.load(out)
        assert reloaded.connections["team"].db_path == "team-mirror.db"


# ---------------------------------------------------------------------------
# techne#863 — recall reports the conflict backlog; it does not return it
# ---------------------------------------------------------------------------


def _conflicting_stores(tmp_path):
    """The sqlite-vs-postgres scenario above, wired as a live recall config."""
    local_db = tmp_path / "local.db"
    team_db = tmp_path / "team.db"
    _init_team_db(local_db)
    _init_team_db(team_db)
    local_id = store(
        local_db, "use postgres for the main application database",
        type="fact", tags="datastore,database",
    )
    store(
        team_db, "we switched to sqlite — postgres was overkill for this workload",
        type="fact", tags="datastore,database",
    )
    for db, sql, params in (
        (team_db, "UPDATE knowledge SET event_at = '2026-07-04 12:00:00'", ()),
        (local_db, "UPDATE knowledge SET event_at = '2026-01-01 00:00:00' WHERE id=?",
         (local_id,)),
    ):
        conn = connect(db)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
    cfg = Config()
    cfg.db_path = local_db
    cfg.connections = {
        "team-archivist": FederationConnectionSpec(role="team", db_path=str(team_db))
    }
    return cfg


def _recall_args(cfg, arguments):
    from techne.mcp_server import handle_request

    resp = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "techne_recall", "arguments": arguments}},
        cfg,
    )
    assert "error" not in resp, resp.get("error")
    return json.loads(resp["result"]["content"][0]["text"])


def test_recall_returns_a_conflict_count_not_the_adjudication_backlog(tmp_path):
    """techne#863: ten conflict items were 90% of a 103k-char recall and the call did
    not complete. By default recall says the backlog exists and how to fetch it; the
    items and their prompts stay out of the payload."""
    cfg = _conflicting_stores(tmp_path)

    data = _recall_args(cfg, {"topic": "database postgres sqlite"})

    assert "federation_conflicts" not in data, "the adjudication items leaked into recall"
    assert data["federation_conflicts_pending"] >= 1
    assert "include_federation_conflicts=true" in data["_federation_note"]
    assert data["search_results"], "the context asked for must still be returned"


def test_recall_opt_in_still_returns_the_items_ingest_needs(tmp_path):
    """The chore stays reachable: the opt-in returns the same items as before, with
    the fields techne_federation_conflict_ingest takes."""
    cfg = _conflicting_stores(tmp_path)

    data = _recall_args(
        cfg, {"topic": "database postgres sqlite", "include_federation_conflicts": True}
    )

    assert "federation_conflicts_pending" not in data
    items = data["federation_conflicts"]
    assert items and all(
        {"prompt", "check_hash", "focus_content", "comparison_ids"} <= set(i) for i in items
    )
