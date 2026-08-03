"""Tests for `techne doctor` — core-DB health / duplicate-row audit.

The duplicate-row check exists to catch silent-accumulation bugs (a table growing
duplicates it should never have) — a failure mode with no functional symptom, so
it is invisible to behavior tests and code review and only shows up as storage
growth. thread_boundaries hit exactly that; this asserts the audit now catches it.
"""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A Config pointed at a fresh (v-current) tmp db, injected via Config.load()."""
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def _seed_duplicate_boundaries(db_path):
    """Force a duplicate-row state by dropping the guard index (simulating a
    pre-v17 / regressed DB) and inserting the same (session, pair) three times."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s1')")
    conn.execute("DROP INDEX IF EXISTS idx_thread_boundaries_triple")
    for _ in range(3):
        conn.execute(
            "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
            "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
            "VALUES ('s1', 0, 1, 60.0, 'fs', 'fs', '{}', 0.0, 0)"
        )
    conn.commit()
    conn.close()


def test_doctor_clean_db_passes(cli_db):
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "no duplicates" in result.output
    assert "All checks passed" in result.output


def test_doctor_flags_duplicates_and_exits_nonzero(cli_db):
    # --no-liveness scopes this to what it is about (storage debt). doctor now
    # answers two independent questions and the summary line names both, so a
    # bare "issue(s) found" no longer appears (techne#468).
    _seed_duplicate_boundaries(cli_db.db_path)
    result = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert result.exit_code == 1, result.output
    assert "thread_boundaries" in result.output
    assert "duplicate rows" in result.output
    assert "storage issue(s)" in result.output


def test_doctor_exits_nonzero_when_a_subsystem_is_not_confirmed(cli_db):
    """Liveness alone must be able to fail the command.

    The regression this guards: on 2026-08-03 `techne doctor` printed "All
    checks passed" while consolidate and dream had been cold for four weeks,
    because it only ever looked at storage.
    """
    from techne.core.database import connect

    # Give the store activity, so missing hook markers are undecidable rather
    # than merely "nothing has happened yet".
    conn = connect(cli_db.db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, ?)", ("s1", "2026-08-01 00:00:00")
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "not confirmed working" in result.output


def test_doctor_vacuum_reports_and_shrinks_the_file(cli_db):
    """The --vacuum path must reclaim space and report an honest figure.

    Guards against the WAL/stat concern: techne runs journal_mode=WAL, so this
    asserts the main-file stat() actually drops after VACUUM (it does — VACUUM
    rewrites the main db file directly, not via the -wal sidecar).
    """
    import os

    # Grow the file with ~2.5 MB of distinct-triple rows, then delete them all so
    # the pages sit on the freelist (reclaimable but not yet returned to the OS).
    conn = sqlite3.connect(cli_db.db_path)
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s1')")
    conn.executemany(
        "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
        "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
        "VALUES ('s1', ?, ?, 60.0, 'fs', 'fs', ?, 0.0, 0)",
        [(i, i + 1, "x" * 500) for i in range(5000)],
    )
    conn.commit()
    conn.execute("DELETE FROM thread_boundaries")
    conn.commit()
    conn.close()

    before = os.path.getsize(cli_db.db_path)
    # --no-liveness: this test is about VACUUM reclaiming pages, and the seeded
    # store has activity but no hook markers, which liveness correctly fails on.
    # Scoping keeps the assertion about the thing under test (techne#468).
    result = CliRunner().invoke(main, ["doctor", "--vacuum", "--no-liveness"])
    assert result.exit_code == 0, result.output
    assert "VACUUM" in result.output and "reclaimed" in result.output
    after = os.path.getsize(cli_db.db_path)
    assert after < before, f"file did not shrink: {before} -> {after}"
