"""The stated recall ladder (techne#552, spec knowledge#1085 S1-S5)."""

from __future__ import annotations

import pytest

from techne.core import tiers


def _reports(**kw):
    base = dict(
        rows=[],
        connections={},
        archivist_name="archivist",
        archivist_is_local=True,
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
    assert all(r.state for r in reports)


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
    reports = _reports(rows=rows, archivist_is_local=True)
    t1, t2 = reports[0], reports[1]
    assert t2.state == tiers.STATE_HIT
    assert t2.results == t1.results == 2
    assert t2.binding == t1.binding == "local:knowledge"
    assert "no second file" in t2.detail


def test_a_shared_archivist_is_not_reported_as_local():
    """The multi-agent case (techne#346): pointed at a shared store, tier 2 is a real
    rung over a real other store and must be labelled as one."""
    rows = [{"_store": "archivist"}]
    t2 = _reports(rows=rows, archivist_is_local=False)[1]
    assert t2.binding == "store:archivist"
    assert t2.results == 1


def test_rows_carry_the_rung_they_came_from():
    """S1's per-row half — a caller reading a single result still knows whether it is
    our experience or somebody's document."""
    rows = [{"origin": "memory"}, {"origin": "library"}, {"_store": "team-b"}]
    tiers.annotate_rows(rows, "archivist")
    assert [r["tier"] for r in rows] == [1, 4, 3]
    assert rows[1]["tier_name"] == "librarian"


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
