"""Candidate-model foundation: action dispatch + decision-provenance (techne#108 PR1).

PR1 is a zero-behavior-change refactor — the existing reflect promote/discard tests
(test_reflect.py) still pass byte-for-byte. These tests cover the new surface: the
action dispatch (including the legacy NULL → 'add' route), the unknown-action guard,
and the decided_by/decision_reason audit trail.
"""

from __future__ import annotations

import pytest

from techne.core.database import connect, init_db
from techne.core.reflect import (
    _mark_promoted,
    discard_candidate,
    promote_candidate,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "c.db"
    init_db(p).close()
    conn = connect(p)
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.commit()
    conn.close()
    return p


def _insert(db, *, scope="knowledge", type="fact", content="a fact",
            action="__default__", target_ids=None, status="pending"):
    """Insert a candidate. action='__default__' omits the column (exercises the DDL
    default); pass action=None to force a legacy NULL row."""
    conn = connect(db)
    try:
        if action == "__default__":
            cur = conn.execute(
                """INSERT INTO knowledge_candidates
                     (scope, type, content, source_session_id, model_used, prompt_hash, status)
                   VALUES (?, ?, ?, 's1', 'm', 'h', ?)""",
                (scope, type, content, status),
            )
        else:
            cur = conn.execute(
                """INSERT INTO knowledge_candidates
                     (scope, type, content, source_session_id, model_used, prompt_hash,
                      status, action, target_ids)
                   VALUES (?, ?, ?, 's1', 'm', 'h', ?, ?, ?)""",
                (scope, type, content, status, action, target_ids),
            )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _get(db, cid):
    conn = connect(db)
    try:
        return dict(conn.execute(
            "SELECT * FROM knowledge_candidates WHERE id=?", (cid,)
        ).fetchone())
    finally:
        conn.close()


def _knowledge_count(db):
    conn = connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    finally:
        conn.close()


def test_default_action_is_add(db):
    cid = _insert(db)  # column omitted → DDL default
    assert _get(db, cid)["action"] == "add"
    new_id = promote_candidate(db, cid)
    assert new_id is not None and _knowledge_count(db) == 1


def test_action_column_cannot_be_null(db):
    """The protective invariant: `action` is NOT NULL DEFAULT 'add', so the reconciler
    backfills pre-#108 rows to 'add' and no NULL can ever be written. promote's
    COALESCE(action,'add') is therefore belt-and-suspenders, never load-bearing."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, action=None)


def test_unknown_action_raises_and_leaves_pending(db):
    cid = _insert(db, action="teleport")
    with pytest.raises(ValueError, match="unknown action"):
        promote_candidate(db, cid)
    # The guard fires before the status flip — the row is untouched, no knowledge written.
    assert _get(db, cid)["status"] == "pending"
    assert _knowledge_count(db) == 0


def test_promote_records_provenance(db):
    cid = _insert(db)
    promote_candidate(db, cid, decided_by="human", decision_reason="still true")
    row = _get(db, cid)
    assert row["decided_by"] == "human"
    assert row["decision_reason"] == "still true"
    assert row["status"] == "promoted"


def test_promote_provenance_defaults_null(db):
    cid = _insert(db)
    promote_candidate(db, cid)  # no provenance args
    row = _get(db, cid)
    assert row["decided_by"] is None and row["decision_reason"] is None


def test_discard_records_provenance(db):
    cid = _insert(db)
    discard_candidate(db, cid, decided_by="agent-triage", decision_reason="duplicate")
    row = _get(db, cid)
    assert row["status"] == "discarded"
    assert row["decided_by"] == "agent-triage"
    assert row["decision_reason"] == "duplicate"


def test_mark_promoted_stamps_reflect_auto(db):
    """The write_to='live' auto-path records reflect-auto — no human reviewed it."""
    cid = _insert(db)
    _mark_promoted(db, cid, promoted_to_id=42)
    row = _get(db, cid)
    assert row["status"] == "promoted"
    assert row["promoted_to_id"] == 42
    assert row["decided_by"] == "reflect-auto"
