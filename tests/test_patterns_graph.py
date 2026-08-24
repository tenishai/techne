"""Tests for the P2 pattern graph: relations + applies backfill, resolution,
queries, the pattern_relations shape migration, and legacy-row retirement (techne#87 P2)."""

from __future__ import annotations

import pytest

from techne.core import architect
from techne.core.architect import ARCHITECT_SCHEMA, ARCHITECT_VERSION
from techne.core.patterns import (
    _apply_targets,
    _norm,
    _wikilink_targets,
    add_apply,
    add_pattern,
    add_relation,
    backfill_graph,
    current_as_of,
    get_pattern,
    graph_data,
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
    assert _apply_targets("**Where to apply in techne**: similar to limenal's gate.") == ["techne"]


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
    # techne#674 — THIS ASSERTION CHANGED DELIBERATELY. Beta's body carries the
    # pre-rename spelling (`apply in limenal`), which now canonicalises at
    # extraction, so it lands under the CURRENT project name. Querying the old
    # spelling returns nothing, which is the point: before this, backfill wrote
    # `limenal` while the direct-tag path wrote `liminal` and 11 live patterns
    # sat under both targets for one project.
    assert [p["name"] for p in patterns_for_target(db, "liminal")] == ["Beta"]
    assert list(patterns_for_target(db, "limenal")) == []


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION
    finally:
        conn.close()

    # Idempotent: a second reconcile on the now-current db is a no-op.
    architect._reconcile_relations(db)
    conn = plain_connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION
    finally:
        conn.close()


def test_schema_apply_then_reconcile_on_existing_v1_db(tmp_path):
    # Reproduces the REAL architect_store() entry path on a PRE-EXISTING v1 db:
    # migrated_store() executescripts ARCHITECT_SCHEMA (schema-apply) BEFORE the
    # numbered migration adds topic/absorbed — so NOTHING in the base schema may
    # reference those columns (e.g. an index on `topic`), or the apply crashes on
    # a db where `CREATE TABLE IF NOT EXISTS` is a no-op. (Regression: an index on
    # `topic` in the base schema broke live-db upgrades though fresh-db tests passed.)
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    # The real pre-#308 (v1) patterns table: every v1 column EXCEPT topic/absorbed.
    conn.executescript(
        """CREATE TABLE patterns (
               id TEXT PRIMARY KEY, investigation_id TEXT, name TEXT NOT NULL,
               source_url TEXT NOT NULL, source_path TEXT, lens_kind TEXT, body TEXT NOT NULL,
               confidence TEXT DEFAULT 'medium', status TEXT DEFAULT 'active',
               superseded_by TEXT,
               created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
           CREATE TABLE pattern_relations (
               src_pattern_id TEXT NOT NULL, dst_name TEXT NOT NULL, dst_pattern_id TEXT,
               kind TEXT NOT NULL DEFAULT 'related',
               resolution_status TEXT NOT NULL DEFAULT 'unresolved',
               PRIMARY KEY (src_pattern_id, dst_name, kind));"""
    )
    conn.execute(
        "INSERT INTO patterns (id, name, source_url, body) VALUES ('p1','A','http://x','b')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Schema-apply (what migrated_store does on every open) must NOT raise here...
    SqliteStore("architect", db).migrate(ARCHITECT_SCHEMA)
    # ...then the numbered migration adds the columns + the topic index.
    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)")}
        assert {"topic", "absorbed"} <= cols
        idx = {r[1] for r in conn.execute("PRAGMA index_list('patterns')")}
        assert "idx_patterns_topic" in idx
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION
    finally:
        conn.close()


def test_reconcile_relations_noop_on_fresh_schema(db):
    # A db built from the current ARCHITECT_SCHEMA is already new-shape.
    before = {r[1] for r in plain_connect(db).execute("PRAGMA table_info(pattern_relations)")}
    architect._reconcile_relations(db)
    after = {r[1] for r in plain_connect(db).execute("PRAGMA table_info(pattern_relations)")}
    assert (
        before
        == after
        == {
            "src_pattern_id",
            "dst_name",
            "dst_pattern_id",
            "kind",
            "resolution_status",
        }
    )


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
    kinds = {e["kind"]: e for e in related_patterns(db, new)["outbound"]}
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


# --- validity-windowed supersession (techne#306) ----------------------------


def test_migrate_v2_to_v3_adds_validity_window(tmp_path):
    # Real architect_store() path on an EXISTING v2 db (has topic/absorbed, not the
    # window cols): schema-apply THEN reconcile — the #315 lesson, applied up front.
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        """CREATE TABLE patterns (
               id TEXT PRIMARY KEY, investigation_id TEXT, name TEXT NOT NULL,
               source_url TEXT NOT NULL, source_path TEXT, lens_kind TEXT, body TEXT NOT NULL,
               confidence TEXT DEFAULT 'medium', status TEXT DEFAULT 'active',
               superseded_by TEXT, topic TEXT, absorbed TEXT,
               created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
           CREATE INDEX idx_patterns_topic ON patterns(topic);
           CREATE TABLE pattern_relations (
               src_pattern_id TEXT NOT NULL, dst_name TEXT NOT NULL, dst_pattern_id TEXT,
               kind TEXT NOT NULL DEFAULT 'related',
               resolution_status TEXT NOT NULL DEFAULT 'unresolved',
               PRIMARY KEY (src_pattern_id, dst_name, kind));"""
    )
    conn.execute(
        "INSERT INTO patterns (id, name, source_url, body, topic) VALUES ('p1','A','http://x','b','t')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    SqliteStore("architect", db).migrate(ARCHITECT_SCHEMA)  # schema-apply must not raise
    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)")}
        assert {"valid_at", "invalid_at"} <= cols
        # Assert the CONSTANT, not a literal — this line hardcoded `3` and broke on
        # the v4 bump (techne#674) while the three sibling assertions in this file
        # already tracked ARCHITECT_VERSION. A version stamp that must be
        # hand-edited on every ladder step is a test that fails for the wrong reason.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION
        row = conn.execute(
            "SELECT topic, valid_at, invalid_at FROM patterns WHERE id='p1'"
        ).fetchone()
        assert (row["topic"], row["valid_at"], row["invalid_at"]) == ("t", None, None)
    finally:
        conn.close()


