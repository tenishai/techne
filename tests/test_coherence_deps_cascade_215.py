"""End-to-end: dream promote populates dep edges → supersede trigger → uncertain.

techne#215 — Activate the dormant knowledge_deps dependency cascade.

Production path under test:
  store(src_a), store(src_b)
  → dream_ingest('connect') → pending dream candidate (target_ids=[src_a, src_b])
  → promote_candidate() → _apply_add writes dep edges from new insight → sources
  → supersede(src_a) → _cascade_uncertain(src_a) → insight goes uncertain

Design rationale (recorded here for reviewers):
  The dream pass is the clearest production insertion point for dep edges because
  the derivation relationship is *structurally explicit* — the dream verdict records
  exactly which source entries the new insight was synthesized from (target_ids).
  FTS/embedding similarity or tag overlap would be weak signals that risk spurious
  `uncertain` cascades; dream target_ids are deliberate and human-reviewed.
  No edges are written for reflect/consolidate add candidates: those have no
  structural source relationship recorded at promotion time.
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import store, supersede
from techne.core.reflect import promote_candidate
from techne.core.sleepdream import _cluster_hash, dream_ingest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "cascade215.db"
    init_db(cfg.db_path)
    return cfg


def _status(db, kid: int) -> str:
    conn = connect(db)
    try:
        return conn.execute(
            "SELECT status FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()[0]
    finally:
        conn.close()


def _dep_triggers(db, dependent_id: int) -> set[int]:
    """Return the set of trigger_ids for all dep edges on *dependent_id*."""
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT trigger_id FROM knowledge_deps WHERE dependent_id = ?",
            (dependent_id,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _promote_dream(config, src_ids: list[int], content: str = "Synthesised insight.") -> int:
    """Stage a dream 'connect' candidate from *src_ids* and immediately promote it.

    Returns the new knowledge.id — exercises the full production path:
    dream_ingest → pending candidate → promote_candidate → _apply_add → dep edges.
    """
    db = config.db_path
    verdict = json.dumps(
        {"verdict": "connect", "content": content, "type": "fact",
         "basis": "sources combined"}
    )
    gh = _cluster_hash(src_ids)
    counts = dream_ingest(config, src_ids, gh, verdict)
    assert counts["written"] == 1, f"dream_ingest must stage one candidate, got {counts}"

    conn = connect(db)
    try:
        cand_id = conn.execute(
            "SELECT id FROM knowledge_candidates "
            "WHERE origin='dream' AND status='pending' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    new_id = promote_candidate(db, cand_id)
    assert new_id is not None, "promote_candidate must return a knowledge.id for a knowledge scope candidate"
    return new_id


# ---------------------------------------------------------------------------
# Edge population — the new production insertion point (techne#215)
# ---------------------------------------------------------------------------


def test_dream_promote_writes_dep_edges(config):
    """Promoting a dream candidate must write derived_from edges from the new
    insight to each of its source entries (the target_ids). These edges are the
    production inputs the cascade needed.

    Before techne#215 this was the gap: promote_candidate called _apply_add, which
    created the knowledge row but wrote NO dep edges — leaving _cascade_uncertain
    with nothing to act on.
    """
    db = config.db_path
    src_a = store(db, "Server runs on port 8080", type="fact",
                  confidence="high", tags="infra:server")
    src_b = store(db, "Server uses TLS", type="fact",
                  confidence="high", tags="infra:server")

    new_id = _promote_dream(config, [src_a, src_b])

    triggers = _dep_triggers(db, new_id)
    assert src_a in triggers, "dep edge: insight → src_a must be written at promotion"
    assert src_b in triggers, "dep edge: insight → src_b must be written at promotion"
    assert len(triggers) == 2, "exactly two dep edges expected (one per source)"


def test_dep_edges_atomic_with_knowledge_row(config):
    """Dep edges must appear in the same commit as the knowledge row — readable
    immediately after promote_candidate returns, before any subsequent statement."""
    db = config.db_path
    src_a = store(db, "DB host is db01", type="fact", confidence="high", tags="infra:db")
    src_b = store(db, "DB uses postgres", type="fact", confidence="high", tags="infra:db")

    new_id = _promote_dream(config, [src_a, src_b])

    # Query in a fresh connection to confirm the commit is visible.
    assert _dep_triggers(db, new_id) == {src_a, src_b}


def test_dep_edge_kind_is_derived_from(config):
    """The edge kind must be 'derived_from' (the semantic that says
    'this insight was computed from that fact')."""
    db = config.db_path
    src_a = store(db, "Kind-check source A", type="fact", confidence="high", tags="t:kind")
    src_b = store(db, "Kind-check source B", type="fact", confidence="high", tags="t:kind")

    new_id = _promote_dream(config, [src_a, src_b])

    conn = connect(db)
    try:
        kinds = {
            r[0]
            for r in conn.execute(
                "SELECT kind FROM knowledge_deps WHERE dependent_id = ?", (new_id,)
            )
        }
    finally:
        conn.close()
    assert kinds == {"derived_from"}, f"expected kind='derived_from', got {kinds}"


# ---------------------------------------------------------------------------
# End-to-end cascade — the 'done when' from techne#215
# ---------------------------------------------------------------------------


def test_supersede_trigger_cascades_dream_insight_to_uncertain(config):
    """THE headline test from issue #215: superseding a source entry (trigger) must
    flip the derived dream insight to `uncertain` via the dep edge written at
    promotion — no hand-inserted edge, no test fixture, purely the production path.

    Before #215: promote wrote no edge → _cascade_uncertain had nothing to act on →
    the insight stayed 'current' even after the fact it rests on changed.
    After  #215: promote writes dep edges → cascade fires → insight goes uncertain.
    """
    db = config.db_path
    src_a = store(db, "Deploy target is server-01", type="fact",
                  confidence="high", tags="infra:deploy")
    src_b = store(db, "Deploy uses blue-green strategy", type="fact",
                  confidence="high", tags="infra:deploy")

    derived_id = _promote_dream(config, [src_a, src_b],
                                content="Blue-green deploys to server-01 enable zero-downtime.")

    assert _status(db, derived_id) == "current", "derived entry must start as current"

    # Supersede src_a — the fact the insight is built on.
    supersede(db, src_a, "Deploy target is server-02 (migration complete)")

    assert _status(db, derived_id) == "uncertain", (
        "superseding a source must cascade the dream-derived insight to uncertain "
        "through the production dep edge written at promote time"
    )


def test_cascade_only_flips_linked_dependent(config):
    """An entry with no dep edge to the superseded trigger must stay `current`.

    Verifies precision: only the derived insight (which has an edge) goes uncertain;
    a separate unrelated entry (no edge) is unaffected.
    """
    db = config.db_path
    src_a = store(db, "Trigger source A", type="fact", confidence="high", tags="t:cascade")
    src_b = store(db, "Trigger source B", type="fact", confidence="high", tags="t:cascade")
    unrelated = store(db, "Unrelated fact — no dep edge", type="fact")

    derived_id = _promote_dream(config, [src_a, src_b])

    supersede(db, src_a, "Trigger source A — revised")

    assert _status(db, derived_id) == "uncertain", "derived insight must go uncertain"
    assert _status(db, unrelated) == "current", (
        "unrelated entry (no dep edge) must be unaffected by the cascade"
    )


def test_retire_trigger_cascades_dream_insight_to_uncertain(config):
    """Retiring (not superseding) a source also fires _cascade_uncertain and must
    flip the derived insight to uncertain.

    _retire_in_conn already called _cascade_uncertain before #215 — the missing piece
    was the dep edge to act on. This confirms the retire path works end-to-end.
    """
    from techne.core.knowledge import _retire_in_conn

    db = config.db_path
    src_a = store(db, "Retire-trigger source A", type="fact",
                  confidence="high", tags="t:retire")
    src_b = store(db, "Retire-trigger source B", type="fact",
                  confidence="high", tags="t:retire")
    # Survivor: separate live entry that src_a is retired into.
    survivor = store(db, "Survivor fact (separate)", type="fact")

    derived_id = _promote_dream(config, [src_a, src_b])
    assert _status(db, derived_id) == "current"

    # Retire src_a into survivor (the retire path used by contradiction detection).
    conn = connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _retire_in_conn(conn, src_a, survivor)
        conn.commit()
    finally:
        conn.close()

    assert _status(db, derived_id) == "uncertain", (
        "retiring the trigger must cascade the dream-derived insight to uncertain "
        "via the dep edge written at promotion"
    )


# ---------------------------------------------------------------------------
# Safety: non-dream add candidates must NOT receive dep edges
# ---------------------------------------------------------------------------


def test_reflect_add_no_dep_edges(config):
    """A reflect-origin add candidate (no structural source relationship) must
    not receive any dep edges — the origin check must be strict."""
    db = config.db_path

    conn = connect(db)
    try:
        conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s-reflect-test')")
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, source_session_id, model_used,
                  prompt_hash, action, origin, status)
               VALUES ('knowledge', 'fact', 'A reflect-extracted fact',
                       's-reflect-test', 'test-model', 'hash-r1',
                       'add', 'reflect', 'pending')"""
        )
        cand_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    new_id = promote_candidate(db, cand_id)
    assert new_id is not None

    assert _dep_triggers(db, new_id) == set(), (
        "reflect-origin add candidate must not write dep edges — "
        "no structural source relationship is known"
    )


def test_consolidate_add_no_dep_edges(config):
    """A consolidate-origin add candidate (discarded marker or similar) must not
    receive dep edges either — the dream check must be origin-specific."""
    db = config.db_path

    conn = connect(db)
    try:
        conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s-consolidate-test')")
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, source_session_id, model_used,
                  prompt_hash, action, origin, status)
               VALUES ('knowledge', 'fact', 'A consolidate-origin add fact',
                       's-consolidate-test', 'test-model', 'hash-c1',
                       'add', 'consolidate', 'pending')"""
        )
        cand_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    new_id = promote_candidate(db, cand_id)
    assert new_id is not None

    assert _dep_triggers(db, new_id) == set(), (
        "consolidate-origin add candidate must not write dep edges"
    )
