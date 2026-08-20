"""The stated recall ladder (techne#552, spec knowledge#1085 S1-S5)."""

from __future__ import annotations

import pytest

from techne.core import tiers


def _reports(**kw):
    base = dict(
        rows=[],
        connections={},
        archivist_name="archivist",
        archivist_binding=tiers.ARCHIVIST_LOCAL,
        librarian_available=True,
    )
    base.update(kw)
    return tiers.build_reports(**base)


def test_every_rung_is_reported_even_when_nothing_matched():
    """S1. The natural implementation reports only rungs that produced rows, and a
    caller then cannot tell 'the team knows nothing' from 'there is no team'. All
    four, always, in order — that invariant IS the feature."""
    reports = _reports()
    assert [r.rung.number for r in reports] == [1, 2, 3, 4]
    # Not `all(r.state ...)`: every state string is truthy, so that assertion cannot
    # fail and reports success by silence. Pin the resolved values instead.
    assert [r.state for r in reports] == [
        tiers.STATE_EMPTY,
        tiers.STATE_EMPTY,
        tiers.STATE_UNCONFIGURED,
        tiers.STATE_EMPTY,
    ]


def test_an_empty_tier_is_empty_not_absent():
    """S5's load-bearing distinction, at the rung level."""
    t4 = _reports(librarian_available=True)[3]
    assert t4.state == tiers.STATE_EMPTY
    assert t4.results == 0


def test_no_library_reads_unavailable_not_empty():
    """'The library has nothing on this' and 'there is no library' are different
    facts about the world. Flattening them is the exact S5 failure."""
    assert _reports(librarian_available=False)[3].state == tiers.STATE_UNAVAILABLE


def test_unconfigured_remote_is_not_the_same_as_an_empty_one():
    """Same distinction on tier 3. `unconfigured` means no remote archivist exists;
    `empty` would mean one was asked and had nothing."""
    assert _reports()[2].state == tiers.STATE_UNCONFIGURED
    with_remote = _reports(connections={"team-b": object()}, rows=[])
    assert with_remote[2].state == tiers.STATE_EMPTY


def test_tier_2_stays_visible_while_resolving_to_tier_1s_store():
    """S2. At n=1 the rung reads NO second file, but it must not vanish — an agent
    that only ever reaches team knowledge through an archivist call keeps that habit
    when a real team store appears behind it. The interface is the deliverable."""
    rows = [{"origin": "memory"}, {"origin": "memory"}]
    reports = _reports(rows=rows, archivist_binding=tiers.ARCHIVIST_LOCAL)
    t1, t2 = reports[0], reports[1]
    assert t2.state == tiers.STATE_HIT
    assert t2.results == t1.results == 2
    assert t2.binding == t1.binding == "local:knowledge"
    assert "read once" in t2.detail


def test_a_shared_archivist_is_not_reported_as_local():
    """The multi-agent case (techne#346): pointed at a shared store, tier 2 is a real
    rung over a real other store and must be labelled as one."""
    rows = [{"_store": "archivist"}]
    t2 = _reports(rows=rows, archivist_binding=tiers.ARCHIVIST_STORE)[1]
    assert t2.binding == "store:archivist"
    assert t2.results == 1


def test_rows_carry_the_rung_they_came_from():
    """S1's per-row half — a caller reading a single result still knows whether it is
    our experience or somebody's document."""
    rows = [{"origin": "memory"}, {"origin": "library"}, {"_store": "team-b"}]
    tiers.annotate_rows(rows, "archivist")
    assert [r["recall_tier"] for r in rows] == [1, 4, 3]
    assert rows[1]["recall_tier_name"] == "librarian"


