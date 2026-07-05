"""Tests for techne#216 — soft-archive lifecycle for decayed+uncertain entries.

Design invariants validated here:
  - A below-floor, uncertain, un-reaffirmed entry IS archived by archive_scan().
  - An archived entry is EXCLUDED from Tier-0 egress (default recall).
  - An archived entry is EXCLUDED from default recall via search(include_archived=False)
    but PRESENT via explicit search (search / search_fts, default include_archived=True).
  - A fresh / confirmed / high-confidence entry is NOT archived.
  - Archiving is REVERSIBLE via unarchive().
  - dry_run=True reports candidates without writing.
  - archive_scan's inline decay scoring agrees with effective_confidence() (drift guard).
  - Config fields (confidence_floor, reaffirmation_window_days) are parsed and wired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.core.knowledge import (
    DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,
    DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
    archive_scan,
    effective_confidence,
    store,
    unarchive,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    p = tmp_path / "archive_test.db"
    init_db(p).close()
    return p


def _insert(
    db_path: Path,
    content: str,
    *,
    confidence: str = "medium",
    status: str = "current",
    confirmed_days_ago: float = 0.0,
) -> int:
    """Store an entry then back-date / set status as needed for tests."""
    entry_id = store(db_path, content, type="fact", confidence=confidence)
    if status != "current" or confirmed_days_ago > 0:
        confirmed_at = (
            datetime.now(timezone.utc) - timedelta(days=confirmed_days_ago)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE knowledge SET status = ?, last_confirmed_at = ? WHERE id = ?",
                (status, confirmed_at, entry_id),
            )
            conn.commit()
        finally:
            conn.close()
    return entry_id


# ---------------------------------------------------------------------------
# archive_scan: core criteria
# ---------------------------------------------------------------------------


class TestArchiveScanCriteria:
    """archive_scan selects entries that meet ALL three criteria."""

    def test_qualifying_entry_is_archived(self, tmp_path):
        """An uncertain, decayed, long-unreaffirmed entry is archived."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "stale uncertain fact about an old API",
            confidence="low",        # confidence_numeric=0.3; with decay+penalty → well below 0.1
            status="uncertain",
            confirmed_days_ago=400,   # well beyond the 180-day default window
        )

        archived = archive_scan(
            db,
            confidence_floor=DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,
            reaffirmation_window_days=DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
            half_life_days=30.0,
        )

        assert any(r["id"] == entry_id for r in archived), (
            "Qualifying entry must appear in archive_scan results"
        )

        # Verify DB state was updated.
        conn = connect(db)
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        assert row["status"] == "archived"

    def test_archived_status_set_in_db(self, tmp_path):
        """archive_scan writes status='archived' to the DB."""
        db = _make_db(tmp_path)
        _insert(
            db,
            "another old uncertain entry zxqvnm",
            confidence="low",
            status="uncertain",
            confirmed_days_ago=400,
        )
        archive_scan(
            db,
            confidence_floor=0.5,      # wide floor to catch it easily
            reaffirmation_window_days=30,
            half_life_days=30.0,
        )
        conn = connect(db)
        row = conn.execute(
            "SELECT status FROM knowledge WHERE content LIKE '%zxqvnm%'"
        ).fetchone()
        conn.close()
        assert row["status"] == "archived"