def test_supersede_stamps_validity_window(db):
    old = _mk(db, "Old", "o")
    new = _mk(db, "New", "n")
    supersede(db, old_id=old, new_id=new, invalid_at="2026-06-15 12:00:00")
    assert get_pattern(db, old)["invalid_at"] == "2026-06-15 12:00:00"
    assert get_pattern(db, new)["valid_at"] == "2026-06-15 12:00:00"
    # Idempotent: re-superseding keeps the original window (COALESCE).
    supersede(db, old_id=old, new_id=new, invalid_at="2026-07-01 00:00:00")
    assert get_pattern(db, old)["invalid_at"] == "2026-06-15 12:00:00"
    assert get_pattern(db, new)["valid_at"] == "2026-06-15 12:00:00"


def test_current_as_of_window(db):
    old = _mk(db, "Old", "o")
    new = _mk(db, "New", "n")
    # Pin created_at so the window math is deterministic.
    conn = plain_connect(db)
    with conn:
        conn.execute(
            "UPDATE patterns SET created_at='2026-01-01 00:00:00' WHERE id IN (?, ?)", (old, new)
        )
    conn.close()
    supersede(db, old_id=old, new_id=new, invalid_at="2026-06-01 00:00:00")

    # Before the handoff: Old was current, New not yet.
    assert {p["name"] for p in current_as_of(db, "2026-03-01 00:00:00")} == {"Old"}
    # After: New is current, Old retired.
    assert {p["name"] for p in current_as_of(db, "2026-09-01 00:00:00")} == {"New"}
    # Before either existed: neither.
    assert current_as_of(db, "2025-12-01 00:00:00") == []


# --- graph export (techne#305) ----------------------------------------------


def test_graph_data_full_and_topic_scoped(db):
    a = _mk(db, "A", "x")
    b = _mk(db, "B", "y")
    c = _mk(db, "C", "z")
    tag_pattern(db, a, topic="retrieval")
    tag_pattern(db, b, topic="retrieval")
    tag_pattern(db, c, topic="policy")
    add_relation(db, src_pattern_id=a, dst_name="B", kind="composes-with")
    add_relation(db, src_pattern_id=a, dst_name="C", kind="related")  # cross-topic

    full = graph_data(db)
    assert {n["name"] for n in full["nodes"]} == {"A", "B", "C"}
    assert {e["kind"] for e in full["edges"]} == {"composes-with", "related"}

    # Topic scope: only retrieval nodes, and the cross-topic edge to C is dropped
    # (both endpoints must be in the node set — no dangling edges).
    ret = graph_data(db, topic="retrieval")
    assert {n["name"] for n in ret["nodes"]} == {"A", "B"}
    assert len(ret["edges"]) == 1 and ret["edges"][0]["kind"] == "composes-with"


