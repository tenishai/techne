"""The ingest tools refuse ids no emit could have produced (techne#1257).

consolidate_ingest, dream_ingest and conflict_check_ingest take their ids from the
caller. Before this, a merge over ids that did not exist, over one id, or over a
superseded row each became a pending candidate, and a bogus conflict group wrote a
permanent discarded marker. Each refusal must write nothing at all.
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.conflict import conflict_check_ingest
from techne.core.database import connect, init_db
from techne.core.knowledge import store, supersede
from techne.core.sleepdream import consolidate_ingest, dream_ingest, invalid_target_ids

MERGE = json.dumps({"verdict": "merge", "content": "merged", "type": "fact", "confidence": "high"})
KEEP = json.dumps({"verdict": "keep"})
CONNECT = json.dumps({"verdict": "connect", "content": "c", "type": "fact", "basis": "b"})


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "ingest.db"
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def rows(config):
    db = config.db_path
    a = store(db, "live fact a", type="fact", confidence="high")
    b = store(db, "live fact b", type="fact", confidence="high")
    old = store(db, "old fact", type="fact", confidence="high")
    supersede(db, old, "newer fact")
    archived = store(db, "archived fact", type="fact", confidence="high")
    conn = connect(db)
    conn.execute("UPDATE knowledge SET status='archived' WHERE id=?", (archived,))
    conn.commit()
    conn.close()
    return {"a": a, "b": b, "old": old, "archived": archived}


def _candidates(config) -> list[tuple]:
    conn = connect(config.db_path)
    try:
        sql = "SELECT origin, status, target_ids FROM knowledge_candidates"
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def _bad_sets(rows) -> dict[str, list]:
    a, b = rows["a"], rows["b"]
    return {
        "nonexistent": [9991, 9992],
        "one id": [a],
        "duplicate": [a, a],
        "superseded": [a, rows["old"]],
        "archived": [a, rows["archived"]],
        "not an int": [a, str(b)],
        "bool": [a, True],
    }


@pytest.mark.parametrize(
    "case", ["nonexistent", "one id", "duplicate", "superseded", "archived", "not an int", "bool"]
)
@pytest.mark.parametrize("response", [MERGE, KEEP], ids=["merge", "keep"])
def test_consolidate_refuses_and_writes_nothing(config, rows, case, response):
    counts = consolidate_ingest(config, _bad_sets(rows)[case], "advisory", response)
    assert counts["invalid"] == 1 and counts["reason"], counts
    assert counts["proposed"] == counts["kept"] == 0
    assert _candidates(config) == []


@pytest.mark.parametrize("case", ["nonexistent", "one id", "superseded", "archived"])
def test_dream_refuses_and_writes_nothing(config, rows, case):
    counts = dream_ingest(config, _bad_sets(rows)[case], "advisory", CONNECT)
    assert counts["invalid"] == 1, counts
    assert _candidates(config) == []


def test_conflict_refuses_a_bogus_focus_and_bogus_comparisons(config, rows):
    supersede_b = json.dumps([{"id": rows["b"], "verdict": "SUPERSEDE", "reason": "r"}])
    counts = conflict_check_ingest(config, 9995, [rows["b"]], "advisory", supersede_b)
    assert counts["invalid"] == 1 and counts["supersede"] == 0, counts
    counts = conflict_check_ingest(config, rows["a"], [9996, 9997], "advisory", "[]")
    assert counts["invalid"] == 1, counts
    assert _candidates(config) == []


def test_a_refusal_leaves_the_valid_group_open(config, rows):
    """No marker is written, so the same ids, once valid, are still judged."""
    a, b = rows["a"], rows["b"]
    consolidate_ingest(config, [a, b, 9999], "advisory", MERGE)
    counts = consolidate_ingest(config, [a, b], "advisory", MERGE)
    assert counts["proposed"] == 1 and counts["invalid"] == 0
    assert [c[:2] for c in _candidates(config)] == [("consolidate", "pending")]


def test_valid_ids_pass(config, rows):
    conn = connect(config.db_path)
    try:
        assert invalid_target_ids(conn, [rows["a"], rows["b"]]) is None
    finally:
        conn.close()
