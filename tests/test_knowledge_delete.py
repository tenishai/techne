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

from techne.config import Config
from techne.core.conflict import conflict_check_emit, select_conflict_candidates
from techne.core.database import connect, init_db
from techne.core.knowledge import (
    _add_dep_in_conn,
    delete,
    get,
    list_entries,
    merge_into,
    store,
    supersede,
)
from techne.core.knowledge_export import query_live_knowledge
from techne.core.search import search
from techne.core.sleepdream import consolidate_clusters, recombine_groups
from techne.core.state import get_resume_context


@pytest.fixture
def db(tmp_path):
    return tmp_path / "knowledge.db"


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "config.db"
    init_db(cfg.db_path)
    return cfg


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


def test_delete_of_a_replacement_does_not_resurrect_through_resume_context(db):
    """Review round 2 on techne#1033 (Prudence's probe): an archived predecessor
    must not render under "Durable facts:" via get_resume_context — the reader
    that feeds techne_resume/techne context and actually injects into a client's
    system prompt. confidence='high' matches get_resume_context's own filter and
    Prudence's repro."""
    old = store(db, "the service port is 8080", "fact", confidence="high")
    new = supersede(db, old, "the service port is 9090")

    assert [k["content"] for k in get_resume_context(db)["knowledge"]] == [
        "the service port is 9090"
    ]

    assert delete(db, new) is True

    contents = [k["content"] for k in get_resume_context(db)["knowledge"]]
    assert "the service port is 8080" not in contents
    assert "the service port is 9090" not in contents


def test_delete_of_a_replacement_labels_predecessor_archived_in_export(db):
    """query_live_knowledge (knowledge_export) is deliberately left status-blind
    in its default query: it returns `status` on every row, so a consumer can
    filter for itself, matching how explicit search() surfaces archived rows.
    Pin that: the predecessor is present but visibly `archived`, not silently
    indistinguishable from a `current` fact."""
    old = store(db, "the service port is 8080", "fact")
    new = supersede(db, old, "the service port is 9090")

    assert delete(db, new) is True

    rows = {r["id"]: r for r in query_live_knowledge(db)}
    assert rows[old]["status"] == "archived"
    assert new not in rows


def test_merge_into_refuses_an_archived_entry(db):
    """merge_into's live-row guard must treat an archived row the same as an
    already-superseded one: minting a merged `current` row from an archived
    predecessor's content would resurrect it under a new id."""
    old = store(db, "the service port is 8080", "fact")
    new = supersede(db, old, "the service port is 9090")
    other = store(db, "an unrelated fact", "fact")

    assert delete(db, new) is True

    with pytest.raises(ValueError, match="archived"):
        merge_into(db, [old, other], "merged content", type="fact", confidence="medium")


def test_delete_of_a_replacement_excludes_predecessor_from_consolidate_clusters(config):
    """consolidate_clusters' Tier-0 exact-duplicate bucket must not pull an
    archived predecessor in alongside a coincidentally identical live row."""
    old = store(config.db_path, "duplicate text", "fact")
    new = supersede(config.db_path, old, "something else")
    assert delete(config.db_path, new) is True
    store(config.db_path, "duplicate text", "fact")  # unrelated live row, same content

    clustered_ids = {i for c in consolidate_clusters(config) for i in c["target_ids"]}
    assert old not in clustered_ids


def test_delete_of_a_replacement_excludes_predecessor_from_recombine_groups(config):
    old = store(config.db_path, "fact one", "fact", confidence="high", tags="shared")
    new = supersede(config.db_path, old, "fact one revised")
    assert delete(config.db_path, new) is True
    store(config.db_path, "fact two", "fact", confidence="high", tags="shared")
    store(config.db_path, "fact three", "fact", confidence="high", tags="shared")

    grouped_ids = {i for g in recombine_groups(config) for i in g["group_ids"]}
    assert old not in grouped_ids


def test_delete_of_a_replacement_excludes_predecessor_from_conflict_candidates(db):
    old = store(db, "we use postgres for storage", "fact", tags="datastore")
    new = supersede(db, old, "we switched from postgres to sqlite")
    assert delete(db, new) is True
    focus = store(db, "postgres is our datastore of record", "fact", tags="datastore")

    candidates = select_conflict_candidates(db, {"id": focus, "type": "fact", "tags": "datastore"})
    assert old not in {c["id"] for c in candidates}


def test_delete_of_a_replacement_excludes_predecessor_from_conflict_check_emit(config):
    old = store(config.db_path, "we use postgres for storage", "fact", tags="datastore")
    new = supersede(config.db_path, old, "we switched from postgres to sqlite")
    assert delete(config.db_path, new) is True
    store(config.db_path, "postgres is our datastore of record", "fact", tags="datastore")

    items = conflict_check_emit(config)
    touched = {i["focus_id"] for i in items} | {c for i in items for c in i["comparison_ids"]}
    assert old not in touched


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
