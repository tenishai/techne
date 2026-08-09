"""Outbox retention (#518) + prune-orphans registration (#484).

THE INVARIANT THIS FILE EXISTS TO PROTECT: the delivered rows on a ledger lane
ARE the dedup pre-filter's state (``core/archivist.py`` reads them back), so a
retention policy that ages them out makes the next teardown re-enqueue the whole
knowledge base. That is not hypothetical — a hand-run sweep of 128,200 rows on
2026-08-07 finished at 20:13 and 761 distinct rows re-enqueued at 20:16:01-06.

``test_sweep_preserves_the_prefilter_set`` is the regression guard for exactly
that, and it fails against an age-only sweep. Teeth-checked, not assumed.
"""

from __future__ import annotations

import pytest

from techne.core.archivist import (
    ARCHIVIST_KNOWLEDGE_TARGET,
    LEDGER_TARGETS,
    push_knowledge_to_archivist,
    register_base_archivist,
)
from techne.sync.outbox import enqueue, sweep_delivered, sweep_preview
from techne.sync.sinks import use_registry

NON_LEDGER = "sink:archivist"


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """A non-standalone store with both archivist sinks registered.

    `enqueue` refuses an unregistered sink: target, so registration is a
    precondition of every test here rather than part of what they assert.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    cfg.standalone = False
    cfg.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(cfg.db_path)
    with use_registry({}):
        register_base_archivist(cfg)
        yield cfg


def _kp(label):
    """A valid KnowledgePayload — enqueue validates against the sink's schema."""
    return {"content": label, "type": "fact", "tags": "", "confidence": "high"}


def _tp(label):
    """A valid ThreadSummaryPayload for the non-ledger lane."""
    return {
        "session_id": "s-1",
        "thread_id": label,
        "thread_label": label,
        "summary": label,
        "resume_points": "",
        "agent_identity": "agentA",
    }


def _rows(config, target=None):
    from techne.core.stores import plain_connect

    conn = plain_connect(config.db_path)
    try:
        if target is None:
            return conn.execute("SELECT id, target, status, content_hash FROM outbox").fetchall()
        return conn.execute(
            "SELECT id, target, status, content_hash FROM outbox WHERE target = ?", (target,)
        ).fetchall()
    finally:
        conn.close()


def _set_all(config, **cols):
    """Force status/created_at on every row — the sweep keys off both."""
    from techne.core.stores import plain_connect

    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn = plain_connect(config.db_path)
    try:
        conn.execute(f"UPDATE outbox SET {assignments}", tuple(cols.values()))
        conn.commit()
    finally:
        conn.close()


# ---- the regression guard ---------------------------------------------------


def test_sweep_preserves_the_prefilter_set(config):
    """After a sweep, a re-push must still enqueue NOTHING.

    This is the whole point of #518's design. An age-based sweep of the ledger
    lane passes every other test in this file and fails this one, because the
    pre-filter's set is gone and every fact looks new again.
    """
    from techne.core.knowledge import store

    for i in range(5):
        store(config.db_path, f"durable fact number {i}", type="fact", confidence="high")

    assert push_knowledge_to_archivist(config) == 5

    # Delivered and old — the state an age-based sweep would delete outright.
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    sweep_delivered(config.db_path, retain_days=7)

    assert push_knowledge_to_archivist(config) == 0, (
        "the sweep destroyed the dedup ledger — a re-push re-enqueued the "
        "knowledge base, which is the bug #518 exists to prevent"
    )


def test_collapse_keeps_exactly_one_row_per_hash(config):
    for _ in range(4):
        enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("same"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("other"), content_hash="h2")
    _set_all(config, status="synced")

    result = sweep_delivered(config.db_path)

    assert result["collapsed"] == 3
    hashes = sorted(r[3] for r in _rows(config, ARCHIVIST_KNOWLEDGE_TARGET))
    assert hashes == ["h1", "h2"]


def test_collapse_prefers_a_non_dead_survivor(config):
    """The pre-filter's query excludes dead rows.

    Keeping the NEWEST row when it happens to be dead would drop that hash from
    the set the pre-filter can see — reintroducing the re-enqueue bug via the fix
    for it. Ordering must prefer a live row over a newer dead one.
    """
    from techne.core.stores import plain_connect

    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    conn = plain_connect(config.db_path)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM outbox ORDER BY id").fetchall()]
        conn.execute("UPDATE outbox SET status = 'synced' WHERE id = ?", (ids[0],))
        conn.execute("UPDATE outbox SET status = 'dead' WHERE id = ?", (ids[1],))
        conn.commit()
    finally:
        conn.close()

    sweep_delivered(config.db_path)

    survivors = _rows(config, ARCHIVIST_KNOWLEDGE_TARGET)
    assert len(survivors) == 1
    assert survivors[0][2] == "synced", (
        "collapse kept the dead row; the pre-filter excludes dead, so that hash would re-enqueue"
    )


# ---- what the sweep must never touch ---------------------------------------


