"""techne#1091 — emit served threads with no `threads` row, as `thread_id: null`.

Emit re-detects from observations, and that detection can run ahead of the last one
persisted: observations recorded after the final session_end detection, or a detector
change since the session was saved. Measured on the operator's store: session 18200f28
was last detected at 323 observations and has 446, so threads 28-34 had no rows;
session 2db12bfe has 11 saved thread numbers and 28 detected today. The overnight
drain treated `thread_id: null` as "cannot ingest" (the MCP schema requires the field),
the #954 heal re-offered the same items every pass, and 13 of 95 sessions stopped
partial.

Emit now persists the missing rows before rendering, so every item it serves carries
a real thread id and a queue row.
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.state import start_session

ANSWER = json.dumps(
    [{"scope": "knowledge", "type": "fact", "content": "a durable fact", "confidence": "high"}]
)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = tmp_path / "reflect.db"
    c.data_dir = tmp_path
    c.reflect.enabled = True
    c.reflect.min_observations_per_thread = 3
    c.reflect.min_confidence = "low"
    c.reflect.include_existing_knowledge = False
    init_db(c.db_path)
    return c


def _add_day(cfg, session_id, day, per_day=6):
    """Observations days apart, so the detector splits a thread per day (GAP_HARD)."""
    conn = connect(cfg.db_path)
    try:
        for i in range(per_day):
            conn.execute(
                "INSERT INTO observations (session_id, timestamp, tool_name, content) "
                "VALUES (?,?,?,?)",
                (session_id, f"2026-06-{10 + day * 3:02d} 0{i}:00:00", "Edit", f"d{day} s{i}"),
            )
        conn.commit()
    finally:
        conn.close()


def _session_that_outgrew_its_detection(cfg, saved_days=2, later_days=2):
    """The 18200f28 shape: detection persisted, then more observations arrive and no
    session_end runs again, so the newest threads have no row."""
    from techne.lifecycle import redetect_threads

    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="test-agent")
    for day in range(saved_days):
        _add_day(cfg, sid, day)
    redetect_threads(cfg, sid)
    for day in range(saved_days, saved_days + later_days):
        _add_day(cfg, sid, day)
    return sid


def _rows(cfg, sql, *args):
    conn = connect(cfg.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _unpersisted_count(cfg, sid):
    from techne.core.reflect import load_session_for_reflection
    from techne.core.thread_orchestrator import persisted_thread_id

    threads, _ = load_session_for_reflection(cfg, sid)
    return sum(persisted_thread_id(cfg.db_path, sid, t) is None for t in threads)


def _total(cfg, sid):
    from techne.core.reflect import load_session_for_reflection

    threads, _ = load_session_for_reflection(cfg, sid)
    return len(threads)


def test_precondition_the_fixture_has_threads_with_no_row(cfg):
    """Every detected thread clears min_observations_per_thread, and the later days'
    threads have no row: 4 of 8 on today's detector."""
    sid = _session_that_outgrew_its_detection(cfg)
    assert _unpersisted_count(cfg, sid) >= 2
    assert _unpersisted_count(cfg, sid) < _total(cfg, sid)


def test_emit_never_serves_a_null_thread_id(cfg):
    from techne.core.reflect import emit_reflection_pass

    sid = _session_that_outgrew_its_detection(cfg)
    total = _total(cfg, sid)
    batch = emit_reflection_pass(cfg, sid)["batch"]
    assert len(batch) == total
    assert all(item["thread_id"] for item in batch), [i["thread_id"] for i in batch]
    served = {item["thread_id"] for item in batch}
    persisted = {r[0] for r in _rows(cfg, "SELECT id FROM threads WHERE session_id = ?", sid)}
    queued = {
        r[0] for r in _rows(cfg, "SELECT thread_id FROM reflection_queue WHERE session_id = ?", sid)
    }
    assert served <= persisted, "an item's thread_id has no threads row"
    assert served <= queued, "a newly persisted thread has no queue row, so #900 cannot heal it"


