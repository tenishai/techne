"""knowledge.delete against the rows that reference the entry.

`knowledge_deps` and `knowledge.superseded_by` both carry foreign keys to
`knowledge.id` with no ON DELETE clause, and every connection turns
`PRAGMA foreign_keys` on. A bare `DELETE FROM knowledge` therefore fails with
`FOREIGN KEY constraint failed` the moment an entry has a dependency edge or is
the replacement another entry points at. Live, 2026-09-24: an agent's seeder
tried to remove a retired seed entry that a dream-promote had drawn an edge to,
and the agent crash-looped at boot for two days on that one delete.

Deleting an entry has to take its edges with it and unlink the entries that
named it as their replacement, in the same transaction.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core.database import connect
from techne.core.knowledge import _add_dep_in_conn, delete, get, store, supersede


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


def test_delete_of_an_entry_nothing_references_is_unchanged(db):
    entry = store(db, "lonely", "fact")
    assert delete(db, entry) is True
    assert get(db, entry) is None
    assert delete(db, entry) is False


def test_foreign_keys_are_still_enforced_after_a_delete(db):
    """The fix is not "turn the pragma off": an edge to a missing entry must
    still be refused."""
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