@pytest.mark.parametrize(
    "experience,documented,direct,expected",
    [
        (3, 1, True, tiers.OUTCOME_FOUND),
        (0, 2, True, tiers.OUTCOME_DOCUMENTED_ONLY),
        (0, 0, True, tiers.OUTCOME_NOTHING),
        (0, 0, False, tiers.OUTCOME_NOTHING),
        # The case the spec did not anticipate: the hybrid searcher always returns a
        # full page, so rows exist for a topic nobody has ever touched.
        (10, 0, False, tiers.OUTCOME_LOOSE_ONLY),
    ],
)
def test_the_outcomes_are_distinguishable(experience, documented, direct, expected):
    """S5. Richard's 'institutional knowledge we may or may not have', made concrete.
    The middle rows are the ones a flat result list destroys."""
    reports = [
        tiers.RungReport(tiers.LADDER[0], tiers.STATE_HIT, "local:knowledge", experience),
        tiers.RungReport(tiers.LADDER[3], tiers.STATE_HIT, "local:librarian.db", documented),
    ]
    assert tiers.classify_outcome(reports, direct=direct)["code"] == expected


def test_loose_and_found_do_not_collapse():
    """The regression that matters: if `direct` is ever ignored, a topic nobody has
    worked on reports as prior experience — which is worse than no ladder at all,
    because it is confidently wrong."""
    reports = [tiers.RungReport(tiers.LADDER[0], tiers.STATE_HIT, "local:knowledge", 10)]
    assert tiers.classify_outcome(reports, direct=True)["code"] == tiers.OUTCOME_FOUND
    assert tiers.classify_outcome(reports, direct=False)["code"] == tiers.OUTCOME_LOOSE_ONLY


def test_has_direct_match_is_asked_of_knowledge_not_observations(tmp_path):
    """The trap that silently defeated this check during development.

    With capture_bash on, every term the operator types to TEST recall is in the
    observation ledger within seconds — so `table="all"` matched the probe itself
    ("zzzqqxx" hit 6 observations, all of them the test). Tiers 1-3 answer 'what have
    we LEARNED', which is the distilled layer, so the question must be asked there.
    """
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.knowledge import store as store_knowledge
    from techne.core.search import has_direct_match
    from techne.core.state import record_observation

    db = tmp_path / "t.db"
    cfg = Config()
    cfg.db_path = db
    cfg.data_dir = tmp_path
    init_db(db)
    store_knowledge(db, "the deploy runbook lives in the ops repo", type="fact")
    record_observation(cfg, "s1", "grep plinthwaggle vortex", tool_name="Bash")

    assert has_direct_match(db, "deploy runbook", table="knowledge") is True
    # Present in observations only — must NOT read as learned experience.
    assert has_direct_match(db, "plinthwaggle vortex", table="knowledge") is False
    assert has_direct_match(db, "nothing at all like this", table="knowledge") is False


def test_has_direct_match_survives_a_store_with_no_fts(tmp_path):
    """A missing FTS table must not make recall claim 'no experience' — that is a
    louder and more misleading answer than declining to characterise the result."""
    from techne.core.search import has_direct_match

    assert has_direct_match(tmp_path / "does-not-exist.db", "anything") is False
    assert has_direct_match(tmp_path / "does-not-exist.db", "") is False


def test_no_archivist_reads_unconfigured_not_empty():
    """Prudence r1, Important 1 — and it lands on the DEFAULT install.

    `standalone: True` is the default, so `archivist_connection()` returns None and no
    archivist exists. Tier 2 used to emit `state="empty"` against `binding="store:archivist"`
    — a POSITIVE claim that a team store was consulted and had nothing, which is strictly
    worse than the flat list it replaces: an omission is silent, a false `empty` recruits
    the reader. Tier 3 always got this right; tier 2 could not, because the parameter was
    a bool with no way to say "there is no archivist".
    """
    t2 = _reports(archivist_binding=tiers.ARCHIVIST_ABSENT)[1]
    assert t2.state == tiers.STATE_UNCONFIGURED
    assert t2.binding == "none"
    assert t2.results == 0
    assert "archivist" not in t2.binding


def test_the_row_tier_key_does_not_collide_with_the_observation_tier_column():
    """Prudence r1, Important 2. `observations.tier` is hot|warm|cold and search surfaces
    it verbatim, so writing an int there replaced a key AND changed its meaning — the #579
    one-key-two-meanings shape, documented in the very card this change re-blesses."""
    rows = [{"source_table": "observation", "tier": "warm", "origin": "memory"}]
    tiers.annotate_rows(rows, "archivist")
    assert rows[0]["tier"] == "warm", "the observation tier column must survive untouched"
    assert rows[0]["recall_tier"] == 1


