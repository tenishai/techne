"""Conflict candidates are ranked before truncation, and the floor is a knob (techne#933).

Two defects on the path that builds the conflict-check prompt. The issue's own
framing is that they COMPOUND: weak matches do not merely add noise, they take
slots from the contradictions the check exists to find.

ORDERING is the one that shipped as a behaviour change. The SQL has no ORDER BY
and nothing sorted the matches, so the rows surviving `max_candidates` were
whichever SQLite returned first. Same class as techne#861.

THE FLOOR SHIPPED AS A KNOB DEFAULTING TO 1 -- today's behaviour -- not as the
proposed 2. Raising it is a measured recall regression; see
`test_a_floor_of_two_drops_a_real_contradiction`, which pins the counterexample
that made the call. With ordering in place a weak match can only occupy a slot
when nothing stronger is competing for it, which is what the issue was actually
worried about.
"""

from __future__ import annotations

import pytest

from techne.config import Config
from techne.core.conflict import _content_keywords, select_conflict_candidates
from techne.core.database import connect, init_db
from techne.core.knowledge import store


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "conflict_rank.db"
    init_db(cfg.db_path)
    return cfg


def _row(db, kid):
    conn = connect(db)
    try:
        return dict(
            conn.execute(
                "SELECT id, type, content, tags, event_at, created_at FROM knowledge WHERE id=?",
                (kid,),
            ).fetchone()
        )
    finally:
        conn.close()


def test_the_strongest_matches_survive_truncation(config):
    """THE REGRESSION. With more matches than slots, the cap must keep the best.

    One strong match is buried behind many weak ones in insertion order, so a
    first-come truncation drops exactly the row the check exists to find.
    """
    db = config.db_path
    for i in range(12):
        store(db, f"deploy rollout stage {i} used the canary window", type="fact", tags="")
    strong = store(db, "the deploy canary window is thirty minutes", type="fact", tags="")

    focus_id = store(db, "the deploy canary window is five minutes", type="fact", tags="")
    focus = _row(db, focus_id)

    picked = select_conflict_candidates(db, focus, max_candidates=3)

    assert strong in {c["id"] for c in picked}, (
        "the closest contradiction was truncated away; "
        f"got {[c['content'][:40] for c in picked]}"
    )
    assert picked[0]["id"] == strong, "the closest contradiction must rank first"


def test_ordering_is_deterministic_for_equal_scores(config):
    """Equal scores tiebreak on id, so the selection is stable run to run.

    techne#873 is the standing reminder: thread ids were unstable because
    detection depended on PYTHONHASHSEED. Set ordering has the same hazard.
    """
    db = config.db_path
    ids = [store(db, "alpha beta gamma delta", type="fact", tags="") for _ in range(6)]
    focus_id = store(db, "alpha beta gamma epsilon", type="fact", tags="")
    focus = _row(db, focus_id)

    runs = [
        [c["id"] for c in select_conflict_candidates(db, focus, max_candidates=4)]
        for _ in range(5)
    ]
    assert all(r == runs[0] for r in runs), f"unstable ordering across runs: {runs}"
    assert runs[0] == sorted(runs[0]), "equal scores must tiebreak ascending by id"
    assert set(runs[0]) <= set(ids)


def test_the_floor_is_a_knob_with_teeth(config):
    """min_shared_keywords=2 rejects a single incidental shared token."""
    db = config.db_path
    store(db, "the session cache warms on boot", type="fact", tags="")
    focus_id = store(db, "session identifiers are uuids", type="fact", tags="")
    focus = _row(db, focus_id)

    shared = _content_keywords("the session cache warms on boot") & _content_keywords(
        "session identifiers are uuids"
    )
    assert shared == {"session"}, f"premise changed: shared tokens are {shared}"

    assert select_conflict_candidates(db, focus, min_shared_keywords=1)
    assert select_conflict_candidates(db, focus, min_shared_keywords=2) == []


def test_a_floor_of_two_drops_a_real_contradiction(config):
    """WHY THE DEFAULT IS 1 AND NOT THE PROPOSED 2. Pins the counterexample.

    This pair is a genuine contradiction sharing exactly one token, and it is the
    case this repo's own test_select_candidates_keyword_fallback_no_tags asserts
    must be found. If someone raises the default, this fails and says why.
    """
    db = config.db_path
    store(db, "we use postgres for storage", type="fact", tags="")
    focus_id = store(db, "switched from postgres to sqlite", type="fact", tags="")
    focus = _row(db, focus_id)

    assert any(
        "postgres" in c["content"] for c in select_conflict_candidates(db, focus)
    ), "the default must still find a one-token real contradiction"
    assert select_conflict_candidates(db, focus, min_shared_keywords=2) == [], (
        "a floor of 2 drops it — this is the recall cost, recorded deliberately"
    )


def test_tag_path_is_ordered_but_keeps_its_single_tag_match(config):
    """Curated tags: one shared tag still qualifies, but ranking applies."""
    db = config.db_path
    weak = store(db, "unrelated note", type="fact", tags="deploy,unrelated,noise,extra")
    strong = store(db, "closely related note", type="fact", tags="deploy,canary")
    focus_id = store(db, "the focus row", type="fact", tags="deploy,canary")
    focus = _row(db, focus_id)

    picked = [c["id"] for c in select_conflict_candidates(db, focus)]
    assert weak in picked, "a single shared curated tag still qualifies"
    assert picked.index(strong) < picked.index(weak), "stronger tag overlap ranks first"
