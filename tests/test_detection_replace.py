"""Persisting a detection REPLACES the previous one (techne#646).

`run_session_end` fires once per turn and re-detects whenever the observation count
moved. It deleted `thread_boundaries` before re-saving — with a comment reasoning about
unbounded growth — and did NOT delete `threads`. `save_threads` writes `INSERT OR IGNORE`
on an id derived from `bucket_hash(session_id, first_obs_id, boundary_signal)`, so shifted
boundaries mint a NEW id and the superseded row stays.

That is the whole of #646. Reflection re-detects from scratch
(`load_session_for_reflection`) while the queue holds ids from a partition that no longer
exists, so the row is real debt whose prompt is impossible. The `ON DELETE CASCADE` on
`reflection_queue.thread_id` exists to retire exactly those rows and never fired, because
nothing deleted the stale partition. Measured on the operator's store before the fix: 86
rows in `threads` against 51 threads in the current detection, and 26 undrainable queue
rows of which 19 pointed at ids no current detection produces.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _config(tmp_path, *, min_obs: int = 2):
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config()
    cfg.db_path = tmp_path / "d.db"
    cfg.data_dir = tmp_path
    cfg.threads.detector = "heuristic"
    cfg.threads.summarizer = "heuristic"
    cfg.reflect.min_observations_per_thread = min_obs
    cfg.reflect.include_existing_knowledge = False
    init_db(cfg.db_path)
    return cfg


def _add(cfg, session_id, n, *, start_hour=12, day=1, tool="Bash"):
    """`n` observations in one temporal cluster.

    Inserted directly rather than through `add_observation`, which stamps `timestamp`
    itself — the heuristic detector reads that column to find gaps, so the cluster
    separation this fixture depends on has to be written explicitly (same reason
    `test_run_session_end_persists_boundaries_under_heuristic` does it this way).
    """
    from techne.core.database import connect

    base = datetime(2026, 1, day, start_hour, 0)
    conn = connect(cfg.db_path)
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO observations (session_id, timestamp, tool_name, content) "
                "VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    (base + timedelta(minutes=i)).isoformat(),
                    tool,
                    f"/proj/f{start_hour}.py step {i}",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _rows(cfg, session_id):
    from techne.core.database import connect

    conn = connect(cfg.db_path)
    try:
        return [
            (r["id"], r["thread_num"])
            for r in conn.execute(
                "SELECT id, thread_num FROM threads WHERE session_id = ?", (session_id,)
            )
        ]
    finally:
        conn.close()


def _current_ids(cfg, session_id):
    from techne.core.reflect import load_session_for_reflection
    from techne.core.thread_orchestrator import persisted_thread_id

    threads, _obs = load_session_for_reflection(cfg, session_id)
    return {
        tid
        for tid in (persisted_thread_id(cfg.db_path, session_id, t) for t in threads)
        if tid
    }


def test_a_second_session_end_replaces_the_partition_instead_of_appending(tmp_path):
    """THE #646 REGRESSION. Two turns' worth of detection over a growing observation set:
    the `threads` table must hold ONE partition afterwards, not two.

    Asserted as "every persisted row is in the current detection" rather than by counting,
    because a count can coincide across two different partitions of the same size while
    every id differs — which is the failure this is meant to catch."""
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")

    _add(cfg, sid, 6, start_hour=12)
    _add(cfg, sid, 6, start_hour=18)
    run_session_end(cfg, sid)
    first = set(i for i, _ in _rows(cfg, sid))
    assert first, "no threads persisted on the first pass — fixture is not exercising detection"

    # INTERLEAVED, NOT APPENDED, and that distinction is the whole fixture. A thread's id
    # is bucket_hash(session_id, first_obs_id, boundary_signal), so appending a distant
    # cluster leaves every existing thread's id untouched and mints no orphan at all — my
    # first version of this test did that and survived deleting the DELETE. Rows landing
    # BETWEEN existing ones move the boundaries and re-key the threads, which is what
    # transcript backfill does in production: it inserts the Bash/Read observations the
    # live hook skipped, with their original (earlier) timestamps.
    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)

    persisted = {i for i, _ in _rows(cfg, sid)}
    current = _current_ids(cfg, sid)

    orphans = persisted - current
    assert not orphans, (
        f"{len(orphans)} persisted thread row(s) are not in the current detection: "
        f"{sorted(orphans)}. The previous partition was left behind — this is #646, and "
        "every one of those ids can carry a reflection_queue row no emit can ever render."
    )


def test_the_replace_does_not_resurrect_paid_debt(tmp_path):
    """#527 r2, which this change makes newly reachable from `run_session_end`.

    The CASCADE takes DRAINED queue rows too, so a delete-and-re-enqueue that forgets to
    restore the stamps reports already-reflected threads as owed — re-firing the banner and
    making its "oldest from" age wrong. Deleting the restore loop in `persist_detection`
    must fail here."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        queued = [r["thread_id"] for r in conn.execute(
            "SELECT thread_id FROM reflection_queue WHERE session_id = ?", (sid,))]
        assert queued, "nothing queued — fixture is not exercising the debt path"
        conn.execute(
            "UPDATE reflection_queue SET drained_at = '2026-01-01 00:00:00' "
            "WHERE thread_id = ?",
            (queued[0],),
        )
        conn.commit()
    finally:
        conn.close()

    # MORE OBSERVATIONS FIRST. run_session_end short-circuits on an unchanged
    # `last_obs_count` — a second call over the same observations skips detection
    # entirely, so the replace never runs and this test passes without testing anything.
    # (My first version did exactly that and survived deleting the restore loop.) A new
    # distant cluster re-triggers detection while leaving the first cluster's thread id
    # stable, which is the case the restore exists for: its queue row IS cascaded away and
    # re-enqueued as pending, and only the restore puts the stamp back.
    _add(cfg, sid, 6, start_hour=18)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", (queued[0],)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "the paid queue row was deleted outright by the replace"
    assert row["drained_at"] == "2026-01-01 00:00:00", (
        "a replaced detection resurrected paid debt as pending (#527 r2). The restore loop "
        "must ride the same transaction as the enqueue it corrects."
    )


