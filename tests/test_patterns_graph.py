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
    get_pattern,
    ingest_patterns_md,
    list_patterns,
    patterns_for_target,
    related_patterns,
    retire_legacy_pattern_knowledge,
    supersede,
    tag_pattern,
    topic_list,
    topic_show,
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


def test_apply_targets_live_heading_form():
    # The live corpus folds the target into the heading bold span.
    body = "**Where to apply in techne**: gate vec0.\n**Where to apply in cabal**: route."
    assert _apply_targets(body) == ["techne", "cabal"]


def test_apply_targets_ignores_prose_bold_false_positives():
    # Bold prose NOT on a where-to-apply line must not manufacture an apply
    # (Prudence, PR#130). `**built in techne**` is a description, not a target.
    assert _apply_targets("This was **built in techne** last year.") == []
    # An unknown token inside a real heading is dropped.
    assert _apply_targets("**Where to apply in widgets**: nope.") == []


def test_apply_targets_prose_after_colon_not_extracted():
    # Honest under-reach: a target mentioned only in the prose AFTER the bold
    # heading (e.g. a comparison) is not turned into an apply.
    assert _apply_targets("**Where to apply in techne**: similar to limenal's gate.") == [
        "techne"
    ]


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
        "Uses [[architectural-patterns#Beta]].\n\n**Where to apply in techne**: gate.",
    )
    b = _mk(db, "Beta", "Refs [[Nonexistent]].\n\n**Where to apply in limenal**: handle.")
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
    _mk(db, "Alpha", "[[Beta]] **Where to apply in techne**: x")
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


def test_migrate_v1_to_v2_adds_topic_absorbed(tmp_path):
    # A v1-shape db: new-shape relations, but patterns predates the topic-index
    # columns (techne#308). Stamp user_version=1 so only the v1→v2 step runs.
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        """CREATE TABLE patterns (
               id TEXT PRIMARY KEY, name TEXT, source_url TEXT, body TEXT,
               status TEXT DEFAULT 'active', superseded_by TEXT);
           CREATE TABLE pattern_relations (
               src_pattern_id TEXT NOT NULL, dst_name TEXT NOT NULL,
               dst_pattern_id TEXT, kind TEXT NOT NULL DEFAULT 'related',
               resolution_status TEXT NOT NULL DEFAULT 'unresolved',
               PRIMARY KEY (src_pattern_id, dst_name, kind));"""
    )
    conn.execute(
        "INSERT INTO patterns (id, name, source_url, body) VALUES ('p1', 'Alpha', 'http://x/a', 'body')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)")}
        assert {"topic", "absorbed"} <= cols
        # Existing data survives the additive migration (new columns default NULL).
        row = conn.execute("SELECT name, topic, absorbed FROM patterns WHERE id='p1'").fetchone()
        assert (row["name"], row["topic"], row["absorbed"]) == ("Alpha", None, None)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()

    # Idempotent: a second reconcile on the now-current db is a no-op.
    architect._reconcile_relations(db)
    conn = plain_connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


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
        "Uses [[architectural-patterns#Beta]].\n\n**Where to apply in techne**: gate.\n\n---\n\n"
        "## Beta\n\n- **Source**: http://x/b\n- **Surveyed**: 2026-06-26\n\n"
        "A second pattern.\n\n**Where to apply in limenal**: handle.\n\n---\n"
    )
    result = ingest_patterns_md(db, md)
    assert "graph" in result
    assert result["graph"]["resolved"] == 1
    assert result["graph"]["applies"] == 2
    assert [p["name"] for p in patterns_for_target(db, "techne")] == ["Alpha"]


# --- supersession + topic index (techne#308) --------------------------------


def test_supersede_stamps_columns_and_writes_edge(db):
    old = _mk(db, "OldWay", "the old approach")
    new = _mk(db, "NewWay", "the better approach")
    assert supersede(db, old_id=old, new_id=new, absorbed="kept the retry-budget idea") is True

    o = get_pattern(db, old)
    assert o["status"] == "superseded"
    assert o["superseded_by"] == new
    assert o["absorbed"] == "kept the retry-budget idea"

    # A resolved `supersedes` edge (old → new) now exists for the graph view.
    edge = related_patterns(db, old)["outbound"][0]
    assert edge["kind"] == "supersedes"
    assert edge["dst_pattern_id"] == new
    assert edge["resolution_status"] == "resolved"

    # Superseded patterns drop out of active-only listings.
    assert [p["name"] for p in list_patterns(db)] == ["NewWay"]

    # Idempotent: re-superseding keeps the prior absorbed note (not re-supplied).
    assert supersede(db, old_id=old, new_id=new) is True
    assert get_pattern(db, old)["absorbed"] == "kept the retry-budget idea"