def test_sweep_never_deletes_pending_rows(config):
    """Pending rows are undelivered work; sweeping one loses it silently.

    Note the DISTINCT hashes: `idx_outbox_dedup` is UNIQUE on content_hash
    WHERE status = 'pending', so duplicate pending rows cannot exist by
    construction. Only delivered rows accumulate — which is precisely why the
    collapse policy has anything to collapse.
    """
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a2"), content_hash="h2")
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="pending", created_at="2020-01-01 00:00:00")

    result = sweep_delivered(config.db_path, retain_days=0)

    assert result == {"collapsed": 0, "aged_out": 0}
    assert len(_rows(config)) == 3, "a pending row was swept before it was ever delivered"


def test_collapse_never_deletes_a_pending_row_sharing_a_hash(config):
    """The collapse partitions by (target, content_hash) ACROSS statuses.

    A pending row that shares a hash with a NEWER delivered row loses the
    `id DESC` tiebreak, so without the `status != 'pending'` filter it would be
    collapsed away — silently discarding work that was never delivered. The
    partial unique index only bars two PENDING rows sharing a hash; it says
    nothing about pending-vs-synced.

    Written because mutation-testing found the guard uncovered: deleting the
    filter left every other test in this file green.
    """
    from techne.core.stores import plain_connect

    # Built directly rather than via enqueue(): with a sink registered, enqueue
    # attempts immediate delivery, so the row would already be 'synced' and the
    # state under test could never be reached.
    conn = plain_connect(config.db_path)
    try:
        # Older row is PENDING; a NEWER row carries the same hash, delivered.
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "pending"),
        )
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "synced"),
        )
        conn.commit()
        # Precondition: assert the state actually exists before exercising the
        # code, so a setup that silently did nothing fails loudly (techne#912).
        assert [
            r[0]
            for r in conn.execute("SELECT status FROM outbox WHERE content_hash = 'h1' ORDER BY id")
        ] == ["pending", "synced"]
    finally:
        conn.close()

    sweep_delivered(config.db_path)

    statuses = sorted(r[2] for r in _rows(config, ARCHIVIST_KNOWLEDGE_TARGET))
    assert "pending" in statuses, (
        "the collapse deleted an undelivered row that shared a hash with a newer "
        "delivered one — that work is now lost"
    )


def test_sweep_keeps_dead_rows_on_non_ledger_lanes(config):
    """`list_dead` reads these — they are diagnostic, not debris."""
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="dead", created_at="2020-01-01 00:00:00")

    sweep_delivered(config.db_path, retain_days=0)

    assert len(_rows(config, NON_LEDGER)) == 1


# ---- the age policy on the other lane ---------------------------------------


def test_non_ledger_lane_ages_out(config):
    enqueue(config.db_path, NON_LEDGER, _tp("old"), content_hash="h1")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    enqueue(config.db_path, NON_LEDGER, _tp("new"), content_hash="h2")

    result = sweep_delivered(config.db_path, cutoff="2021-01-01 00:00:00")

    assert result["aged_out"] == 1
    assert len(_rows(config, NON_LEDGER)) == 1


def test_non_ledger_lane_is_not_collapsed_by_hash(config):
    """Unique events, not repeats of a bounded set.

    Collapsing here would silently drop distinct deliveries that merely share a
    payload, and reclaims ~21% against age's ~99% anyway.
    """
    for _ in range(3):
        enqueue(config.db_path, NON_LEDGER, _tp("same"), content_hash="dup")
    _set_all(config, status="synced")

    result = sweep_delivered(config.db_path, cutoff="2000-01-01 00:00:00")

    assert result["collapsed"] == 0
    assert len(_rows(config, NON_LEDGER)) == 3


# ---- the dry run has to describe the real thing -----------------------------


def test_preview_matches_apply_exactly(config):
    for _ in range(3):
        enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")

    preview = sweep_preview(config.db_path, retain_days=0)
    before = len(_rows(config))
    applied = sweep_delivered(config.db_path, retain_days=0)
    after = len(_rows(config))

    assert (preview["collapsed"], preview["aged_out"]) == (
        applied["collapsed"],
        applied["aged_out"],
    )
    assert preview["retained"] == after
    assert before - after == applied["collapsed"] + applied["aged_out"]


def test_preview_does_not_mutate(config):
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    _set_all(config, status="synced")

    sweep_preview(config.db_path, retain_days=0)

    assert len(_rows(config)) == 2


# ---- the drift guard --------------------------------------------------------


