"""Tests for techne#214 — effective_confidence() wired into egress/recall ranking.

Design principles validated here:
  - Decay is a **tiebreak/penalty**, never a gate (entries are never excluded).
  - Half-life default is generous (≥ 30 days).
  - A deeply-decayed 'high' entry ranks BELOW a fresh 'medium' entry in the
    recall/search path.
  - Config field ``knowledge_confidence_half_life_days`` is wired and respected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import store
from techne.core.search import _knowledge_decay_score, _rerank, search_fts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "decay_rank_test.db"
    init_db(p).close()
    return p


def _insert_knowledge(
    db_path: Path,
    content: str,
    *,
    confidence: str = "medium",
    confirmed_at: str | None = None,
) -> int:
    """Store a knowledge entry and optionally backdate last_confirmed_at."""
    entry_id = store(db_path, content, type="fact", confidence=confidence)
    if confirmed_at is not None:
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE knowledge SET last_confirmed_at = ? WHERE id = ?",
                (confirmed_at, entry_id),
            )
            conn.commit()
        finally:
            conn.close()
    return entry_id


# ---------------------------------------------------------------------------
# Unit tests for _knowledge_decay_score
# ---------------------------------------------------------------------------


class TestKnowledgeDecayScore:
    """Unit tests for the helper that computes row-dict effective confidence."""

    def _row(
        self,
        confidence_numeric: float,
        days_old: float = 0.0,
        status: str = "current",
    ) -> dict:
        last = datetime.now(timezone.utc) - timedelta(days=days_old)
        return {
            "confidence_numeric": confidence_numeric,
            "last_confirmed_at": last.isoformat(),
            "status": status,
        }

    def test_fresh_entry_returns_base_confidence(self):
        row = self._row(0.9)
        score = _knowledge_decay_score(row, half_life_days=30.0)
        assert score == pytest.approx(0.9, rel=0.01)

    def test_half_life_decays_to_half(self):
        row = self._row(0.9, days_old=30.0)
        score = _knowledge_decay_score(row, half_life_days=30.0)
        # 0.9 × 0.5^(30/30) = 0.45
        assert score == pytest.approx(0.45, rel=0.01)

    def test_zero_half_life_disables_decay(self):
        row = self._row(0.9, days_old=10_000.0)
        score = _knowledge_decay_score(row, half_life_days=0)
        assert score == pytest.approx(0.9)

    def test_absent_confidence_numeric_returns_neutral_one(self):
        """Rows without confidence_numeric (threads, observations) return 1.0."""
        row = {"type": "thread", "label": "some thread"}
        assert _knowledge_decay_score(row, half_life_days=30.0) == 1.0

    def test_uncertain_status_applies_penalty(self):
        """An uncertain row gets an additional ×0.5 penalty on top of decay."""
        row = self._row(0.9, days_old=0.0, status="uncertain")
        score = _knowledge_decay_score(row, half_life_days=30.0)
        # 0.9 (fresh, no time decay) × 0.5 (uncertain penalty) = 0.45
        assert score == pytest.approx(0.45, rel=0.01)

    def test_decayed_high_falls_below_fresh_medium(self):
        """Core criterion: a 2-year-old 'high' decays below a fresh 'medium'."""
        decayed_high = self._row(0.9, days_old=730.0)  # 2 years, conf='high'
        fresh_medium = self._row(0.6, days_old=0.0)     # today,   conf='medium'
        assert _knowledge_decay_score(decayed_high, half_life_days=30.0) < _knowledge_decay_score(
            fresh_medium, half_life_days=30.0
        )

    def test_half_life_config_respected(self):
        """Doubling half_life_days halves the per-period decay rate."""
        row = self._row(0.9, days_old=60.0)
        score_30 = _knowledge_decay_score(row, half_life_days=30.0)  # 2 half-lives
        score_60 = _knowledge_decay_score(row, half_life_days=60.0)  # 1 half-life
        # 30-day half-life: 0.9 × 0.25 = 0.225; 60-day: 0.9 × 0.5 = 0.45
        assert score_30 == pytest.approx(0.225, rel=0.02)
        assert score_60 == pytest.approx(0.45, rel=0.02)
        assert score_60 > score_30


# ---------------------------------------------------------------------------
# _rerank integration: decayed 'high' below fresh 'medium'
# ---------------------------------------------------------------------------


class TestRerankDecay:
    """_rerank with confidence_decay_weight > 0 nudges decayed entries lower."""

    def _make_knowledge_row(
        self,
        row_id: str,
        confidence_numeric: float,
        days_old: float = 0.0,
        status: str = "current",
        rank: float = -5.0,  # identical BM25 rank for all rows
    ) -> dict:
        """Build a candidate dict as _fts_match_rows produces for knowledge."""
        last = datetime.now(timezone.utc) - timedelta(days=days_old)
        return {
            "type": "knowledge",
            "id": row_id,
            "content": "some content",
            "rank": rank,
            "updated_at": last.isoformat(),
            "confidence_numeric": confidence_numeric,
            "last_confirmed_at": last.isoformat(),
            "status": status,
        }

    def test_decayed_high_ranks_below_fresh_medium(self):
        """Core 'done-when' criterion: a deeply-decayed 'high' entry must rank
        below a fresh 'medium' entry when confidence_decay_weight > 0."""
        decayed_high = self._make_knowledge_row(
            "old_high", confidence_numeric=0.9, days_old=730.0
        )
        fresh_medium = self._make_knowledge_row(
            "fresh_medium", confidence_numeric=0.6, days_old=0.0
        )
        # Give both identical BM25 rank so only decay matters.
        result = _rerank(
            [decayed_high, fresh_medium],
            limit=2,
            recency_weight=0.0,   # zero out recency so decay term drives it
            bm25_weight=1.0,
            confidence_decay_weight=0.1,
            half_life_days=30.0,
        )
        ids = [r["id"] for r in result]
        assert ids.index("fresh_medium") < ids.index("old_high"), (
            "fresh 'medium' must rank above a 2-year-old 'high' entry"
        )

    def test_decayed_entry_never_excluded(self):
        """Decay is a penalty, not a gate — decayed entries must still appear."""
        decayed = self._make_knowledge_row("old", confidence_numeric=0.9, days_old=1000.0)
        fresh = self._make_knowledge_row("new", confidence_numeric=0.6, days_old=0.0)
        result = _rerank(
            [decayed, fresh],
            limit=5,  # limit > number of candidates
            recency_weight=0.0,
            bm25_weight=1.0,
            confidence_decay_weight=0.1,
            half_life_days=30.0,
        )
        result_ids = {r["id"] for r in result}
        assert "old" in result_ids, "Decayed entry must not be excluded from results"
        assert "new" in result_ids

    def test_no_decay_when_weight_zero(self):
        """confidence_decay_weight=0.0 restores original BM25-only ordering."""
        # Better BM25 rank (more negative = better) for the old entry.
        old_high = self._make_knowledge_row("old", 0.9, days_old=1000.0, rank=-9.0)
        fresh_med = self._make_knowledge_row("new", 0.6, days_old=0.0, rank=-1.0)
        result = _rerank(
            [old_high, fresh_med],
            limit=2,
            recency_weight=0.0,
            bm25_weight=1.0,
            confidence_decay_weight=0.0,  # decay disabled
            half_life_days=30.0,
        )
        # BM25 only: old_high wins (rank -9.0 > -1.0)
        assert result[0]["id"] == "old"

    def test_fresh_high_still_outranks_fresh_medium(self):
        """When both entries are fresh, the 'high' entry should still outrank 'medium'."""
        fresh_high = self._make_knowledge_row("high", confidence_numeric=0.9, days_old=0.0)
        fresh_medium = self._make_knowledge_row("medium", confidence_numeric=0.6, days_old=0.0)
        result = _rerank(
            [fresh_high, fresh_medium],
            limit=2,
            recency_weight=0.0,
            bm25_weight=1.0,
            confidence_decay_weight=0.1,
            half_life_days=30.0,
        )
        assert result[0]["id"] == "high", "Fresh 'high' must rank above fresh 'medium'"

    def test_scratch_keys_cleaned_with_decay(self):
        """Internal scratch keys must be removed from returned rows."""
        row = self._make_knowledge_row("k1", 0.9, days_old=10.0)
        result = _rerank([row], limit=1, recency_weight=0.3, bm25_weight=0.7,
                         confidence_decay_weight=0.1, half_life_days=30.0)
        assert "_norm_bm25" not in result[0]
        assert "_recency" not in result[0]
        assert "_combined" not in result[0]


# ---------------------------------------------------------------------------
# Config field wired end-to-end
# ---------------------------------------------------------------------------


class TestConfigHalfLifeWired:
    def test_config_field_default(self):
        """knowledge_confidence_half_life_days defaults to ≥ 30 days."""
        cfg = Config()
        assert cfg.knowledge_confidence_half_life_days >= 30.0

    def test_config_field_parsed_from_yaml(self, tmp_path):
        """The config field is correctly read from YAML."""
        import yaml

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"knowledge_confidence_half_life_days": 60.0}))
        cfg = Config.load(cfg_file)
        assert cfg.knowledge_confidence_half_life_days == pytest.approx(60.0)

    def test_config_field_serialized(self, tmp_path):
        """save() + load() round-trips knowledge_confidence_half_life_days."""
        cfg = Config()
        cfg.knowledge_confidence_half_life_days = 45.0
        cfg.db_path = tmp_path / "techne.db"
        cfg.data_dir = tmp_path
        cfg_path = tmp_path / "config.yaml"
        cfg.save(cfg_path)
        reloaded = Config.load(cfg_path)
        assert reloaded.knowledge_confidence_half_life_days == pytest.approx(45.0)

    def test_zero_half_life_disables_time_decay(self):
        """half_life_days=0 disables time decay in _rerank (returns raw confidence_numeric)."""
        old = {
            "type": "knowledge", "id": "old", "rank": -5.0, "updated_at": None,
            "confidence_numeric": 0.9, "last_confirmed_at": "2020-01-01 00:00:00",
            "status": "current",
        }
        fresh = {
            "type": "knowledge", "id": "fresh", "rank": -5.0, "updated_at": None,
            "confidence_numeric": 0.6, "last_confirmed_at": datetime.now(timezone.utc).isoformat(),
            "status": "current",
        }
        result = _rerank(
            [old, fresh],
            limit=2,
            recency_weight=0.0,
            bm25_weight=1.0,
            confidence_decay_weight=0.1,
            half_life_days=0.0,  # decay disabled
        )
        # With decay disabled, raw confidence_numeric applies: old (0.9) > fresh (0.6)
        assert result[0]["id"] == "old", (
            "With half_life_days=0, raw confidence_numeric should rank 'high' first"
        )


# ---------------------------------------------------------------------------
# search_fts integration: decay threaded through the full search path
# ---------------------------------------------------------------------------


class TestSearchFtsDecay:
    """End-to-end: search_fts with decay enabled reranks knowledge correctly."""

    def test_decayed_high_ranks_below_fresh_medium_via_search_fts(self, db_path):
        """The 'done-when' test through the full search_fts path."""
        # Store a 'high' entry and backdate its last_confirmed_at to 3 years ago.
        old_high_id = _insert_knowledge(
            db_path,
            "unique_decay_term_alpha memory fact one",
            confidence="high",
            confirmed_at="2021-01-01 00:00:00",  # ~5+ years ago relative to 2026
        )
        # Store a fresh 'medium' entry with the same keyword.
        fresh_medium_id = _insert_knowledge(
            db_path,
            "unique_decay_term_alpha memory fact two",
            confidence="medium",
        )

        results = search_fts(
            db_path,
            "unique_decay_term_alpha",
            table="knowledge",
            limit=10,
            recency_weight=0.0,   # zero out recency to isolate decay signal
            bm25_weight=1.0,
            confidence_decay_weight=0.5,  # strong decay for this test
            half_life_days=30.0,
        )

        ids = [r["id"] for r in results]
        assert old_high_id in ids, "Decayed entry must still be in results (penalty not gate)"
        assert fresh_medium_id in ids, "Fresh medium must be in results"
        assert ids.index(fresh_medium_id) < ids.index(old_high_id), (
            "Fresh 'medium' must rank above the 5-year-old 'high' entry "
            "when confidence_decay_weight is applied"
        )

    def test_decayed_entry_present_in_results(self, db_path):
        """Decayed entries must never be excluded — this is a penalty, not a gate."""
        old_id = _insert_knowledge(
            db_path,
            "unique_penalty_not_gate_term",
            confidence="high",
            confirmed_at="2019-01-01 00:00:00",
        )
        fresh_id = _insert_knowledge(
            db_path,
            "unique_penalty_not_gate_term",
            confidence="medium",
        )

        results = search_fts(
            db_path,
            "unique_penalty_not_gate_term",
            table="knowledge",
            limit=10,
            confidence_decay_weight=0.9,  # extreme weight to prove no exclusion
            half_life_days=30.0,
        )
        result_ids = {r["id"] for r in results}
        assert old_id in result_ids, "Decayed 'high' must still be returned, not excluded"
        assert fresh_id in result_ids


# ---------------------------------------------------------------------------
# Egress tier-0: within-band decay ordering
# ---------------------------------------------------------------------------


class TestEgressDecayOrdering:
    """Tier-0 egress: within each band, recently-confirmed entries come first."""

    def test_fresher_confirmed_ranks_first_in_band(self, db_path):
        """Within the remaining band, a recently-confirmed 'high' entry should
        surface before a long-unconfirmed 'high' entry when decay is applied."""
        from techne.core.egress import select_tier0_injection

        old_id = _insert_knowledge(
            db_path,
            "an old high confidence fact about deployment",
            confidence="high",
            confirmed_at="2021-01-01 00:00:00",
        )
        fresh_id = _insert_knowledge(
            db_path,
            "a recently confirmed high confidence fact about deployment",
            confidence="high",
            confirmed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

        rows = select_tier0_injection(db_path, char_budget=4000, half_life_days=30.0)
        ids = [r["id"] for r in rows]

        assert old_id in ids, "Old entry must still be included (penalty not gate)"
        assert fresh_id in ids
        assert ids.index(fresh_id) < ids.index(old_id), (
            "Recently-confirmed entry should rank before long-unconfirmed entry"
        )

    def test_old_entry_not_excluded_in_egress(self, db_path):
        """Decayed entries must still appear in tier-0 injection output."""
        from techne.core.egress import select_tier0_injection

        old_id = _insert_knowledge(
            db_path,
            "ancient high confidence fact that must still appear",
            confidence="high",
            confirmed_at="2019-01-01 00:00:00",
        )

        rows = select_tier0_injection(db_path, char_budget=4000, half_life_days=30.0)
        ids = [r["id"] for r in rows]
        assert old_id in ids, "Decayed entry must not be excluded from tier-0 egress"