def test_both_persistence_paths_go_through_one_implementation(tmp_path, monkeypatch):
    """`run_session_end` and `redetect_threads` had SEPARATE copies of this sequence and
    drifted: redetect deleted both tables, session_end deleted only `thread_boundaries`.

    A two-implementation rail drifts on its BOOKKEEPING first, because that half is
    invisible to whoever runs it — the lesson #642 already paid for on the reflect CLI.
    Pinning the shared call site so a future edit cannot re-fork them silently."""
    import techne.lifecycle as lc
    from techne.core.state import start_session

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)

    seen: list[str] = []
    real = lc.persist_detection

    def _spy(*a, **k):
        seen.append("called")
        return real(*a, **k)

    monkeypatch.setattr(lc, "persist_detection", _spy)

    lc.run_session_end(cfg, sid)
    assert seen == ["called"], "run_session_end no longer persists through persist_detection"

    lc.redetect_threads(cfg, sid)
    assert seen == ["called", "called"], (
        "redetect_threads no longer persists through persist_detection — the two paths have "
        "re-forked, which is how #646 happened."
    )


def test_superseded_is_distinguished_from_below_threshold(tmp_path):
    """The distinction that makes reconciliation legitimate rather than sweeping.

    A row whose thread id is absent from the current detection is provably stale. A row
    whose thread is current but has fallen under `min_observations_per_thread` is #646's
    real signal and must survive the reconciler. Collapsing the two — returning all
    undrainable rows as superseded — destroys the evidence the issue exists to preserve."""
    from techne.core.database import connect
    from techne.core.reflect import (
        enqueue_reflection_debt,
        superseded_queue_rows,
        unemittable_queue_rows,
    )
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path, min_obs=2)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    # A thread id that no detection of this session will ever produce.
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-ghost-partition', ?, 99, 'ghost', 's', 'active')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    enqueue_reflection_debt(cfg.db_path, sid, thread_ids=["t-ghost-partition"])

    superseded = superseded_queue_rows(cfg, sid)
    unemittable = unemittable_queue_rows(cfg, sid)

    assert "t-ghost-partition" in superseded, (
        f"the stale id was not reported as superseded: {superseded}"
    )
    assert "t-ghost-partition" in unemittable, "superseded rows must also be unemittable"
    assert set(superseded) <= set(unemittable), (
        "superseded must be a strict subset of unemittable — it is the provably-stale part"
    )