def test_supersede_rejects_self_and_unknown(db):
    a = _mk(db, "Alpha", "x")
    with pytest.raises(ValueError):
        supersede(db, old_id=a, new_id=a)
    with pytest.raises(ValueError):
        supersede(db, old_id=a, new_id="nope")
    with pytest.raises(ValueError):
        supersede(db, old_id="nope", new_id=a)


def test_tag_pattern_sets_topic_and_routes_supersede(db):
    a = _mk(db, "Alpha", "x")
    b = _mk(db, "Beta", "y")
    assert tag_pattern(db, a, topic="policy-enforcement") is True
    assert get_pattern(db, a)["topic"] == "policy-enforcement"

    # superseded_by routes through the atomic supersede AND applies topic.
    assert tag_pattern(db, a, topic="policy-enforcement", superseded_by=b) is True
    ga = get_pattern(db, a)
    assert ga["status"] == "superseded" and ga["superseded_by"] == b
    assert ga["topic"] == "policy-enforcement"

    with pytest.raises(ValueError):
        tag_pattern(db, "ghost", topic="x")


def test_topic_list_and_show_fold_superseded(db):
    a = _mk(db, "Active-A", "aa")
    b = _mk(db, "Active-B", "bb")
    old = _mk(db, "Old-A", "old")
    _mk(db, "Ungrouped", "no topic yet")

    tag_pattern(db, a, topic="retrieval")
    tag_pattern(db, b, topic="retrieval")
    tag_pattern(db, old, topic="retrieval")
    supersede(db, old_id=old, new_id=a, absorbed="folded the caching trick in")

    # Index: retrieval has 2 active (a, b); one untopiced bucket. Superseded excluded.
    idx = {r["topic"]: r["count"] for r in topic_list(db)}
    assert idx == {"retrieval": 2, "(untopiced)": 1}

    # Read-path: Old-A folds under Active-A with its salvage note.
    shown = {p["name"]: p for p in topic_show(db, "retrieval")}
    assert set(shown) == {"Active-A", "Active-B"}
    folded = shown["Active-A"]["superseded"]
    assert [s["name"] for s in folded] == ["Old-A"]
    assert folded[0]["absorbed"] == "folded the caching trick in"
    assert shown["Active-B"]["superseded"] == []

    # The untopiced bucket is addressable.
    assert [p["name"] for p in topic_show(db, "(untopiced)")] == ["Ungrouped"]


# --- typed relation fields (techne#308) -------------------------------------


def test_parse_extracts_typed_relation_fields():
    from techne.import_md import parse

    md = (
        "## New Way\n\n"
        "- **Source**: http://x/new\n"
        "- **Surveyed**: 2026-07-11\n"
        "- **Composes-with**: [[Base Pattern]], [[architectural-patterns#Other]]\n"
        "- **Variant-of**: [[Base Pattern]]\n\n"
        "The body prose. Not a [[Relation]] here — body links stay in the body.\n"
    )
    p = parse(md).patterns[0]
    assert p.relations == (
        ("composes-with", "Base Pattern"),
        ("composes-with", "Other"),
        ("variant-of", "Base Pattern"),
    )
    # Relation bullets are metadata, stripped from the stored body.
    assert "Composes-with" not in p.body
    assert p.body.startswith("The body prose.")


def test_ingest_writes_typed_relation_edges(db):
    md = (
        "## Base Pattern\n\n- **Source**: http://x/base\n- **Surveyed**: 2026-07-11\n\n"
        "The base.\n\n---\n\n"
        "## New Way\n\n- **Source**: http://x/new\n- **Surveyed**: 2026-07-11\n"
        "- **Composes-with**: [[Base Pattern]]\n- **Variant-of**: [[Base Pattern]]\n\n"
        "The refinement.\n\n---\n"
    )
    ingest_patterns_md(db, md)
    new = search_by_name(db, "New Way")
    kinds = {
        e["kind"]: e for e in related_patterns(db, new)["outbound"]
    }
    assert "composes-with" in kinds and "variant-of" in kinds
    assert kinds["composes-with"]["resolution_status"] == "resolved"
    assert kinds["composes-with"]["dst_pattern_name"] == "Base Pattern"


def search_by_name(db, name):
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        return conn.execute("SELECT id FROM patterns WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


# --- legacy retirement ------------------------------------------------------


def test_retire_legacy_pattern_knowledge(tmp_path):
    from techne.core.database import connect
    from techne.core.knowledge import store

    techne_db = tmp_path / "techne.db"
    store(techne_db, "a real fact", type="fact")
    # Legacy type='pattern' rows are inserted directly — store() rejects the type
    # post techne#137; retirement targets rows that predate that boundary.
    conn = connect(techne_db)
    try:
        conn.execute("INSERT INTO knowledge (type, content) VALUES ('pattern', 'a lifted pattern')")
        conn.execute(
            "INSERT INTO knowledge (type, content) VALUES ('pattern', 'another lifted pattern')"
        )
        conn.commit()
    finally:
        conn.close()

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