def test_graph_data_superseded_included_unresolved_excluded(db):
    a = _mk(db, "A", "x")
    old = _mk(db, "Old", "o")
    add_relation(db, src_pattern_id=a, dst_name="Ghost", kind="related")  # unresolved
    supersede(db, old_id=old, new_id=a)  # resolved supersedes edge old→a; Old goes superseded

    data = graph_data(db)
    statuses = {n["name"]: n["status"] for n in data["nodes"]}
    assert statuses == {"A": "active", "Old": "superseded"}
    # Only the resolved supersedes edge; the dangling 'Ghost' edge is excluded.
    assert [e["kind"] for e in data["edges"]] == ["supersedes"]


def test_render_mermaid_structure():
    from techne.core.pattern_graph import render_mermaid

    data = {
        "nodes": [
            {"id": "1a", "name": "Alpha", "topic": "t1", "status": "active"},
            {"id": "2b", "name": "Old", "topic": "t1", "status": "superseded"},
        ],
        "edges": [
            {"src": "2b", "dst": "1a", "kind": "supersedes"},
            {"src": "1a", "dst": "2b", "kind": "composes-with"},
        ],
    }
    out = render_mermaid(data)
    assert out.startswith("flowchart LR")
    assert 'subgraph t0["t1"]' in out
    assert 'n1a["Alpha"]' in out
    assert "n2b -.->|supersedes| n1a" in out  # supersedes reads dashed
    assert "n1a -->|composes-with| n2b" in out
    assert "classDef superseded" in out and "class n2b superseded" in out


def test_render_dot_structure():
    from techne.core.pattern_graph import render_dot

    data = {
        "nodes": [
            {"id": "1a", "name": "Alpha", "topic": None, "status": "active"},
            {"id": "2b", "name": "Old", "topic": None, "status": "superseded"},
        ],
        "edges": [{"src": "1a", "dst": "2b", "kind": "variant-of"}],
    }
    out = render_dot(data)
    assert out.startswith("digraph patterns {")
    assert 'label="(untopiced)"' in out
    assert '"n1a" [label="Alpha"]' in out
    assert 'style="dashed"' in out  # superseded node de-emphasized
    assert '"n1a" -> "n2b" [label="variant-of", color="seagreen"]' in out


def test_render_escapes_quotes():
    from techne.core.pattern_graph import render_dot, render_mermaid

    data = {
        "nodes": [{"id": "1", "name": 'Quote " here', "topic": "t", "status": "active"}],
        "edges": [],
    }
    assert "&quot;" in render_mermaid(data)
    assert '\\"' in render_dot(data)


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
            return conn.execute("SELECT COUNT(*) FROM knowledge WHERE type = ?", (typ,)).fetchone()[
                0
            ]
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


# --- techne#674: the applies graph could not see new patterns -----------------
#
# Three causes, all measured on the live store before the fix: the skill's
# template emitted an unparseable form, `_KNOWN_TARGETS` still held the
# pre-rename `limenal` and not `liminal`, and real projects named in the
# investigate-url context block (Prudence chief among them) were absent from the
# set entirely. These pin the second and third; the template is documentation.


def test_apply_targets_accepts_the_projects_the_skill_actually_names():
    """14 pattern bodies named Prudence and none could link (techne#674).

    Every name here appears in the /investigate-url context block as a real
    "where might this apply" target, so a pattern naming one must resolve.
    """
    for target in ("prudence", "daedalus", "kaminos", "argus", "ladon", "agora"):
        body = f"**Where to apply in {target}**: the reviewer's own audit surface."
        assert _apply_targets(body) == [target], target


def test_apply_targets_canonicalises_the_pre_rename_spelling():
    """`limenal` -> `liminal` (renamed 2026-08-11).

    THE BUG THIS PINS: the set held only the misspelling, so backfill emitted
    `limenal` while the direct-tag path wrote `liminal` — 11 patterns ended up
    double-listed under two targets for one project. Canonicalising at
    extraction is what stops one project occupying two rows.
    """
    assert _apply_targets("**Where to apply in limenal**: gate it.") == ["liminal"]
    assert _apply_targets("**Where to apply in liminal**: gate it.") == ["liminal"]


