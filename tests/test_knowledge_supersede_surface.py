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
    old = store(cfg.db_path, "runner is dev02", type="decision", tags="ci", confidence="high")
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(old), "runner is mash1"])
    assert res.exit_code == 0, res.output
    new = _row(cfg.db_path, old)["superseded_by"]
    assert new is not None and f"#{new}" in res.output
    new_row = _row(cfg.db_path, new)
    assert new_row["content"] == "runner is mash1"
    assert (new_row["type"], new_row["tags"], new_row["confidence"]) == ("decision", "ci", "high")


def test_cli_supersede_content_is_redacted(cfg):
    old = store(cfg.db_path, "token lives in vault")
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    res = CliRunner().invoke(main, ["knowledge", "supersede", str(old), f"key is {secret}"])
    assert res.exit_code == 0, res.output
    new = _row(cfg.db_path, old)["superseded_by"]
    assert secret not in _row(cfg.db_path, new)["content"]


def test_cli_supersede_by_retires_into_existing_entry(cfg):
    old = store(cfg.db_path, "runner is dev02")
    dep = store(cfg.db_path, "dev02 has 2 cores")
    _add_dep(cfg.db_path, dep, old)
    new = store(cfg.db_path, "SUPERSEDES #1: runner is mash1")
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
    a = store(cfg.db_path, "runner dev02")
    b = store(cfg.db_path, "runner prinkle")
    dep = store(cfg.db_path, "derived from a")
    _add_dep(cfg.db_path, dep, a)
    result = _mcp_store(
        cfg, {"content": "runner fleet is mash1", "type": "decision", "supersedes": [a, b]}
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
            "SUPERSEDES techne#882 (dev02 runner), #887 (dev02 hardening) and #995 (second)",
            [882, 887, 995],
        ),
        ("CORRECTION to knowledge #12: the value is 4", [12]),
        ("Corrects #7.", [7]),
        ("supersedes 459", [459]),
        ("superseded by #5", []),
        ("supersedes my recommendation to close cabal#3", []),
        ("SUPERSEDES cabal#3", []),
        ("x" * 400 + " SUPERSEDES #9", []),
    ],
)
def test_declared_targets(text, expected):
    assert declared_targets(text) == expected


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
    a = store(db, "runner is dev02")
    b = store(db, f"SUPERSEDES #{a}: runner is mash1")
    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert f"techne knowledge supersede {a} --by {b}" in res.output
    assert res.exit_code == 0, "advisory: a prose match may name an issue, not a row"
    assert _row(db, a)["superseded_by"] is None, "doctor reports; it never repairs"


def test_doctor_says_none_when_clean(cfg):
    store(cfg.db_path, "plain")
    res = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert "Supersessions declared in prose" in res.output
    assert "✓ none" in res.output
