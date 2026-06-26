"""Tests for the P2 pattern graph: relations + applies backfill, resolution,
queries, the pattern_relations shape migration, and legacy-row retirement (techne#87 P2)."""

from __future__ import annotations

import pytest

from techne.core import architect
from techne.core.architect import ARCHITECT_SCHEMA
from techne.core.patterns import (
    _apply_targets,
    _norm,
    _wikilink_targets,
    add_apply,
    add_pattern,
    add_relation,
    backfill_graph,
    ingest_patterns_md,
    patterns_for_target,
    related_patterns,
    retire_legacy_pattern_knowledge,
    upsert_investigation,
)
from techne.core.stores import SqliteStore, plain_connect


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def _mk(db, name, body, url=None):
    url = url or f"http://x/{name}"
    inv = upsert_investigation(db, url=url, surveyed_at="2026-06-26", verdict="full-eval")
    pid, _ = add_pattern(db, investigation_id=inv, name=name, source_url=url, body=body)
    return pid


# --- parsers ----------------------------------------------------------------


def test_wikilink_targets_strips_file_prefix_and_dedups():
    body = (
        "Refs [[architectural-patterns#Hybrid rank fusion]] and [[Bare Name]] and "
        "again [[architectural-patterns#Hybrid rank fusion]]."
    )
    assert _wikilink_targets(body) == ["Hybrid rank fusion", "Bare Name"]


def test_wikilink_targets_empty():
    assert _wikilink_targets("no links here") == []
    assert _wikilink_targets("") == []


def test_apply_targets_canonical_only():
    body = "**in techne**: do x. **In Limenal**: do y. **in widgets**: ignore me."
    assert _apply_targets(body) == ["techne", "limenal"]


def test_apply_targets_live_heading_form():
    # The live corpus folds the target into the heading bold span.
    body = "**Where to apply in techne**: gate vec0.\n**Where to apply in cabal**: route."
    assert _apply_targets(body) == ["techne", "cabal"]


def test_norm_collapses_noise():
    assert _norm("BM25 + vec!!") == "bm25 vec"
    assert _norm("Hybrid  rank-fusion") == "hybrid rank fusion"


# --- add_relation -----------------------------------------------------------


def test_add_relation_resolves_and_unresolves(db):
    a = _mk(db, "Alpha", "x")
    b = _mk(db, "Beta", "y")
    dst, resolved = add_relation(db, src_pattern_id=a, dst_name="Beta")
    assert (dst, resolved) == (b, True)
    dst2, resolved2 = add_relation(db, src_pattern_id=a, dst_name="Ghost")
    assert dst2 is None and resolved2 is False


def test_add_relation_self_edge_dropped(db):
    a = _mk(db, "Alpha", "x")
    add_relation(db, src_pattern_id=a, dst_name="Alpha")
    # No self-edge persisted.
    assert related_patterns(db, a) == {"outbound": [], "inbound": []}


def test_add_relation_idempotent_upgrade(db):
    a = _mk(db, "Alpha", "see ghost")
    add_relation(db, src_pattern_id=a, dst_name="Beta")  # unresolved
    out = related_patterns(db, a)["outbound"][0]
    assert out["resolution_status"] == "unresolved"
    _mk(db, "Beta", "y")  # target appears
    add_relation(db, src_pattern_id=a, dst_name="Beta")  # re-run upgrades
    out = related_patterns(db, a)["outbound"][0]
    assert out["resolution_status"] == "resolved"
    # Still exactly one edge (upsert, not duplicate).
    conn = plain_connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM pattern_relations WHERE src_pattern_id=?", (a,))
        assert n.fetchone()[0] == 1
    finally:
        conn.close()


# --- add_apply --------------------------------------------------------------


def test_add_apply_idempotent(db):
    a = _mk(db, "Alpha", "x")
    assert add_apply(db, pattern_id=a, target="techne") is True
    assert add_apply(db, pattern_id=a, target="techne") is False
    assert [p["name"] for p in patterns_for_target(db, "techne")] == ["Alpha"]


# --- backfill_graph ---------------------------------------------------------