def test_apply_targets_dedupes_across_the_alias():
    """Both spellings in one body collapse to a single target, not two."""
    body = (
        "**Where to apply in liminal**: the dispatch chokepoint.\n"
        "**Where to apply in limenal**: the same gateway, older spelling."
    )
    assert _apply_targets(body) == ["liminal"]


def test_widening_the_target_set_did_not_weaken_the_pr130_guard():
    """The under-reach is DELIBERATE — do not loosen `_APPLY_RE` to fix coverage.

    Prudence caught these in PR#130. Adding targets must not let bold prose or a
    trailing comparison manufacture an apply row.
    """
    assert _apply_targets("This was **built in techne** last year.") == []
    assert _apply_targets("**Where to apply in widgets**: still unknown.") == []
    assert _apply_targets("**Where to apply in techne**: similar to liminal's gate.") == ["techne"]


# ---------------------------------------------------------------------------
# techne#430 — source_url was absent from the FTS index
# ---------------------------------------------------------------------------


def _seed_pattern(db, *, pid, name, body, source_url):
    from techne.core.architect import ARCHITECT_SCHEMA

    conn = plain_connect(db)
    try:
        conn.executescript(ARCHITECT_SCHEMA)
        conn.execute(
            "INSERT INTO patterns (id, name, body, source_url, status, created_at) "
            "VALUES (?,?,?,?,'active',datetime('now'))",
            (pid, name, body, source_url),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_pattern_is_findable_by_its_source_url(tmp_path):
    """techne#430. `patterns_fts` indexed only (name, body), so `architect search
    "<url>"` — the dedup step investigate-url runs BEFORE spending a subagent — could
    not structurally match a pattern by its source and returned a confident "No
    patterns matched". An already-surveyed repo got re-investigated, and the caller was
    told the corpus had nothing."""
    from techne.core.patterns import search_patterns

    db = tmp_path / "architect.db"
    _seed_pattern(
        db,
        pid="p-430",
        name="Wholly unrelated title",
        body="Body text that shares no term with the URL.",
        source_url="https://github.com/DemoDevelops/lens",
    )

    assert search_patterns(db, "https://github.com/DemoDevelops/lens"), (
        "a full URL must find the pattern it sourced"
    )
    assert search_patterns(db, "DemoDevelops/lens"), "the owner/repo slug must too"
    assert not search_patterns(db, "github.com/NotTheOwner/other"), (
        "and an unsurveyed URL must still find nothing — otherwise the dedup check "
        "trades a false negative for a false positive"
    )


def test_the_migration_backfills_the_index_rather_than_emptying_it(tmp_path):
    """The v5->v6 step DROPs and recreates an external-content fts5 table, which cannot
    gain a column in place. Recreating alone leaves it EMPTY on an existing store —
    turning a silent false negative into a total one — so `rebuild` is load-bearing."""
    from techne.core import architect

    db = tmp_path / "architect.db"
    _seed_pattern(
        db, pid="p-mig", name="n", body="b", source_url="https://example.invalid/a/b"
    )
    conn = plain_connect(db)
    try:  # force the store back to the pre-#430 version so the step re-runs
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    finally:
        conn.close()

    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM patterns_fts").fetchone()[0] == 1, (
            "the rebuild must repopulate the index, not leave it empty"
        )
        assert "source_url" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='patterns_fts'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_the_step_skips_a_store_whose_patterns_table_predates_source_url(tmp_path):
    """The ladder runs from wherever the store is, including v0. An external-content
    fts5 table resolves its columns against the content table at REBUILD time, so a
    missing `source_url` fails with `no such column: T.source_url` — the step guards on
    column presence rather than assuming the ladder already added it."""
    from techne.core import architect

    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        "CREATE TABLE patterns (id TEXT PRIMARY KEY, name TEXT, body TEXT, status TEXT);"
        "CREATE TABLE pattern_relations (src_pattern_id TEXT, dst_pattern_id TEXT,"
        " kind TEXT NOT NULL, PRIMARY KEY (src_pattern_id, dst_pattern_id, kind));"
    )
    conn.commit()
    conn.close()

    architect._reconcile_relations(db)  # must not raise
