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
        # NOT asserting the child row survives: the DELETE is scoped
        # `WHERE session_id = ?`, so no mutation of this predicate can falsify that.
        # It would read as coverage it is not (Prudence, #798 r1 nit 4).
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


def test_an_observation_attributed_to_a_thread_blocks_its_prune(tmp_path):
    """THE FOURTH `NO ACTION` FK, which my predicate enumerated in a comment and then did
    not guard (Prudence, #798 r1). `observations.thread_id` references `threads(id)` with
    NO ACTION exactly like `parent_thread_id`, and she built the state and reproduced the
    same `IntegrityError` on the same line.

    Nothing in the tree writes that column today — every `INSERT INTO observations` and
    every `UPDATE ... SET thread_id` was checked — so there is no live hazard. That is the
    point: `parent_thread_id` was also "unused in practice", on a fresh store, for about
    five days before #797. The column exists and is indexed, so per-thread observation
    attribution is an obvious next feature, and without this guard whoever wires it up
    re-detonates #797 on the per-turn path."""
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
            "VALUES ('t-obs-attributed', ?, 94, 'old partition', 's', 'active')",
            (sid,),
        )
        conn.execute(
            "UPDATE observations SET thread_id = 't-obs-attributed' "
            "WHERE session_id = ? AND id = (SELECT MIN(id) FROM observations WHERE session_id = ?)",
            (sid, sid),
        )
        conn.commit()
    finally:
        conn.close()

    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)  # must not raise

    conn = connect(cfg.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM threads WHERE id = 't-obs-attributed'"
        ).fetchone(), "a thread with an observation attributed to it was pruned"
    finally:
        conn.close()


def test_the_prune_guards_every_no_action_fk(tmp_path):
    """DERIVE THE GUARD LIST FROM THE SCHEMA, NOT FROM THE INCIDENT (Prudence, #798 r1).

    The predicate was written from the columns that broke rather than the columns that CAN
    break, which is why `observations.thread_id` was missed. This asks SQLite what actually
    references `threads(id)` without a cascade and requires each one to appear in
    `persist_detection`'s prune. It would have caught that miss on the day it was written,
    and it catches the fifth column on the day someone adds it."""
    import inspect

    from techne.core.database import connect, init_db
    from techne.lifecycle import persist_detection

    db = tmp_path / "schema.db"
    init_db(db)
    conn = connect(db)
    try:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        unguarded = []
        for t in tables:
            for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
                if fk["table"] == "threads" and (fk["on_delete"] or "").upper() != "CASCADE":
                    unguarded.append((t, fk["from"]))
    finally:
        conn.close()

    assert unguarded, "no NO ACTION FK to threads found — the probe is not measuring anything"

    # ANCHOR ON THE SQL, NOT THE COMMENT, AND REQUIRE TABLE + COLUMN IN THE SAME CLAUSE
    # (Prudence, #798 r2). The first version searched from `src.index("DELETE FROM
    # threads")`, which matches inside the explanatory comment above the query — so a
    # guard written in PROSE would have counted. Worse, it looked only for
    # `.<col> = threads.id`, and THREE of the four referencing columns are named
    # `thread_id`: the cascading `reflection_queue.thread_id` clause is in the searched
    # text, so it satisfied the needle that `observations.thread_id` was supposed to
    # require. She proved it by adding a real fifth column
    # (`thread_notes.thread_id`, entirely unguarded) — the test still passed. Splitting
    # on NOT EXISTS and demanding `FROM <table>` and `.<col> = threads.id` in the SAME
    # clause is what makes the forward-looking claim true rather than decorative.
    src = inspect.getsource(persist_detection)
    start = src.index('"""DELETE FROM threads')
    prune = " ".join(
        src[start : src.index("DELETE FROM thread_boundaries", start)].split()
    )
    clauses = prune.split("NOT EXISTS")
    missing = [
        f"{table}.{col}"
        for table, col in unguarded
        if not any(
            f"FROM {table} " in c and f".{col} = threads.id" in c for c in clauses
        )
    ]

    assert not missing, (
        f"these columns reference threads(id) with NO ACTION and are NOT guarded by the "
        f"prune: {missing}. Deleting a thread they point at raises IntegrityError and takes "
        "run_session_end down — that is techne#797. Add a NOT EXISTS clause for each."
    )


