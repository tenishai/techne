"""Base Archivist + the #224 topology lane selector (techne#346 PR 1).

standalone=true keeps the sync lane (unchanged); standalone=false routes thread
summaries to sink:archivist, which writes them into a techne-schema archivist.db.
The read-back loop (federation) is PR 2.
"""

from __future__ import annotations

import json

import pytest

from techne.core.archivist import (
    ARCHIVIST_KNOWLEDGE_TARGET,
    ARCHIVIST_SINK,
    archivist_connection,
    archivist_db,
    ensure_registered_for,
    push_knowledge_to_archivist,
    register_base_archivist,
)
from techne.lifecycle import (
    ARCHIVIST_SINK_TARGET,
    THREAD_SUMMARY_SYNC_TARGET,
    _has_live_consumer,
    _thread_summary_target,
)
from techne.sync.sinks import get_sink, use_registry


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    return Config.load()


def _payload():
    return {
        "session_id": "src-sess",
        "thread_id": "t-1",
        "thread_label": "Work on X",
        "summary": "did the thing",
        "resume_points": "next: Y",
        "agent_identity": "agentA",
    }


# ---- config -----------------------------------------------------------------


def test_standalone_defaults_true_and_round_trips(config, tmp_path):
    from techne.config import Config

    assert config.standalone is True  # behaviour-preserving default
    config.standalone = False
    config.archivist_db_path = "/tmp/custom-archivist.db"
    path = tmp_path / "rt.yaml"
    config.save(path)
    reloaded = Config.load(path)
    assert reloaded.standalone is False
    assert reloaded.archivist_db_path == "/tmp/custom-archivist.db"


# ---- lane selector (#224) ---------------------------------------------------


def test_raw_thread_summaries_never_take_the_archivist_lane(config):
    """DISTILLED-ONLY EGRESS (techne#552 part 3). A thread summary is RAW operational
    memory, and the Archivist federates only the distilled — but nothing enforced that
    at write time, so `sink:archivist` carried raw across the seam for every
    non-standalone instance. MEASURED before this change: archivist.db held 17,819
    observations against 1,104 knowledge rows, so RAW WAS 94% OF THE TEAM STORE.

    The lane is now the same whatever the topology says, which is the point: topology
    selects a BINDING, never a lane.
    """
    for standalone in (True, False):
        config.standalone = standalone
        assert _thread_summary_target(config) == THREAD_SUMMARY_SYNC_TARGET

    config.archivist_db_path = "/tmp/some-team-store.db"
    assert _thread_summary_target(config) == THREAD_SUMMARY_SYNC_TARGET, (
        "a store binding must not resurrect the raw lane — that is the whole of part 3"
    )


def test_live_consumer_follows_the_one_lane_summaries_actually_use(config):
    """Summaries always take the sync lane now, so the only consumer that decides
    whether enqueueing is safe is the sync adapter. A `noop` adapter means nobody
    drains, and enqueueing into an undrainable lane grows a backlog nobody asked for
    (#79, #224)."""
    config.sync.adapter = "noop"
    assert _has_live_consumer(config) is False

    config.sync.adapter = "filesystem"
    assert _has_live_consumer(config) is True


def test_archivist_db_is_techne_schema(config):
    db = archivist_db(config)
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    # A real techne schema so federation can read it as a team connection (PR 2).
    assert {"knowledge", "observations"} <= tables


def test_default_archivist_path_under_data_dir(config):
    assert archivist_db(config) == config.data_dir / "archivist.db"