def test_ledger_targets_covers_every_prefilter():
    """LEDGER_TARGETS must name every target read back as an enqueue ledger.

    Derived from the source, not from a second hand-typed list: any module
    outside `sync/outbox.py` that SELECTs content_hash FROM outbox is consulting
    enqueue history, and its target has to be declared or the sweep will delete
    state that module depends on.

    Adding such a read without declaring the target fails HERE rather than in
    production three weeks later.
    """
    import re
    from pathlib import Path

    import techne

    root = Path(techne.__file__).parent
    readers = set()
    for path in root.rglob("*.py"):
        if path.name == "outbox.py" and path.parent.name == "sync":
            continue  # the module that OWNS the table, not a consumer of its history
        # `content_hash` need not be the LAST column selected. An earlier version
        # required `content_hash\s+FROM`, so the most ordinary spelling of a new
        # consumer — `SELECT content_hash, status FROM outbox` — walked straight
        # past the guard. Not a remote shape: the archivist pre-filter already
        # predicates on status, so a maintainer adding it to that select list
        # would have silently disabled the guard protecting the query they were
        # editing.
        if re.search(r"content_hash\b[^;]*?\bFROM\s+outbox", path.read_text(), re.I | re.S):
            readers.add(str(path.relative_to(root)))

    # ASSERT THE SET, DO NOT SEARCH THE TEXT FOR A DECLARATION. An earlier version
    # of this test checked whether each reader's file MENTIONED a LEDGER_TARGETS
    # member — which the declaration itself satisfies, so it passed while the
    # declaration was pointed at a target the pre-filter never queries. It was
    # hollow, and mutation-testing is what exposed it.
    #
    # A new entry here is not necessarily a bug: it means someone added a second
    # consumer of enqueue history, and its target has to join LEDGER_TARGETS or
    # the sweep will delete state it depends on. Failing loudly is the point.
    assert readers == {"core/archivist.py"}, (
        f"the set of modules reading outbox enqueue history changed: {sorted(readers)}. "
        f"Add the new reader's target to LEDGER_TARGETS (and cover it in "
        f"test_sweep_preserves_the_prefilter_set) before updating this assertion."
    )


# ---- #484: prune-orphans must register sinks before judging orphanhood -------


def test_prune_orphans_leaves_a_registered_lane_alone(tmp_path, monkeypatch):
    """A registered lane must not present as unregistered in a fresh process.

    Sinks are PER-PROCESS state. The CLI builds a new process with an empty
    registry, so without `ensure_registered_for` every sink:archivist_knowledge
    row looks like a row for an unknown target and the whole pending backlog is
    marked dead. The suite had nothing covering the REGISTERED case, which is why
    this survived.

    Invoked through the CLI on purpose: the defect lives in the command's wiring,
    not in `prune_orphans`, so a unit test that pre-registers would pass against
    the broken code.
    """
    from click.testing import CliRunner

    from techne.cli import main
    from techne.core.database import init_db
    from techne.core.stores import plain_connect

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    cfg = Config.load()
    cfg.standalone = False
    cfg.archivist_db_path = str(tmp_path / "shared-archivist.db")
    cfg.save(tmp_path / "config.yaml")
    init_db(cfg.db_path)

    conn = plain_connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "pending"),
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["outbox", "prune-orphans"])
    assert result.exit_code == 0, result.output

    conn = plain_connect(cfg.db_path)
    try:
        status = conn.execute("SELECT status FROM outbox WHERE content_hash = 'h1'").fetchone()[0]
    finally:
        conn.close()

    assert status == "pending", (
        "prune-orphans marked a REGISTERED lane's backlog dead — the sink registry "
        "was empty because the command never called ensure_registered_for (#484)"
    )


def test_ledger_targets_is_not_empty():
    """A silently empty set would make the sweep age out the ledger lane."""
    assert ARCHIVIST_KNOWLEDGE_TARGET in LEDGER_TARGETS


def test_age_out_always_leaves_one_row_per_target(config):
    """A swept lane must never present as a lane that never existed.

    `check_outbox_targets` reads outbox history per target and falls into its
    `_declared_outbox_targets - seen` branch when a target has no rows at all —
    reporting "declared lane has NEVER enqueued anything", which is false and
    which REPLACES the STALE diagnosis that check exists to produce. The two
    thresholds make it worse than incidental: `retain_days` defaults to 7 and
    `_PRODUCER_SILENT_AFTER` is also 7 days, so the evidence is destroyed in
    exactly the window where it becomes actionable.
    """
    for i in range(3):
        enqueue(config.db_path, NON_LEDGER, _tp(f"old{i}"), content_hash=f"h{i}")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")

    result = sweep_delivered(config.db_path, retain_days=0)

    rows = _rows(config, NON_LEDGER)
    assert result["aged_out"] == 2, "expected all but the newest row to go"
    assert len(rows) == 1, "the lane was emptied — check_outbox_targets now reads it as never used"


def test_a_swept_lane_is_still_visible_to_check_outbox_targets(config):
    """The property the tombstone exists for, asserted against the real query.

    Pinned against `check_outbox_targets`' own SQL rather than a paraphrase, so
    this fails if that check's grouping changes shape.
    """
    from techne.core.stores import plain_connect

    enqueue(config.db_path, NON_LEDGER, _tp("only"), content_hash="h1")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    sweep_delivered(config.db_path, retain_days=0)

    conn = plain_connect(config.db_path)
    try:
        seen = {
            r[0]
            for r in conn.execute(
                "SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending'), "
                "MAX(created_at) FROM outbox GROUP BY target"
            )
        }
    finally:
        conn.close()

    assert NON_LEDGER in seen, (
        "the lane vanished from the check's GROUP BY — it will report "
        "'declared lane has NEVER enqueued anything'"
    )
