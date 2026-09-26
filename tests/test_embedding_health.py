"""techne#942 and techne#948 — embedding health that told the operator the wrong thing.

#942: `vectors:coverage` printed "0/0 live knowledge embedded and current (100%)"
on a store with 35,096 embedded observations and no knowledge. True, and it said
nothing about either.

#948: `techne vectors status` printed "re-embed needed: False" with 25 promoted
knowledge rows and an empty knowledge_vec. That field only means "the backend
changed"; nothing on the command answered "is everything embedded?".

The vec0 tables need sqlite-vec, but the `<vec>_rowids` shadow tables are plain
SQLite, and both code paths read only those. The tests create plain rowids tables
so they run without the extension.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import connect, init_db


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    db = tmp_path / "techne.db"
    init_db(db).close()
    c = Config()
    c.db_path = db
    c.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


def _rowids(cfg, table, ids=()):
    """A plain stand-in for a vec0 table's rowids shadow, holding `ids`."""
    conn = connect(cfg.db_path)
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table}_vec_rowids (rowid INTEGER PRIMARY KEY)")
        conn.executemany(f"INSERT INTO {table}_vec_rowids(rowid) VALUES (?)", [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()


def _observations(cfg, n, age_minutes=60):
    """`n` observations, backdated past `vectors:coverage`'s grace window by
    default so an unembedded one counts as a real gap (techne#704)."""
    from techne.core.state import add_observation, start_session

    sid = start_session(cfg.db_path, project_path="/tmp/p")
    for i in range(n):
        add_observation(cfg.db_path, sid, "Edit", f"edit {i}")
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE observations SET timestamp = datetime('now', ?)", (f"-{age_minutes} minutes",)
        )
        conn.commit()
        return [r[0] for r in conn.execute("SELECT id FROM observations ORDER BY id")]
    finally:
        conn.close()


# --- #942 -------------------------------------------------------------------


def test_empty_knowledge_does_not_print_a_percentage(cfg):
    from techne.core.checks import check_vector_coverage

    ids = _observations(cfg, 3)
    _rowids(cfg, "knowledge")
    _rowids(cfg, "observations", ids)
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert "0/0" not in res.detail and "100%" not in res.detail, res.detail
    assert "no live knowledge yet" in res.detail


def test_empty_knowledge_reports_what_is_embedded(cfg):
    """The operator's actual question: with no knowledge, is search healthy?
    The observations are the vector channel, so say how covered they are."""
    from techne.core.checks import Verdict, check_vector_coverage

    ids = _observations(cfg, 3)
    _rowids(cfg, "knowledge")
    _rowids(cfg, "observations", ids)
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert "observations 3/3 embedded" in res.detail, res.detail
    assert res.verdict is Verdict.LIVE, "fully embedded observations are a live channel"


def test_empty_knowledge_with_unembedded_observations_is_stale(cfg):
    from techne.core.checks import Verdict, check_vector_coverage

    ids = _observations(cfg, 3)
    _rowids(cfg, "knowledge")
    _rowids(cfg, "observations", ids[:1])
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert res.verdict is Verdict.STALE
    assert "observations 1/3 embedded" in res.detail, res.detail
    assert res.remedy == "techne vectors backfill"


def test_a_store_with_nothing_in_it_says_so(cfg):
    from techne.core.checks import check_vector_coverage

    for t in ("knowledge", "observations", "threads"):
        _rowids(cfg, t)
    res = check_vector_coverage(cfg)
    assert "nothing to embed yet" in res.detail, res.detail


def test_non_empty_knowledge_keeps_its_percentage(cfg):
    from techne.core.checks import check_vector_coverage
    from techne.core.knowledge import store

    kid = store(cfg.db_path, "a durable fact", type="fact")
    _rowids(cfg, "knowledge", [kid])
    _rowids(cfg, "observations")
    _rowids(cfg, "threads")
    res = check_vector_coverage(cfg)
    assert "1/1 live knowledge embedded and current (100%)" in res.detail, res.detail


# --- #704 -------------------------------------------------------------------


def _knowledge_complete(cfg):
    from techne.core.knowledge import store

    _rowids(cfg, "knowledge", [store(cfg.db_path, "a durable fact", type="fact")])


def test_a_seconds_old_unembedded_tail_is_pending_not_stale(cfg):
    """The remedy's own Bash call lands an unembedded row; that must not keep the
    check yellow after the remedy ran."""
    from techne.core.checks import Verdict, check_vector_coverage

    _knowledge_complete(cfg)
    _observations(cfg, 2, age_minutes=0)
    _rowids(cfg, "observations")
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert res.verdict is Verdict.LIVE, res.detail
    assert res.remedy is None
    assert "2 observations pending" in res.detail, "the tail stays visible"
    assert "unembedded" not in res.detail


def test_a_backlog_older_than_the_grace_window_is_still_stale(cfg):
    """The #814 incident (883 observations behind) must still alarm."""
    from techne.core.checks import Verdict, check_vector_coverage

    _knowledge_complete(cfg)
    _observations(cfg, 3, age_minutes=60)
    conn = connect(cfg.db_path)
    try:
        # One fresh row on top of the old backlog: only the old ones count.
        conn.execute("UPDATE observations SET timestamp = datetime('now') WHERE id = 3")
        conn.commit()
    finally:
        conn.close()
    _rowids(cfg, "observations")
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert res.verdict is Verdict.STALE, res.detail
    assert "2 observations unembedded" in res.detail, res.detail
    assert "1 observations pending" in res.detail, res.detail
    assert res.remedy == "techne vectors backfill"


def test_the_threads_lane_gets_the_same_grace(cfg):
    """A summary re-save re-inserts the thread row, so threads leave a tail too."""
    from techne.core.checks import Verdict, check_vector_coverage

    _knowledge_complete(cfg)
    _observations(cfg, 1)
    conn = connect(cfg.db_path)
    try:
        sid = conn.execute("SELECT session_id FROM observations").fetchone()[0]
        for tid, age in (("t-new", "-0 minutes"), ("t-old", "-60 minutes")):
            conn.execute(
                "INSERT INTO threads (id, session_id, thread_num, label, summary, updated_at) "
                "VALUES (?, ?, 1, 'l', 's', datetime('now', ?))",
                (tid, sid, age),
            )
        conn.commit()
        ids = [r[0] for r in conn.execute("SELECT id FROM observations")]
    finally:
        conn.close()
    _rowids(cfg, "observations", ids)
    _rowids(cfg, "threads")

    res = check_vector_coverage(cfg)
    assert res.verdict is Verdict.STALE, "the hour-old thread is a real gap"
    assert "1 threads unembedded" in res.detail, res.detail
    assert "1 threads pending" in res.detail, res.detail


# --- #948 -------------------------------------------------------------------


def test_status_counts_promoted_knowledge_with_no_vector(cfg):
    """The reported state: knowledge rows exist, knowledge_vec has none of them."""
    from techne.core.knowledge import store
    from techne.core.vector_status import vector_channel_status

    for i in range(3):
        store(cfg.db_path, f"promoted fact {i}", type="fact")
    _rowids(cfg, "knowledge")

    status = vector_channel_status(cfg.db_path, probe_backend_when_empty=False)
    assert status["unembedded"]["knowledge_vec"] == 3
    assert status["backfill_needed"] is True


def test_status_counts_the_population_backfill_embeds(cfg):
    """Stale rows count (backfill re-embeds them); superseded rows do not
    (backfill skips and purges them). Mirrors search.backfill_vectors."""
    from techne.core.knowledge import store
    from techne.core.vector_status import vector_channel_status

    fresh = store(cfg.db_path, "embedded and current", type="fact")
    stale = store(cfg.db_path, "embedded then edited", type="fact")
    old = store(cfg.db_path, "superseded, never embedded", type="fact")
    new = store(cfg.db_path, "its replacement, embedded", type="fact")
    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE knowledge SET vec_stale = 1 WHERE id = ?", (stale,))
        conn.execute("UPDATE knowledge SET superseded_by = ? WHERE id = ?", (new, old))
        conn.commit()
    finally:
        conn.close()
    _rowids(cfg, "knowledge", [fresh, stale, new])

    status = vector_channel_status(cfg.db_path, probe_backend_when_empty=False)
    assert status["unembedded"]["knowledge_vec"] == 1, "only the vec_stale row is owed"


def test_status_on_a_fresh_db_needs_no_backfill(cfg):
    from techne.core.vector_status import vector_channel_status

    status = vector_channel_status(cfg.db_path, probe_backend_when_empty=False)
    assert status["unembedded"] == {"threads_vec": 0, "observations_vec": 0, "knowledge_vec": 0}
    assert status["backfill_needed"] is False


def test_cli_prints_backfill_needed_separately_from_reembed(cfg):
    from techne.core.knowledge import store

    for i in range(2):
        store(cfg.db_path, f"promoted fact {i}", type="fact")

    result = CliRunner().invoke(main, ["vectors", "status"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "re-embed needed: False" in result.output
    assert "backfill needed: True" in result.output
    assert "knowledge_vec: absent, 2 not embedded" in result.output
    assert "run `techne vectors backfill` to embed them" in result.output


def test_cli_fresh_db_says_no_backfill_needed(cfg):
    result = CliRunner().invoke(main, ["vectors", "status"], catch_exceptions=False)
    assert "backfill needed: False" in result.output
    assert "not embedded" not in result.output
