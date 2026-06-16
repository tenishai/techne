"""Tests for candidate-expansion + reranking layer in search_fts (issue #9)."""

from datetime import datetime, timedelta, timezone

import pytest

from techne.core.database import connect, init_db
from techne.core.search import _recency_score, _rerank, search_fts
from techne.core.state import start_session
from techne.core.threads import Thread, save_threads


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "rerank_test.db"
    conn = init_db(path)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Unit tests for _recency_score
# ---------------------------------------------------------------------------


class TestRecencyScore:
    def test_now_is_one(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _recency_score(now) > 0.99

    def test_old_is_low(self):
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        assert _recency_score(old) < 0.1

    def test_none_returns_neutral(self):
        assert _recency_score(None) == 0.5

    def test_bad_string_returns_neutral(self):
        assert _recency_score("not-a-date") == 0.5


# ---------------------------------------------------------------------------
# Unit tests for _rerank
# ---------------------------------------------------------------------------


class TestRerank:
    def _make_row(self, rank: float, ts: str | None = None) -> dict:
        """Build a minimal candidate dict as search_fts produces."""
        return {
            "type": "knowledge",
            "id": f"k-{rank}",
            "rank": rank,
            "updated_at": ts,
        }

    def test_returns_limit_rows(self):
        rows = [self._make_row(float(-i)) for i in range(1, 11)]
        result = _rerank(rows, limit=5, recency_weight=0.3, bm25_weight=0.7)
        assert len(result) == 5

    def test_pure_bm25_ordering(self):
        """bm25_weight=1.0 → most-negative BM25 (best match) wins."""
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            self._make_row(-5.0, now),  # best BM25 (most negative)
            self._make_row(-1.0, now),  # worst BM25 (least negative)
            self._make_row(-3.0, now),
        ]
        result = _rerank(rows, limit=3, recency_weight=0.0, bm25_weight=1.0)
        assert result[0]["id"] == "k--5.0"

    def test_pure_recency_ordering(self):
        """recency_weight=1.0 → newer timestamp wins regardless of BM25."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        # Give OLD row the best BM25 (most-negative) to confirm recency overrides it.
        rows = [
            self._make_row(-9.9, old_ts),  # best BM25 (most-negative), old
            self._make_row(-0.1, new_ts),  # poor BM25 (least-negative), new
        ]
        result = _rerank(rows, limit=2, recency_weight=1.0, bm25_weight=0.0)
        assert result[0]["updated_at"] == new_ts

    def test_recency_boost_equal_bm25(self):
        """Two rows with identical BM25; the newer one should rank higher."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        rows = [
            {"type": "t", "id": "old", "rank": -5.0, "updated_at": old_ts},
            {"type": "t", "id": "new", "rank": -5.0, "updated_at": new_ts},
        ]
        result = _rerank(rows, limit=2, recency_weight=0.3, bm25_weight=0.7)
        assert result[0]["id"] == "new"

    def test_empty_input(self):
        assert _rerank([], limit=5, recency_weight=0.3, bm25_weight=0.7) == []

    def test_scratch_keys_cleaned(self):
        rows = [self._make_row(-1.0)]
        result = _rerank(rows, limit=1, recency_weight=0.3, bm25_weight=0.7)
        assert "_norm_bm25" not in result[0]
        assert "_recency" not in result[0]
        assert "_combined" not in result[0]


# ---------------------------------------------------------------------------
# Integration tests via search_fts
# ---------------------------------------------------------------------------


class TestSearchFtsRerank:
    def test_pulls_expanded_candidates_returns_limit(self, db_path):
        """search_fts returns at most `limit` results even with expansion_ratio=2."""
        session_id = start_session(db_path)
        threads = [
            Thread(thread_num=i, label=f"topic{i} unique_term_zeta", summary=f"detail {i}")
            for i in range(1, 8)
        ]
        save_threads(threads, session_id, db_path)

        results = search_fts(
            db_path,
            "unique_term_zeta",
            table="threads",
            limit=3,
            expansion_ratio=2,
        )
        assert len(results) <= 3

    def test_recency_weight_1_orders_by_recency(self, db_path):
        """recency_weight=1.0, bm25_weight=0.0 → newest thread wins."""
        sid = start_session(db_path)
        conn = connect(db_path)

        old_ts = "2020-01-01 00:00:00"
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status, updated_at) "
            "VALUES (?, ?, 1, ?, 'body lemon', 'active', ?)",
            ("old-thread", sid, "lemon search term", old_ts),
        )
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status, updated_at) "
            "VALUES (?, ?, 2, ?, 'body lemon', 'active', ?)",
            ("new-thread", sid, "lemon search term", new_ts),
        )
        conn.execute(
            "INSERT INTO threads_fts(rowid, label, summary) "
            "SELECT rowid, label, summary FROM threads WHERE id IN ('old-thread', 'new-thread')"
        )
        conn.commit()
        conn.close()

        results = search_fts(
            db_path,
            "lemon",
            table="threads",
            limit=2,
            recency_weight=1.0,
            bm25_weight=0.0,
        )
        assert results[0]["id"] == "new-thread"

    def test_bm25_weight_1_respects_bm25_ordering(self, db_path):
        """bm25_weight=1.0, recency_weight=0.0 → reranker preserves BM25 top result.

        We determine the expected winner directly from SQLite's bm25() to avoid
        encoding FTS5 internals — the point is that reranker doesn't invert it.
        """
        sid = start_session(db_path)
        conn = connect(db_path)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status, updated_at) "
            "VALUES (?, ?, 1, ?, ?, 'active', ?)",
            ("row-a", sid, "mango fruit tropical", "mango", ts),
        )
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status, updated_at) "
            "VALUES (?, ?, 2, ?, ?, 'active', ?)",
            ("row-b", sid, "unrelated topic here", "nothing about mango here", ts),
        )
        conn.execute(
            "INSERT INTO threads_fts(rowid, label, summary) "
            "SELECT rowid, label, summary FROM threads WHERE id IN ('row-a', 'row-b')"
        )
        conn.commit()

        # Find which row SQLite BM25 ranks first.
        raw = conn.execute(
            "SELECT t.id FROM threads_fts f JOIN threads t ON t.rowid=f.rowid "
            "WHERE threads_fts MATCH 'mango' ORDER BY bm25(threads_fts)"
        ).fetchall()
        conn.close()
        expected_first = raw[0]["id"]

        results = search_fts(
            db_path,
            "mango",
            table="threads",
            limit=2,
            recency_weight=0.0,
            bm25_weight=1.0,
        )
        assert results[0]["id"] == expected_first
