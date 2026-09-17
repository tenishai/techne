"""techne#952 (promote a reviewer's revision) and techne#949 (promoted knowledge
records its origin session).

#952: review had promote and discard only. A candidate carrying a durable claim in a
wrapper of counts, ids or versions forced a choice between losing the claim and
storing the wrapper. `promote --content` applies an edit; the proposal stays in
`content`, which is half of the exactly-once dedup index.

#949: every promoted knowledge row had `origin_session` NULL — 3,090 of 3,090 on the
operator's store — so the session was reachable only through a join or by parsing
`source`.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    c = Config()
    c.db_path = db
    c.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


def _session(cfg):
    from techne.core.state import start_session

    return start_session(cfg.db_path, project_path="/tmp/p")


def _candidate(cfg, content, *, action="add", target_ids=None, scope="knowledge", sid=None):
    from techne.core.database import connect

    sid = sid or _session(cfg)
    conn = connect(cfg.db_path)
    try:
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
               (scope, type, content, confidence, source_session_id, model_used,
                prompt_hash, status, origin, action, target_ids)
               VALUES (?, 'fact', ?, 'high', ?, 'agent-assisted', ?, 'pending',
                       'reflect', ?, ?)""",
            (
                scope,
                content,
                sid,
                f"h-{content}",
                action,
                json.dumps(target_ids) if target_ids else None,
            ),
        )
        conn.commit()
        return cur.lastrowid, sid
    finally:
        conn.close()


