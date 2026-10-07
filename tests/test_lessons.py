"""Agent self-lessons (techne#1195): lesson / grant / charter with lesson_meta."""

from __future__ import annotations

import json

import pytest

from techne.core import knowledge, lessons
from techne.core.database import connect, init_db

GRIP_EXCERPT = """# Personal Observations
Meta-cognition, working patterns, and behavioral lessons.

## Core Principles (always loaded)
- **Never assert a negative or a count you didn't just run.** Before saying
  "none" or citing a number, run the exact check. (week 0) **The check must
  match the scope of the claim** (week 4). **Validate the probe itself** (week 6).
- **Subagent verdicts are candidates, not conclusions.** The main thread is
  the skeptic.

## Standing Permissions
- (operator-granted autonomy, e.g. "may merge doc-only changes after self-review")

## 2026-08-14 — a display filter manufactured a false negative
Checking a colleague's cost calculation, I piped a sorted query through
`tail` and lost the top rows, the largest items. Same family as probe-validation.
New habit: head/tail/grep on tool output are part of the probe.

_Graduated 2026-09-01: 11 entries -> 8 new principles + 1 refined._

## Scratch
- not a lesson section
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


def _log_band(db, session_id, ids):
    from techne.hooks._injection_log import log_context_injection

    log_context_injection(
        db,
        source=lessons.INJECTION_SOURCE,
        session_id=session_id,
        trigger=None,
        rows=[{"id": i} for i in ids],
        injected_text="x",
    )


# ---- write paths -------------------------------------------------------------


def test_plain_store_refuses_lesson_types(db):
    for t in ("lesson", "grant", "charter"):
        with pytest.raises(ValueError, match="techne lesson"):
            knowledge.store(db, "rule", type=t)
    assert "lesson" not in knowledge.GENERIC_TYPES
    assert set(knowledge.LESSON_TYPES) <= set(knowledge.VALID_TYPES)


def test_entry_links_only_to_a_live_principle(db):
    p = lessons.add(db, rule="Check before asserting.", tier="principle")
    e = lessons.add(db, rule="Said none without reading.", tier="entry", parent_id=p)
    with pytest.raises(ValueError, match="not a principle"):
        lessons.add(db, rule="x", tier="entry", parent_id=e)
    with pytest.raises(ValueError, match="only an entry"):
        lessons.add(db, rule="x", tier="principle", parent_id=p)
    [row] = lessons.list_lessons(db, tier="principle")
    assert row["recurrences"] == 1


def test_refine_carries_meta_and_family(db):
    p = lessons.add(db, rule="Old rule.", tier="principle", body="why it exists")
    e = lessons.add(db, rule="incident", tier="entry", parent_id=p)
    new = lessons.refine(db, p, "New one-line rule.")
    assert knowledge.get(db, p)["superseded_by"] == new
    rows = {r["id"]: r for r in lessons.list_lessons(db)}
    assert rows[new]["rule"] == "New one-line rule."
    assert rows[new]["tier"] == "principle"
    assert rows[new]["recurrences"] == 1
    assert rows[e]["parent_id"] == new
    assert knowledge.get(db, new)["content"] == "New one-line rule.\n\nwhy it exists"


def test_an_entry_is_not_refined(db):
    e = lessons.add(db, rule="incident", tier="entry")
    with pytest.raises(ValueError, match="only a principle"):
        lessons.refine(db, e, "x")


def test_grant_and_charter_are_operator_only(db):
    g = lessons.add_operator(db, kind="grant", rule="May merge doc-only PRs.")
    c = lessons.add_operator(db, kind="charter", rule="Identity lives in ~/.claude.")
    for kid in (g, c):
        with pytest.raises(ValueError, match="only the operator"):
            lessons.refine(db, kid, "widened")
        with pytest.raises(ValueError, match="only the operator"):
            lessons.set_enforced_by(db, kid, "an agent wrote this")
        with pytest.raises(ValueError, match="not a lesson"):
            lessons.reaffirm(db, kid)
        conn = connect(db)
        try:
            with pytest.raises(ValueError, match="only the operator"):
                knowledge._supersede_in_conn(conn, kid, "x", "reflect")
            survivor = knowledge.store(db, "survivor")
            with pytest.raises(ValueError, match="only the operator"):
                knowledge._retire_in_conn(conn, kid, survivor)
        finally:
            conn.close()
    assert lessons.refine(db, g, "May merge doc-only PRs after self-review.", operator=True)
    lessons.set_enforced_by(db, c, "operator hook", operator=True)
    conn = connect(db)
    try:
        row = conn.execute("SELECT enforced_by FROM lesson_meta WHERE knowledge_id = ?", (c,))
        assert row.fetchone()["enforced_by"] == "operator hook"
    finally:
        conn.close()


def test_reaffirm_touches_only_live_lessons(db):
    p = lessons.add(db, rule="Verify before asserting a negative.", tier="principle")
    conn = connect(db)
    try:
        conn.execute("UPDATE knowledge SET last_confirmed_at = '2000-01-01 00:00:00' WHERE id = ?", (p,))
        conn.commit()
    finally:
        conn.close()
    lessons.reaffirm(db, p)
    conn = connect(db)
    try:
        seen = conn.execute("SELECT last_confirmed_at FROM knowledge WHERE id = ?", (p,)).fetchone()
        assert seen["last_confirmed_at"] > "2000-01-01 00:00:00"
    finally:
        conn.close()
    newer = lessons.refine(db, p, "Verify before asserting any negative.")
    with pytest.raises(ValueError, match="superseded by"):
        lessons.reaffirm(db, p)
    lessons.reaffirm(db, newer)
    with pytest.raises(ValueError, match="not a lesson"):
        lessons.reaffirm(db, knowledge.store(db, "a plain fact"))


def test_merge_refuses_lessons(db):
    a = lessons.add(db, rule="a", tier="principle")
    b = lessons.add(db, rule="b", tier="principle")
    with pytest.raises(ValueError, match="techne lesson refine"):
        knowledge.merge_into(db, [a, b], "ab", type="fact", confidence="high")
    with pytest.raises(ValueError, match="invalid type"):
        knowledge.merge_into(
            db,
            [knowledge.store(db, "x"), knowledge.store(db, "y")],
            "xy",
            type="lesson",
            confidence="high",
        )


def test_delete_drops_meta_and_unlinks_family(db):
    p = lessons.add(db, rule="p", tier="principle")
    e = lessons.add(db, rule="e", tier="entry", parent_id=p)
    assert knowledge.delete(db, p)
    [row] = lessons.list_lessons(db)
    assert row["id"] == e and row["parent_id"] is None


def test_retire_stops_injection_and_keeps_the_row(db):
    p = lessons.add(db, rule="Retire me.", tier="principle")
    lessons.retire(db, p)
    assert lessons.select_principles(db, agent_id=None, char_budget=4000).text == ""
    conn = connect(db)
    try:
        status = conn.execute("SELECT status FROM knowledge WHERE id = ?", (p,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "archived"


# ---- loaded_when_failed -----------------------------------------------------


def test_loaded_when_failed_has_three_states(db):
    p = lessons.add(db, rule="p", tier="principle")
    other = lessons.add(db, rule="other", tier="principle")
    _log_band(db, "s-loaded", [p, other])
    _log_band(db, "s-absent", [other])

    def entry(session):
        kid = lessons.add(db, rule="e", tier="entry", parent_id=p, evidence_session=session)
        return {r["id"]: r for r in lessons.list_lessons(db)}[kid]["loaded_when_failed"]

    assert entry("s-loaded") == 1
    assert entry("s-absent") == 0
    assert entry("s-never-logged") is None, "no band record is unknown, not absent"
    assert entry(None) is None


def test_loaded_when_failed_follows_refinement(db):
    p = lessons.add(db, rule="v1", tier="principle")
    _log_band(db, "s1", [p])
    p2 = lessons.refine(db, p, "v2")
    kid = lessons.add(db, rule="e", tier="entry", parent_id=p2, evidence_session="s1")
    rows = {r["id"]: r for r in lessons.list_lessons(db)}
    assert rows[kid]["loaded_when_failed"] == 1
    assert rows[p2]["loaded_failures"] == 1


# ---- the band -----------------------------------------------------------------


def test_band_orders_charter_grants_principles_with_newest_clauses(db):
    p = lessons.add(db, rule="Never assert a negative.", tier="principle")
    for i in range(3):
        lessons.add(db, rule=f"variant {i}", tier="entry", parent_id=p)
    lessons.add_operator(db, kind="grant", rule="May merge docs.")
    lessons.add_operator(db, kind="charter", rule="Core identity is fixed.")
    text = lessons.select_principles(db, agent_id=None, char_budget=4000, clauses=2).text
    assert text.index("Core identity") < text.index("May merge") < text.index("Never assert")
    assert "variant 2" in text and "variant 1" in text and "variant 0" not in text


def test_band_drops_visibly_when_over_budget(db):
    for i in range(20):
        lessons.add(db, rule=f"Principle number {i} " + "x" * 80, tier="principle")
    block = lessons.select_principles(db, agent_id=None, char_budget=800)
    assert block.dropped_ids and block.injected_ids
    assert f"{len(block.dropped_ids)} more did not fit" in block.text
    assert block.chars <= 800


def test_band_is_scoped_by_agent(db):
    lessons.add(db, rule="shared", tier="principle", agent_id=None)
    lessons.add(db, rule="poe only", tier="principle", agent_id="poe@richard")
    lessons.add(db, rule="prudence only", tier="principle", agent_id="prudence@richard")
    text = lessons.select_principles(db, agent_id="poe@richard", char_budget=4000).text
    assert "shared" in text and "poe only" in text and "prudence only" not in text
    unscoped = lessons.select_principles(db, agent_id=None, char_budget=4000).text
    assert "shared" in unscoped and "poe only" not in unscoped


def test_tier0_egress_leaves_lessons_to_their_own_band(db):
    from techne.core.egress import select_tier0_injection

    lessons.add(db, rule="a principle", tier="principle")
    knowledge.store(db, "a fact", confidence="high")
    types = {r["type"] for r in select_tier0_injection(db)}
    assert types == {"fact"}


def test_consolidate_never_clusters_lessons(db):
    from techne.core.sleepdream import _LIVE_CLUSTERABLE_SQL

    lessons.add(db, rule="a principle", tier="principle")
    knowledge.store(db, "a fact")
    conn = connect(db)
    try:
        types = {r["type"] for r in conn.execute(_LIVE_CLUSTERABLE_SQL)}
    finally:
        conn.close()
    assert types == {"fact"}



def test_conflict_check_never_pairs_lessons(db):
    from techne.config import Config
    from techne.core.conflict import conflict_check_emit

    cfg = Config()
    cfg.db_path = db
    a = lessons.add(db, rule="use postgres for the datastore", tier="principle", tags="datastore")
    b = lessons.add(db, rule="we switched to sqlite", tier="principle", tags="datastore")
    f1 = knowledge.store(db, "use postgres for the datastore", tags="datastore")
    f2 = knowledge.store(db, "we switched to sqlite", tags="datastore")
    seen = set()
    for item in conflict_check_emit(cfg):
        seen |= {item["focus_id"], *item["comparison_ids"]}
    assert {f1, f2} <= seen  # the control: same-tag facts are paired
    assert not seen & {a, b}


# ---- review -------------------------------------------------------------------


def test_review_surfaces_and_never_retires(db):
    hot = lessons.add(db, rule="fails while loaded", tier="principle")
    _log_band(db, "s1", [hot])
    _log_band(db, "s2", [hot])
    lessons.add(db, rule="e1", tier="entry", parent_id=hot, evidence_session="s1")
    lessons.add(db, rule="e2", tier="entry", parent_id=hot, evidence_session="s2")
    long_id = lessons.add(db, rule="L" * 250, tier="principle")
    orphan = lessons.add(db, rule="orphan", tier="entry")
    enforced = lessons.add(db, rule="enforced", tier="principle")
    lessons.set_enforced_by(db, enforced, "pre-push hook")

    report = lessons.review(db, agent_id=None, char_budget=4000, cold_days=0)
    assert [r["id"] for r in report["enforce_candidates"]] == [hot]
    assert [r["id"] for r in report["long"]] == [long_id]
    assert [r["id"] for r in report["unlinked_entries"]] == [orphan]
    assert [r["id"] for r in report["enforced"]] == [enforced]
    assert enforced not in [r["id"] for r in report["cold"]]
    assert len(lessons.list_lessons(db, tier="principle")) == 3


# ---- import -------------------------------------------------------------------


def test_import_grip_excerpt(db):
    parsed = lessons.parse_markdown(GRIP_EXCERPT)
    assert [p["rule"][:40] for p in parsed["principle"]] == [
        "Never assert a negative or a count you d",
        "Subagent verdicts are candidates, not co",
    ]
    assert len(parsed["grant"]) == 1
    [entry] = parsed["entry"]
    assert entry["date"] == "2026-08-14"
    assert entry["rule"] == "a display filter manufactured a false negative"
    assert "_Graduated" in entry["body"]
    assert parsed["skipped"] == ["Scratch"]

    first = lessons.import_markdown(db, GRIP_EXCERPT, agent_id="grip")
    assert first["imported"] == {
        "principle": 2,
        "grant": 1,
        "charter": 0,
        "entry": 1,
        "duplicate": 0,
    }
    again = lessons.import_markdown(db, GRIP_EXCERPT, agent_id="grip")
    assert again["imported"]["duplicate"] == 4 and again["imported"]["principle"] == 0
    rows = lessons.list_lessons(db, agent_id="grip")
    assert {r["type"] for r in rows} == {"lesson", "grant"}
    [e] = [r for r in rows if r["tier"] == "entry"]
    assert e["event_at"] == "2026-08-14"


def test_import_dry_run_writes_nothing(db):
    out = lessons.import_markdown(db, GRIP_EXCERPT, agent_id=None, dry_run=True)
    assert out["parsed"]["principle"] == 2
    assert lessons.list_lessons(db) == []


# ---- surfaces -----------------------------------------------------------------


def test_migration_29_creates_lesson_meta(tmp_path):
    import sqlite3

    from techne.core.database import _migrate

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    _migrate(conn, 28, 29)
    _migrate(conn, 28, 29)  # a replayed step must not fail
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lesson_meta)")}
    assert {"agent_id", "tier", "rule", "parent_id", "loaded_when_failed"} <= cols
    conn.close()


def test_mcp_lesson_tool_round_trip(db):
    from types import SimpleNamespace

    from techne.config import InjectionConfig
    from techne.mcp_server import _techne_lesson

    config = SimpleNamespace(db_path=db, instance_name="poe@richard", injection=InjectionConfig())
    p = _techne_lesson({"action": "add", "tier": "principle", "rule": "Check first."}, config)
    _techne_lesson({"action": "add", "rule": "said none", "parent_id": p["id"]}, config)
    listed = _techne_lesson({"action": "list", "tier": "principle"}, config)
    assert listed["agent_id"] == "poe@richard"
    assert listed["lessons"][0]["recurrences"] == 1
    assert _techne_lesson({"action": "review"}, config)["counts"]["principles"] == 1
    with pytest.raises(ValueError, match="action"):
        _techne_lesson({"action": "grant", "rule": "let me merge"}, config)


def _run_session_start(tmp_path, payload, seed):
    import os
    import subprocess
    import sys

    home = tmp_path / payload["session_id"]
    cc_dir, techne_home = home / ".claude", home / ".techne"
    cc_dir.mkdir(parents=True)
    techne_home.mkdir(parents=True)
    (techne_home / "config.yaml").write_text("platform: claude-code\n")
    (cc_dir / "settings.json").write_text("{}")
    db = techne_home / "techne.db"
    init_db(db).close()
    seed(db)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cc_dir),
        "TECHNE_HOME": str(techne_home),
    }
    r = subprocess.run(
        [sys.executable, "-m", "techne.hooks.session_start"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout), db


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_principles_survive_compact_and_clear(tmp_path, source):
    from techne.hooks.session_start import REACTIVATION_LINE

    def seed(db):
        lessons.add(db, rule="Never assert a negative.", tier="principle")

    out, db = _run_session_start(tmp_path, {"session_id": f"s-{source}", "source": source}, seed)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(REACTIVATION_LINE.format(source=source))
    assert "Never assert a negative." in ctx
    conn = connect(db)
    try:
        logged = conn.execute(
            "SELECT session_id FROM context_injections WHERE source = ?",
            (lessons.INJECTION_SOURCE,),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in logged] == [f"s-{source}"]


def test_resume_does_not_reinject(tmp_path):
    from techne.hooks.session_start import REACTIVATION_LINE

    def seed(db):
        lessons.add(db, rule="Never assert a negative.", tier="principle")

    out, _ = _run_session_start(tmp_path, {"session_id": "s-resume", "source": "resume"}, seed)
    assert out["hookSpecificOutput"]["additionalContext"] == REACTIVATION_LINE.format(
        source="resume"
    )