def test_backfill_graph_counts_and_queries(db):
    a = _mk(
        db,
        "Alpha",
        "Uses [[architectural-patterns#Beta]].\n\n**in techne**: gate.",
    )
    b = _mk(db, "Beta", "Refs [[Nonexistent]].\n\n**in limenal**: handle.")
    res = backfill_graph(db)
    assert res == {"relations": 2, "resolved": 1, "unresolved": 1, "applies": 2}

    ra = related_patterns(db, a)
    assert ra["outbound"][0]["dst_pattern_id"] == b
    assert ra["inbound"] == []

    rb = related_patterns(db, b)
    assert rb["outbound"][0]["resolution_status"] == "unresolved"
    assert rb["outbound"][0]["dst_name"] == "Nonexistent"
    assert rb["inbound"][0]["src_pattern_id"] == a

    assert [p["name"] for p in patterns_for_target(db, "techne")] == ["Alpha"]
    assert [p["name"] for p in patterns_for_target(db, "limenal")] == ["Beta"]


def test_backfill_graph_idempotent(db):
    _mk(db, "Alpha", "[[Beta]] **in techne**: x")
    _mk(db, "Beta", "y")
    first = backfill_graph(db)
    second = backfill_graph(db)
    assert first == second  # re-run produces identical counts, no duplicate rows


# --- migration --------------------------------------------------------------


def test_reconcile_relations_old_to_new(tmp_path):
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        """CREATE TABLE patterns (id TEXT PRIMARY KEY, name TEXT, body TEXT, status TEXT);
           CREATE TABLE pattern_relations (
               src_pattern_id TEXT, dst_pattern_id TEXT, kind TEXT NOT NULL,
               PRIMARY KEY (src_pattern_id, dst_pattern_id, kind));"""
    )
    conn.commit()
    conn.close()

    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pattern_relations)")}
    finally:
        conn.close()
    assert {"dst_name", "resolution_status"} <= cols

    # Idempotent: a second run on the now-new shape is a no-op.
    architect._reconcile_relations(db)


def test_reconcile_relations_noop_on_fresh_schema(db):
    # A db built from the current ARCHITECT_SCHEMA is already new-shape.
    before = {r[1] for r in plain_connect(db).execute("PRAGMA table_info(pattern_relations)")}
    architect._reconcile_relations(db)
    after = {r[1] for r in plain_connect(db).execute("PRAGMA table_info(pattern_relations)")}
    assert before == after == {
        "src_pattern_id",
        "dst_name",
        "dst_pattern_id",
        "kind",
        "resolution_status",
    }


# --- ingest populates the graph ---------------------------------------------


def test_ingest_md_populates_graph(db):
    md = (
        "## Alpha\n\n- **Source**: http://x/a\n- **Surveyed**: 2026-06-26\n\n"
        "Uses [[architectural-patterns#Beta]].\n\n**in techne**: gate.\n\n---\n\n"
        "## Beta\n\n- **Source**: http://x/b\n- **Surveyed**: 2026-06-26\n\n"
        "A second pattern.\n\n**in limenal**: handle.\n\n---\n"
    )
    result = ingest_patterns_md(db, md)
    assert "graph" in result
    assert result["graph"]["resolved"] == 1
    assert result["graph"]["applies"] == 2
    assert [p["name"] for p in patterns_for_target(db, "techne")] == ["Alpha"]


# --- legacy retirement ------------------------------------------------------


def test_retire_legacy_pattern_knowledge(tmp_path):
    from techne.core.database import connect
    from techne.core.knowledge import store

    techne_db = tmp_path / "techne.db"
    store(techne_db, "a real fact", type="fact")
    store(techne_db, "a lifted pattern", type="pattern")
    store(techne_db, "another lifted pattern", type="pattern")

    def _count(typ):
        conn = connect(techne_db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE type = ?", (typ,)
            ).fetchone()[0]
        finally:
            conn.close()

    # Dry run reports but does not delete.
    assert retire_legacy_pattern_knowledge(techne_db, dry_run=True) == 2
    assert _count("pattern") == 2

    # Real run deletes only the pattern rows, leaving the fact intact.
    assert retire_legacy_pattern_knowledge(techne_db) == 2
    assert _count("pattern") == 0
    assert _count("fact") == 1

    # Idempotent: nothing left to delete.
    assert retire_legacy_pattern_knowledge(techne_db) == 0