def _cfg(tmp_path, name="local.db"):
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    cfg.db_path = tmp_path / name
    cfg.data_dir = tmp_path
    cfg.connections = {}
    cfg.archivist_db_path = ""
    init_db(cfg.db_path)
    return cfg


def test_default_standalone_install_reports_no_archivist(tmp_path):
    """Prudence r1, Important 1, at the gate rather than the branch. `standalone: True`
    is the DEFAULT, and this is the shape the majority of installs will actually see."""
    from techne.mcp_server import _techne_recall

    cfg = _cfg(tmp_path)
    cfg.standalone = True
    t2 = _techne_recall({"topic": "anything"}, cfg)["tiers"][1]
    assert t2["state"] == "unconfigured"
    assert t2["binding"] == "none"


def test_an_explicitly_configured_archivist_peer_is_still_queried(tmp_path):
    """Prudence r1, Important 3(b) — the mutant that survived all 3048 tests.

    Dropping `ARCHIVIST_CONNECTION_NAME not in config.connections` from the gate left
    an operator-configured archivist peer silently unqueried: 0 team rows instead of 1,
    no error and no log. That is the same silent team-recall loss that 3(a) already cost
    once, and nothing was under it. Asserts the ROWS, not the binding label, because the
    harm is missing data rather than a mislabelled rung.
    """
    from techne.config import FederationConnectionSpec
    from techne.core.knowledge import store as store_knowledge
    from techne.mcp_server import _techne_recall

    peer = _cfg(tmp_path, "peer.db")
    store_knowledge(peer.db_path, "the deploy runbook lives in ops/deploy.md", type="fact")

    cfg = _cfg(tmp_path)
    cfg.standalone = False
    cfg.connections = {
        "archivist": FederationConnectionSpec(role="team", db_path=str(peer.db_path))
    }
    result = _techne_recall({"topic": "deploy runbook"}, cfg)
    team = [r for r in result["search_results"] if r.get("_store") == "archivist"]
    assert len(team) == 1, "an explicitly configured archivist peer must still be read"
    assert result["tiers"][1]["binding"] == "store:archivist"


def test_the_outcome_signal_is_asked_of_knowledge_at_the_call_site(tmp_path):
    """Prudence r1, Important 3(c) — one keyword, unguarded, three prose blocks deep.

    Deleting `table="knowledge"` from the `has_direct_match` call in `_with_ladder`
    passed the ENTIRE suite while flipping `loose-match-only` to `found`. With
    capture_bash on, `all` matches the operator's own probe commands, so the check
    silently answers "yes we have experience" for a term that exists only because
    somebody typed it. This pins the call site, not just the helper.
    """
    from techne.core.state import record_observation
    from techne.mcp_server import _techne_recall

    cfg = _cfg(tmp_path)
    cfg.standalone = True
    # The term exists ONLY as a captured command, never as anything learned.
    record_observation(cfg, "s1", "grep plinthwaggle vortex ./src", tool_name="Bash")

    result = _techne_recall({"topic": "plinthwaggle vortex"}, cfg)
    # `!= "found"` alone is too weak, and Prudence r2 demonstrated it rather than
    # asserting it: break the precondition AND leave the regression live and the test
    # still passes green, because `nothing-known` also satisfies it. Pinning that the
    # row IS present keeps the assertion about the outcome CLASSIFIER rather than
    # about whether anything matched at all.
    assert result["tiers"][0]["results"] == 1, "the observation must actually be found"
    assert result["outcome"]["code"] != "found", "an observed command is not experience"


