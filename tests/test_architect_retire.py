"""`retired`: a pattern leaves the active set with no successor (issue #1345)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.core import architect
from techne.core.architect import ARCHITECT_SCHEMA, ARCHITECT_VERSION
from techne.core.patterns import (
    add_pattern,
    backfill_graph,
    get_pattern,
    patterns_for_target,
    record_outcome,
    retire,
    search_patterns,
    stats,
    store_snapshot,
    supersede,
    tag_pattern,
    topic_show,
    upsert_investigation,
)
from techne.core.stores import SqliteStore, plain_connect


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "techne-home"
    h.mkdir()
    (h / "config.yaml").write_text("architect:\n  pattern_targets: [techne, kobo]\n")
    monkeypatch.setenv("TECHNE_HOME", str(h))
    return h


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def _mk(db, name, body="**Where to apply in techne**: the egress rail.", topic="eval-harness"):
    url = f"http://x/{name}"
    inv = upsert_investigation(db, url=url, surveyed_at="2026-06-26", verdict="patterns-only")
    pid, _ = add_pattern(db, investigation_id=inv, name=name, source_url=url, body=body)
    tag_pattern(db, pid, topic=topic)
    return pid


def test_retired_pattern_leaves_every_active_read(db, home):
    a = _mk(db, "Gauge Alpha")
    b = _mk(db, "Gauge Beta")
    backfill_graph(db)
    assert {p["id"] for p in search_patterns(db, "Gauge")} == {a, b}

    assert retire(db, pattern_id=a, reason="standard idiom") is True

    assert {p["id"] for p in search_patterns(db, "Gauge")} == {b}
    assert [p["id"] for p in topic_show(db, "eval-harness")] == [b]
    assert [p["id"] for p in patterns_for_target(db, "techne")] == [b]
    s = stats(db)
    assert (s["patterns"], s["patterns_retired"], s["patterns_superseded"]) == (1, 1, 0)


def test_retire_records_reason_and_end_of_window(db):
    a = _mk(db, "Alpha")
    retire(db, pattern_id=a, reason="docs only", invalid_at="2026-11-06 10:00:00")
    p = get_pattern(db, a)
    assert (p["status"], p["retired_reason"], p["invalid_at"]) == (
        "retired",
        "docs only",
        "2026-11-06 10:00:00",
    )


def test_re_retiring_updates_the_reason_and_keeps_the_date(db):
    a = _mk(db, "Alpha")
    retire(db, pattern_id=a, reason="first", invalid_at="2026-11-06 10:00:00")
    assert retire(db, pattern_id=a, reason="first") is False
    assert retire(db, pattern_id=a, reason="second", invalid_at="2027-01-01 00:00:00") is True
    p = get_pattern(db, a)
    assert (p["retired_reason"], p["invalid_at"]) == ("second", "2026-11-06 10:00:00")


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_retire_needs_a_reason(db, reason):
    a = _mk(db, "Alpha")
    with pytest.raises(ValueError, match="needs a reason"):
        retire(db, pattern_id=a, reason=reason)
    assert get_pattern(db, a)["status"] == "active"


def test_retire_refuses_a_pattern_with_an_outcome(db):
    a = _mk(db, "Alpha")
    record_outcome(db, pattern_id=a, ref="tenishai/cabal#105")
    with pytest.raises(ValueError, match="recorded outcome"):
        retire(db, pattern_id=a, reason="standard idiom")
    assert get_pattern(db, a)["status"] == "active"


def test_retire_refuses_a_superseded_pattern(db):
    a = _mk(db, "Alpha")
    b = _mk(db, "Beta")
    supersede(db, old_id=a, new_id=b)
    with pytest.raises(ValueError, match="already superseded by"):
        retire(db, pattern_id=a, reason="x")
    assert get_pattern(db, a)["status"] == "superseded"


def test_retire_refuses_an_unknown_id(db):
    with pytest.raises(ValueError, match="unknown pattern id"):
        retire(db, pattern_id="0" * 16, reason="x")


def test_tag_routes_retired_and_guards_reason(db):
    a = _mk(db, "Alpha")
    b = _mk(db, "Beta")
    with pytest.raises(ValueError, match="--reason goes with --status retired"):
        tag_pattern(db, a, topic="policy-enforcement", reason="x")
    with pytest.raises(ValueError, match="no successor"):
        tag_pattern(db, a, status="retired", superseded_by=b, reason="x")
    with pytest.raises(ValueError, match="needs a reason"):
        tag_pattern(db, a, status="retired")
    assert get_pattern(db, a)["status"] == "active"

    assert tag_pattern(db, a, status="retired", reason="prose only", topic="sandbox-isolation")
    p = get_pattern(db, a)
    assert (p["status"], p["retired_reason"], p["topic"]) == (
        "retired",
        "prose only",
        "sandbox-isolation",
    )


def test_snapshot_moves_on_retire(db):
    a = _mk(db, "Alpha")
    before = store_snapshot(db)
    retire(db, pattern_id=a, reason="x")
    assert store_snapshot(db) != before


def test_v7_store_gains_retired_reason_and_keeps_its_rows(tmp_path):
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        """CREATE TABLE patterns (id TEXT PRIMARY KEY, name TEXT, source_url TEXT,
                                  body TEXT, status TEXT DEFAULT 'active');
           INSERT INTO patterns (id, name, source_url, body) VALUES ('p1', 'n', 'u', 'b');
           PRAGMA user_version = 7;"""
    )
    conn.commit()
    conn.close()

    architect._reconcile_relations(db)
    architect._reconcile_relations(db)  # idempotent

    conn = plain_connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION == 8
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)")}
        assert "retired_reason" in cols
        row = conn.execute("SELECT id, status, retired_reason FROM patterns").fetchone()
        assert (row[0], row[1], row[2]) == ("p1", "active", None)
    finally:
        conn.close()


def _cli(*args):
    from techne.cli import main

    return CliRunner().invoke(main, list(args))


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    """A real initialised home, so the bare `architect` summary runs too."""
    from techne.config import Config
    from techne.core.architect import architect_db
    from techne.core.database import init_db

    init_db(tmp_path / "techne.db").close()
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    db = architect_db(cfg)
    SqliteStore("architect", db).migrate(ARCHITECT_SCHEMA)
    return db


def test_cli_retire_show_and_summary(cli_db):
    a = _mk(cli_db, "Alpha")
    _mk(cli_db, "Beta")

    r = _cli("architect", "tag", a, "--status", "retired")
    assert r.exit_code == 1
    assert "needs a reason" in r.output

    r = _cli("architect", "tag", a, "--status", "retired", "--reason", "vendor blog, no code")
    assert r.exit_code == 0, r.output
    assert "updated" in r.output

    r = _cli("architect", "show", a)
    assert r.exit_code == 0, r.output
    assert "status: retired" in r.output
    assert "vendor blog, no code" in r.output

    r = _cli("architect")
    assert r.exit_code == 0, r.output
    assert "Patterns: 1 active" in r.output
    assert "+1 retired" in r.output
