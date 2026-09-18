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
that made the call. With the CORRECTED ordering in place (shared count primary, overlap
coefficient only as a tiebreak) a weak match can only occupy a slot when nothing
stronger is competing for it. Ranking on the coefficient alone did the opposite.
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
        f"the closest contradiction was truncated away; got {[c['content'][:40] for c in picked]}"
    )
    assert picked[0]["id"] == strong, "the closest contradiction must rank first"


def test_ordering_is_deterministic_for_equal_scores(config):
    """Equal scores tiebreak on id, so the selection is stable run to run.

    THE REAL HAZARD IS THE MISSING ORDER BY, not a hash seed. An earlier version
    of this docstring cited techne#873's PYTHONHASHSEED instability by analogy;
    that retraction was made in the source comment and never reached here.
    `all_rows` is a list from a cursor and no set is iterated in ranking -- sets
    are only intersected for their size -- so there is no hash-seed dependence.
    What there is: SQL with no ORDER BY, so two rows with identical scores would
    otherwise come back in whatever order SQLite chose.
    """
    db = config.db_path
    ids = [store(db, "alpha beta gamma delta", type="fact", tags="") for _ in range(6)]
    focus_id = store(db, "alpha beta gamma epsilon", type="fact", tags="")
    focus = _row(db, focus_id)

    runs = [
        [c["id"] for c in select_conflict_candidates(db, focus, max_candidates=4)] for _ in range(5)
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

    assert any("postgres" in c["content"] for c in select_conflict_candidates(db, focus)), (
        "the default must still find a one-token real contradiction"
    )
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


def test_a_short_row_does_not_outrank_a_strong_match(config):
    """THE PRE-PASS FINDING. The overlap coefficient alone ranked SHORT above STRONG.

    shared/min(len) scores a one-token row 1.0 against anything, so sorting on it
    put a row saying just "canary" above a row matching four of the focus's five
    tokens. The earlier test missed this because its fixture contained no short
    rows -- every distractor was a full sentence, so the coefficient happened to
    agree with strength.

    Measured against focus {canary, deploy, five, minutes, window}:

        "the deploy canary window is thirty minutes"   4 shared, overlap 0.800
        "canary"                                       1 shared, overlap 1.000
    """
    db = config.db_path
    short = store(db, "canary", type="fact", tags="")
    strong = store(db, "the deploy canary window is thirty minutes", type="fact", tags="")
    store(db, "deploy window rotated yesterday", type="fact", tags="")

    focus_id = store(db, "the deploy canary window is five minutes", type="fact", tags="")
    picked = [c["id"] for c in select_conflict_candidates(db, _row(db, focus_id))]

    assert picked.index(strong) < picked.index(short), (
        "a one-token row outranked a four-token match; the coefficient is "
        f"normalising strength away. order was {picked}"
    )


def test_the_tag_path_ranks_by_shared_count_too(config):
    """The tag path had the same defect, on the fixture below.

    SCOPE: a tag overlap scores 1.0 only when one tag set CONTAINS the other,
    which is what happens here (a one-tag row inside a two-tag focus). An earlier
    docstring said "every tag overlap scored 1.0", which is not true in general --
    `test_tag_path_is_ordered_but_keeps_its_single_tag_match` in this same file
    builds a 4-tag row against a 2-tag focus and scores 0.5.
    """
    db = config.db_path
    one_tag = store(db, "barely related", type="fact", tags="deploy")
    two_tag = store(db, "closely related", type="fact", tags="deploy,canary")
    focus_id = store(db, "the focus", type="fact", tags="deploy,canary")

    picked = [c["id"] for c in select_conflict_candidates(db, _row(db, focus_id))]
    assert picked.index(two_tag) < picked.index(one_tag), (
        f"a single shared tag tied a two-tag match; order was {picked}"
    )


def test_the_three_copies_of_the_default_agree(config):
    """W2: the knob's default is spelled in THREE places and only one was pinned.

    `select_conflict_candidates`'s signature, `ConflictConfig`, and
    `detect_cross_store_contradictions` each carry their own default. A test
    calling the function with no kwarg reads the SIGNATURE default, so bumping
    the config default left every test green -- the pinning test pinned the copy
    operators do not get.
    """
    import inspect

    from techne.config import ConflictConfig
    from techne.core.federation import detect_cross_store_contradictions

    sig_default = (
        inspect.signature(select_conflict_candidates).parameters["min_shared_keywords"].default
    )
    fed_default = (
        inspect.signature(detect_cross_store_contradictions)
        .parameters["min_shared_keywords"]
        .default
    )
    cfg_default = ConflictConfig().min_shared_keywords

    assert sig_default == fed_default == cfg_default, (
        "the three copies of min_shared_keywords' default disagree: "
        f"signature={sig_default} federation={fed_default} config={cfg_default}. "
        "Operators get the config one; tests calling with no kwarg get the signature one."
    )


def test_the_operator_facing_default_is_what_the_counterexample_pins(config):
    """Pins the CONFIG default, not the signature default.

    test_a_floor_of_two_drops_a_real_contradiction reads the signature default,
    so a config-only bump would not trip it.
    """
    from techne.config import ConflictConfig

    db = config.db_path
    store(db, "we use postgres for storage", type="fact", tags="")
    focus_id = store(db, "switched from postgres to sqlite", type="fact", tags="")

    picked = select_conflict_candidates(
        db,
        _row(db, focus_id),
        min_shared_keywords=ConflictConfig().min_shared_keywords,
    )
    assert any("postgres" in c["content"] for c in picked), (
        "at the OPERATOR-facing default, a one-token real contradiction is dropped"
    )
