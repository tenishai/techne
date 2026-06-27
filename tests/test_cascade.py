"""Dependency-driven staleness — cascade-to-uncertain (techne#117).

A derived fact whose basis is superseded must stop reading as confidently current.
These tests exercise the Stage-1 core: the dependency edges, the cascade on
supersede(), and reconfirm(). The recall-time annotation/down-rank is Stage 2.
"""

from __future__ import annotations

import pytest

from techne.core.database import connect
from techne.core.knowledge import (
    add_dependency,
    cascade_uncertain,
    reconfirm,
    store,
    supersede,
)


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
    add_dependency(db, dependent_id=derived, trigger_id=trigger)

    assert _status(db, derived) == "current"
    supersede(db, trigger, "Richard's primary machine is dev05")
    assert _status(db, derived) == "uncertain"


def test_cascade_leaves_non_dependents_untouched(db):
    trigger = store(db, "trigger fact", type="fact")
    derived = store(db, "depends on trigger", type="fact")
    unrelated = store(db, "an unrelated fact", type="fact")
    add_dependency(db, dependent_id=derived, trigger_id=trigger)

    supersede(db, trigger, "trigger fact, revised")
    assert _status(db, derived) == "uncertain"
    assert _status(db, unrelated) == "current"  # no edge → untouched


def test_cascade_skips_already_superseded_dependent(db):
    """A dependent that is itself superseded is replaced, not uncertain — the
    cascade only touches live (superseded_by IS NULL) rows."""
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived v1", type="fact")
    add_dependency(db, dependent_id=derived, trigger_id=trigger)
    supersede(db, derived, "derived v2")  # derived is now superseded

    cascade_uncertain(db, trigger)
    assert _status(db, derived) == "current"  # superseded row not flipped


def test_reconfirm_clears_uncertain(db):
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived", type="fact")
    add_dependency(db, dependent_id=derived, trigger_id=trigger)
    supersede(db, trigger, "trigger, revised")
    assert _status(db, derived) == "uncertain"

    assert reconfirm(db, derived) is True
    assert _status(db, derived) == "current"
    assert reconfirm(db, derived) is False  # already current → no-op


def test_cascade_uncertain_returns_count_and_is_idempotent(db):
    trigger = store(db, "trigger", type="fact")
    d1 = store(db, "derived one", type="fact")
    d2 = store(db, "derived two", type="fact")
    add_dependency(db, dependent_id=d1, trigger_id=trigger)
    add_dependency(db, dependent_id=d2, trigger_id=trigger)

    assert cascade_uncertain(db, trigger) == 2  # both flipped
    assert cascade_uncertain(db, trigger) == 0  # already uncertain → no re-flip


def test_add_dependency_rejects_self_edge(db):
    kid = store(db, "a fact", type="fact")
    with pytest.raises(ValueError, match="cannot depend on itself"):
        add_dependency(db, dependent_id=kid, trigger_id=kid)


def test_add_dependency_idempotent(db):
    trigger = store(db, "trigger", type="fact")
    derived = store(db, "derived", type="fact")
    add_dependency(db, dependent_id=derived, trigger_id=trigger)
    add_dependency(db, dependent_id=derived, trigger_id=trigger)  # no-op, no error

    conn = connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_deps WHERE dependent_id=? AND trigger_id=?",
            (derived, trigger),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