def test_push_lands_in_archivist_as_observation(config):
    """End-to-end: enqueue to sink:archivist → the summary is written into
    archivist.db as a raw thread_summary observation (§3)."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue

    init_db(config.db_path)
    ensure_session(config.db_path, "src-sess")

    with use_registry({}):
        register_base_archivist(config)
        enqueue(config.db_path, ARCHIVIST_SINK_TARGET, _payload(), deliver=True)

    conn = plain_connect(archivist_db(config))
    try:
        rows = conn.execute(
            "SELECT content FROM observations WHERE tool_name = 'thread_summary'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert "Work on X" in rows[0]["content"]
    assert "did the thing" in rows[0]["content"]


def test_register_base_archivist_is_idempotent(config):
    with use_registry({}):
        register_base_archivist(config)
        register_base_archivist(config)  # must not raise (same singleton)
        assert get_sink(ARCHIVIST_SINK_TARGET) is ARCHIVIST_SINK


def test_archivist_db_rejects_personal_db_collision(config):
    """Guard: archivist_db_path must not be the personal techne db, else team intake
    pollutes personal memory (techne#346 review)."""
    config.archivist_db_path = str(config.db_path)
    with pytest.raises(ValueError, match="separate team store"):
        archivist_db(config)


def test_ensure_registered_for_is_unconditional(config):
    """REGISTRATION IS NOT A GATE, AND MAKING IT ONE WAS A REGRESSION (techne#552).

    Registering a sink only makes DELIVERY POSSIBLE. An UNREGISTERED target is what
    `techne outbox prune-orphans` treats as an orphan and marks DEAD — permanently
    undeliverable. Every box that was non-standalone before this change has rows in
    these lanes (11,442 on the author's), so gating registration on the binding would
    have destroyed any pending one at the first prune after upgrade.

    The gate belongs on ENQUEUE — `_thread_summary_target` and
    `push_knowledge_to_archivist` decide whether NEW rows exist; this decides whether
    already-promised ones can still be delivered.
    """
    from techne.core.archivist import ARCHIVIST_KNOWLEDGE_TARGET

    for binding in ("none", "local", ""):
        with use_registry({}):
            config.archivist = binding
            ensure_registered_for(config)
            assert get_sink(ARCHIVIST_SINK_TARGET) is not None, binding
            assert get_sink(ARCHIVIST_KNOWLEDGE_TARGET) is not None, binding


def test_pending_sink_row_drains_from_fresh_process(config):
    """The recovery path (techne#346 review): a sink:archivist row deferred at
    enqueue is drainable by a later flush_sinks in a FRESH process (empty registry)
    once ensure_registered_for re-registers the sink — as the CLI/hook now do."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue, flush_sinks

    init_db(config.db_path)
    ensure_session(config.db_path, "src-sess")
    config.standalone = False

    # Session A: enqueue with delivery DEFERRED (row stays pending), then registry gone.
    with use_registry({}):
        register_base_archivist(config)
        enqueue(config.db_path, ARCHIVIST_SINK_TARGET, _payload(), deliver=False)

    # Fresh process (empty registry): ensure_registered_for re-registers, flush drains.
    with use_registry({}):
        ensure_registered_for(config)
        summary = flush_sinks(config.db_path)

    assert summary.get(ARCHIVIST_SINK_TARGET, (0, 0))[0] == 1  # 1 synced
    conn = plain_connect(archivist_db(config))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE tool_name = 'thread_summary'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ---- PR 2: knowledge egress + federation read-back loop ---------------------


def test_knowledge_push_lands_in_archivist_knowledge_table(config):
    from techne.core.database import init_db
    from techne.core.stores import plain_connect
    from techne.sync.outbox import enqueue

    init_db(config.db_path)
    config.standalone = False
    with use_registry({}):
        register_base_archivist(config)
        enqueue(
            config.db_path, ARCHIVIST_KNOWLEDGE_TARGET,
            {"content": "fact X", "type": "fact", "tags": "t", "confidence": "high"},
            deliver=True,
        )
    conn = plain_connect(archivist_db(config))
    try:
        row = conn.execute("SELECT content, source FROM knowledge").fetchone()
    finally:
        conn.close()
    assert row["content"] == "fact X"
    assert row["source"] == "team"  # marks team provenance


def test_push_knowledge_noop_when_standalone(config):
    from techne.core.database import init_db
    from techne.core.knowledge import store

    init_db(config.db_path)
    store(config.db_path, "local fact", type="fact")
    config.standalone = True
    with use_registry({}):
        assert push_knowledge_to_archivist(config) == 0


def test_push_knowledge_is_idempotent(config):
    """Re-pushing doesn't duplicate — the outbox dedups by content-hash (#346 PR2).

    Binds an explicit STORE rather than setting `standalone = False`: since techne#552
    the push is a no-op unless the archivist is somewhere ELSE, because under `local`
    the "team" knowledge IS this instance's knowledge and the copy was a measured 98.9%
    mirror costing 93% of session teardown. The idempotence being asserted is a
    property of the egress path, which only exists for a store.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.core.stores import plain_connect

    init_db(config.db_path)
    config.archivist_db_path = str(config.data_dir / "team-archivist.db")
    store(config.db_path, "fact one", type="fact")
    with use_registry({}):
        register_base_archivist(config)
        push_knowledge_to_archivist(config)
        push_knowledge_to_archivist(config)  # second pass must not re-deliver
    conn = plain_connect(archivist_db(config))
    try:
        n = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_archivist_connection_exists_only_for_a_separate_store(config, tmp_path):
    """None for BOTH `absent` and `local`, for different reasons: `absent` has no
    archivist, and `local` IS this instance's own knowledge — already tier 1, so
    fanning out to it would read the same rows twice and report one source as two.
    Only `store` names somewhere else.
    """
    from techne.core.archivist import archivist_binding

    config.archivist = "none"
    assert archivist_connection(config) is None
    assert archivist_binding(config) == "absent"

    config.archivist = "local"
    assert archivist_connection(config) is None
    assert archivist_binding(config) == "local"

    config.archivist = ""
    config.archivist_db_path = str(tmp_path / "team.db")
    spec = archivist_connection(config)
    assert spec is not None and spec.role == "team"
    assert archivist_binding(config) == "store"


def test_end_to_end_team_recall_loop(config, tmp_path):
    """The whole point (#346): agent A stores a fact + pushes to a shared Archivist;
    agent B (different local db, same Archivist) recalls it back as team memory."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.mcp_server import _techne_recall

    shared = str(tmp_path / "shared-archivist.db")

    # Agent A: store locally, push to the shared Archivist.
    config.standalone = False
    config.archivist_db_path = shared
    init_db(config.db_path)
    store(config.db_path, "The deploy runbook lives in ops/deploy.md",
          type="fact", tags="ops", confidence="high")
    with use_registry({}):
        register_base_archivist(config)
        assert push_knowledge_to_archivist(config) == 1

    # Agent B: a DIFFERENT empty local db, the SAME Archivist.
    cfg_b = Config.load()
    cfg_b.db_path = tmp_path / "agentB.db"
    cfg_b.standalone = False
    cfg_b.archivist_db_path = shared
    init_db(cfg_b.db_path)

    result = _techne_recall({"topic": "deploy runbook"}, cfg_b)
    team = [r for r in result["search_results"] if r.get("_role") == "team"]
    assert len(team) == 1
    assert "deploy runbook" in team[0]["content"]
    assert team[0]["_store"] == "archivist"


def test_invalid_payload_rejected_at_enqueue(config):
    """The sink's payload schema validates at enqueue — a bad payload raises rather
    than silently landing malformed team memory."""
    from techne.core.database import init_db
    from techne.sync.outbox import enqueue
    from techne.sync.sinks import InvalidPayloadError

    init_db(config.db_path)
    with use_registry({}):
        register_base_archivist(config)
        with pytest.raises(InvalidPayloadError):
            enqueue(config.db_path, ARCHIVIST_SINK_TARGET, {"bogus": "shape"}, deliver=False)


def test_pattern_knowledge_is_never_enqueued(config, tmp_path):
    """type='pattern' must not reach the archivist lane (#450).

    techne#137 moved patterns into the Architect store, and the sink handler
    rejects them outright:

        ValueError: knowledge type 'pattern' is not stored in personal memory

    Enqueuing them anyway produced a PERMANENT PENDING LOOP: delivery fails, the
    row stays pending, and since push_knowledge_to_archivist re-enqueues all live
    knowledge on every call, the next session end re-enqueues the same rows. On
    the operator's store three pre-#137 legacy rows had been failing on every
    teardown since the team lane was switched on.

    Pins both halves: the pattern row is skipped, and non-pattern rows beside it
    still go through — a filter that dropped everything would also pass a test
    that only asserted the pattern was absent.
    """
    import json

    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.core.stores import plain_connect

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)

    store(config.db_path, "A durable fact worth sharing", type="fact", confidence="high")
    # store() REFUSES type='pattern' (knowledge.py's #137 guard), so a pattern row
    # can only exist as PRE-#137 LEGACY. Insert it the way the operator's three
    # actually exist — directly — otherwise the fixture cannot reproduce the bug.
    conn = plain_connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge (type, content, confidence) VALUES (?, ?, ?)",
            ("pattern", "## Some Architecture Pattern", "high"),
        )
        conn.commit()
    finally:
        conn.close()

    with use_registry({}):
        register_base_archivist(config)
        pushed = push_knowledge_to_archivist(config)

    assert pushed == 1, f"only the fact should be enqueued, got {pushed}"

    conn = plain_connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT data_json FROM outbox WHERE target = ?", (ARCHIVIST_KNOWLEDGE_TARGET,)
        ).fetchall()
    finally:
        conn.close()
    types = sorted(json.loads(r[0])["type"] for r in rows)
    assert "pattern" not in types, f"a pattern row reached the lane: {types}"
    assert "fact" in types, f"the fact was dropped too — filter is too broad: {types}"


def test_redirected_types_filter_follows_the_dict_not_a_literal(config, tmp_path, monkeypatch):
    """The filter must track REDIRECTED_TYPES, not hardcode 'pattern' (#450 review).

    REDIRECTED_TYPES is the single definition of "this type lives in another
    store" — `store()` raises straight off it. A literal filter in the pusher
    re-implements that membership test and drifts the moment a type is added.

    This is the drift, demonstrated: add a second redirected type and a
    literal-based filter lets it through into the lane that rejects it,
    reproducing the identical permanent-pending loop. Against the dict, it is
    skipped like any other.
    """
    import json

    from techne.core import knowledge as knowledge_mod
    from techne.core.database import init_db
    from techne.core.stores import plain_connect

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)

    monkeypatch.setitem(knowledge_mod.REDIRECTED_TYPES, "document", "the Librarian store")

    conn = plain_connect(config.db_path)
    try:
        for t in ("fact", "document", "document_card"):
            conn.execute(
                "INSERT INTO knowledge (type, content, confidence) VALUES (?, ?, ?)",
                (t, f"a {t} row", "high"),
            )
        conn.commit()
    finally:
        conn.close()

    with use_registry({}):
        register_base_archivist(config)
        pushed = push_knowledge_to_archivist(config)

    assert pushed == 2, f"wrong set filtered (pushed={pushed})"

    conn = plain_connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT data_json FROM outbox WHERE target = ?", (ARCHIVIST_KNOWLEDGE_TARGET,)
        ).fetchall()
    finally:
        conn.close()
    types = sorted(json.loads(r[0])["type"] for r in rows)
    # document_card is in VALID_TYPES and NOT in REDIRECTED_TYPES, so it SHOULD
    # egress. Asserting it survives is what kills the PULL_ONLY_TYPES mutant:
    # that dict also contains "pattern", so a filter using it passes every other
    # test in this file while silently blocking document_card from team memory.
    assert types == ["document_card", "fact"], f"expected fact + document_card, got {types}"


# ---------------------------------------------------------------------------
# techne#475 — the re-enqueue was the dominant term in session teardown
# ---------------------------------------------------------------------------


def _outbox_rows(config):
    from techne.core.stores import plain_connect

    conn = plain_connect(config.db_path)
    try:
        return conn.execute(
            "SELECT content_hash, data_json FROM outbox WHERE target = ?",
            (ARCHIVIST_KNOWLEDGE_TARGET,),
        ).fetchall()
    finally:
        conn.close()


def test_second_push_enqueues_nothing_new(config, tmp_path):
    """THE BUG: every teardown re-enqueued ALL live knowledge.

    ``enqueue`` opens, commits and closes its own SQLite connection per row, so
    this was 5.15s of a 5.52s teardown against a 5.0s hook budget — measured on a
    380MB store — and the budget then ABORTED teardown mid-flight. The cost scaled
    with the size of the KNOWLEDGE BASE, not the session, so it looked flat
    against session length and grew monotonically as memory did (116,174
    accumulated rows in this lane on the operator's store, from ~772 facts).
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)
    for i in range(5):
        store(config.db_path, f"durable fact number {i}", type="fact", confidence="high")

    with use_registry({}):
        register_base_archivist(config)
        first = push_knowledge_to_archivist(config)
        after_first = len(_outbox_rows(config))
        second = push_knowledge_to_archivist(config)
        after_second = len(_outbox_rows(config))

    assert first == 5, first
    assert second == 0, f"a second push must enqueue nothing new, got {second}"
    assert after_second == after_first, (
        f"the lane grew on a no-op push: {after_first} -> {after_second}"
    )


def test_a_changed_fact_is_still_pushed(config, tmp_path):
    """The pre-filter must not suppress real work.

    Skipping by content hash is only safe because a changed fact hashes
    differently — if this ever stops holding, the optimisation becomes silent
    data loss to the team store, which is strictly worse than the cost it saved.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)
    store(config.db_path, "the original wording", type="fact", confidence="high")

    with use_registry({}):
        register_base_archivist(config)
        assert push_knowledge_to_archivist(config) == 1
        store(config.db_path, "a genuinely different fact", type="fact", confidence="high")
        assert push_knowledge_to_archivist(config) == 1, "a new fact must reach the lane"

    contents = {json.loads(r[1])["content"] for r in _outbox_rows(config)}
    assert contents == {"the original wording", "a genuinely different fact"}, contents


def test_the_prefilter_hash_is_the_outboxs_own(config, tmp_path):
    """DRIFT GUARD. The pre-filter must ask the outbox for its dedup key rather
    than re-deriving it: a second copy of the formula stops matching the moment
    either side changes, and a silently non-matching filter degrades back to
    re-enqueuing everything — with no symptom except the clock."""
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.sync.outbox import content_hash_for

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)
    store(config.db_path, "a fact to hash", type="fact", confidence="high", tags="x")

    with use_registry({}):
        register_base_archivist(config)
        push_knowledge_to_archivist(config)

    rows = _outbox_rows(config)
    assert len(rows) == 1
    stored_hash, data_json = rows[0]
    assert stored_hash == content_hash_for(json.loads(data_json)), (
        "the stored hash is not what content_hash_for produces — the pre-filter "
        "will never match and the re-enqueue returns silently"
    )


def test_push_does_not_open_a_connection_per_row(config, tmp_path):
    """The mechanism, not just the count.

    A pre-filter that skipped rows but still opened a connection each time would
    pass the count assertions above and fix nothing — the connections WERE the
    cost (2,306 of them per teardown).
    """
    import techne.core.database as db_mod
    from techne.core.database import init_db
    from techne.core.knowledge import store

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)
    for i in range(20):
        store(config.db_path, f"fact {i}", type="fact", confidence="high")

    with use_registry({}):
        register_base_archivist(config)
        push_knowledge_to_archivist(config)  # first push: pays the real cost

        calls = {"n": 0}
        real = db_mod.connect

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        db_mod.connect = counting
        try:
            assert push_knowledge_to_archivist(config) == 0
        finally:
            db_mod.connect = real

    # One SELECT for what the lane holds, plus whatever ensure_registered_for
    # needs. Generous ceiling — the point is that it does NOT scale with 20 rows.
    assert calls["n"] <= 5, f"a no-op push opened {calls['n']} connections for 20 rows"


def test_enqueue_default_hash_is_content_hash_for(config, tmp_path):
    """The drift guard in the OTHER direction.

    The archivist push passes its hash explicitly, so mutating ``enqueue``'s
    internal formula does not break it — a mutation sweep found exactly that gap.
    But every other caller relies on the default, and if the two formulas part
    company the pre-filter silently stops matching those rows. Pin the default to
    the shared helper so one definition really is one definition.
    """
    from techne.core.database import init_db
    from techne.core.stores import plain_connect
    from techne.sync.outbox import content_hash_for, enqueue

    init_db(config.db_path)
    data = {"content": "z", "type": "fact", "tags": "a", "confidence": "high"}
    enqueue(config.db_path, "sync:thread_summary", data)  # no content_hash passed

    conn = plain_connect(config.db_path)
    try:
        stored = conn.execute(
            "SELECT content_hash FROM outbox WHERE target = 'sync:thread_summary'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == content_hash_for(data)


def test_rows_marked_dead_are_re_enqueued_not_stranded(config, tmp_path):
    """A `dead` row is PERMANENTLY NOT DELIVERED — the opposite of "already
    enqueued", and sweeping it into the pre-filter strands it forever.

    The trigger is real, not contrived: `techne outbox prune-orphans` does not
    call `ensure_registered_for` (unlike `flush-sinks` beside it), so in that
    fresh process every sink:archivist_knowledge row looks like an orphan and the
    whole pending backlog is marked dead. Before the pre-filter existed the next
    teardown re-enqueued them and the archivist recovered; with an unfiltered
    pre-filter it recovers never, and `INSERT OR IGNORE` cannot help because the
    filter `continue`s before enqueue is reached.

    Kills the mutation that drops `AND status != 'dead'` — which survived the
    entire suite when this PR was first submitted. (Prudence, #482 review.)
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.core.stores import plain_connect

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(config.db_path)
    for i in range(3):
        store(config.db_path, f"fact for the dead-row case {i}", type="fact", confidence="high")

    with use_registry({}):
        register_base_archivist(config)
        assert push_knowledge_to_archivist(config) == 3

        # Exactly what prune_orphans does to a lane it cannot see registered.
        conn = plain_connect(config.db_path)
        try:
            conn.execute(
                "UPDATE outbox SET status = 'dead' WHERE target = ?",
                (ARCHIVIST_KNOWLEDGE_TARGET,),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = push_knowledge_to_archivist(config)

    assert recovered == 3, (
        f"dead rows must be re-enqueued so the lane can recover; got {recovered}"
    )


# ---- techne#552: the binding replaces the fork -------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "local"),
        ({"standalone": True}, "local"),
        ({"standalone": False}, "local"),
        ({"archivist": "local"}, "local"),
        ({"archivist": "none"}, "absent"),
        ({"archivist_db_path": "/tmp/team.db"}, "store"),
        # NOTE the contradictory pair — `archivist: none` beside a named store — is NOT
        # here: an earlier revision asserted it resolved to `store`, which is the bug
        # Prudence found in #714 (the opt-out was silently overridden). It is now
        # rejected at load; see test_an_opt_out_beside_a_named_store_is_rejected.
        ({"standalone": True, "archivist_db_path": "/tmp/team.db"}, "store"),
    ],
)
def test_the_binding_resolves_from_every_input(raw, expected):
    """ONE FUNCTION DECIDES (techne#552). Six call sites used to branch on
    `config.standalone` independently, which is how the read path and the write path
    came to disagree about whether an archivist existed.

    Two rows carry the intent that made this issue: a bare install resolves to `local`
    rather than to nothing (the archivist is a CALL, so a lone agent still makes it),
    and a NAMED STORE outranks the legacy flag — `standalone: true` alongside an
    `archivist_db_path` used to be a config that could not do what it said.
    """
    from techne.config import Config
    from techne.core.archivist import archivist_binding

    assert archivist_binding(Config._from_dict(dict(raw))) == expected


def test_local_never_egresses(config):
    """THE MEASURED REASON `local` DOES NOT COPY. techne#552 S1-S5 found this
    instance's archivist.db to be a 98.9% mirror of local knowledge and stopped READING
    it; writing it is pointless by the same argument, and it is not free —
    `push_knowledge_to_archivist`'s own docstring records the copy as 5.15s of a 5.52s
    teardown (760 enqueues, 2,306 connections) against a 5.0s hook budget it then blew.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store

    init_db(config.db_path)
    store(config.db_path, "a fact that stays home", type="fact", confidence="high")

    for binding in ("", "local", "none"):
        config.archivist = binding
        with use_registry({}):
            register_base_archivist(config)
            assert push_knowledge_to_archivist(config) == 0, binding


def test_a_store_binding_still_egresses(config, tmp_path):
    """The other half — binding somewhere else is exactly when data should move."""
    from techne.core.database import init_db
    from techne.core.knowledge import store

    init_db(config.db_path)
    store(config.db_path, "a fact worth sharing", type="fact", confidence="high")
    config.archivist_db_path = str(tmp_path / "team.db")

    with use_registry({}):
        register_base_archivist(config)
        assert push_knowledge_to_archivist(config) == 1


#: The ONE legitimate reader of `config.standalone`: `config.py` DEFINES the field, so
#: parsing and serialising it there is not a private opinion about the archivist.
_STANDALONE_ALLOWED = {"techne/config.py"}


def _standalone_readers() -> list[str]:
    """Every module under `techne/` that BRANCHES on `standalone`.

    SELECTS ON THE DEFECT, NOT ON THE COUPLING, and that distinction is the whole
    guard. The previous version selected modules whose text mentioned `core.archivist`
    or `core.tiers` — i.e. "the module already asks the right question", which is
    exactly the property a REGRESSED module lacks. Prudence demonstrated it two ways:

      * checking out `checks.py` from the parent commit, with all three live
        `config.standalone` branches, left this tripwire GREEN — the only thing that
        caught it was a hand-typed two-module list in a second test, i.e. the mechanism
        this derivation was supposed to replace;
      * a live module could LEAVE the seam without regressing at all, just by spelling
        its import `from techne.core import archivist as _archivist`.

      * and `techne/core/tiers.py` — which DEFINES ARCHIVIST_LOCAL/_STORE/_ABSENT — was
        never in it, because it does not import itself.

    Membership can no longer be dropped by editing an import, because membership IS the
    defect. (Prudence, #714 round 2.)
    """
    import pathlib as _p

    out = []
    for path in sorted(_p.Path("techne").rglob("*.py")):
        rel = str(path)
        if rel in _STANDALONE_ALLOWED:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "config.standalone" in code or "cfg.standalone" in code:
                out.append(f"{rel}:{i}")
    return out


def test_the_seam_has_exactly_one_decision_point():
    """A TRIPWIRE ON THE DEFECT ITSELF. #552's finding was not a wrong default, it was
    six call sites each deciding for themselves what the archivist is. Any module that
    branches on `standalone` again is a seventh, wherever it lives — and the previous
    revisions of this guard could not see the modules that mattered, first because the
    list was typed by hand and then because the derivation selected on coupling.
    """
    offenders = _standalone_readers()
    assert not offenders, (
        f"the seam branches on `standalone` again at {offenders} — ask "
        "`archivist_binding(config)` instead (techne#552). If a new module has a "
        "legitimate reason to read the raw flag, add it to _STANDALONE_ALLOWED with "
        "the reason, so the exemption is a decision rather than an omission."
    )


def test_the_guard_would_have_caught_the_regression_it_missed():
    """THE GUARD'S OWN REGRESSION TEST. `checks.py` held three live `config.standalone`
    branches while an earlier version of this tripwire reported green. Rather than
    trusting that the new derivation would catch them, this reconstructs the shape:
    a module outside the allow-list containing the branch must be reported.
    """
    import pathlib as _p
    import tempfile

    scratch = _p.Path(tempfile.mkdtemp()) / "regressed.py"
    scratch.write_text("def f(config):\n    if config.standalone:\n        return 1\n")

    found = [
        i
        for i, line in enumerate(scratch.read_text().splitlines(), 1)
        if "config.standalone" in line.split("#", 1)[0]
    ]
    assert found, "the detection predicate itself does not match a live branch"

    # And the real scan reaches modules the import-based derivation could not: tiers.py
    # defines the binding constants, and checks.py is where the Critical lived.
    import techne.core.checks  # noqa: F401
    import techne.core.tiers  # noqa: F401

    scanned = {p for p in _p.Path("techne").rglob("*.py")}
    for required in ("techne/core/checks.py", "techne/core/tiers.py", "techne/mcp_server.py"):
        assert _p.Path(required) in scanned, f"{required} is outside the scan"


def test_registration_opens_no_store_unless_one_is_bound(config, tmp_path):
    """Registering a sink must not CREATE the store. `register_base_archivist(config)`
    pins `_HANDLER_DB` via `archivist_db(config)` -> `init_db`, which creates the file —
    so unconditional registration made `archivist.db` appear under `local` and under an
    explicit `archivist: none`, contradicting this module's own "no second file".

    The drainability fix is preserved: both sinks still register in every case. Only
    the config pin is conditional. (Prudence, #714.)
    """
    from techne.core.archivist import ARCHIVIST_KNOWLEDGE_TARGET

    for binding, expect_file in (("none", False), ("local", False), ("", False)):
        data_dir = tmp_path / f"dd-{binding or 'auto'}"
        data_dir.mkdir()
        config.data_dir = data_dir
        config.archivist = binding
        config.archivist_db_path = ""
        with use_registry({}):
            ensure_registered_for(config)
            assert get_sink(ARCHIVIST_SINK_TARGET) is not None, binding
            assert get_sink(ARCHIVIST_KNOWLEDGE_TARGET) is not None, binding
        created = [f.name for f in data_dir.iterdir() if f.name.startswith("archivist.db")]
        assert bool(created) is expect_file, (
            f"binding {binding!r} created {created} — `local`/`none` must open nothing"
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"archivist": "none", "archivist_db_path": "/tmp/team.db"},
        {"archivist": "local", "archivist_db_path": "/tmp/team.db"},
        {"archivist": "none", "connections": {"archivist": {"role": "team", "db_path": "/t.db"}}},
    ],
)
def test_an_opt_out_beside_a_named_store_is_rejected(raw):
    """OPTING OUT MUST REMAIN SAYABLE (tiers.py states it as a requirement). A leftover
    `archivist_db_path` outranks the explicit key in the resolver, so `archivist: none`
    plus a stale path still egressed knowledge to that store — the opt-out silently
    overridden, writing to a destination the operator asked to stop using. Rejected at
    load like `store`-with-no-target, because a config that cannot do what it says
    should say so. (Prudence, #714.)
    """
    from techne.config import Config

    with pytest.raises(ValueError) as e:
        Config._from_dict(dict(raw))
    assert "contradicts" in str(e.value)