def test_reconcile_is_dry_by_default_and_spares_the_real_signal(tmp_path):
    """Two claims in one place because they are the same guarantee: the reconciler must not
    write unless asked, and when it does write it must remove ONLY rows whose thread no
    longer exists. #646 forbids draining undrainable rows to green the banner; retiring a
    provably-superseded row is a different act, and the difference is this test."""
    from techne.core.database import connect
    from techne.core.reflect import enqueue_reflection_debt, reconcile_reflection_queue
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path, min_obs=2)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-ghost-partition', ?, 99, 'ghost', 's', 'active')",
            (sid,),
        )
        conn.commit()
        real_rows = [r["thread_id"] for r in conn.execute(
            "SELECT thread_id FROM reflection_queue WHERE session_id = ? AND drained_at IS NULL",
            (sid,))]
    finally:
        conn.close()
    enqueue_reflection_debt(cfg.db_path, sid, thread_ids=["t-ghost-partition"])

    def _pending():
        c = connect(cfg.db_path)
        try:
            return {r["thread_id"] for r in c.execute(
                "SELECT thread_id FROM reflection_queue "
                "WHERE session_id = ? AND drained_at IS NULL", (sid,))}
        finally:
            c.close()

    before = _pending()
    dry = reconcile_reflection_queue(cfg, apply=False)

    assert dry["superseded"] >= 1
    assert dry["removed"] == 0, "a dry run wrote to the ledger"
    assert _pending() == before, "a dry run changed the queue"

    applied = reconcile_reflection_queue(cfg, apply=True)

    assert applied["removed"] >= 1
    after = _pending()
    assert "t-ghost-partition" not in after, "the superseded row survived --apply"
    for tid in real_rows:
        assert tid in after, (
            f"reconcile removed {tid}, a row whose thread IS in the current detection. Only "
            "provably-superseded rows may be retired; the rest is #646's evidence."
        )


# ------------------------------------------------- #797: a store WITH HISTORY


def _link_from_another_session(cfg, target_thread_id):
    """A second session whose thread names `target_thread_id` as its parent.

    THE SHAPE EVERY FIXTURE IN THIS FILE WAS MISSING. `link_threads` links similar
    threads ACROSS sessions, so on a real store one session's threads are routinely the
    parent of another's — 1249 such links on the operator's store when #797 was found.
    A `tmp_path` store built from one session has none, which is why a whole-session
    `DELETE FROM threads` passed every test here and then raised
    `IntegrityError: FOREIGN KEY constraint failed` in production.
    """
    from techne.core.database import connect
    from techne.core.state import start_session

    other = start_session(cfg.db_path, project_path="/tmp/p2", agent_identity="t2")
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status, "
            "parent_thread_id) VALUES ('t-child-of-target', ?, 1, 'child', 's', 'active', ?)",
            (other, target_thread_id),
        )
        conn.commit()
    finally:
        conn.close()
    return other


