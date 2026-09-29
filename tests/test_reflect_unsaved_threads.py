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


def test_an_obs_key_answer_ingested_after_its_thread_is_persisted_drains_its_row(cfg, monkeypatch):
    """techne#1106. The carry-over above runs only in the emit that first persists a
    thread. A batch rendered on the fallback path and ingested by `obs_key` AFTER that
    emit (an `emit -o file` / `ingest file` round trip spanning a failed persist and a
    successful one) records the `obs-` key, but the thread's queue row stays owed. On
    main the next emit's #900 heal served it again under its thread id."""
    from techne.core.reflect import emit_reflection_pass, ingest_reflection_batch

    sid = _session_that_outgrew_its_detection(cfg)
    missing = _unpersisted_count(cfg, sid)
    restore = _fallback_off(monkeypatch)
    held = emit_reflection_pass(cfg, sid)["batch"]  # rendered on the fallback path
    late = [i for i in held if i["thread_id"] is None]
    assert len(late) == missing and all(i.get("obs_key") for i in late)

    restore()
    emit_reflection_pass(cfg, sid)  # persists the threads; nothing answered yet
    assert _unpersisted_count(cfg, sid) == 0
    for item in held:
        item["raw_response"] = ANSWER
    ingest_reflection_batch(cfg, held)  # the late answers arrive by obs_key

    again = emit_reflection_pass(cfg, sid)
    assert again["count"] == 0, (
        f"late-answered threads re-offered: {[i['thread_num'] for i in again['batch']]}"
    )
    owed = _rows(
        cfg,
        "SELECT thread_id FROM reflection_queue WHERE session_id = ? AND drained_at IS NULL",
        sid,
    )
    assert owed == [], "a late obs_key answer left its thread's queue row owed"
    (payload,) = _rows(
        cfg, "SELECT payload_json FROM extension_state WHERE name = ?", f"reflect:emitted:{sid}"
    )[0]
    assert json.loads(payload)["answered"] == [], "moved obs- keys left behind in `answered`"


@pytest.mark.parametrize("answered_both", [False, True])
def test_a_collided_thread_id_drains_only_when_every_key_is_answered(
    cfg, monkeypatch, answered_both
):
    """Two detected threads can resolve to one thread id (#733), and emit then renders
    them as ONE item. If only one of their `obs-` keys was answered, draining that id
    would stamp the other thread's observations paid without anyone reading them."""
    import techne.core.thread_orchestrator as orch
    from techne.core.extension_state import get_state, set_state
    from techne.core.reflect import (
        _bucket_observations,
        _observations_key,
        load_session_for_reflection,
        persist_unsaved_threads,
    )

    sid = _session_that_outgrew_its_detection(cfg, saved_days=2, later_days=0)
    threads, observations = load_session_for_reflection(cfg, sid)
    a, b = threads[0], threads[1]
    real = orch.persisted_thread_id
    tid = real(cfg.db_path, sid, a)
    assert tid and _rows(cfg, "SELECT 1 FROM reflection_queue WHERE thread_id = ?", tid)
    monkeypatch.setattr(
        orch,
        "persisted_thread_id",
        lambda db, s, t, **k: tid if t.thread_num == b.thread_num else real(db, s, t, **k),
    )
    by_num = _bucket_observations(threads, observations)
    key_a, key_b = (_observations_key(by_num[t.thread_num]) for t in (a, b))
    answered = [key_a, key_b] if answered_both else [key_a]
    conn = connect(cfg.db_path)
    try:
        set_state(
            conn,
            f"reflect:emitted:{sid}",
            {"key": "thread_id", "emitted": [key_a, key_b], "answered": answered},
        )
    finally:
        conn.close()

    persist_unsaved_threads(cfg, sid, threads, observations)
    (drained,) = _rows(cfg, "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", tid)[0]
    if answered_both:
        assert drained is not None, "every key answered, but the shared row stayed owed"
    else:
        assert drained is None, "one unanswered thread's observations were marked paid"
        # techne#1110: a doubly late #733 collision. key_b's answer arrives after the
        # persist above, and the row must drain on the next one. That depends on the
        # persist keeping key_a in `answered` (it strips only the keys it moved); if it
        # stripped every key it saw, key_a is gone, the pair never completes, and the
        # item is re-offered once.
        conn = connect(cfg.db_path)
        try:
            marker = get_state(conn, f"reflect:emitted:{sid}")
            assert key_a in marker["answered"], "an unpaid key was dropped from `answered`"
            marker["answered"] = sorted({*marker["answered"], key_b})
            set_state(conn, f"reflect:emitted:{sid}", marker)
        finally:
            conn.close()

        persist_unsaved_threads(cfg, sid, threads, observations)
        (drained,) = _rows(
            cfg, "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", tid
        )[0]
        assert drained is not None, "both keys answered across two persists, row still owed"