def test_an_explicit_connection_outranks_the_standalone_default(tmp_path):
    """The tri-state gate's precedence, verified rather than reasoned about.

    `standalone: True` suppresses the AUTO-registered archivist, but an operator who
    wrote a `connections["archivist"]` entry has stated a binding explicitly, and that
    must win — otherwise the default flag silently disables a store somebody configured
    by hand. `_effective_connections` uses `setdefault`, so config already wins there;
    this pins that the ladder agrees with it.
    """
    from techne.config import FederationConnectionSpec
    from techne.mcp_server import _techne_recall

    peer = _cfg(tmp_path, "peer2.db")
    cfg = _cfg(tmp_path)
    cfg.standalone = True
    cfg.connections = {
        "archivist": FederationConnectionSpec(role="team", db_path=str(peer.db_path))
    }
    t2 = _techne_recall({"topic": "anything"}, cfg)["tiers"][1]
    assert t2["state"] != "unconfigured", "an explicit connection must not read as absent"
    assert t2["binding"] == "store:archivist"


def test_standalone_suppresses_an_archivist_path_and_says_so(tmp_path):
    """The contradictory config: `archivist_db_path` set while `standalone: True`.

    `archivist_connection()` returns None on standalone REGARDLESS of the path, so the
    store genuinely is not read. The ladder must report `unconfigured` — matching what
    actually happened — rather than describing a binding to a file it never opened. This
    is the #552 `standalone`-as-lane-selector fork showing through; the ladder's job here
    is to be honest about it, not to paper over it.
    """
    from techne.mcp_server import _techne_recall

    cfg = _cfg(tmp_path)
    cfg.standalone = True
    cfg.archivist_db_path = str(tmp_path / "some-archivist.db")
    t2 = _techne_recall({"topic": "anything"}, cfg)["tiers"][1]
    assert t2["state"] == "unconfigured"
    assert t2["binding"] == "none"


def test_the_local_binding_arm_is_pinned_end_to_end(tmp_path):
    """Prudence r2, Important — the third arm of the gate, and the `pop` it drives.

    Two arms were pinned; this one (`standalone=False`, no `connections["archivist"]`,
    no `archivist_db_path` — the F/–/– row) was not, and both its mutants survived all
    3059 tests. The harms she demonstrated:

    * flipping this arm to ABSENT yields a response where tier 2 reports "no archivist
      is configured" while a row tagged `_store: archivist` sits in the SAME payload —
      the r1 defect recurring under a new shape;
    * disabling the `fan_out_connections.pop` makes the tier-2 detail string ("one store
      read once… the rows below are tier 1's") a falsehood, because a second store IS
      then read.

    Asserted end-to-end rather than on `build_reports`, because the second harm lives in
    `_techne_recall`'s fan-out and a unit-level report cannot see it.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store as store_knowledge
    from techne.mcp_server import _techne_recall

    cfg = _cfg(tmp_path)
    cfg.standalone = False
    cfg.archivist_db_path = ""
    cfg.connections = {}
    store_knowledge(cfg.db_path, "the deploy runbook lives in ops/deploy.md", type="fact")

    # THE ARCHIVIST STORE MUST EXIST AND HOLD A MATCHING ROW, or this test cannot see
    # the pop at all. Without it `fan_out_search` skips a missing store and the "no row
    # tagged archivist" assertion passes whether or not the pop happened — a test that
    # reads as rigorous and bites nothing. Verified: with this row planted, disabling
    # the pop fails here; without it, the mutant survives.
    # DISTINCT content, deliberately: `_cross_store_dedup` strips a remote row whose
    # content hash matches a local one, so planting the SAME text here is invisible even
    # when the store IS read. First attempt did exactly that and the mutant survived.
    archivist = tmp_path / "archivist.db"
    init_db(archivist)
    store_knowledge(archivist, "deploy runbook rotation is owned by the platform team", type="fact")

    result = _techne_recall({"topic": "deploy runbook"}, cfg)
    t1, t2 = result["tiers"][0], result["tiers"][1]

    # The arm itself: tier 2 is a VISIBLE rung bound to tier 1's store, not absent.
    assert t2["state"] == tiers.STATE_HIT
    assert t2["binding"] == "local:knowledge" == t1["binding"]
    assert t2["results"] == t1["results"] >= 1

    # The pop: no second store was read, so nothing is tagged as coming from one.
    assert not [r for r in result["search_results"] if r.get("_store") == "archivist"]
    # ...and the detail string that claims exactly that is therefore true.
    assert "read once" in t2["detail"]
