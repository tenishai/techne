"""techne#954 — imported sessions had no persisted threads.

`techne import` wrote observations and stopped. With no `threads` rows, reflect's
emit cursor keyed every imported thread on `prompt_hash`, which moves on any
knowledge write, so a backfill that promoted as it went was re-offered every thread
it had already ingested. Measured on an external store: 35,096 imported
observations, 0 threads, `thread_id` null on 9/9 and 18/18 emitted items.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main

SESSION = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    c = Config()
    c.db_path = db
    c.data_dir = tmp_path
    c.reflect.enabled = True
    c.reflect.min_observations_per_thread = 3
    c.reflect.min_confidence = "low"
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


def _transcript(path, session_id=SESSION, n_edits=14):
    """A Claude Code transcript with n_edits Edit tool calls, each with its result."""
    rows = []
    for i in range(n_edits):
        ts = f"2026-09-17T10:{i:02d}:00Z"
        tu = f"toolu_{i:04d}"
        rows.append(
            {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": "/tmp/proj",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tu,
                            "name": "Edit",
                            "input": {
                                "file_path": "/tmp/proj/app.py",
                                "old_string": f"value = {i}",
                                "new_string": f"value = {i + 1}",
                            },
                        }
                    ],
                },
            }
        )
        rows.append(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": "/tmp/proj",
                "timestamp": ts,
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tu, "content": "ok"}],
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _count(cfg, table, session_id=SESSION):
    from techne.core.database import connect

    conn = connect(cfg.db_path)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _answer(batch):
    for item in batch:
        item["raw_response"] = json.dumps(
            [
                {
                    "scope": "knowledge",
                    "type": "fact",
                    "content": "imported fact",
                    "confidence": "high",
                }
            ]
        )
    return batch


def test_import_persists_threads(cfg, tmp_path):
    t = _transcript(tmp_path / f"{SESSION}.jsonl")
    result = CliRunner().invoke(main, ["import", str(t)])
    assert result.exit_code == 0, result.output

    assert _count(cfg, "observations") >= 14, "precondition: the observations landed"
    assert _count(cfg, "threads") >= 1, (
        "an imported session must have persisted threads — without them the emit "
        "cursor keys on prompt_hash and re-offers ingested threads (techne#954)"
    )
    assert "threads detected in 1 sessions" in result.output


def test_emitted_items_from_an_import_carry_a_thread_id(cfg, tmp_path):
    from techne.core.reflect import emit_reflection_pass

    CliRunner().invoke(main, ["import", str(_transcript(tmp_path / f"{SESSION}.jsonl"))])
    batch = emit_reflection_pass(cfg, SESSION)["batch"]
    assert batch, "precondition: the imported session renders at least one thread"
    assert all(item["thread_id"] for item in batch), (
        "thread_id was null on every item from an imported session (18/18 measured)"
    )


def test_a_backfill_that_promotes_as_it_goes_is_not_re_offered(cfg, tmp_path):
    """The reported failure, end to end: import, emit, ingest, write knowledge,
    emit again. Before the fix the second emit served every thread again."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_pass, ingest_reflection_batch

    cfg.reflect.include_existing_knowledge = True  # the block is what moves the hash
    CliRunner().invoke(main, ["import", str(_transcript(tmp_path / f"{SESSION}.jsonl"))])

    first = emit_reflection_pass(cfg, SESSION)
    assert first["count"] >= 1
    ingest_reflection_batch(cfg, _answer(first["batch"]))

    store(cfg.db_path, "Edit app.py value tmp proj imported fact", type="fact")

    second = emit_reflection_pass(cfg, SESSION)
    assert second["count"] == 0, (
        f"re-offered {second['count']} already-ingested thread(s) after a knowledge write"
    )


def test_re_running_import_repairs_a_store_imported_before_the_fix(cfg, tmp_path):
    """Stores already imported have observations and no threads. The documented safe
    habit — re-run `techne import` — must fill them in, not skip past them."""
    from techne.platforms.claude_code import import_transcript

    t = _transcript(tmp_path / f"{SESSION}.jsonl")
    import_transcript(cfg.db_path, t)  # the pre-fix path: observations only
    assert _count(cfg, "threads") == 0, "precondition: the #954 state"

    result = CliRunner().invoke(main, ["import", str(t)])
    assert result.exit_code == 0, result.output
    assert "already has" in result.output, "precondition: the observations were not re-imported"
    assert _count(cfg, "threads") >= 1


def test_re_running_import_leaves_a_detected_session_alone(cfg, tmp_path):
    from techne.lifecycle import ensure_threads

    CliRunner().invoke(main, ["import", str(_transcript(tmp_path / f"{SESSION}.jsonl"))])
    assert _count(cfg, "threads") >= 1
    assert ensure_threads(cfg, SESSION) is None, (
        "replace=False must not re-detect a session that already has threads — "
        "that is every hook-captured session"
    )


def test_ensure_threads_skips_a_session_with_no_observations(cfg):
    from techne.core.state import start_session
    from techne.lifecycle import ensure_threads

    sid = start_session(cfg.db_path, project_path="/tmp/p")
    assert ensure_threads(cfg, sid, replace=True) is None


@pytest.fixture
def fallback(monkeypatch):
    """Force the #954 obs_key fallback. Since techne#1091 emit persists a thread that
    has no row before rendering it, so an item carries `obs_key` only when that write
    fails. These tests pin the fallback, so they make it fail."""
    import techne.core.reflect as reflect

    def _fail(*a, **k):
        raise RuntimeError("persistence disabled for this test")

    monkeypatch.setattr(reflect, "persist_unsaved_threads", _fail)


