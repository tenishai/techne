"""The supersession surface (techne#877, #887): the CLI verb, `techne_store`'s
`supersedes`, the derived `--type` choices, and doctor's prose-declaration report.

Before this, core could supersede but nothing outside Python could reach it, so an
agent that noticed a stale entry could only write "SUPERSEDES #N" in prose, which
recall never reads. These tests drive the public surfaces, not the core helpers.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import (
    VALID_TYPES,
    _declared_targets_with_ambiguity,
    declared_targets,
    find_declared_supersessions,
    retire,
    store,
)
from techne.mcp_server import handle_request


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    db = tmp_path / "techne.db"
    init_db(db).close()
    c = Config()
    c.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


def _row(db, kid):
    conn = connect(db)
    try:
        return dict(conn.execute("SELECT * FROM knowledge WHERE id = ?", (kid,)).fetchone())
    finally:
        conn.close()


def _count(db):
    conn = connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    finally:
        conn.close()


def _set_created_at(db, kid, ts):
    conn = connect(db)
    try:
        conn.execute("UPDATE knowledge SET created_at = ? WHERE id = ?", (ts, kid))
        conn.commit()
    finally:
        conn.close()


def _add_dep(db, dependent, trigger):
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO knowledge_deps (dependent_id, trigger_id) VALUES (?, ?)",
            (dependent, trigger),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- --type choices (#877)


def test_type_choices_are_core_valid_types():
    """The CLI offered `pattern`, which core always refuses, and omitted the document
    types core accepts. The choices now come from VALID_TYPES, both commands."""
    for cmd in ("add", "list"):
        opt = next(p for p in main.commands["knowledge"].commands[cmd].params if p.name == "ktype")
        assert list(opt.type.choices) == list(VALID_TYPES), cmd
    assert "pattern" not in VALID_TYPES


def test_add_pattern_is_a_usage_error_not_a_traceback(cfg):
    res = CliRunner().invoke(main, ["knowledge", "add", "probe", "--type", "pattern"])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert _count(cfg.db_path) == 0


def test_add_core_refusal_prints_cleanly(cfg, monkeypatch):
    """A ValueError from store() must surface as a CLI error, not a traceback."""
    import techne.core.knowledge as k

    def boom(*a, **kw):
        raise ValueError("core said no, for a reason worth reading")

    monkeypatch.setattr(k, "store", boom)
    res = CliRunner().invoke(main, ["knowledge", "add", "x"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "core said no, for a reason worth reading" in res.output


# --------------------------------------------------------------- CLI supersede (#877/#887)


def test_cli_supersede_with_content_writes_a_revision(cfg):
    old = store(cfg.db_path, "runner is host-a", type="decision", tags="ci", confidence="high")
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(old), "runner is host-c"])
    assert res.exit_code == 0, res.output
    new = _row(cfg.db_path, old)["superseded_by"]
    assert new is not None and f"#{new}" in res.output
    new_row = _row(cfg.db_path, new)
    assert new_row["content"] == "runner is host-c"
    assert (new_row["type"], new_row["tags"], new_row["confidence"]) == ("decision", "ci", "high")


def test_cli_supersede_content_is_redacted(cfg):
    old = store(cfg.db_path, "token lives in vault")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(old), f"key is {secret}"])
    assert res.exit_code == 0, res.output
    new = _row(cfg.db_path, old)["superseded_by"]
    assert secret not in _row(cfg.db_path, new)["content"]


def test_cli_supersede_by_retires_into_existing_entry(cfg):
    old = store(cfg.db_path, "runner is host-a")
    dep = store(cfg.db_path, "host-a has 2 cores")
    _add_dep(cfg.db_path, dep, old)
    new = store(cfg.db_path, "SUPERSEDES #1: runner is host-c")
    before = _count(cfg.db_path)
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(old), "--by", str(new)])
    assert res.exit_code == 0, res.output
    assert _row(cfg.db_path, old)["superseded_by"] == new
    assert _count(cfg.db_path) == before, "--by must not mint a row"
    assert _row(cfg.db_path, dep)["status"] == "uncertain", "the #117 cascade must run"


@pytest.mark.parametrize(
    "args",
    [["1"], ["1", "text", "--by", "2"]],
    ids=["neither", "both"],
)
def test_cli_supersede_needs_exactly_one_form(cfg, args):
    store(cfg.db_path, "a")
    store(cfg.db_path, "b")
    res = CliRunner().invoke(main, ["knowledge", "supersede", *args])
    assert res.exit_code == 2
    assert _row(cfg.db_path, 1)["superseded_by"] is None


@pytest.mark.parametrize("form", ["content", "by"])
def test_cli_supersede_refusals_are_clean_errors(cfg, form):
    a = store(cfg.db_path, "a")
    b = store(cfg.db_path, "b")
    retire(cfg.db_path, a, b)
    extra = ["again"] if form == "content" else ["--by", str(b)]
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(a), *extra])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "already superseded" in res.output


def test_retire_refuses_an_archived_survivor(cfg):
    old = store(cfg.db_path, "old")
    survivor = store(cfg.db_path, "survivor")
    conn = connect(cfg.db_path)
    conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (survivor,))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="archived"):
        retire(cfg.db_path, old, survivor)
    assert _row(cfg.db_path, old)["superseded_by"] is None


# --------------------------------------------------------------- store(supersedes=...) (#887)


def _mcp_store(cfg, args):
    resp = handle_request(
        {"id": 1, "method": "tools/call", "params": {"name": "techne_store", "arguments": args}},
        cfg,
    )
    return resp["result"]


def test_mcp_store_supersedes_retires_the_named_entries(cfg):
    a = store(cfg.db_path, "runner host-a")
    b = store(cfg.db_path, "runner host-b")
    dep = store(cfg.db_path, "derived from a")
    _add_dep(cfg.db_path, dep, a)
    result = _mcp_store(
        cfg, {"content": "runner fleet is host-c", "type": "decision", "supersedes": [a, b]}
    )
    assert "isError" not in result, result
    payload = json.loads(result["content"][0]["text"])
    new = payload["id"]
    assert payload["superseded"] == [a, b]
    assert _row(cfg.db_path, a)["superseded_by"] == new
    assert _row(cfg.db_path, b)["superseded_by"] == new
    # The caller's own type is kept (unlike supersede(), which inherits the old one).
    assert _row(cfg.db_path, new)["type"] == "decision"
    assert _row(cfg.db_path, dep)["status"] == "uncertain"


def test_mcp_store_schema_advertises_supersedes():
    from techne.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "techne_store")
    prop = tool["inputSchema"]["properties"]["supersedes"]
    assert prop["type"] == "array" and prop["items"]["type"] == "integer"


@pytest.mark.parametrize(
    "bad",
    ["missing", "already-superseded", "duplicate"],
)
def test_store_supersedes_is_all_or_nothing(cfg, bad):
    """A bad id must roll back the insert too: no new row, no partial retirement."""
    a = store(cfg.db_path, "a")
    b = store(cfg.db_path, "b")
    if bad == "missing":
        ids = [a, 999]
    elif bad == "already-superseded":
        retire(cfg.db_path, b, a)
        a = store(cfg.db_path, "c")
        ids = [a, b]
    else:
        ids = [a, a]
    before = _count(cfg.db_path)
    with pytest.raises(ValueError):
        store(cfg.db_path, "replacement", supersedes=ids)
    assert _count(cfg.db_path) == before
    assert _row(cfg.db_path, a)["superseded_by"] is None


def test_store_supersedes_accepts_a_single_id(cfg):
    a = store(cfg.db_path, "a")
    new = store(cfg.db_path, "b", supersedes=a)
    assert _row(cfg.db_path, a)["superseded_by"] == new


def test_store_supersedes_rejects_a_bool():
    with pytest.raises(ValueError, match="not a knowledge id"):
        from techne.core.knowledge import _normalize_ids

        _normalize_ids([True])


# --------------------------------------------------------------- prose declarations (#887)


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "SUPERSEDES techne#882 (host-a runner), #887 (host-a hardening) and #995 (second)",
            [882, 887, 995],
        ),
        ("CORRECTION to knowledge #12: the value is 4", [12]),
        ("Corrects #7.", [7]),
        ("supersedes 459", []),
        ("Corrects 2 errors in the runbook", []),
        ("SUPERSEDES: #7", [7]),
        ("SUPERSEDES \N{BRAIN}3394 and \N{BRAIN}3395", [3394, 3395]),
        ("superseded by #5", []),
        ("supersedes my recommendation to close cabal#3", []),
        ("SUPERSEDES cabal#3", []),
        ("x" * 400 + " SUPERSEDES #9", []),
    ],
)
def test_declared_targets(text, expected):
    assert declared_targets(text) == expected


# ------------------------------------------------- ambiguous vs confident refs (techne#1065)


@pytest.mark.parametrize(
    "text,expected",
    [
        # A literal "techne#N" is the one marker that can equally name a GitHub
        # issue, not this knowledge row — always ambiguous.
        ("CORRECTION to techne#621: the checklist is wrong", [(621, True)]),
        ("CORRECTS techne#896's framing of the hardware", [(896, True)]),
        # A bare "#N", "knowledge #N", and the brain sigil carry no such doubt.
        ("SUPERSEDES #7", [(7, False)]),
        ("CORRECTION to knowledge #12: the value is 4", [(12, False)]),
        ("SUPERSEDES \N{BRAIN}3394", [(3394, False)]),
        # A list continuation after a "techne#N" opener carries no marker of its
        # own, so it inherits the opener's ambiguity — whatever makes 882
        # ambiguous makes 887 and 995 ambiguous too (techne#1065 review round 1).
        (
            "SUPERSEDES techne#882 (old runner), #887 (hardening) and #995 (third)",
            [(882, True), (887, True), (995, True)],
        ),
        # A continuation with its OWN "knowledge #" marker is confident
        # regardless of how the list opened.
        (
            "SUPERSEDES techne#1 and knowledge #2",
            [(1, True), (2, False)],
        ),
        # A continuation inherits from the NEAREST preceding marked reference,
        # not the list opener: #3 follows techne#2, so it is as ambiguous as 2.
        (
            "SUPERSEDES knowledge #1, techne#2 and #3",
            [(1, False), (2, True), (3, True)],
        ),
        # The other direction: #3 follows "knowledge #2", so it is confident
        # even though the list opened with techne#1 (the writer switched
        # markers on purpose).
        (
            "SUPERSEDES techne#1, knowledge #2 and #3",
            [(1, True), (2, False), (3, False)],
        ),
        # Same id named twice, once ambiguously and once not: the unambiguous
        # occurrence wins the dedup (techne#1065 review nit).
        (
            "SUPERSEDES techne#5 and knowledge #5",
            [(5, False)],
        ),
    ],
)
def test_declared_targets_with_ambiguity(text, expected):
    assert _declared_targets_with_ambiguity(text) == expected


def test_find_declared_supersessions_reports_only_live_older_targets(cfg):
    db = cfg.db_path
    t1 = store(db, "old fact")  # 1: live, declared by 3 and 4 -> newest (4) wins
    t2 = store(db, "already retired")  # 2
    d3 = store(db, f"SUPERSEDES #{t1}")  # 3
    d4 = store(db, f"SUPERSEDES #{t1} and #{t2}")  # 4
    retire(db, t2, d3)
    store(db, "SUPERSEDES #99")  # missing target
    store(db, "SUPERSEDES #8")  # 6 -> newer id: skipped
    store(db, "newer")  # 7
    store(db, "SUPERSEDES #8")  # 8 -> self: skipped
    pairs = find_declared_supersessions(db)
    # t1 is claimed twice (newest claimant wins); t2 is already retired; #99 is
    # missing; #8 is newer than 6 and is 8 itself.
    assert [(p["target"], p["declarer"]) for p in pairs] == [(t1, d4)]


def test_find_declared_supersessions_chain_order_applies_cleanly(cfg):
    """Ascending target order is the order that works: retire needs a live survivor."""
    db = cfg.db_path
    a = store(db, "v1")
    b = store(db, f"SUPERSEDES #{a} v2")
    c = store(db, f"SUPERSEDES #{b} v3")
    pairs = find_declared_supersessions(db)
    assert [(p["target"], p["declarer"]) for p in pairs] == [(a, b), (b, c)]
    for p in pairs:
        retire(db, p["target"], p["declarer"])
    assert find_declared_supersessions(db) == []
    assert _row(db, c)["superseded_by"] is None


def test_find_skips_a_superseded_or_archived_declarer(cfg):
    db = cfg.db_path
    a = store(db, "v1")
    b = store(db, f"SUPERSEDES #{a}")
    conn = connect(db)
    conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (b,))
    conn.commit()
    conn.close()
    assert find_declared_supersessions(db) == []


def test_doctor_lists_declared_supersessions_without_failing_on_them(cfg):
    db = cfg.db_path
    a = store(db, "runner is host-a")
    b = store(db, f"SUPERSEDES #{a}: runner is host-c")
    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"techne knowledge supersede {a} --by {b}" in res.output
    assert res.exit_code == 0, "advisory: a prose match may name an issue, not a row"
    assert _row(db, a)["superseded_by"] is None, "doctor reports; it never repairs"


def test_doctor_says_none_when_clean(cfg):
    store(cfg.db_path, "plain")
    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert "Supersessions declared in prose" in res.output
    assert "✓ none" in res.output


# ------------------------------------------- techne#1065: techne#N is not a verdict


# `techne#N` is the day before the 2026-08-31 convention line, so `techne#N`
# still gets the benefit of the doubt as a knowledge ref there (just ambiguous).
_PRE_CONVENTION = "2026-08-30 00:00:00"
_POST_CONVENTION = "2026-09-28 10:00:00"


@pytest.mark.parametrize(
    "target_fact,declarer_text",
    [
        # techne#1065's first real false pair: knowledge 1769 read "a correction
        # to techne#621's own rationale" (meaning issue #621) and matched live,
        # unrelated knowledge 621 (a grep-assist restart checklist).
        ("grep-assist restart checklist", "a correction to techne#{t}'s own rationale"),
        # The second: knowledge 3537 read "CORRECTS techne#896's ... framing"
        # (meaning issue #896) and matched knowledge 896 (the dev02 hardware
        # profile, which never mentions the issue's subject at all).
        ("dev02 hardware profile: 8 cores, 32G", "CORRECTS techne#{t}'s memory framing"),
    ],
    ids=["knowledge-621-vs-issue-621", "knowledge-896-vs-issue-896"],
)
def test_find_declared_supersessions_flags_bare_techne_ref_ambiguous(
    cfg, target_fact, declarer_text
):
    """Reconstructs the two real false pairs from techne#1065. Pre-convention, the
    match is still reported (an older declarer may genuinely mean the knowledge
    row) but MUST come back `ambiguous`."""
    db = cfg.db_path
    t = store(db, target_fact)
    _set_created_at(db, t, _PRE_CONVENTION)
    d = store(db, declarer_text.format(t=t))
    _set_created_at(db, d, _PRE_CONVENTION)
    pairs = find_declared_supersessions(db)
    assert [(p["target"], p["declarer"], p["ambiguous"]) for p in pairs] == [(t, d, True)]


def test_find_declared_supersessions_drops_bare_techne_ref_post_convention(cfg):
    """On or after 2026-08-31, `techne#N` names a GitHub issue, full stop — a
    declarer written then gets no knowledge-ref benefit of the doubt from it, so
    the pair must not be reported at all (techne#1065 option 2)."""
    db = cfg.db_path
    t = store(db, "dev02 hardware profile: 8 cores, 32G")
    _set_created_at(db, t, _PRE_CONVENTION)
    d = store(db, f"CORRECTS techne#{t}'s memory framing")
    _set_created_at(db, d, _POST_CONVENTION)
    assert find_declared_supersessions(db) == []


def test_find_declared_supersessions_drops_techne_list_continuation_post_convention(cfg):
    """Prudence review round 1, techne#1065: `techne#1 and #2` is one list, and
    `#2` is exactly as ambiguous as `techne#1` — it must not stay confident just
    because it has no word of its own. A post-convention declarer gets neither."""
    db = cfg.db_path
    t1 = store(db, "grep-assist restart checklist")
    _set_created_at(db, t1, _PRE_CONVENTION)
    t2 = store(db, "dev02 hardware profile: 8 cores, 32G")
    _set_created_at(db, t2, _PRE_CONVENTION)
    d = store(db, f"a correction to techne#{t1} and #{t2}: the issue rationale was wrong")
    _set_created_at(db, d, _POST_CONVENTION)
    assert find_declared_supersessions(db) == []


def test_find_declared_supersessions_genuine_ref_still_confident_post_convention(cfg):
    """A `knowledge #N` or brain-sigil declaration is never ambiguous, and a
    declarer written after the convention date still gets it."""
    db = cfg.db_path
    t1 = store(db, "runner is host-a")
    _set_created_at(db, t1, _PRE_CONVENTION)
    d1 = store(db, f"CORRECTION to knowledge #{t1}: runner is host-c")
    _set_created_at(db, d1, _POST_CONVENTION)

    t2 = store(db, "runner is host-b")
    _set_created_at(db, t2, _PRE_CONVENTION)
    d2 = store(db, f"SUPERSEDES \N{BRAIN}{t2}: runner is host-d")
    _set_created_at(db, d2, _POST_CONVENTION)

    pairs = {p["target"]: p for p in find_declared_supersessions(db)}
    assert pairs[t1]["declarer"] == d1 and pairs[t1]["ambiguous"] is False
    assert pairs[t2]["declarer"] == d2 and pairs[t2]["ambiguous"] is False


def test_doctor_renders_ambiguous_pairs_as_check_items_not_commands(cfg):
    """techne#1065: the two real false pairs must render as CHECK items, never as
    a paste-ready `techne knowledge supersede` line."""
    db = cfg.db_path
    t = store(db, "grep-assist restart checklist")
    _set_created_at(db, t, _PRE_CONVENTION)
    d = store(db, f"a correction to techne#{t}'s own rationale")
    _set_created_at(db, d, _PRE_CONVENTION)

    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    ready_line = f"    techne knowledge supersede {t} --by {d}   #"
    assert ready_line not in res.output, res.output
    assert f"CHECK: entry {d} says it replaces techne#{t}" in res.output
    assert "GitHub issue" in res.output
    assert res.exit_code == 0, "advisory: never counted as a doctor failure"


def test_doctor_prints_no_command_for_a_techne_list_continuation_post_convention(cfg):
    """Prudence's probe (review round 1): a declarer dated 2026-09-28 naming
    `techne#1 and #2` must print no `techne knowledge supersede` command for
    EITHER id — #2 inherited techne#1's ambiguity, so post-convention it is
    dropped exactly like the opener, not rendered as a ready command."""
    db = cfg.db_path
    t1 = store(db, "grep-assist restart checklist")
    _set_created_at(db, t1, _PRE_CONVENTION)
    t2 = store(db, "dev02 hardware profile: 8 cores, 32G")
    _set_created_at(db, t2, _PRE_CONVENTION)
    d = store(db, f"a correction to techne#{t1} and #{t2}: the issue rationale was wrong")
    _set_created_at(db, d, _POST_CONVENTION)

    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"techne knowledge supersede {t1} --by {d}" not in res.output, res.output
    assert f"techne knowledge supersede {t2} --by {d}" not in res.output, res.output
    assert "✓ none" in res.output
    assert res.exit_code == 0


def test_doctor_still_renders_genuine_supersessions_as_commands(cfg):
    """A confident match (bare `#N` here) is unaffected by the ambiguous path and
    still prints as a ready-to-run command."""
    db = cfg.db_path
    a = store(db, "runner is host-a")
    b = store(db, f"SUPERSEDES #{a}: runner is host-c")
    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"techne knowledge supersede {a} --by {b}" in res.output
    assert "CHECK:" not in res.output
    assert res.exit_code == 0


def test_doctor_prints_no_command_for_a_continuation_after_a_mid_list_techne_ref(cfg):
    """`knowledge #A, techne#B and #C`: #C follows techne#B, so it inherits
    techne#B's ambiguity, not the confident opener's. A post-convention declarer
    gets a command for A only."""
    db = cfg.db_path
    ta = store(db, "runner is host-a")
    tb = store(db, "grep-assist restart checklist")
    tc = store(db, "dev02 hardware profile: 8 cores, 32G")
    for t in (ta, tb, tc):
        _set_created_at(db, t, _PRE_CONVENTION)
    d = store(db, f"SUPERSEDES knowledge #{ta}, techne#{tb} and #{tc}: see the issues")
    _set_created_at(db, d, _POST_CONVENTION)

    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"techne knowledge supersede {ta} --by {d}" in res.output, res.output
    assert f"techne knowledge supersede {tb} --by {d}" not in res.output, res.output
    assert f"techne knowledge supersede {tc} --by {d}" not in res.output, res.output
    assert res.exit_code == 0


def test_doctor_check_line_quotes_an_inherited_continuation_as_written(cfg):
    """The CHECK line for an inherited-ambiguous pair must quote `#C` as the text
    has it, naming the `techne#B` it follows, not claim the text says `techne#C`."""
    db = cfg.db_path
    tb = store(db, "grep-assist restart checklist")
    tc = store(db, "dev02 hardware profile: 8 cores, 32G")
    for t in (tb, tc):
        _set_created_at(db, t, _PRE_CONVENTION)
    d = store(db, f"a correction to techne#{tb} and #{tc}: the issue rationale was wrong")
    _set_created_at(db, d, _PRE_CONVENTION)

    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"CHECK: entry {d} says it replaces techne#{tb} —" in res.output, res.output
    assert f"CHECK: entry {d} says it replaces #{tc} (listed after techne#{tb}) —" in res.output
    assert f"replaces techne#{tc}" not in res.output
