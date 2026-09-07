"""Tests for techne#584 state-claim gate — the conversational half.

Coverage:
- ownership: backed / unbacked / unverifiable against the coord bus, and the
  handoff case that motivated the issue (most recent claim wins).
- artifact_state: open / closed / merged, unreachable GitHub, refusal to guess
  an owner.
- capability: present / absent / contradicted / unreadable.
- The load-bearing invariant: ABSENCE IS NEVER 'unbacked'.
- The gate never raises.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core.state_claims import StateContext, check_state_claims


@pytest.fixture
def bus(tmp_path):
    """A coord_messages table with a handoff already recorded."""
    db = tmp_path / "techne.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    rows = [
        (
            "s1",
            "poe_3",
            "coord",
            "Claiming techne#570 — starting the retrieval fix.",
            "2026-08-15 09:00:00",
        ),
        (
            "s2",
            "poe_1",
            "coord",
            "Claiming branch fix/51-single-slot-lock in agents.",
            "2026-08-15 10:00:00",
        ),
        ("s3", "poe_2", "coord", "Taking techne#570 from poe_3 by handoff.", "2026-08-16 11:00:00"),
    ]
    for sess, name, rec, body, at in rows:
        conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
            " VALUES (?,?,?,?,?)",
            (sess, name, rec, body, at),
        )
    conn.commit()
    conn.close()
    return db


def _one(claim, ctx):
    v = check_state_claims([claim], ctx)
    assert len(v) == 1
    return v[0]


# ----------------------------- ownership ---------------------------------
def test_ownership_backed(bus):
    v = _one(
        {"type": "ownership", "subject": "fix/51-single-slot-lock", "owner": "poe_1"},
        StateContext(db_path=bus),
    )
    assert v["status"] == "backed"
    assert "poe_1" in v["detail"]


def test_ownership_unbacked_names_the_real_claimant(bus):
    v = _one(
        {"type": "ownership", "subject": "fix/51-single-slot-lock", "owner": "poe_2"},
        StateContext(db_path=bus),
    )
    assert v["status"] == "unbacked"
    assert "poe_1" in v["detail"]


def test_ownership_handoff_most_recent_claim_wins(bus):
    """The techne#584 failure: 'techne#570 is mine' after it moved by handoff.

    poe_3 claimed it first and poe_2 took it later. The newest claim is what
    makes the handoff visible, so the stale claimant must come back unbacked.
    """
    ctx = StateContext(db_path=bus)
    assert (
        _one({"type": "ownership", "subject": "techne#570", "owner": "poe_2"}, ctx)["status"]
        == "backed"
    )
    stale = _one({"type": "ownership", "subject": "techne#570", "owner": "poe_3"}, ctx)
    assert stale["status"] == "unbacked"
    assert "poe_2" in stale["detail"]


def test_ownership_unmentioned_is_unverifiable_not_unbacked(bus):
    """THE load-bearing case. Plenty of real work is never announced."""
    v = _one(
        {"type": "ownership", "subject": "feat/never-announced", "owner": "poe_1"},
        StateContext(db_path=bus),
    )
    assert v["status"] == "unverifiable"
    assert "never mentions" in v["detail"]


def test_ownership_missing_fields_and_db(bus):
    assert (
        _one({"type": "ownership", "subject": "x"}, StateContext(db_path=bus))["status"]
        == "unverifiable"
    )
    assert (
        _one({"type": "ownership", "subject": "x", "owner": "y"}, StateContext())["status"]
        == "unverifiable"
    )


# --------------------------- artifact_state ------------------------------
def _gh(payload, rc=0):
    return lambda argv: (rc, payload)


def test_artifact_state_open_and_closed():
    ctx = StateContext(gh=_gh('{"state":"open","merged":false,"pr":false}'))
    assert (
        _one({"type": "artifact_state", "ref": "tenishai/agents#73", "state": "open"}, ctx)[
            "status"
        ]
        == "backed"
    )
    assert (
        _one({"type": "artifact_state", "ref": "tenishai/agents#73", "state": "closed"}, ctx)[
            "status"
        ]
        == "unbacked"
    )


def test_artifact_state_merged_is_distinct_from_closed():
    """A merged PR reads as closed on GitHub. Conflating them hides 'never landed'."""
    ctx = StateContext(gh=_gh('{"state":"closed","merged":true,"pr":true}'))
    assert (
        _one({"type": "artifact_state", "ref": "o/r#1", "state": "merged"}, ctx)["status"]
        == "backed"
    )
    # closed-but-not-merged must NOT satisfy a 'merged' claim
    ctx2 = StateContext(gh=_gh('{"state":"closed","merged":false,"pr":true}'))
    v = _one({"type": "artifact_state", "ref": "o/r#1", "state": "merged"}, ctx2)
    assert v["status"] == "unbacked"
    assert "closed" in v["detail"]


def test_artifact_state_unreachable_is_unverifiable():
    """Load-bearing: a network failure is not a contradiction."""
    v = _one(
        {"type": "artifact_state", "ref": "o/r#1", "state": "open"},
        StateContext(gh=_gh("gh: not authenticated", rc=1)),
    )
    assert v["status"] == "unverifiable"


def test_artifact_state_refuses_to_guess_an_owner():
    v = _one(
        {"type": "artifact_state", "ref": "agents#73", "state": "open"},
        StateContext(gh=_gh('{"state":"open","merged":false,"pr":false}')),
    )
    assert v["status"] == "unverifiable"
    assert "owner" in v["detail"]


def test_artifact_state_bad_state_field():
    v = _one({"type": "artifact_state", "ref": "o/r#1", "state": "vibes"}, StateContext())
    assert v["status"] == "unverifiable"


# ----------------------------- capability --------------------------------
def test_capability_present_and_absent(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("def already_do_x():\n    pass\n")
    ctx = StateContext(repo_root=tmp_path)
    assert (
        _one({"type": "capability", "path": "src.py", "pattern": "already_do_x"}, ctx)["status"]
        == "backed"
    )
    assert (
        _one({"type": "capability", "path": "src.py", "pattern": "nope", "present": False}, ctx)[
            "status"
        ]
        == "backed"
    )


def test_capability_contradicted(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("def already_do_x(): pass\n")
    ctx = StateContext(repo_root=tmp_path)
    v = _one({"type": "capability", "path": "src.py", "pattern": "missing_thing"}, ctx)
    assert v["status"] == "unbacked"
    v2 = _one(
        {"type": "capability", "path": "src.py", "pattern": "already_do_x", "present": False}, ctx
    )
    assert v2["status"] == "unbacked"


def test_capability_unreadable_is_unverifiable(tmp_path):
    """Load-bearing: a missing file cannot disprove 'we do X'."""
    v = _one(
        {"type": "capability", "path": "nope.py", "pattern": "x"}, StateContext(repo_root=tmp_path)
    )
    assert v["status"] == "unverifiable"


def test_capability_bad_regex_is_unverifiable(tmp_path):
    (tmp_path / "s.py").write_text("x")
    v = _one(
        {"type": "capability", "path": "s.py", "pattern": "([", "regex": True},
        StateContext(repo_root=tmp_path),
    )
    assert v["status"] == "unverifiable"


# ------------------------------ the gate ---------------------------------
def test_unknown_type_is_unverifiable():
    assert _one({"type": "vibes"}, StateContext())["status"] == "unverifiable"


def test_gate_never_raises():
    """A gate that can crash a turn is worse than one that abstains."""

    def boom(argv):
        raise RuntimeError("network on fire")

    v = _one({"type": "artifact_state", "ref": "o/r#1", "state": "open"}, StateContext(gh=boom))
    assert v["status"] == "unverifiable"
    assert "RuntimeError" in v["detail"]


def test_absence_is_never_unbacked(bus, tmp_path):
    """The invariant, asserted as one property across all three types."""
    cases = [
        ({"type": "ownership", "subject": "unheard-of", "owner": "x"}, StateContext(db_path=bus)),
        (
            {"type": "artifact_state", "ref": "o/r#1", "state": "open"},
            StateContext(gh=_gh("boom", rc=1)),
        ),
        (
            {"type": "capability", "path": "gone.py", "pattern": "x"},
            StateContext(repo_root=tmp_path),
        ),
    ]
    for claim, ctx in cases:
        assert _one(claim, ctx)["status"] == "unverifiable", claim


# --------------------------- MCP round trip -------------------------------
def test_mcp_check_evidence_mixes_deed_and_state_claims(tmp_path):
    """One call, one list back, aligned with the caller's claim order."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.mcp_server import _techne_check_evidence

    db = tmp_path / "t.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_1','coord','Claiming branch feat/x','2026-09-06')"
    )
    conn.commit()
    conn.close()
    (tmp_path / "src.py").write_text("def already_do_x(): pass\n")

    cfg = Config()
    cfg.db_path = db
    claims = [
        {"type": "completion"},
        {"type": "ownership", "subject": "feat/x", "owner": "poe_1"},
        {"type": "ownership", "subject": "feat/x", "owner": "poe_9"},
        {"type": "capability", "path": "src.py", "pattern": "already_do_x"},
        {"type": "capability", "path": "gone.py", "pattern": "z"},
    ]
    r = _techne_check_evidence(
        {"session_id": "s1", "claims": claims, "repo_root": str(tmp_path)}, cfg
    )

    assert len(r["verdicts"]) == len(claims)
    assert all(v for v in r["verdicts"]), "a verdict slot was left empty"
    assert r["verdicts"][1]["status"] == "backed"
    assert r["verdicts"][2]["status"] == "unbacked"
    assert r["verdicts"][3]["status"] == "backed"
    assert r["verdicts"][4]["status"] == "unverifiable"
    assert r["unbacked_count"] == 1
    # Surfaced separately so '0 unbacked' cannot be misread as 'all checked'.
    assert r["unverifiable_count"] >= 2


