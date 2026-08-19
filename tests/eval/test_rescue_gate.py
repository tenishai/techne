"""Rescue@10 — does a supersede actually reach the RANKER? (techne#583)

WRITE-SIDE SUPERSESSION AND READ-SIDE RANKING ARE SEPARATE SUBSYSTEMS, AND ONLY
THE FIRST WAS VERIFIED. techne supersedes in three places — the consolidate rail,
`techne architect tag --superseded-by`, and knowledge revision — and every one is
tested for marking the row correctly. None was tested for what the operator
actually experiences: after a fact is superseded, is the SURVIVOR retrievable for
the PREDECESSOR's own cue?

A store can mark a row superseded, emit a clean audit trail, satisfy every unit
test, and still lose the memory — because nothing joined the write path to the
ranker.

TECHNE'S VARIANT OF THE MEASUREMENT DIFFERS FROM THE PUBLISHED ONE, and the
difference matters. `Rescue@10` as published asks whether the updated fact
outranks the stale one. techne hides superseded rows from recall entirely
(`superseded_by IS NULL` on both channels), so the predecessor CANNOT outrank —
it is not returned at all. The live risk here is the other failure: the survivor
being unfindable by the old cue, which loses the memory rather than staling it.
Both assertions are made below; the second is the one with teeth here.

Run explicitly (excluded from the default suite via addopts):
    pytest -m eval_gate -s

Determinism: `use_vectors=False` and a pinned `half_life_days`, matching
test_retrieval_gate.py's convention — the vector backend is python-version
dependent, which would make this non-reproducible across environments.
"""

from __future__ import annotations

import pytest

from techne.core.database import init_db
from techne.core.knowledge import store, supersede
from techne.core.search import search

pytestmark = pytest.mark.eval_gate

_HALF_LIFE_DAYS = 30.0


def _rank_of(rows: list[dict], target_id: int) -> int | None:
    for i, r in enumerate(rows, start=1):
        if r.get("id") == target_id:
            return i
    return None


def _search(db, cue: str) -> list[dict]:
    return search(
        db, cue, table="knowledge", limit=10, use_vectors=False, half_life_days=_HALF_LIFE_DAYS
    )


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "rescue.db"
    init_db(p).close()
    return p


def test_the_survivor_is_retrievable_for_the_predecessors_cue(db):
    """THE CORE MEASUREMENT. Seed → supersede → re-query the ORIGINAL cue."""
    old_id = store(db, "The deploy gate runs on quimby before every release", type="fact")
    new_id = supersede(db, old_id, "The deploy gate runs on quimby after every release")

    rows = _search(db, "deploy gate quimby")

    assert _rank_of(rows, new_id) is not None, (
        "the survivor is not retrievable at all for the predecessor's own cue — "
        "the supersede was recorded and the memory was lost (techne#583)"
    )


def test_the_predecessor_does_not_come_back(db):
    """techne's stronger guarantee: a superseded row is hidden from recall
    entirely, so it cannot outrank its survivor the way the published benchmark
    measures. Pinned because that hiding is what makes the assertion above the
    meaningful one."""
    old_id = store(db, "The deploy gate runs on quimby before every release", type="fact")
    supersede(db, old_id, "The deploy gate runs on quimby after every release")

    rows = _search(db, "deploy gate quimby")

    assert _rank_of(rows, old_id) is None, (
        "a superseded row surfaced in default recall — the stale fact is being "
        "served alongside the one that replaced it"
    )


def test_a_supersede_chain_resolves_to_the_final_survivor(db):
    """Chains are the case a naive check gets wrong, and I got it wrong first.

    Querying the predecessor's cue and expecting its IMMEDIATE successor fails on
    a chain, because that successor is itself superseded and therefore hidden.
    Only the final survivor is live. Measured on the live store: 7 of 16
    superseded rows form a single chain (461→462→464→465→466→468→469→470).
    """
    a = store(db, "The nightly job is owned by the platform team", type="fact")
    b = supersede(db, a, "The nightly job is owned by the infra team")
    c = supersede(db, b, "The nightly job is owned by the release team")

    rows = _search(db, "nightly job owned team")

    assert _rank_of(rows, c) is not None, "the final survivor must be retrievable"
    for dead in (a, b):
        assert _rank_of(rows, dead) is None, (
            "an intermediate link in a supersede chain surfaced — only the final survivor is live"
        )


def test_a_rewrite_that_drops_the_cue_terms_is_NOT_rescued(db):
    """THE FAILURE THIS GATE EXISTS TO CATCH, pinned as a known limit rather than
    asserted away.

    `supersede` inherits type, tags and confidence but the CONTENT is a fresh
    write — so a survivor that rewrites rather than edits can drop the very terms
    the old cue was made of. Measured on the live store 2026-08-18: survivor #470
    contains none of its predecessor's distinctive terms (`replaceability`,
    `consolidation`, `observability`), because project-state cards are rewritten
    wholesale on each refresh.

    This test asserts the CURRENT behaviour so a future change that fixes it —
    carrying predecessor terms into the FTS index, or an anti-hit surfacing the
    old row beside the new (pattern c1dcb26a, deliberately out of scope here) —
    breaks this test loudly and gets to decide deliberately.
    """
    old_id = store(db, "Quimby handles replaceability scoring for observations", type="fact")
    new_id = supersede(db, old_id, "Zorp handles deduplication for threads")

    rows = _search(db, "replaceability scoring observations")

    assert _rank_of(rows, new_id) is None, (
        "a survivor that shares no terms with the predecessor's cue became "
        "retrievable — that is an IMPROVEMENT over the behaviour measured in "
        "techne#583. Update this gate deliberately rather than deleting it."
    )
    assert _rank_of(rows, old_id) is None, "and the predecessor stays hidden"
