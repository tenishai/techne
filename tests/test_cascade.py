"""Dependency-driven staleness — cascade-to-uncertain (techne#117).

A derived fact whose basis is superseded must stop reading as confidently current.
These tests exercise the Stage-1 core: the dependency edges, the cascade on
supersede(), and reconfirm(). The recall-time annotation/down-rank is Stage 2.
"""

from __future__ import annotations

import pytest

from techne.core.database import connect
from techne.core.knowledge import (
    UNCERTAIN_CONFIDENCE_PENALTY,
    _add_dep_in_conn,
    annotate_uncertain,
    archive_scan,
    cascade_uncertain,
    effective_confidence,
    mark_uncertain,
    reconfirm,
    store,
    supersede,
)


def _write_dep_edge(db, dependent_id: int, trigger_id: int, kind: str = "derived_from") -> None:
    """Test helper: write a dependency edge via _add_dep_in_conn inside a short-lived
    connection.  Mirrors what promote_candidate does on the dream-promote path but
    exposes a db_path interface for test setup convenience. (add_dependency was removed
    as dead production code in techne#251; tests that exercised its cascade behaviour
    are preserved here using this helper instead.)"""
    conn = connect(db)
    try:
        _add_dep_in_conn(conn, dependent_id, trigger_id, kind)
        conn.commit()
    finally:
        conn.close()


def _status(db, kid: int) -> str:
    conn = connect(db)
    try:
        return conn.execute("SELECT status FROM knowledge WHERE id = ?", (kid,)).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "k.db"


def test_new_knowledge_defaults_to_current(db):
    kid = store(db, "a plain fact", type="fact")
    assert _status(db, kid) == "current"


def test_supersede_cascades_dependents_to_uncertain(db):
    """The headline #117 behavior: superseding a trigger flips its live dependents
    to `uncertain` (their basis changed; reconfirm before trusting)."""
    trigger = store(db, "Richard's primary machine is dev04", type="fact")
    derived = store(db, "the canonical techne.db lives on dev04", type="fact")
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)

    assert _status(db, derived) == "current"
    supersede(db, trigger, "Richard's primary machine is dev05")
    assert _status(db, derived) == "uncertain"


def test_cascade_leaves_non_dependents_untouched(db):
    trigger = store(db, "trigger fact", type="fact")
    derived = store(db, "depends on trigger", type="fact")
    unrelated = store(db, "an unrelated fact", type="fact")
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)

    supersede(db, trigger, "trigger fact, revised")
    assert _status(db, derived) == "uncertain"
    assert _status(db, unrelated) == "current"  # no edge → untouched


def test_cascade_skips_already_superseded_dependent(db):
    """A dependent that is itself superseded is replaced, not uncertain — the
    cascade only touches live (superseded_by IS NULL) rows."""
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived v1", type="fact")
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)
    supersede(db, derived, "derived v2")  # derived is now superseded

    cascade_uncertain(db, trigger)
    assert _status(db, derived) == "current"  # superseded row not flipped


def test_reconfirm_clears_uncertain(db):
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived", type="fact")
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)
    supersede(db, trigger, "trigger, revised")
    assert _status(db, derived) == "uncertain"

    assert reconfirm(db, derived) is True
    assert _status(db, derived) == "current"
    assert reconfirm(db, derived) is False  # already current → no-op


def test_cascade_uncertain_returns_count_and_is_idempotent(db):
    trigger = store(db, "trigger", type="fact")
    d1 = store(db, "derived one", type="fact")
    d2 = store(db, "derived two", type="fact")
    _write_dep_edge(db, dependent_id=d1, trigger_id=trigger)
    _write_dep_edge(db, dependent_id=d2, trigger_id=trigger)

    assert cascade_uncertain(db, trigger) == 2  # both flipped
    assert cascade_uncertain(db, trigger) == 0  # already uncertain → no re-flip


def test_dep_self_edge_silently_not_written(db):
    """_add_dep_in_conn silently skips self-edges (no row written, no exception).
    The old public add_dependency() raised ValueError for self-edges; _add_dep_in_conn
    silently skips them to support bulk iteration without per-item raises (#215)."""
    kid = store(db, "a fact", type="fact")
    _write_dep_edge(db, dependent_id=kid, trigger_id=kid)  # must not raise
    conn = connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_deps WHERE dependent_id=? AND trigger_id=?",
            (kid, kid),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0, "self-edge must not be written"


def _make_uncertain(db, content="derived fact", confidence="medium"):
    """Store a derived fact, supersede its trigger, return the now-uncertain id."""
    trigger = store(db, "trigger fact", type="fact")
    derived = store(db, content, type="fact", confidence=confidence)
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)
    supersede(db, trigger, "trigger fact, revised")
    assert _status(db, derived) == "uncertain"
    return derived


# ---- Stage 2: the recall gate (annotate + down-rank) ----------------------


def test_annotate_uncertain_marks_uncertain_rows(db):
    """A recalled row whose status is uncertain gains an `uncertainty` payload —
    the recall-side mirror of #90's annotate_freshness."""
    kid = _make_uncertain(db)
    rows = [{"source_table": "knowledge", "id": kid, "content": "derived fact"}]
    annotate_uncertain(db, rows)
    assert "uncertainty" in rows[0]
    assert rows[0]["uncertainty"]["state"] == "uncertain"
    assert "reconfirm" in rows[0]["uncertainty"]["tag"]


def test_annotate_uncertain_leaves_current_rows_clean(db):
    kid = store(db, "a current fact", type="fact")
    rows = [{"source_table": "knowledge", "id": kid, "content": "a current fact"}]
    annotate_uncertain(db, rows)
    assert "uncertainty" not in rows[0]


