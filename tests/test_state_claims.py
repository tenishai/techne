"""Tests for techne#584 state-claim gate — the conversational half.

Coverage:
- artifact_state: open / closed / merged, unreachable GitHub, refusal to guess
  an owner.  (An ownership type was built and cut before merge — techne#894.)
- capability: present / absent / contradicted / unreadable.
- The load-bearing invariant: ABSENCE IS NEVER 'unbacked'.
- The gate never raises.
"""

from __future__ import annotations

from techne.core.state_claims import StateContext, check_state_claims


def _one(claim, ctx):
    v = check_state_claims([claim], ctx)
    assert len(v) == 1
    return v[0]


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


def test_absence_is_never_unbacked(tmp_path):
    """The invariant, asserted as one property across all three types."""
    cases = [
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
    (tmp_path / "src.py").write_text("def already_do_x(): pass\n")

    cfg = Config()
    cfg.db_path = db
    claims = [
        {"type": "completion"},
        {"type": "capability", "path": "src.py", "pattern": "already_do_x"},
        {"type": "capability", "path": "gone.py", "pattern": "z"},
    ]
    r = _techne_check_evidence(
        {"session_id": "s1", "claims": claims, "repo_root": str(tmp_path)}, cfg
    )

    assert len(r["verdicts"]) == len(claims)
    assert all(v for v in r["verdicts"]), "a verdict slot was left empty"
    assert r["verdicts"][1]["status"] == "backed"
    assert r["verdicts"][2]["status"] == "unverifiable"
    # Surfaced separately so '0 unbacked' cannot be misread as 'all checked'.
    assert r["unverifiable_count"] >= 2


# ---------------------- Prudence round 1 (#892) ---------------------------


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
