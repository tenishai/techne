"""Tests for the utilization audit (techne#329) — techne.core.utilization.

Read-only descriptive panels over the observation ledger + injection log. These
seed a temp db with the real writers (add_observation, log_context_injection)
and assert the aggregates, ratios, windowing, and empty-db behaviour.
"""

from __future__ import annotations

import pytest

from techne.core.database import init_db
from techne.core.state import add_observation, ensure_session
from techne.core.utilization import injection_utilization, read_write_ledger
from techne.hooks._injection_log import log_context_injection


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p)
    ensure_session(p, "s1")
    return p


def _obs(db, tool, n=1):
    for _ in range(n):
        add_observation(db, "s1", tool, "x")


def _backdate(db, tool, days):
    """Shift a tool's rows to `days` ago so the recency window excludes them."""
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    conn.execute(
        "UPDATE observations SET timestamp = datetime('now', ?) WHERE tool_name = ?",
        (f"-{days} days", tool),
    )
    conn.commit()
    conn.close()


# ---- injection_utilization -------------------------------------------------


def test_injection_utilization_aggregates_per_source(db):
    # session_start: 2 events, one with 3 hits (real), one empty (fired, 0 hits).
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[{"id": 1}, {"id": 2}, {"id": 3}], injected_text="x" * 40,
    )
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[], injected_text="",
    )
    # codex_edit_rail: 1 event, 2 hits, a different session.
    log_context_injection(
        db, source="codex_edit_rail", session_id="s2", trigger="sym",
        rows=[{"id": "a"}, {"id": "b"}], injected_text="y" * 20,
    )

    rows = injection_utilization(db)
    by_source = {r["source"]: r for r in rows}

    ss = by_source["session_start"]
    assert ss["events"] == 2
    assert ss["total_hits"] == 3
    assert ss["empty_events"] == 1  # the fired-but-selected-nothing event
    assert ss["sessions"] == 1
    assert ss["total_tokens"] == 10  # 40 chars ≈ 10 tokens (empty event adds 0)
    assert ss["avg_tokens"] == 5  # 10 tokens over 2 events

    er = by_source["codex_edit_rail"]
    assert er["events"] == 1 and er["total_hits"] == 2 and er["sessions"] == 1

    # Ordered by event count desc.
    assert rows[0]["source"] == "session_start"


def test_injection_utilization_empty_when_no_table(db):
    """No injection ever logged → the table may not exist → [] (not a crash)."""
    assert injection_utilization(db) == []


def test_injection_utilization_reports_avg_radius(db):
    """techne#343: avg_radius = mean pre-cap total_hits for a capping source;
    None for a source that never sets it."""
    log_context_injection(
        db, source="codex_edit_rail", session_id="s1", trigger="sym",
        rows=[{"id": 1}, {"id": 2}], injected_text="x" * 20, total_hits=14,
    )
    log_context_injection(
        db, source="codex_edit_rail", session_id="s1", trigger="sym2",
        rows=[{"id": 3}], injected_text="y" * 10, total_hits=6,
    )
    # session_start never sets total_hits → avg_radius stays None.
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[{"id": 9}], injected_text="z" * 8,
    )
    by = {r["source"]: r for r in injection_utilization(db)}
    assert by["codex_edit_rail"]["avg_radius"] == 10.0  # (14 + 6) / 2
    assert by["codex_edit_rail"]["total_hits"] == 3  # sum of hit_count (2 + 1)
    assert by["session_start"]["avg_radius"] is None


# ---- read_write_ledger -----------------------------------------------------


_OLD_INJECTION_DDL = (
    "CREATE TABLE context_injections (id INTEGER PRIMARY KEY, source TEXT, "
    "session_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, trigger_snippet TEXT, "
    "hit_count INTEGER, hit_ids TEXT, scores TEXT, approx_tokens INTEGER)"  # no total_hits
)


def test_log_context_injection_self_migrates_total_hits(tmp_path):
    """techne#343: writing to a pre-total_hits table ADDs the column so the value
    persists rather than erroring (the guarded ALTER)."""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_OLD_INJECTION_DDL)
    c.commit()
    c.close()

    log_context_injection(
        p, source="codex_edit_rail", session_id="s", trigger="x",
        rows=[{"id": 1}], injected_text="x" * 8, total_hits=9,
    )
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT total_hits FROM context_injections").fetchone()
    c.close()
    assert row["total_hits"] == 9


def test_injection_utilization_handles_pre_migration_table(tmp_path):
    """The audit must not crash on a table lacking total_hits (a row written before
    #343) — avg_radius is guarded to None."""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_OLD_INJECTION_DDL)
    c.execute(
        "INSERT INTO context_injections (source, hit_count, approx_tokens) "
        "VALUES ('session_start', 3, 40)"
    )
    c.commit()
    c.close()

    rows = injection_utilization(p)
    assert rows[0]["avg_radius"] is None  # column absent → NULL, no crash


def test_read_write_ledger_ratios(db):
    _obs(db, "mcp__techne__techne_store", 10)
    _obs(db, "mcp__techne__techne_recall", 3)
    _obs(db, "mcp__techne__techne_search", 1)  # reads = 4
    _obs(db, "Edit", 80)
    _obs(db, "mcp__techne__codex_search", 2)  # codex reads = 2

    led = read_write_ledger(db)
    assert led["memory"]["write"] == 10
    assert led["memory"]["read"] == 4
    assert led["memory"]["writes_per_read"] == 2.5
    assert led["codex"]["read"] == 2
    assert led["codex"]["edit"] == 80
    assert led["codex"]["edits_per_read"] == 40.0


def test_read_write_ledger_never_queried_is_none_not_zerodiv(db):
    _obs(db, "mcp__techne__techne_store", 5)  # writes but no reads
    led = read_write_ledger(db)
    assert led["memory"]["read"] == 0
    assert led["memory"]["writes_per_read"] is None  # not a ZeroDivisionError
    assert led["codex"]["edits_per_read"] is None  # no codex reads
    assert led["architect"]["read"] == 0
    assert led["librarian"]["read"] == 0


def test_librarian_flagged_not_mcp_exposed(db):
    """techne exposes no librarian MCP tool, so the ledger can't measure it —
    the row must be flagged mcp_read_tool=False (not reported as 'never queried',
    which would imply an ignored tool). architect DOES have MCP tools → True.
    (techne#329 review.)"""
    led = read_write_ledger(db)
    assert led["librarian"]["mcp_read_tool"] is False
    assert led["architect"]["mcp_read_tool"] is True


def test_read_write_ledger_counts_librarian_by_prefix(db):
    """The prefix match still lights up if a librarian_* MCP tool is ever added."""
    _obs(db, "mcp__techne__librarian_search", 2)
    _obs(db, "mcp__techne__librarian_show", 1)
    led = read_write_ledger(db)
    assert led["librarian"]["read"] == 3


def test_since_days_windows_the_ledger(db):
    _obs(db, "mcp__techne__techne_store", 4)
    _backdate(db, "mcp__techne__techne_store", days=10)  # push them out of a 2d window
    _obs(db, "mcp__techne__techne_recall", 1)  # recent

    all_time = read_write_ledger(db)
    assert all_time["memory"]["write"] == 4

    recent = read_write_ledger(db, since_days=2)
    assert recent["memory"]["write"] == 0  # backdated writes excluded
    assert recent["memory"]["read"] == 1  # recent read kept