def test_session_end_survives_a_thread_referenced_by_another_session(tmp_path):
    """techne#797 — THE REGRESSION #793 SHIPPED, and the one this file could not see.

    `threads.parent_thread_id` is `NO ACTION`, so deleting a thread another session points
    at raises IntegrityError and takes `run_session_end` down with it. Reverting
    persist_detection to `DELETE FROM threads WHERE session_id = ?` must fail here."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    # A SUPERSEDED target, not a live one. A thread still in the current detection is
    # excluded by the prune's first condition anyway, so linking to it exercises nothing:
    # the reference guard is only reachable for a row the prune would otherwise take.
    # (Measured — with a live target, deleting the parent guard left this file green.)
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-superseded-parent', ?, 96, 'old partition', 's', 'active')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    target = "t-superseded-parent"
    _link_from_another_session(cfg, target)

    # A later turn re-runs detection over a changed observation set — the path that
    # wiped the table before.
    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)  # must not raise

    conn = connect(cfg.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM threads WHERE id = ?", (target,)
        ).fetchone(), "the referenced parent thread was deleted — its child now dangles"
        assert conn.execute(
            "SELECT 1 FROM threads WHERE id = 't-child-of-target'"
        ).fetchone(), "the child thread in the other session was destroyed"
    finally:
        conn.close()


def test_a_thread_that_produced_a_candidate_is_not_pruned(tmp_path):
    """The second `NO ACTION` reference: `knowledge_candidates.source_thread_id`, 547 live
    rows on the operator's store. Reflection provenance must survive a re-detection."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    # Superseded, for the same reason as the parent-link test above.
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-superseded-source', ?, 95, 'old partition', 's', 'active')",
            (sid,),
        )
        target = "t-superseded-source"
        conn.execute(
            "INSERT INTO knowledge_candidates (scope, type, content, confidence, status, "
            "model_used, prompt_hash, action, origin, source_session_id, source_thread_id) "
            "VALUES ('knowledge','fact','c','high','pending','agent-assisted','h0','add',"
            "'reflect',?,?)",
            (sid, target),
        )
        conn.commit()
    finally:
        conn.close()

    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)  # must not raise

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT source_thread_id FROM knowledge_candidates WHERE source_thread_id = ?",
            (target,),
        ).fetchone()
        assert row, "the candidate's source thread was pruned — provenance destroyed"
    finally:
        conn.close()


def test_a_paid_queue_row_is_never_pruned_away(tmp_path):
    """The third guard, and the one my first fixture did not reach.

    The condition it protects is narrow: a thread that is SUPERSEDED (absent from the
    current detection), UNREFERENCED (nothing points at it), and carries a DRAINED queue
    row. `reflection_queue.thread_id` CASCADEs, so pruning such a row silently deletes a
    payment record for work that really was reflected. One of the 947 prunable rows on the
    operator's store was in exactly this state.

    CONSTRUCTED, NOT COAXED. My first attempt marked a real thread paid and then added
    observations, expecting re-partitioning to supersede it — it did not: thread ids derive
    from `first_obs_id` + `boundary_signal`, and appending LATER observations leaves the
    first cluster's identity untouched. The paid thread stayed in the detection, so the
    prune never considered it and the test passed with the guard deleted. Building the
    state directly is the only way to be sure the guard is the thing under test."""
    from techne.core.database import connect
    from techne.core.reflect import enqueue_reflection_debt
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    # A thread no detection of this session will produce, carrying a PAID queue row.
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-paid-superseded', ?, 98, 'paid ghost', 's', 'active')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    enqueue_reflection_debt(cfg.db_path, sid, thread_ids=["t-paid-superseded"])
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE reflection_queue SET drained_at = '2026-01-01 00:00:00' "
            "WHERE thread_id = 't-paid-superseded'"
        )
        conn.commit()
    finally:
        conn.close()

    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT drained_at FROM reflection_queue WHERE thread_id = 't-paid-superseded'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, (
        "the paid queue row was cascaded away by the prune — a payment record for work "
        "that really was reflected is now gone from the ledger."
    )
    assert row["drained_at"] == "2026-01-01 00:00:00"


def test_an_unpaid_superseded_thread_IS_pruned(tmp_path):
    """The counterweight: the guards must not turn the prune into a no-op.

    Without this, every condition added above could be widened until nothing is ever
    deleted and the whole file would stay green — which is #646's accumulation returning
    by the back door."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-unpaid-superseded', ?, 97, 'ghost', 's', 'active')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        gone = conn.execute(
            "SELECT 1 FROM threads WHERE id = 't-unpaid-superseded'"
        ).fetchone()
    finally:
        conn.close()
    assert gone is None, (
        "a superseded, unreferenced, unpaid thread row survived — the prune has been "
        "narrowed into a no-op and #646's accumulation is back."
    )
