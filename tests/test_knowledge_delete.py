"""knowledge.delete against the rows that reference the entry.

`knowledge_deps` and `knowledge.superseded_by` both carry foreign keys to
`knowledge.id` with no ON DELETE clause, and every connection turns
`PRAGMA foreign_keys` on. A bare `DELETE FROM knowledge` therefore fails with
`FOREIGN KEY constraint failed` the moment an entry has a dependency edge or is
the replacement another entry points at. Live, 2026-09-24: an agent's seeder
tried to remove a retired seed entry that a dream-promote had drawn an edge to,
and the agent crash-looped at boot for two days on that one delete.

Deleting an entry has to take its edges with it and unlink the entries that
named it as their replacement, in the same transaction — without resurrecting
what it replaced (review finding 1 on techne#1033: `superseded_by IS NULL` is
how the rest of techne recognises a live row, so nulling it on delete brought a
retired fact back as current) and without leaving a deleted trigger's
dependents looking more trustworthy than a supersede would have (review
finding 2: `_cascade_uncertain` fires on delete same as on supersede).
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core.database import connect
from techne.core.knowledge import _add_dep_in_conn, delete, get, list_entries, store, supersede
from techne.core.search import search


@pytest.fixture
def db(tmp_path):
    return tmp_path / "knowledge.db"


def _edge(db, dependent_id: int, trigger_id: int) -> None:
    conn = connect(db)
    try:
        _add_dep_in_conn(conn, dependent_id, trigger_id)
        conn.commit()
    finally:
        conn.close()


def _deps(db) -> list[tuple[int, int]]:
    conn = connect(db)
    try:
        rows = conn.execute("SELECT dependent_id, trigger_id FROM knowledge_deps").fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def _status(db, kid: int) -> str:
    conn = connect(db)
    try:
        return conn.execute("SELECT status FROM knowledge WHERE id = ?", (kid,)).fetchone()[0]
    finally:
        conn.close()


def test_delete_takes_the_entrys_dependency_edges_with_it(db):
    trigger = store(db, "the region is us-east", "fact", source="seed:facts.md")
    dependent = store(db, "so the bucket lives in us-east", "fact")
    _edge(db, dependent, trigger)

    assert delete(db, trigger) is True

    assert get(db, trigger) is None
    assert get(db, dependent) is not None
    assert _deps(db) == []


def test_delete_of_a_dependent_removes_only_its_own_edges(db):
    trigger = store(db, "alpha", "fact")
    dependent = store(db, "beta, from alpha", "fact")
    other = store(db, "gamma, from alpha", "fact")
    _edge(db, dependent, trigger)
    _edge(db, other, trigger)

    assert delete(db, dependent) is True

    assert _deps(db) == [(other, trigger)]
    assert get(db, trigger) is not None


def test_delete_of_a_replacement_unlinks_the_entry_it_replaced(db):
    old = store(db, "the port is 8080", "fact")
    new = supersede(db, old, "the port is 9090")
    assert get(db, old)["superseded_by"] == new

    assert delete(db, new) is True

    assert get(db, new) is None
    assert get(db, old)["superseded_by"] is None
    # Unlinking alone would make `old` look live again (superseded_by IS NULL is
    # the liveness test everywhere else); it must be archived, not resurrected.
    assert _status(db, old) == "archived"


def test_delete_of_a_replacement_does_not_resurrect_the_entry_it_replaced(db):
    """Review finding 1 on techne#1033: deleting B (which superseded A) must not
    bring A back as a currently-believed fact in the two places that matter —
    the default knowledge listing and the default recall search."""
    old = store(db, "the service port is 8080", "fact")
    new = supersede(db, old, "the service port is 9090")

    assert [e["id"] for e in list_entries(db, type="fact")] == [new]

    assert delete(db, new) is True

    ids = [e["id"] for e in list_entries(db, type="fact")]
    assert old not in ids
    assert new not in ids

    hits = [r["id"] for r in search(db, "service port", table="knowledge", include_archived=False)]
    assert old not in hits
    assert new not in hits


def test_delete_of_a_mid_chain_replacement_skips_it_for_the_predecessor(db):
    """A -> B -> C; deleting B (which has its own successor C) repoints A at C
    instead of archiving it — the chain just skips the deleted link."""
    a = store(db, "the port is 8080", "fact")
    b = supersede(db, a, "the port is 9090")
    c = supersede(db, b, "the port is 9091")

    assert delete(db, b) is True

    assert get(db, b) is None
    assert get(db, a)["superseded_by"] == c
    assert _status(db, a) == "current"  # untouched — it has a valid live successor
    assert [e["id"] for e in list_entries(db, type="fact")] == [c]


def test_delete_of_a_trigger_marks_its_dependents_uncertain(db):
    """Review finding 2 on techne#1033: deleting a trigger is a stronger
    retirement than superseding it, so its live dependents must be flipped to
    `uncertain` before the edge that recorded the relationship is dropped —
    same as `_cascade_uncertain` already does for supersede()."""
    trigger = store(db, "the region is us-east", "fact")
    dependent = store(db, "so the bucket lives in us-east", "fact")
    _edge(db, dependent, trigger)

    assert _status(db, dependent) == "current"

    assert delete(db, trigger) is True

    assert get(db, trigger) is None
    assert get(db, dependent) is not None
    assert _status(db, dependent) == "uncertain"
    assert _deps(db) == []


def test_delete_of_an_entry_nothing_references_is_unchanged(db):
    entry = store(db, "lonely", "fact")
    assert delete(db, entry) is True
    assert get(db, entry) is None
    assert delete(db, entry) is False


def test_foreign_keys_are_still_enforced_after_a_delete(db):
    """`connect()` still turns `PRAGMA foreign_keys` on after a delete — this
    checks a fresh connection's enforcement, not delete()'s own connection
    (the pragma is per-connection and delete() has already closed by the time
    this runs). The other tests above exercise delete()'s own connection and
    would catch a `PRAGMA foreign_keys=OFF` regression there."""
    entry = store(db, "alpha", "fact")
    delete(db, entry)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_deps (dependent_id, trigger_id) VALUES (?, ?)",
                (entry, entry + 1),
            )
    finally:
        conn.close()