# ---------------------- Prudence round 1 (#892) ---------------------------
def test_a_mention_is_not_a_claim(tmp_path):
    """A message that CORROBORATES ownership must not report it contradicted.

    The false-`unbacked` Prudence found: a substring match cannot tell "claims
    X" from "merely mentions X", so a later conversational message flipped the
    true owner to unbacked — and the triggering message named that owner.
    """
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    for name, body in [
        ("poe_1", "Claiming techne#570 - starting work."),
        ("poe_9", "Nice work on techne#570, poe_1 - reviewed, no notes."),
    ]:
        conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
            " VALUES ('s',?,'coord',?,'2026-09-07')",
            (name, body),
        )
    conn.commit()
    conn.close()

    v = _one(
        {"type": "ownership", "subject": "techne#570", "owner": "poe_1"}, StateContext(db_path=db)
    )
    assert v["status"] == "backed", v["detail"]
    assert "poe_9" not in v["detail"]


def test_a_subject_that_is_a_prefix_of_another(tmp_path):
    """`techne#57` must not be judged against `techne#570`'s claimant."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    for name, body in [
        ("poe_1", "Claiming techne#57 - the small one."),
        ("poe_9", "Claiming techne#570 - unrelated work."),
    ]:
        conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
            " VALUES ('s',?,'coord',?,'2026-09-07')",
            (name, body),
        )
    conn.commit()
    conn.close()

    ctx = StateContext(db_path=db)
    assert (
        _one({"type": "ownership", "subject": "techne#57", "owner": "poe_1"}, ctx)["status"]
        == "backed"
    )
    assert (
        _one({"type": "ownership", "subject": "techne#570", "owner": "poe_9"}, ctx)["status"]
        == "backed"
    )


def test_mentioned_but_never_claimed_is_unverifiable(bus):
    """Mentioned with no claim-shaped row: unknown, not contradicted."""
    db = bus
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_9','coord','Anyone looking at feat/orphan?','2026-09-07')"
    )
    conn.commit()
    conn.close()
    v = _one(
        {"type": "ownership", "subject": "feat/orphan", "owner": "poe_1"}, StateContext(db_path=db)
    )
    assert v["status"] == "unverifiable"
    assert "none of them claims it" in v["detail"]


def test_gh_answered_but_unintelligibly_is_unverifiable():
    """The mutation that survived: source responded and was not understood.

    test_absence_is_never_unbacked covers bus-silent, gh-unreachable and
    file-missing — but not "source responded, output unparseable", which is the
    case the substring parse actually introduces.
    """
    v = _one(
        {"type": "artifact_state", "ref": "o/r#1", "state": "open"},
        StateContext(gh=_gh("not json at all", rc=0)),
    )
    assert v["status"] == "unverifiable", v["detail"]


def test_a_merged_pr_satisfies_a_closed_claim():
    """The second surviving mutation. A merged PR IS closed on GitHub, so
    calling that claim contradicted would be another false unbacked."""
    v = _one(
        {"type": "artifact_state", "ref": "o/r#1", "state": "closed"},
        StateContext(gh=_gh('{"state":"closed","merged":true,"pr":true}')),
    )
    assert v["status"] == "backed"


def test_undecodable_file_is_unverifiable(tmp_path):
    """Mojibake must not manufacture a contradiction."""
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\xff\xfe\x00\x01 already_do_x \xc3\x28")
    v = _one(
        {"type": "capability", "path": "blob.bin", "pattern": "already_do_x"},
        StateContext(repo_root=tmp_path),
    )
    assert v["status"] == "unverifiable", v["detail"]


# ------------------- Prudence round 2 (#892) — her corpus ------------------
def _two_row_bus(tmp_path, second_body, first="Claiming techne#570 - starting work."):
    db = tmp_path / f"bus{abs(hash(second_body))}.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_1','coord',?, '2026-09-01')",
        (first,),
    )
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_9','coord',?, '2026-09-02')",
        (second_body,),
    )
    conn.commit()
    conn.close()
    return db


# Ordinary bus traffic that must NOT be read as poe_9 claiming the subject.
# Every one of these produced a false `unbacked` in round 2 (Prudence, #892).
@pytest.mark.parametrize(
    "body",
    [
        "Are you still working on techne#570?",
        "techne#570 is blocked on review, so starting on techne#571 instead.",
        "Is anyone starting on techne#570 today?",
        "I'm no longer working on techne#570, it's poe_1's.",
        "techne#570 is not mine, poe_1 has it.",
        "Taking a look at techne#570 before you push.",
        "techne#570 takes about nine minutes in CI.",
        "I'd land techne#570 on your own branch rather than main.",
        "The state-claims gate reports an unbacked claim in techne#570.",
        "Heads up: I'm working on the docs rail, ignore my noise near techne#570.",
        "Nice work on techne#570, poe_1 - reviewed, no notes.",
    ],
)
def test_ordinary_traffic_is_not_a_claim(tmp_path, body):
    v = _one(
        {"type": "ownership", "subject": "techne#570", "owner": "poe_1"},
        StateContext(db_path=_two_row_bus(tmp_path, body)),
    )
    assert v["status"] != "unbacked", f"{body!r} -> {v['detail']}"


# Real claims that must be RECOGNISED. A missed claim is not safe: when an older
# row is claim-shaped and the newer real claim is not, the verdict is `backed`
# for the STALE owner — the handoff blindness techne#584 exists to catch.
@pytest.mark.parametrize(
    "body",
    [
        "I owned techne#570 since Tuesday.",
        "Picking techne#570 up now.",
        "Grabbing techne#570.",
        "techne#570 assigned to me.",
        "I'll handle techne#570.",
        "On it: techne#570.",
        "Claiming techne#570 now.",
        "Taking techne#570 from poe_1 by handoff.",
    ],
)
def test_a_real_claim_is_recognised(tmp_path, body):
    v = _one(
        {"type": "ownership", "subject": "techne#570", "owner": "poe_1"},
        StateContext(db_path=_two_row_bus(tmp_path, body)),
    )
    assert v["status"] == "unbacked", f"{body!r} -> stale-owner {v['status']}"


# `-` is not a word character, so the numeric collision closed for free while
# branch and worktree-path siblings stayed open (Prudence, #892 r2).
@pytest.mark.parametrize(
    "subject,body",
    [
        ("feat/584", "Claiming feat/584-state-claims - unrelated."),
        ("fix/51-thing", "Claiming fix/51-thing-followup - unrelated."),
        ("/tmp/wt-3", "Claiming /tmp/wt-3-old - unrelated."),
    ],
)
def test_a_suffixed_sibling_is_not_the_subject(tmp_path, subject, body):
    db = tmp_path / f"s{abs(hash(subject))}.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_9','coord',?, '2026-09-02')",
        (body,),
    )
    conn.commit()
    conn.close()
    v = _one({"type": "ownership", "subject": subject, "owner": "poe_9"}, StateContext(db_path=db))
    assert v["status"] != "unbacked", v["detail"]


# The anchor must not stop matching the shapes we actually use.
@pytest.mark.parametrize(
    "subject,body",
    [
        ("techne#570", "Claiming techne#570 - starting work."),
        ("techne#570", "Claiming `techne#570` now."),
        ("techne#570", "Claiming (techne#570)."),
        ("feat/584-state-claims", "Claiming branch feat/584-state-claims in wt-3."),
        ("feat/584-state-claims", "Claiming /tmp/wt/feat/584-state-claims/ now."),
        ("fix/51-thing", "Claiming fix/51-thing."),
    ],
)
def test_real_subject_shapes_still_match(tmp_path, subject, body):
    db = tmp_path / f"m{abs(hash(subject + body))}.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE coord_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_session TEXT NOT NULL, from_name TEXT, recipient TEXT NOT NULL, "
        "thread_id TEXT, body TEXT NOT NULL, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO coord_messages (from_session, from_name, recipient, body, created_at)"
        " VALUES ('s','poe_1','coord',?, '2026-09-02')",
        (body,),
    )
    conn.commit()
    conn.close()
    v = _one({"type": "ownership", "subject": subject, "owner": "poe_1"}, StateContext(db_path=db))
    assert v["status"] == "backed", v["detail"]