def _one(cfg, sql, *args):
    from techne.core.database import connect

    conn = connect(cfg.db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


WRAPPED = "capture.py skips Bash, measured 15 / 3 / 26,697 rows on 2026-09-17"
CORE = "capture.py skips Bash tool calls, so Bash work never reaches the store"


# --- #952 -----------------------------------------------------------------------


def test_a_revision_is_what_gets_promoted_and_the_proposal_is_kept(cfg):
    from techne.core.reflect import promote_candidate

    cid, _ = _candidate(cfg, WRAPPED)
    kid = promote_candidate(
        cfg.db_path,
        cid,
        decided_by="judge",
        decision_reason="dropped decaying counts",
        revised_content=CORE,
    )

    assert _one(cfg, "SELECT content FROM knowledge WHERE id = ?", kid)["content"] == CORE
    row = _one(cfg, "SELECT content, revised_content FROM knowledge_candidates WHERE id = ?", cid)
    assert row["content"] == WRAPPED, "the proposal must stay as proposed"
    assert row["revised_content"] == CORE


def test_a_revision_keeps_the_candidate_in_the_dedup_index(cfg):
    """Rewriting `content` would let re-ingesting the same answer insert a fresh
    pending duplicate. The index must still match the revised candidate."""
    from techne.core.database import connect
    from techne.core.reflect import promote_candidate

    cid, sid = _candidate(cfg, WRAPPED)
    promote_candidate(cfg.db_path, cid, decision_reason="trim", revised_content=CORE)

    conn = connect(cfg.db_path)
    try:
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
               (scope, type, content, confidence, source_session_id, model_used,
                prompt_hash, status, origin)
               VALUES ('knowledge', 'fact', ?, 'high', ?, 'agent-assisted', ?,
                       'pending', 'reflect')
               ON CONFLICT DO NOTHING""",
            (WRAPPED, sid, f"h-{WRAPPED}"),
        )
        conn.commit()
        assert cur.rowcount == 0, "a re-ingest of the same answer inserted a duplicate"
    finally:
        conn.close()


def test_a_revision_needs_a_reason(cfg):
    from techne.core.reflect import promote_candidate

    cid, _ = _candidate(cfg, WRAPPED)
    with pytest.raises(ValueError, match="decision_reason"):
        promote_candidate(cfg.db_path, cid, revised_content=CORE)
    assert _one(cfg, "SELECT status FROM knowledge_candidates WHERE id = ?", cid)["status"] == (
        "pending"
    ), "a refused revision must leave the candidate pending"


def test_an_empty_revision_is_refused(cfg):
    from techne.core.reflect import promote_candidate

    cid, _ = _candidate(cfg, WRAPPED)
    with pytest.raises(ValueError, match="empty"):
        promote_candidate(cfg.db_path, cid, decision_reason="x", revised_content="   ")


@pytest.mark.parametrize("action", ["merge", "supersede", "retire"])
def test_only_add_candidates_can_be_revised(cfg, action):
    """A merge or supersede carries a faithfulness verdict on its original text;
    revising it would apply text the verdict never saw."""
    from techne.core.reflect import promote_candidate

    cid, _ = _candidate(cfg, WRAPPED, action=action, target_ids=[1, 2])
    with pytest.raises(ValueError, match="only 'add'"):
        promote_candidate(cfg.db_path, cid, decision_reason="x", revised_content=CORE)
    assert _one(cfg, "SELECT status FROM knowledge_candidates WHERE id = ?", cid)["status"] == (
        "pending"
    )


def test_a_revision_is_redacted_like_a_proposal(cfg):
    from techne.core.reflect import promote_candidate

    secret = "AKIAIOSFODNN7EXAMPLE"
    cid, _ = _candidate(cfg, WRAPPED)
    kid = promote_candidate(
        cfg.db_path, cid, decision_reason="x", revised_content=f"key is {secret}"
    )
    assert secret not in _one(cfg, "SELECT content FROM knowledge WHERE id = ?", kid)["content"]


def test_cli_content_and_stats(cfg, tmp_path):
    cid, _ = _candidate(cfg, WRAPPED)
    r = CliRunner().invoke(
        main,
        ["reflect", "promote", str(cid), "--content", CORE, "--by", "judge", "--reason", "trim"],
    )
    assert r.exit_code == 0, r.output
    assert "(revised)" in r.output

    stats = json.loads(CliRunner().invoke(main, ["reflect", "stats"]).output)
    assert stats["revised"] == 1 and stats["promoted"] == 1

    review = CliRunner().invoke(main, ["reflect", "review", "--status", "promoted"]).output
    assert f"revised to: {CORE}" in review and WRAPPED in review


def test_cli_content_file(cfg, tmp_path):
    cid, _ = _candidate(cfg, WRAPPED)
    f = tmp_path / "rev.txt"
    f.write_text(CORE + "\n")
    r = CliRunner().invoke(
        main, ["reflect", "promote", str(cid), "--content-file", str(f), "--reason", "trim"]
    )
    assert r.exit_code == 0, r.output
    assert (
        _one(cfg, "SELECT revised_content FROM knowledge_candidates WHERE id = ?", cid)[
            "revised_content"
        ]
        == CORE
    )


def test_cli_refuses_content_without_reason_and_both_sources(cfg, tmp_path):
    cid, _ = _candidate(cfg, WRAPPED)
    r = CliRunner().invoke(main, ["reflect", "promote", str(cid), "--content", CORE])
    assert r.exit_code == 1 and "decision_reason" in r.output

    f = tmp_path / "rev.txt"
    f.write_text(CORE)
    r = CliRunner().invoke(
        main,
        [
            "reflect",
            "promote",
            str(cid),
            "--content",
            CORE,
            "--content-file",
            str(f),
            "--reason",
            "x",
        ],
    )
    assert r.exit_code == 1 and "not both" in r.output


# --- #949 -----------------------------------------------------------------------


def test_promotion_stamps_origin_session(cfg):
    from techne.core.reflect import promote_candidate

    cid, sid = _candidate(cfg, "a promoted fact")
    kid = promote_candidate(cfg.db_path, cid, decided_by="t")
    assert (
        _one(cfg, "SELECT origin_session FROM knowledge WHERE id = ?", kid)["origin_session"] == sid
    )


def test_retire_does_not_stamp_the_survivor(cfg):
    """Retire returns an EXISTING row; it did not originate in the retiring session."""
    from techne.core.knowledge import store
    from techne.core.reflect import promote_candidate

    old = store(cfg.db_path, "old claim about retire", type="fact")
    survivor = store(cfg.db_path, "surviving claim about retire", type="fact")
    cid, _ = _candidate(cfg, "surviving claim", action="retire", target_ids=[old, survivor])
    assert promote_candidate(cfg.db_path, cid, decided_by="t") == survivor
    assert (
        _one(cfg, "SELECT origin_session FROM knowledge WHERE id = ?", survivor)["origin_session"]
        is None
    )


def test_the_live_auto_path_stamps_origin_session(cfg):
    from techne.core.reflect import Candidate, _write_live

    c = Candidate(scope="knowledge", type="fact", content="auto fact", tags="", confidence="high")
    kid = _write_live(cfg.db_path, c, source="reflect:t-1", session_id="sess-auto")
    assert (
        _one(cfg, "SELECT origin_session FROM knowledge WHERE id = ?", kid)["origin_session"]
        == "sess-auto"
    )


def test_migration_backfills_promoted_rows_and_skips_retire_survivors(cfg):
    from techne.core.database import _migrate, connect
    from techne.core.knowledge import store
    from techne.core.reflect import promote_candidate

    cid, sid = _candidate(cfg, "fact before the fix")
    kid = promote_candidate(cfg.db_path, cid, decided_by="t")
    old = store(cfg.db_path, "old one", type="fact")
    survivor = store(cfg.db_path, "survivor one", type="fact")
    rid, _ = _candidate(cfg, "survivor one", action="retire", target_ids=[old, survivor])
    promote_candidate(cfg.db_path, rid, decided_by="t")

    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE knowledge SET origin_session = NULL")  # the pre-fix state
        conn.execute("DELETE FROM schema_version WHERE version = 26")
        conn.commit()
        _migrate(conn, 25, 26)
        got = {
            r["id"]: r["origin_session"]
            for r in conn.execute("SELECT id, origin_session FROM knowledge")
        }
    finally:
        conn.close()

    assert got[kid] == sid
    assert got[survivor] is None
    assert got[old] is None