def test_the_unattended_drain_reaches_zero_serving_each_thread_once(cfg):
    """The overnight drainer's loop, over MCP: emit(limit=1), and ingest only an item
    with a thread_id. On main this cycles the unpersisted threads and never ends."""
    from techne.mcp_server import _techne_reflect_emit, _techne_reflect_ingest

    sid = _session_that_outgrew_its_detection(cfg)
    total = _total(cfg, sid)
    served: list[int] = []
    for _ in range(20):
        r = _techne_reflect_emit({"session_id": sid, "limit": 1}, cfg)
        if r["count"] == 0:
            break
        item = r["batch"][0]
        served.append(item["thread_num"])
        if not item["thread_id"]:
            continue  # what the drainer does: it cannot ingest this
        res = _techne_reflect_ingest(
            {
                "session_id": sid,
                "thread_id": item["thread_id"],
                "prompt_hash": item["prompt_hash"],
                "response": ANSWER,
            },
            cfg,
        )
        assert not res.get("errors"), res
    else:
        pytest.fail(f"drain did not reach count 0; served {served}")
    assert sorted(served) == sorted(set(served)), f"a thread was served twice: {served}"
    assert len(served) == total
    owed = _rows(
        cfg,
        "SELECT thread_id FROM reflection_queue WHERE session_id = ? AND drained_at IS NULL",
        sid,
    )
    assert owed == [], "the drain finished but left queue rows owed"
    null_fk = _rows(
        cfg,
        "SELECT COUNT(*) FROM knowledge_candidates WHERE source_thread_id IS NULL",
    )[0][0]
    assert null_fk == 0, "a candidate was stored without its thread (#949)"


def test_persisted_rows_are_left_alone(cfg):
    """Only missing rows are written. Rewriting existing ones would re-key threads a
    past drain already paid for."""
    from techne.core.reflect import emit_reflection_pass

    sid = _session_that_outgrew_its_detection(cfg)
    missing = _unpersisted_count(cfg, sid)
    before = _rows(cfg, "SELECT id, thread_num, updated_at FROM threads WHERE session_id = ?", sid)
    emit_reflection_pass(cfg, sid)
    after = _rows(cfg, "SELECT id, thread_num, updated_at FROM threads WHERE session_id = ?", sid)
    assert set(map(tuple, before)) <= set(map(tuple, after))
    assert len(after) == len(before) + missing


def test_a_thread_resolved_by_thread_num_keeps_its_id(cfg):
    """`persisted_thread_id` falls back to (session_id, thread_num) when the computed
    id has no row: a legacy `{session}-{num}` row, or a row saved by an older detector
    (2db12bfe threads 3-11 on the operator's store). Such a thread IS persisted. Writing
    its computed id as well would make `persisted_thread_id` prefer the new row, so a
    thread a past drain already paid for would come back under a new key."""
    from techne.core.reflect import emit_reflection_pass

    sid = _session_that_outgrew_its_detection(cfg)
    conn = connect(cfg.db_path)
    try:
        (old_id,) = conn.execute(
            "SELECT id FROM threads WHERE session_id = ? AND thread_num = 1", (sid,)
        ).fetchone()
        conn.execute("DELETE FROM reflection_queue WHERE thread_id = ?", (old_id,))
        conn.execute("UPDATE threads SET id = ? WHERE id = ?", (f"{sid}-1", old_id))
        conn.commit()
    finally:
        conn.close()

    batch = emit_reflection_pass(cfg, sid)["batch"]
    (first,) = [i for i in batch if i["thread_num"] == 1]
    assert first["thread_id"] == f"{sid}-1"
    assert _rows(cfg, "SELECT 1 FROM threads WHERE id = ?", old_id) == []


def _fallback_off(monkeypatch):
    import techne.core.reflect as reflect

    real = reflect.persist_unsaved_threads

    def _fail(*a, **k):
        raise RuntimeError("persistence disabled")

    monkeypatch.setattr(reflect, "persist_unsaved_threads", _fail)
    return lambda: monkeypatch.setattr(reflect, "persist_unsaved_threads", real)


