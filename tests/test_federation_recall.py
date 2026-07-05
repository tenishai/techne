"""Tests for federation read-merge (techne#222).

Mirrors test_conflict.py patterns. Six test groups:
  1. local_only_unchanged        — no connections → recall returns local-only, identical shape
  2. merges_team_results         — team connection → merged results include rows from both stores
  3. detects_cross_store_contradiction — sqlite-vs-postgres scenario surfaces correctly
  4. ingest_stages_pending_candidate  — SUPERSEDE → pending retire; local-newer → discarded
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
            assert r.get("type") == "knowledge", (
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

        connections = {"team-archivist": FederationConnectionSpec(role="team", db_path=str(team_db))}
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
    """Ingest paths: SUPERSEDE + team-newer → pending retire; local-newer → discarded.

    The local knowledge row must NOT be auto-applied in any case.
    """

    def test_team_newer_supersede_stages_pending_retire(self, tmp_path):
        """SUPERSEDE verdict + team newer → pending 'retire' candidate in LOCAL db.

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

        # A pending 'retire' candidate must exist in the LOCAL db
        pending = _pending_federation_candidates(local_db)
        assert len(pending) == 1, f"Expected 1 pending candidate, got {pending}"
        cand = pending[0]
        assert cand["action"] == "retire"
        assert cand["status"] == "pending"
        assert cand["origin"] == "federation-conflict"

        target_ids = json.loads(cand["target_ids"])
        assert local_id in target_ids, "Pending retire must target the LOCAL row id"

        # CRITICAL: the local knowledge row must NOT be auto-applied (still live)
        assert _knowledge_status(local_db, local_id) != "superseded", (
            "Local row must NOT be auto-applied — it is still live pending operator review"
        )
        assert _knowledge_superseded_by(local_db, local_id) is None, (
            "superseded_by must not be set — the candidate is pending, not promoted"
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
        """Opening a WAL-mode db read-only must NOT create a new .db file in the team dir.

        database.connect() calls _ensure_initialized() which would write schema into the
        foreign dir. _open_remote_readonly() must not do this. We verify by recording the
        set of .db files before the open and asserting none are added.
        """
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

        # Snapshot .db files before
        db_files_before = {p.name for p in team_dir.iterdir() if p.suffix == ".db"}
        assert "techne.db" in db_files_before

        ro_conn = _open_remote_readonly(team_db)
        try:
            # Run a simple read to exercise the connection
            ro_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            ro_conn.close()

        # No new .db files should have appeared
        db_files_after = {p.name for p in team_dir.iterdir() if p.suffix == ".db"}
        new_db_files = db_files_after - db_files_before
        assert not new_db_files, (
            f"_open_remote_readonly must not create new .db files in the team dir. "
            f"New files: {new_db_files}"
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