def test_a_kept_threads_pending_debt_does_not_age_backwards(tmp_path):
    """Condition (1) preserves DEBT AGE, and nothing else pinned it (Prudence, #798 r1).

    She mutated `keep` to the legacy `f"{session_id}-{thread_num}"` id form — which the
    codebase still contains — so it matched nothing and every current thread was pruned and
    re-inserted. The whole file stayed green. The cost is not cosmetic: the CASCADE takes a
    still-current thread's PENDING queue row and the re-enqueue re-inserts it with today's
    date. `prior_drained` restores drained stamps; nothing restores `enqueued_at`, which is
    the column the SessionStart banner's "oldest from" reads. Under a drifted `keep` every
    turn resets the age of every open debt to now, forever, with no error."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        rows = [r["thread_id"] for r in conn.execute(
            "SELECT thread_id FROM reflection_queue WHERE session_id = ? AND drained_at IS NULL",
            (sid,))]
        assert rows, "fixture queued no pending debt"
        conn.execute(
            "UPDATE reflection_queue SET enqueued_at = '2020-01-01 00:00:00' "
            "WHERE thread_id = ?", (rows[0],))
        conn.commit()
    finally:
        conn.close()

    _add(cfg, sid, 5, start_hour=15, tool="Edit")
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        aged = conn.execute(
            "SELECT enqueued_at FROM reflection_queue WHERE thread_id = ?", (rows[0],)
        ).fetchone()
    finally:
        conn.close()

    assert aged is not None, "the kept thread's pending queue row was deleted"
    assert aged["enqueued_at"] == "2020-01-01 00:00:00", (
        f"a kept thread's pending debt was re-enqueued at {aged['enqueued_at']} — its age "
        "was reset, so the banner's 'oldest from' will read as minutes old forever."
    )


def test_a_kept_threads_summary_still_refreshes(tmp_path):
    """Sparing a current thread must not FREEZE it (Prudence, #798 r1, Important 2).

    `save_threads` used `INSERT OR IGNORE`, which was only correct while the caller deleted
    first. Once the prune spares a thread whose id is unchanged — every turn after the first
    for a single-thread session — the insert no-opped and `label`/`summary`/`resume_points`
    stayed as the FIRST detection wrote them. Nothing else refreshes those columns, so
    `techne_resume` and `threads_fts` would have served the opening minutes of a session
    forever. It is now an upsert."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        first = conn.execute(
            "SELECT id, summary FROM threads WHERE session_id = ?", (sid,)
        ).fetchone()
    finally:
        conn.close()

    # More observations in the SAME cluster: the thread's id is stable, so the prune spares
    # it — which is exactly the case that used to freeze.
    _add(cfg, sid, 6, start_hour=12, day=1, tool="Read")
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        after = conn.execute(
            "SELECT summary FROM threads WHERE id = ?", (first["id"],)
        ).fetchone()
    finally:
        conn.close()

    assert after is not None, "the thread vanished"
    assert after["summary"] != first["summary"], (
        f"a kept thread's summary did not refresh: still {after['summary']!r}. It froze at "
        "the first detection, which is what INSERT OR IGNORE did before the upsert."
    )


