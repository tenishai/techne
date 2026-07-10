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
    _seed_duplicate_boundaries(cli_db.db_path)
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "thread_boundaries" in result.output
    assert "duplicate rows" in result.output
    assert "issue(s) found" in result.output