class TestArchiveScanNegatives:
    """Entries that do NOT meet all criteria must NOT be archived."""

    def test_current_status_not_archived(self, tmp_path):
        """An entry with status='current' is never archived, regardless of decay."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "current entry even if old alpha1beta2",
            confidence="low",
            status="current",        # NOT 'uncertain'
            confirmed_days_ago=400,
        )
        archived = archive_scan(
            db,
            confidence_floor=0.9,
            reaffirmation_window_days=1,
            half_life_days=30.0,
        )
        assert not any(r["id"] == entry_id for r in archived)

    def test_high_confidence_not_archived(self, tmp_path):
        """A high-confidence uncertain entry stays above the floor even past the window.

        Isolates the FLOOR criterion: with decay disabled (half_life_days=0), the
        entry's effective_confidence is confidence_numeric × uncertain_penalty =
        0.9 × 0.5 = 0.45, which is well above the 0.1 floor — so despite being
        uncertain AND past the reaffirmation window, it is NOT archived.
        """
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "uncertain but high-confidence entry gamma3delta4",
            confidence="high",       # confidence_numeric=0.9 → eff_conf 0.45 with penalty
            status="uncertain",
            confirmed_days_ago=400,  # past the window, but decay is off below
        )
        archived = archive_scan(
            db,
            confidence_floor=DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,  # 0.1
            reaffirmation_window_days=DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
            half_life_days=0.0,      # decay disabled → floor is the only gate that matters
        )
        assert not any(r["id"] == entry_id for r in archived), (
            "A high-confidence entry (eff_conf 0.45) must stay above the 0.1 floor"
        )

    def test_recently_reaffirmed_not_archived(self, tmp_path):
        """An uncertain entry reaffirmed recently (within the window) is not archived."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "recently reaffirmed uncertain entry zeta6",
            confidence="low",
            status="uncertain",
            confirmed_days_ago=10,   # < 180d default window
        )
        archived = archive_scan(
            db,
            confidence_floor=DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,
            reaffirmation_window_days=DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
            half_life_days=30.0,
        )
        assert not any(r["id"] == entry_id for r in archived), (
            "Recently-reaffirmed entry must NOT be archived"
        )

    def test_fresh_high_confidence_current_not_archived(self, tmp_path):
        """The canonical 'does not qualify' case: fresh, high-conf, current."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "a current high-confidence fresh fact eta7",
            confidence="high",
            status="current",
            confirmed_days_ago=1,
        )
        archived = archive_scan(
            db,
            confidence_floor=0.9,    # even a very high floor
            reaffirmation_window_days=1,
            half_life_days=30.0,
        )
        assert not any(r["id"] == entry_id for r in archived)


# ---------------------------------------------------------------------------
# Recall exclusion: archived entries OUT of Tier-0 egress
# ---------------------------------------------------------------------------


class TestArchivedExcludedFromEgress:
    """Archived entries must NOT appear in select_tier0_injection output."""

    def test_archived_excluded_from_tier0(self, tmp_path):
        """An archived high-conf entry is excluded from Tier-0 injection."""
        from techne.core.egress import select_tier0_injection

        db = _make_db(tmp_path)
        # Store a high-confidence entry that would normally appear in Tier-0.
        entry_id = _insert(
            db,
            "high confidence fact for egress exclusion test theta8",
            confidence="high",
            status="current",
        )

        # Verify it appears before archiving.
        rows_before = select_tier0_injection(db, char_budget=10_000)
        ids_before = [r["id"] for r in rows_before]
        assert entry_id in ids_before, "Entry should appear in Tier-0 before archiving"

        # Manually flip to 'archived' (simulating what archive_scan() does).
        conn = connect(db)
        conn.execute(
            "UPDATE knowledge SET status = 'archived' WHERE id = ?", (entry_id,)
        )
        conn.commit()
        conn.close()

        # Must be absent after archiving.
        rows_after = select_tier0_injection(db, char_budget=10_000)
        ids_after = [r["id"] for r in rows_after]
        assert entry_id not in ids_after, (
            "Archived entry must be excluded from Tier-0 egress"
        )

    def test_non_archived_entries_unaffected_in_egress(self, tmp_path):
        """Archiving one entry does not remove unrelated entries from Tier-0."""
        from techne.core.egress import select_tier0_injection

        db = _make_db(tmp_path)
        keep_id = _insert(db, "keep this high-conf fact iota9", confidence="high")
        archive_id = _insert(db, "archive this high-conf fact kappa10", confidence="high")

        conn = connect(db)
        conn.execute(
            "UPDATE knowledge SET status = 'archived' WHERE id = ?", (archive_id,)
        )
        conn.commit()
        conn.close()

        rows = select_tier0_injection(db, char_budget=10_000)
        ids = [r["id"] for r in rows]
        assert keep_id in ids, "Non-archived entry must remain in Tier-0"
        assert archive_id not in ids, "Archived entry must not appear in Tier-0"


# ---------------------------------------------------------------------------
# Explicit search: archived entries still returned
# ---------------------------------------------------------------------------


class TestArchivedInExplicitSearch:
    """Archived entries must still be returned by search_fts (explicit search)."""

    def test_archived_entry_in_explicit_search(self, tmp_path):
        """An archived entry is still returned by FTS search."""
        from techne.core.search import search_fts

        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "unique_soft_archive_search_term_lambda11 some archived knowledge",
            confidence="low",
            status="uncertain",
            confirmed_days_ago=400,
        )

        # Archive it.
        archived = archive_scan(
            db,
            confidence_floor=0.9,    # cast wide to catch it
            reaffirmation_window_days=30,
            half_life_days=30.0,
        )
        assert any(r["id"] == entry_id for r in archived), "Should have been archived"

        # Explicit search must still return it.
        results = search_fts(
            db,
            "unique_soft_archive_search_term_lambda11",
            table="knowledge",
            limit=20,
        )
        result_ids = [r["id"] for r in results]
        assert entry_id in result_ids, (
            "Archived entry must still appear in explicit FTS search"
        )

    def test_archived_entry_absent_from_tier0_but_in_search(self, tmp_path):
        """Full three-way assertion: archived state + absent from Tier-0 + present in search."""
        from techne.core.egress import select_tier0_injection
        from techne.core.search import search_fts

        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "unique_soft_archive_triple_check_mu12 old uncertain fact",
            confidence="high",      # high-conf so it would normally appear in Tier-0
            status="uncertain",
            confirmed_days_ago=400,
        )

        # Force archive by flipping status directly (avoids decay computation dependency).
        conn = connect(db)
        conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()

        # 1. DB state.
        conn = connect(db)
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        assert row["status"] == "archived"

        # 2. Absent from Tier-0 egress.
        egress_rows = select_tier0_injection(db, char_budget=50_000)
        assert entry_id not in [r["id"] for r in egress_rows], (
            "Archived entry must be absent from default recall (Tier-0)"
        )

        # 3. Present in explicit FTS search.
        search_results = search_fts(
            db, "unique_soft_archive_triple_check_mu12", table="knowledge", limit=20
        )
        assert entry_id in [r["id"] for r in search_results], (
            "Archived entry must be present in explicit search"
        )


# ---------------------------------------------------------------------------
# DoD core split: default recall EXCLUDES, explicit search INCLUDES
# ---------------------------------------------------------------------------


class TestRecallExclusionVsSearchInclusion:
    """The issue's done-when split, at the search() layer (techne#216).

    search(include_archived=False) is DEFAULT recall (techne_recall) — must EXCLUDE
    archived. search(include_archived=True), the default, is EXPLICIT search
    (techne_search) — must INCLUDE archived. This test BITES against the pre-fix
    code where _fts_match_rows had no status filter (archived leaked into recall).
    """

    def test_archived_excluded_from_default_recall_present_in_explicit_search(self, tmp_path):
        from techne.core.search import search

        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "unique_recall_split_term_nu18 an archived uncertain fact",
            confidence="high",
            status="uncertain",
            confirmed_days_ago=400,
        )
        # Archive it directly (status flip is what archive_scan writes).
        conn = connect(db)
        conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()

        # DEFAULT recall (include_archived=False) — MUST EXCLUDE. use_vectors=False so
        # the assertion is deterministic regardless of whether fastembed is installed.
        recall_results = search(
            db,
            "unique_recall_split_term_nu18",
            table="all",
            limit=20,
            use_vectors=False,
            include_archived=False,
        )
        assert entry_id not in [r["id"] for r in recall_results], (
            "Archived entry must be EXCLUDED from default recall "
            "(search(include_archived=False))"
        )

        # EXPLICIT search (default include_archived=True) — MUST INCLUDE.
        search_results = search(
            db,
            "unique_recall_split_term_nu18",
            table="all",
            limit=20,
            use_vectors=False,
        )
        assert entry_id in [r["id"] for r in search_results], (
            "Archived entry must be PRESENT in explicit search "
            "(search(), default include_archived=True)"
        )

    def test_techne_recall_excludes_archived(self, tmp_path):
        """End-to-end through the MCP _techne_recall handler: archived is excluded."""
        from techne.config import Config
        from techne.mcp_server import _techne_recall, _techne_search

        db = _make_db(tmp_path)
        cfg = Config()
        cfg.db_path = db

        entry_id = _insert(
            db,
            "unique_mcp_recall_term_xi19 archived uncertain knowledge",
            confidence="high",
            status="uncertain",
            confirmed_days_ago=400,
        )
        conn = connect(db)
        conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()

        # _techne_recall — DEFAULT recall — must NOT surface the archived entry.
        recall = _techne_recall({"topic": "unique_mcp_recall_term_xi19"}, cfg)
        recall_ids = [r["id"] for r in recall["search_results"]]
        assert entry_id not in recall_ids, (
            "techne_recall (default recall) must exclude archived entries"
        )

        # _techne_search — EXPLICIT search — must still return it.
        found = _techne_search(
            {"query": "unique_mcp_recall_term_xi19", "table": "knowledge"}, cfg
        )
        found_ids = [r["id"] for r in found["results"]]
        assert entry_id in found_ids, (
            "techne_search (explicit search) must still return archived entries"
        )


# ---------------------------------------------------------------------------
# Drift guard: archive_scan's inline scoring == effective_confidence()
# ---------------------------------------------------------------------------


class TestDecayFormulaDriftGuard:
    """archive_scan inlines the effective_confidence() formula (to avoid an N-row
    DB round-trip). This guards against the two drifting apart (techne#216 review)."""

    def test_archive_scoring_agrees_with_effective_confidence(self, tmp_path):
        """The eff_conf reported by archive_scan matches effective_confidence() on
        the same uncertain row, for a sampled age/half-life."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "drift guard sampled uncertain row omicron20",
            confidence="low",         # 0.3 base
            status="uncertain",
            confirmed_days_ago=250,   # past window, decayed but computable
        )

        # dry_run so the row's status stays 'uncertain' — effective_confidence()
        # applies the uncertain penalty, which archive_scan must also apply.
        candidates = archive_scan(
            db,
            confidence_floor=1.0,     # floor=1.0 → every uncertain past-window row qualifies
            reaffirmation_window_days=180,
            half_life_days=30.0,
            dry_run=True,
        )
        scored = {c["id"]: c["effective_confidence"] for c in candidates}
        assert entry_id in scored, "sampled row should be a candidate under floor=1.0"

        # Reference value from the public API (same half_life, same 'now' semantics).
        reference = effective_confidence(db, entry_id, half_life_days=30.0)
        assert reference is not None
        assert scored[entry_id] == pytest.approx(reference, rel=1e-6, abs=1e-9), (
            "archive_scan's inline decay score must agree with effective_confidence()"
        )


# ---------------------------------------------------------------------------
# Reversibility: unarchive()
# ---------------------------------------------------------------------------


class TestUnarchive:
    """unarchive() restores an archived entry to 'uncertain' status."""

    def test_unarchive_restores_uncertain_status(self, tmp_path):
        """unarchive() flips status back to 'uncertain'."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "entry to unarchive nu13",
            confidence="low",
            status="uncertain",
            confirmed_days_ago=400,
        )
        archive_scan(db, confidence_floor=0.9, reaffirmation_window_days=30, half_life_days=30.0)

        conn = connect(db)
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        assert row["status"] == "archived"

        restored = unarchive(db, entry_id)
        assert restored is True

        conn = connect(db)
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        assert row["status"] == "uncertain"

    def test_unarchive_returns_false_for_non_archived(self, tmp_path):
        """unarchive() returns False when the entry is not archived."""
        db = _make_db(tmp_path)
        entry_id = _insert(db, "current entry xi14", confidence="medium", status="current")

        restored = unarchive(db, entry_id)
        assert restored is False

    def test_unarchive_returns_false_for_missing_id(self, tmp_path):
        """unarchive() returns False for a non-existent entry id."""
        db = _make_db(tmp_path)
        restored = unarchive(db, 99999)
        assert restored is False

    def test_unarchived_entry_reappears_in_egress(self, tmp_path):
        """After unarchive(), the entry is visible again in Tier-0 (if high-conf)."""
        from techne.core.egress import select_tier0_injection

        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "high-conf entry to unarchive omicron15",
            confidence="high",
        )
        conn = connect(db)
        conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()

        assert entry_id not in [
            r["id"] for r in select_tier0_injection(db, char_budget=50_000)
        ]

        unarchive(db, entry_id)

        assert entry_id in [
            r["id"] for r in select_tier0_injection(db, char_budget=50_000)
        ], "Unarchived entry must reappear in Tier-0 egress"


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    """dry_run=True returns candidates without modifying the DB."""

    def test_dry_run_does_not_write(self, tmp_path):
        """dry_run=True reports qualifying entries without updating status."""
        db = _make_db(tmp_path)
        entry_id = _insert(
            db,
            "entry for dry-run test pi16",
            confidence="low",
            status="uncertain",
            confirmed_days_ago=400,
        )

        candidates = archive_scan(
            db,
            confidence_floor=0.9,
            reaffirmation_window_days=30,
            half_life_days=30.0,
            dry_run=True,
        )
        assert any(r["id"] == entry_id for r in candidates), "Dry run must report candidate"

        conn = connect(db)
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        assert row["status"] == "uncertain", "dry_run must NOT change status in DB"