def test_a_kept_threads_fts_row_follows_its_summary(tmp_path):
    """THE OTHER HALF OF THE UPSERT'S RATIONALE, which had no direct witness (Prudence,
    #798 r2 nit 4). The comment on `save_threads` justifies DO UPDATE over OR REPLACE by
    the `threads_fts` shadow — and she confirmed `test_a_kept_threads_summary_still_
    refreshes` PASSES under `INSERT OR REPLACE`, because it reads only the `threads` table.
    So the stated reason was unheld; OR REPLACE was caught only incidentally, by the
    debt-age test tripping first.

    `INSERT OR REPLACE` is DELETE+INSERT, and with `recursive_triggers` off the implicit
    delete does not fire `threads_ad` — so the FTS keeps a row for content that no longer
    exists and thread search matches stale summaries. `DO UPDATE` fires `threads_au`, which
    removes the old FTS row and inserts the new one."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT id, summary FROM threads WHERE session_id = ?", (sid,)
        ).fetchone()
        # A token that exists only in the FIRST summary, so a stale FTS row is detectable.
        conn.execute(
            "UPDATE threads SET summary = 'zzuniqueoriginal' WHERE id = ?", (row["id"],)
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM threads_fts WHERE threads_fts MATCH 'zzuniqueoriginal'"
        ).fetchone(), "fixture failed: the marker never reached the FTS index"
    finally:
        conn.close()

    _add(cfg, sid, 6, start_hour=12, day=1, tool="Read")
    run_session_end(cfg, sid)

    conn = connect(cfg.db_path)
    try:
        stale = conn.execute(
            "SELECT 1 FROM threads_fts WHERE threads_fts MATCH 'zzuniqueoriginal'"
        ).fetchone()
    finally:
        conn.close()

    assert stale is None, (
        "threads_fts still matches the replaced summary — the shadow index is serving "
        "content the threads table no longer holds, so thread search returns stale hits."
    )


def test_a_stored_parent_link_survives_a_turn_that_cannot_rederive_it(tmp_path):
    """techne#797 / Prudence #798 r2 Important 1 — AND MY OWN FIFTH HOLLOW SPOT.

    I applied her one-word `COALESCE` fix and shipped it with NO witness: reverting it to a
    bare `excluded.parent_thread_id` left 308 tests passing. This is that witness.

    `parent_id` comes from `link_map.get(thread_id)`, which is None whenever `link_threads`
    did not re-derive a parent this turn. Under `INSERT OR IGNORE` that was harmless — the
    row was never updated. Under a naive upsert it writes None over a link already stored.

    The trigger is a CLOCK, not a race: `link_threads` skips candidates with
    `status='stale'`, and `age_threads` (called by `run_session_end` every turn) walks
    active -> completed -> stale, so roughly 32 days after a link is written its parent
    stops being re-derivable and the child's link is silently erased. Driven here through
    `save_threads` directly, with an empty link map standing in for that clock — the unit
    under test is the conflict clause, not the ageing schedule."""
    from techne.core.database import connect
    from techne.core.state import start_session
    from techne.core.threads import Thread, save_threads

    cfg = _config(tmp_path)
    parent_sid = start_session(cfg.db_path, project_path="/tmp/p1", agent_identity="t")
    child_sid = start_session(cfg.db_path, project_path="/tmp/p2", agent_identity="t")

    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-the-parent', ?, 1, 'parent', 's', 'active')",
            (parent_sid,),
        )
        conn.commit()
    finally:
        conn.close()

    child = Thread(thread_num=1, label="child", summary="v1", observation_indices=[0])
    child_id = f"{child_sid}-1"
    save_threads([child], child_sid, cfg.db_path, parent_links=[(child_id, "t-the-parent")])

    conn = connect(cfg.db_path)
    try:
        linked = conn.execute(
            "SELECT parent_thread_id FROM threads WHERE id = ?", (child_id,)
        ).fetchone()["parent_thread_id"]
    finally:
        conn.close()
    assert linked == "t-the-parent", f"fixture failed to establish the link: {linked!r}"

    # A later turn where link_threads re-derives NOTHING — the parent has aged out.
    child_v2 = Thread(thread_num=1, label="child", summary="v2", observation_indices=[0])
    save_threads([child_v2], child_sid, cfg.db_path, parent_links=[])

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT parent_thread_id, summary FROM threads WHERE id = ?", (child_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["summary"] == "v2", "the upsert did not refresh content — wrong failure"
    assert row["parent_thread_id"] == "t-the-parent", (
        "a stored parent link was erased by a turn that could not re-derive it. "
        "`excluded.parent_thread_id` is NULL here; it must be COALESCEd with the stored "
        "value, because there is no integrity reason to ever clear this column."
    )


# ------------------------------------ #805 / #870: the split as numbers, and parking


def _one_of_each(tmp_path):
    """A session owing exactly one superseded row and one below-threshold row.

    The superseded row is the ghost-partition shape used above. The below-threshold row
    is the session's real thread, pushed under the floor by raising it after enqueue —
    the lowered/raised-floor route #870 names, not the retracted #646 cause.
    """
    from techne.core.database import connect
    from techne.core.reflect import enqueue_reflection_debt
    from techne.core.state import start_session
    from techne.lifecycle import run_session_end

    cfg = _config(tmp_path, min_obs=2)
    sid = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    _add(cfg, sid, 6, start_hour=12)
    run_session_end(cfg, sid)
    conn = connect(cfg.db_path)
    try:
        real = [r["thread_id"] for r in conn.execute(
            "SELECT thread_id FROM reflection_queue WHERE session_id = ?", (sid,))]
        conn.execute(
            "INSERT INTO threads (id, session_id, thread_num, label, summary, status) "
            "VALUES ('t-ghost-partition', ?, 99, 'ghost', 's', 'active')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    assert len(real) == 1, f"fixture expected one real queued thread, got {real}"
    enqueue_reflection_debt(cfg.db_path, sid, thread_ids=["t-ghost-partition"])
    cfg.reflect.min_observations_per_thread = 9999
    return cfg, sid, real[0]


def test_emit_reports_the_unemittable_split_as_numbers(tmp_path):
    """techne#805: the note prints two numbers the database produced, not prose.

    A substring pin on "superseded"/"below"/"reconcile" was defeated by a rewrite that
    kept every token while asserting one population. A note that claims every row is
    below-threshold cannot print `1 superseded` here."""
    from techne.core.reflect import emit_reflection_pass

    cfg, sid, _ = _one_of_each(tmp_path)
    result = emit_reflection_pass(cfg, sid)

    assert result["unemittable_queued"] == 2
    assert result["unemittable_superseded"] == 1
    assert result["unemittable_below_threshold"] == 1
    note = result["unemittable_note"]
    assert "1 superseded" in note and "1 below" in note, note


def _pending_ids(cfg):
    from techne.core.reflect import pending_reflection_queue

    return {q["thread_id"] for q in pending_reflection_queue(cfg.db_path)}


def test_reconcile_apply_parks_below_threshold_rows_out_of_the_debt(tmp_path):
    """techne#870: a below-threshold row leaves the actionable debt but not the table.

    Parking is not draining. The row keeps `drained_at` NULL, `reconcile` still reports
    it as below_threshold, and a dry run changes nothing."""
    from techne.core.database import connect
    from techne.core.reflect import (
        parked_reflection_count,
        reconcile_reflection_queue,
        reflection_debt_stats,
    )

    cfg, sid, real = _one_of_each(tmp_path)

    dry = reconcile_reflection_queue(cfg, apply=False)
    assert dry["below_threshold"] == 1 and dry["parked"] == 0
    assert real in _pending_ids(cfg), "a dry run parked a row"

    applied = reconcile_reflection_queue(cfg, apply=True)
    assert applied["parked"] == 1
    assert real not in _pending_ids(cfg), "a parked row is still counted as owed"
    assert parked_reflection_count(cfg.db_path) == (1, 1)
    stats = reflection_debt_stats(cfg)
    assert stats["pending"] == 0 and stats["parked"] == 1 and stats["drained"] == 0

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT drained_at, parked_reason FROM reflection_queue WHERE thread_id = ?",
            (real,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["drained_at"] is None, "parking drained or deleted the row"
    assert row["parked_reason"] == "below_threshold"

    again = reconcile_reflection_queue(cfg, apply=True)
    assert again["below_threshold"] == 1, "a parked row vanished from reconcile's report"
    assert again["parked"] == 0


def test_a_parked_row_unparks_when_it_renders_again(tmp_path):
    """techne#870: parking is about the CURRENT threshold. Each route back un-parks:
    reconcile re-checking, emit rendering the thread, and session_end re-enqueueing it."""
    from techne.core.reflect import (
        emit_reflection_pass,
        enqueue_reflection_debt,
        reconcile_reflection_queue,
    )

    def _parked(tmp):
        cfg, sid, real = _one_of_each(tmp)
        reconcile_reflection_queue(cfg, apply=True)
        assert real not in _pending_ids(cfg)
        cfg.reflect.min_observations_per_thread = 2  # the floor comes back down
        return cfg, sid, real

    cfg, _, real = _parked(tmp_path / "reconcile")
    assert reconcile_reflection_queue(cfg, apply=True)["unparked"] == 1
    assert real in _pending_ids(cfg)

    cfg, sid, real = _parked(tmp_path / "emit")
    emit_reflection_pass(cfg, sid)
    assert real in _pending_ids(cfg), "emit rendered a parked thread and left it parked"

    cfg, sid, real = _parked(tmp_path / "enqueue")
    enqueue_reflection_debt(cfg.db_path, sid, thread_ids=[real])
    assert real in _pending_ids(cfg), "re-enqueueing an emittable thread left it parked"


def test_the_banner_counts_parked_rows_apart_from_the_debt(tmp_path):
    """techne#870: the debt figure is what reflecting can drain, and it names a drainable
    row as the place to start. Parked rows get their own line, not silence."""
    from techne.core.reflect import reconcile_reflection_queue
    from techne.hooks.session_start import _run_phases_2_to_9

    cfg, _, _ = _one_of_each(tmp_path)
    cfg.reflect.enabled = True
    reconcile_reflection_queue(cfg, apply=True)

    parts: list[str] = []
    _run_phases_2_to_9(
        cfg,
        {"cwd": str(tmp_path), "session_id": "other-session"},
        run_ts="2026-01-01T00:00:00+00:00",
        db_path=cfg.db_path,
        output_parts=parts,
        failures=[],
    )
    text = "\n".join(parts)
    assert "1 queued thread(s) across 1 session(s) are parked" in text, text
    # --apply retired the superseded ghost, so the one undrained row is parked and
    # nothing is drainable: no debt figure, and no row offered as the place to start.
    assert "queued for reflection" not in text, text