def test_threads_answered_by_obs_key_are_not_offered_again_once_persisted(cfg, monkeypatch):
    """The upgrade path. A store drained under #954 claimed unpersisted threads by
    `obs-` key. When emit later gives them rows, that ledger must follow them: answered
    ones drain their new queue row, and nothing is served a second time."""
    from techne.core.reflect import emit_reflection_pass, ingest_reflection_batch

    sid = _session_that_outgrew_its_detection(cfg)
    missing = _unpersisted_count(cfg, sid)
    restore = _fallback_off(monkeypatch)
    first = emit_reflection_pass(cfg, sid)
    unpersisted = [i for i in first["batch"] if i["thread_id"] is None]
    assert len(unpersisted) == missing and all(i.get("obs_key") for i in unpersisted)
    for item in first["batch"]:
        item["raw_response"] = ANSWER
    ingest_reflection_batch(cfg, first["batch"])

    restore()
    again = emit_reflection_pass(cfg, sid)
    assert again["count"] == 0, f"answered threads re-offered: {again['batch']}"
    assert _unpersisted_count(cfg, sid) == 0
    owed = _rows(
        cfg,
        "SELECT thread_id FROM reflection_queue WHERE session_id = ? AND drained_at IS NULL",
        sid,
    )
    assert owed == [], "an answered thread's new queue row was left owed"
    (payload,) = _rows(
        cfg, "SELECT payload_json FROM extension_state WHERE name = ?", f"reflect:emitted:{sid}"
    )[0]
    assert json.loads(payload)["answered"] == [], "moved obs- keys left behind in `answered`"


def test_a_sub_threshold_thread_is_not_persisted_or_queued(cfg):
    """Emit never renders a thread under min_observations_per_thread, so persisting and
    queueing one would add a row nothing can drain: the #527 r4 banner bug."""
    from techne.core.reflect import emit_reflection_pass, unemittable_queue_rows

    sid = _session_that_outgrew_its_detection(cfg, later_days=1)
    _add_day(cfg, sid, day=5, per_day=8)  # today's detector: one thread of 5, rest of 3
    cfg.reflect.min_observations_per_thread = 4
    before = len(_rows(cfg, "SELECT id FROM threads WHERE session_id = ?", sid))
    assert _unpersisted_count(cfg, sid) >= 2, "precondition: sub-threshold threads unsaved"
    # The saved 3-observation threads were queued under the old floor; those rows are
    # not this change's to judge, so compare against them rather than against [].
    already_unemittable = unemittable_queue_rows(cfg, sid)

    batch = emit_reflection_pass(cfg, sid)["batch"]
    after = len(_rows(cfg, "SELECT id FROM threads WHERE session_id = ?", sid))
    assert batch and all(i["thread_id"] for i in batch)
    assert after - before == len(batch), "persisted a thread emit does not render"
    assert unemittable_queue_rows(cfg, sid) == already_unemittable


def test_a_declined_obs_key_stays_declined_once_persisted(cfg, monkeypatch):
    from techne.core.reflect import clear_declines, emit_reflection_pass, ingest_reflection_batch

    sid = _session_that_outgrew_its_detection(cfg)
    missing = _unpersisted_count(cfg, sid)
    restore = _fallback_off(monkeypatch)
    first = emit_reflection_pass(cfg, sid)
    for item in first["batch"]:
        item["raw_response"] = "" if item["thread_id"] is None else ANSWER
    ingest_reflection_batch(cfg, first["batch"])

    restore()
    assert emit_reflection_pass(cfg, sid)["count"] == 0, "a declined thread was re-offered"
    assert clear_declines(cfg, sid) == missing, "undecline could not reach the carried-over decline"
    back = emit_reflection_pass(cfg, sid)["batch"]
    assert len(back) == missing and all(i["thread_id"] for i in back)


def test_a_rendered_unanswered_obs_key_is_still_offered_once_persisted(cfg, monkeypatch):
    """Rendered but never answered is owed: the new queue row stays undrained and the
    #900 heal re-offers it, under its thread id this time."""
    from techne.core.reflect import emit_reflection_pass

    sid = _session_that_outgrew_its_detection(cfg)
    total = _total(cfg, sid)
    restore = _fallback_off(monkeypatch)
    first = emit_reflection_pass(cfg, sid)
    assert first["count"] == total

    restore()
    items = emit_reflection_pass(cfg, sid)["batch"]
    assert len(items) == total and all(i["thread_id"] for i in items)