def _unpersisted_session(cfg):
    from techne.core.state import add_observation, start_session

    sid = start_session(cfg.db_path, project_path="/tmp/p")
    for i in range(14):
        add_observation(cfg.db_path, sid, "Edit", f"edit app.py value {i}")
    assert _count(cfg, "threads", sid) == 0, "precondition: no persisted threads"
    return sid


def test_an_answered_unpersisted_thread_stays_claimed_across_a_knowledge_write(cfg, fallback):
    """The fallback half. A thread with no persisted row is claimed by its obs_key when
    answered, so a knowledge write does not re-offer it."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_pass, ingest_reflection_batch

    cfg.reflect.include_existing_knowledge = True
    sid = _unpersisted_session(cfg)

    first = emit_reflection_pass(cfg, sid)
    assert first["count"] >= 1
    assert all(item["thread_id"] is None and item.get("obs_key") for item in first["batch"])
    ingest_reflection_batch(cfg, _answer(first["batch"]))

    store(cfg.db_path, "edit app.py value observations Edit", type="fact")

    second = emit_reflection_pass(cfg, sid)
    assert second["count"] == 0, (
        "an answered unpersisted thread was re-offered after a knowledge write"
    )


def test_a_rendered_but_unanswered_unpersisted_thread_is_re_offered(cfg, fallback):
    """Prudence #955 r1: claiming an unpersisted thread on RENDER dropped it silently.
    It has no queue row, so the #900 heal cannot re-offer it. It must stay offered
    until answered."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_pass

    cfg.reflect.include_existing_knowledge = True
    sid = _unpersisted_session(cfg)

    first = emit_reflection_pass(cfg, sid)
    assert first["count"] >= 1
    store(cfg.db_path, "edit app.py value observations Edit", type="fact")

    second = emit_reflection_pass(cfg, sid)
    assert second["count"] == first["count"], (
        "a rendered, never-answered unpersisted thread was dropped"
    )


def test_an_unpersisted_decline_is_excluded_and_undecline_reaches_it(cfg, fallback):
    from techne.core.reflect import clear_declines, emit_reflection_pass, ingest_reflection_batch

    sid = _unpersisted_session(cfg)
    first = emit_reflection_pass(cfg, sid)
    assert first["count"] >= 1
    for item in first["batch"]:
        item["raw_response"] = ""  # declined
    ingest_reflection_batch(cfg, first["batch"])

    assert emit_reflection_pass(cfg, sid)["count"] == 0, "a declined thread was re-offered"
    assert clear_declines(cfg, sid) == first["count"], "undecline could not reach the decline"
    assert emit_reflection_pass(cfg, sid)["count"] == first["count"]


def test_the_drain_loop_terminates_for_unpersisted_threads(cfg, fallback):
    """The guide's loop: emit, answer, ingest, until emit returns 0."""
    from techne.core.knowledge import store
    from techne.core.reflect import emit_reflection_pass, ingest_reflection_batch

    cfg.reflect.include_existing_knowledge = True
    sid = _unpersisted_session(cfg)
    for n in range(10):
        r = emit_reflection_pass(cfg, sid)
        if r["count"] == 0:
            break
        ingest_reflection_batch(cfg, _answer(r["batch"]))
        store(cfg.db_path, f"edit app.py value observations Edit pass {n}", type="fact")
    else:
        pytest.fail("the emit/ingest loop did not terminate in 10 passes")


def test_mcp_ingest_claims_an_unpersisted_thread(cfg, fallback):
    from techne.core.reflect import emit_reflection_pass
    from techne.mcp_server import _techne_reflect_ingest

    sid = _unpersisted_session(cfg)
    item = emit_reflection_pass(cfg, sid)["batch"][0]
    _techne_reflect_ingest(
        {
            "session_id": sid,
            "thread_id": "",
            "prompt_hash": item["prompt_hash"],
            "obs_key": item["obs_key"],
            "response": _answer([dict(item)])[0]["raw_response"],
        },
        cfg,
    )
    offered = [i["obs_key"] for i in emit_reflection_pass(cfg, sid)["batch"]]
    assert item["obs_key"] not in offered


def test_mcp_ingest_without_obs_key_warns(cfg, fallback):
    """Prudence #955 r2: `obs_key` is optional over MCP, so an agent that follows the
    tool description can answer an unpersisted thread and have it offered again. The
    answer is still stored; the result says what happened."""
    from techne.core.reflect import emit_reflection_pass
    from techne.mcp_server import _techne_reflect_ingest

    sid = _unpersisted_session(cfg)
    item = emit_reflection_pass(cfg, sid)["batch"][0]
    result = _techne_reflect_ingest(
        {
            "session_id": sid,
            "thread_id": "",
            "prompt_hash": item["prompt_hash"],
            "response": _answer([dict(item)])[0]["raw_response"],
        },
        cfg,
    )
    assert result["candidates_written"] >= 1, "the answer must still be stored"
    assert "obs_key" in result.get("warning", ""), (
        "ingesting an unpersisted thread without obs_key must say the thread stays unclaimed"
    )
    assert item["obs_key"] in [i["obs_key"] for i in emit_reflection_pass(cfg, sid)["batch"]]


def test_the_mcp_tool_description_names_obs_key():
    """The sentence an agent reads to decide what to send (Prudence #955 r2)."""
    from techne.mcp_server import TOOLS

    (tool,) = [t for t in TOOLS if t["name"] == "techne_reflect_ingest"]
    assert "obs_key" in tool["description"]
    assert "obs_key" in tool["inputSchema"]["properties"]


def test_observations_key_needs_every_id():
    from techne.core.reflect import _observations_key

    assert _observations_key([{"id": 3}, {"id": 1}]) == _observations_key([{"id": 1}, {"id": 3}])
    assert _observations_key([{"id": 1}, {"content": "no id"}]) is None
    assert _observations_key([]) is None