def test_annotate_uncertain_ignores_non_knowledge_rows(db):
    """Observation/thread rows are never touched, even if an id collides."""
    kid = _make_uncertain(db)
    rows = [{"source_table": "observation", "id": kid, "content": "an observation"}]
    annotate_uncertain(db, rows)
    assert "uncertainty" not in rows[0]


def test_annotate_uncertain_reconfirm_clears_marker(db):
    kid = _make_uncertain(db)
    assert reconfirm(db, kid) is True
    rows = [{"source_table": "knowledge", "id": kid, "content": "derived fact"}]
    annotate_uncertain(db, rows)
    assert "uncertainty" not in rows[0]  # reconfirmed → current → no marker


def test_effective_confidence_penalizes_uncertain(db):
    """An uncertain row ranks below an otherwise-identical current row."""
    current = store(db, "solid fact", type="fact", confidence="high")
    uncertain = _make_uncertain(db, content="shaky fact", confidence="high")
    c_conf = effective_confidence(db, current, half_life_days=0)
    u_conf = effective_confidence(db, uncertain, half_life_days=0)
    assert u_conf == c_conf * UNCERTAIN_CONFIDENCE_PENALTY
    assert u_conf < c_conf


def test_effective_confidence_penalty_independent_of_decay(db):
    """The penalty is a trust discount, not decay — it still applies when decay
    is disabled (half_life_days=0)."""
    uncertain = _make_uncertain(db, content="shaky fact")
    base = effective_confidence(db, uncertain, half_life_days=0)
    # reconfirm clears uncertain; the same row should jump back to full weight.
    reconfirm(db, uncertain)
    restored = effective_confidence(db, uncertain, half_life_days=0)
    assert restored == base / UNCERTAIN_CONFIDENCE_PENALTY


def test_dep_edge_idempotent(db):
    """_add_dep_in_conn is idempotent — writing the same edge twice produces one row."""
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived", type="fact")
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)
    _write_dep_edge(db, dependent_id=derived, trigger_id=trigger)  # no-op, no error

    conn = connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_deps WHERE dependent_id=? AND trigger_id=?",
            (derived, trigger),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# --- the write side: a writer can say "unchecked" (cabal#141) ----------------


def test_mark_uncertain_flips_a_current_row(db):
    """Until now `uncertain` was only REACHABLE by the cascade — something was
    superseded, so its dependents became doubtful. A writer had no way to say
    "this is what I concluded and nothing has checked it", so agent-authored
    rows landed `current` and came back indistinguishable from curated ones."""
    kid = store(db, "something an agent concluded", type="fact")
    assert _status(db, kid) == "current"

    assert mark_uncertain(db, kid) is True
    assert _status(db, kid) == "uncertain"


def test_mark_uncertain_is_idempotent(db):
    kid = store(db, "a claim", type="fact")
    assert mark_uncertain(db, kid) is True
    assert mark_uncertain(db, kid) is False, "already uncertain → no-op, not an error"


def test_mark_uncertain_leaves_a_superseded_row_alone(db):
    """Superseded is a STRONGER statement than uncertain and must not be
    weakened into it — the same invariant the cascade already honours."""
    kid = store(db, "v1", type="fact")
    supersede(db, kid, "v2")

    assert mark_uncertain(db, kid) is False
    assert _status(db, kid) == "current", "superseded row must not be touched"


def test_mark_uncertain_and_reconfirm_are_inverses(db):
    """The pair is the whole verdict loop: an agent proposes (uncertain), a
    judge confirms (reconfirm). cabal#140."""
    kid = store(db, "proposed by an agent", type="fact")

    assert mark_uncertain(db, kid) is True
    assert _status(db, kid) == "uncertain"
    assert reconfirm(db, kid) is True
    assert _status(db, kid) == "current"


def test_mark_uncertain_leaves_an_archived_row_alone(db):
    """The other half of the guard, and the one the status column alone would not
    pin. `archive_scan` soft-archives a stale uncertain row; re-marking it must not
    quietly resurrect it into the live band. The mutant this uniquely kills is
    `status IN ('current','archived')` — measured: every other mark_uncertain test
    passes under it. (`status != 'superseded'` is NOT that mutant; "superseded" is
    never a status value, so it matches every row and the idempotence test kills it.)"""
    from datetime import datetime, timedelta

    kid = store(db, "an old claim nobody reaffirmed", type="fact")
    mark_uncertain(db, kid)
    archive_scan(db, now=datetime.now() + timedelta(days=3650))
    assert _status(db, kid) == "archived", "precondition: the row is archived"

    assert mark_uncertain(db, kid) is False
    assert _status(db, kid) == "archived", "archived row must not be touched"


def test_an_uncertain_row_is_not_recalled_by_default(db):
    """THE PROPERTY THAT MATTERS. Writing as uncertain is only useful because
    search excludes it unless a caller opts in (techne#501: filter from
    auto-injection, tag on explicit search)."""
    from techne.core.search import search

    kid = store(db, "zebra mnemonics for cabal dispatch", type="fact")
    assert [r for r in search(db, "zebra mnemonics", limit=5) if r["id"] == kid]

    mark_uncertain(db, kid)

    default = [r for r in search(db, "zebra mnemonics", limit=5) if r["id"] == kid]
    assert not default, "an unchecked claim must not come back as established"
    opted_in = [
        r
        for r in search(db, "zebra mnemonics", limit=5, include_uncertain=True)
        if r["id"] == kid
    ]
    assert opted_in, "but it must still be retrievable when asked for explicitly"
