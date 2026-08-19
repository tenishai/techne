"""Tests for event_at bi-temporal plumbing (issue #52).

Covers:
- add_observation writes event_at when supplied
- add_observation leaves event_at NULL when omitted
- search reranker prefers event_at over updated_at / timestamp
- No regression on existing search behaviour
"""

from datetime import datetime, timedelta, timezone

import pytest

from techne.core.database import connect, init_db
from techne.core.search import _rerank, search_fts
from techne.core.state import add_observation, start_session


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "event_at_test.db"
    conn = init_db(path)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# add_observation — event_at persistence
# ---------------------------------------------------------------------------


class TestAddObservationEventAt:
    def test_event_at_is_written_when_supplied(self, db_path):
        sid = start_session(db_path)
        # #515: supplied as ISO (the documented contract), STORED canonical.
        event_ts = "2025-01-01T12:00:00"
        stored_ts = "2025-01-01 12:00:00"
        add_observation(db_path, sid, "Bash", "ran ls", event_at=event_ts)

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT event_at FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["event_at"] == stored_ts

    def test_event_at_is_null_when_omitted(self, db_path):
        sid = start_session(db_path)
        add_observation(db_path, sid, "Bash", "ran pwd")

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT event_at FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["event_at"] is None

    def test_multiple_observations_mixed_event_at(self, db_path):
        """One with event_at, one without — both written correctly."""
        sid = start_session(db_path)
        add_observation(db_path, sid, "Read", "content A", event_at="2024-06-01T00:00:00")
        add_observation(db_path, sid, "Write", "content B")

        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT tool_name, event_at FROM observations WHERE session_id = ? ORDER BY id",
                (sid,),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        assert rows[0]["tool_name"] == "Read"
        # #515: normalised at the write boundary.
        assert rows[0]["event_at"] == "2024-06-01 00:00:00"
        assert rows[1]["tool_name"] == "Write"
        assert rows[1]["event_at"] is None


# ---------------------------------------------------------------------------
# Reranker prefers event_at
# ---------------------------------------------------------------------------


class TestRerankerEventAtPreference:
    def _make_obs_row(
        self,
        rank: float,
        timestamp: str | None = None,
        updated_at: str | None = None,
        event_at: str | None = None,
    ) -> dict:
        return {
            "source_table": "observation",
            "id": f"obs-{rank}",
            "rank": rank,
            "timestamp": timestamp,
            "updated_at": updated_at,
            "event_at": event_at,
        }

    def test_event_at_beats_updated_at(self):
        """Row with old updated_at but recent event_at should rank above
        a row with recent updated_at and no event_at."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        rows = [
            # Best BM25, stale updated_at, but event_at is recent
            self._make_obs_row(-9.0, timestamp=old_ts, updated_at=old_ts, event_at=new_ts),
            # Worse BM25, fresh updated_at, no event_at
            self._make_obs_row(-1.0, timestamp=new_ts, updated_at=new_ts, event_at=None),
        ]

        result = _rerank(rows, limit=2, recency_weight=1.0, bm25_weight=0.0)
        # recency_weight=1 so only recency matters; event_at row should win
        assert result[0]["id"] == "obs--9.0"

    def test_event_at_beats_timestamp(self):
        """event_at overrides timestamp for recency computation."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        rows = [
            # old timestamp, recent event_at
            self._make_obs_row(-5.0, timestamp=old_ts, event_at=new_ts),
            # recent timestamp, no event_at
            self._make_obs_row(-5.0, timestamp=new_ts, event_at=None),
        ]

        result = _rerank(rows, limit=2, recency_weight=1.0, bm25_weight=0.0)
        assert result[0]["id"] == "obs--5.0"  # first row (event_at recent) wins

    def test_null_event_at_falls_back_to_updated_at(self):
        """When event_at is None, updated_at is still used (no regression)."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        rows = [
            {"source_table": "t", "id": "old", "rank": -5.0, "event_at": None, "updated_at": old_ts, "timestamp": None},
            {"source_table": "t", "id": "new", "rank": -5.0, "event_at": None, "updated_at": new_ts, "timestamp": None},
        ]

        result = _rerank(rows, limit=2, recency_weight=1.0, bm25_weight=0.0)
        assert result[0]["id"] == "new"


# ---------------------------------------------------------------------------
# Integration: observations search returns event_at field
# ---------------------------------------------------------------------------


class TestSearchFtsEventAt:
    def test_event_at_returned_in_observation_results(self, db_path):
        """search_fts observation results include event_at field."""
        sid = start_session(db_path)
        event_ts = "2025-03-15T10:00:00"
        stored_ts = "2025-03-15 10:00:00"
        add_observation(
            db_path, sid, "Bash",
            "ran git log unique_plumbing_marker_xyzzy",
            event_at=event_ts,
        )

        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO observations_fts(rowid, content) "
                "SELECT id, content FROM observations WHERE session_id = ?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()

        results = search_fts(db_path, "unique_plumbing_marker_xyzzy", table="observations", limit=5)
        assert results, "expected at least one result"
        assert results[0]["event_at"] == stored_ts

    def test_event_at_none_in_observation_results_when_not_set(self, db_path):
        """search_fts observation results have event_at=None when unset."""
        sid = start_session(db_path)
        add_observation(
            db_path, sid, "Bash",
            "ran df -h unique_no_event_at_marker_xyzzy",
        )

        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO observations_fts(rowid, content) "
                "SELECT id, content FROM observations WHERE session_id = ?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()

        results = search_fts(db_path, "unique_no_event_at_marker_xyzzy", table="observations", limit=5)
        assert results, "expected at least one result"
        assert results[0]["event_at"] is None

    def test_reranker_uses_event_at_for_observations_ordering(self, db_path):
        """End-to-end: two observations with swapped event_at vs timestamp;
        search returns them ordered by event_at when recency_weight=1.0."""
        sid = start_session(db_path)

        old_ts = "2020-01-01 00:00:00"
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn = connect(db_path)
        try:
            # obs A: recent timestamp but stale event_at (simulate import of old transcript)
            conn.execute(
                """INSERT INTO observations (session_id, tool_name, content, tier, timestamp, event_at)
                   VALUES (?, 'Bash', 'ran grep unique_rerank_xyzzy', 'hot', ?, ?)""",
                (sid, new_ts, old_ts),
            )
            # obs B: stale timestamp but recent event_at
            conn.execute(
                """INSERT INTO observations (session_id, tool_name, content, tier, timestamp, event_at)
                   VALUES (?, 'Bash', 'ran grep unique_rerank_xyzzy', 'hot', ?, ?)""",
                (sid, old_ts, new_ts),
            )
            # Populate FTS
            conn.execute(
                "INSERT INTO observations_fts(rowid, content) "
                "SELECT id, content FROM observations WHERE session_id = ?",
                (sid,),
            )
            conn.commit()
            obs_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM observations WHERE session_id = ? ORDER BY id", (sid,)
                ).fetchall()
            ]
        finally:
            conn.close()

        obs_a_id, obs_b_id = obs_ids  # A has old event_at, B has new event_at

        results = search_fts(
            db_path,
            "unique_rerank_xyzzy",
            table="observations",
            limit=2,
            recency_weight=1.0,
            bm25_weight=0.0,
        )
        assert len(results) == 2
        # B (recent event_at) must rank first
        assert results[0]["id"] == obs_b_id
        assert results[1]["id"] == obs_a_id