# ---------------------------------------------------------------------------
# Config field parsing and wiring
# ---------------------------------------------------------------------------


class TestArchiveConfig:
    """Config.archive fields are parsed from YAML and have correct defaults."""

    def test_config_defaults(self):
        """ArchiveConfig defaults match the constants in knowledge.py."""
        from techne.config import Config

        cfg = Config()
        assert cfg.archive.confidence_floor == DEFAULT_ARCHIVE_CONFIDENCE_FLOOR
        assert cfg.archive.reaffirmation_window_days == DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS

    def test_config_parsed_from_yaml(self, tmp_path):
        """archive.* fields are parsed from a YAML config file."""
        import yaml

        from techne.config import Config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.dump(
                {"archive": {"confidence_floor": 0.05, "reaffirmation_window_days": 90}}
            )
        )
        cfg = Config.load(cfg_file)
        assert cfg.archive.confidence_floor == pytest.approx(0.05)
        assert cfg.archive.reaffirmation_window_days == 90

    def test_config_serialized(self, tmp_path):
        """save() + load() round-trips archive config."""
        import yaml

        from techne.config import Config

        cfg = Config()
        cfg.db_path = tmp_path / "techne.db"
        cfg.data_dir = tmp_path
        cfg.archive.confidence_floor = 0.07
        cfg.archive.reaffirmation_window_days = 120
        cfg_path = tmp_path / "config.yaml"
        cfg.save(cfg_path)

        reloaded = Config.load(cfg_path)
        assert reloaded.archive.confidence_floor == pytest.approx(0.07)
        assert reloaded.archive.reaffirmation_window_days == 120
