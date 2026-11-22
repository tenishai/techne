"""Hand curation of the pattern corpus (issue #1293).

Two gaps: a widened `architect.pattern_targets` tagged no existing pattern, because
applies rows came only from "where to apply in <target>" phrases and nothing in the
CLI could set one; and `reverdict` changed a verdict while leaving the skip reason
that explained the old one.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.core import architect
from techne.core.architect import ARCHITECT_SCHEMA, ARCHITECT_VERSION
from techne.core.patterns import (
    _apply_targets,
    add_pattern,
    applies_of,
    backfill_graph,
    get_investigation,
    patterns_for_target,
    reason_cites_evidence,
    set_verdict,
    store_snapshot,
    tag_pattern,
    target_counts,
    upsert_investigation,
)
from techne.core.stores import SqliteStore, plain_connect


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "techne-home"
    h.mkdir()
    (h / "config.yaml").write_text(
        "architect:\n"
        "  pattern_targets: [techne, cabal, kobo, agents]\n"
        "  target_aliases: {prudence: agents}\n"
    )
    monkeypatch.setenv("TECHNE_HOME", str(h))
    return h


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def _mk(db, name, body, url=None):
    url = url or f"http://x/{name}"
    inv = upsert_investigation(db, url=url, surveyed_at="2026-06-26", verdict="patterns-only")
    pid, _ = add_pattern(db, investigation_id=inv, name=name, source_url=url, body=body)
    return pid


def _names(db, target):
    return [p["name"] for p in patterns_for_target(db, target)]


# --- tag --applies / --no-applies -------------------------------------------


def test_a_curated_target_survives_backfill(db, home):
    a = _mk(db, "Alpha", "Nothing here names a project.")
    assert tag_pattern(db, a, applies=["kobo"]) is True
    backfill_graph(db)
    assert _names(db, "kobo") == ["Alpha"]
    assert applies_of(db, a) == [{"target": "kobo", "origin": "curated"}]


def test_an_exclusion_survives_a_backfill_that_would_re_derive_it(db, home):
    a = _mk(db, "Alpha", "**Where to apply in techne**: here.")
    backfill_graph(db)
    assert _names(db, "techne") == ["Alpha"]

    assert tag_pattern(db, a, no_applies=["techne"]) is True
    backfill_graph(db)
    assert _names(db, "techne") == []
    assert all(r["target"] != "techne" for r in target_counts(db))
    assert applies_of(db, a) == [{"target": "techne", "origin": "excluded"}]


def test_applies_after_an_exclusion_brings_the_target_back(db, home):
    a = _mk(db, "Alpha", "x")
    tag_pattern(db, a, no_applies=["cabal"])
    assert tag_pattern(db, a, applies=["cabal"]) is True
    assert _names(db, "cabal") == ["Alpha"]


def test_applies_on_a_body_derived_row_is_no_change(db, home):
    a = _mk(db, "Alpha", "**Where to apply in cabal**: here.")
    backfill_graph(db)
    assert tag_pattern(db, a, applies=["cabal"]) is False
    assert applies_of(db, a) == [{"target": "cabal", "origin": "body"}]


def test_an_unconfigured_target_is_refused_before_anything_is_written(db, home):
    a = _mk(db, "Alpha", "x")
    with pytest.raises(ValueError, match="not a configured target: notaproject"):
        tag_pattern(db, a, topic="policy-enforcement", applies=["notaproject"])
    with plain_connect(db) as conn:
        assert conn.execute("SELECT topic FROM patterns WHERE id = ?", (a,)).fetchone()[0] is None
    assert applies_of(db, a) == []


def test_removing_an_unconfigured_target_is_allowed(db, home):
    a = _mk(db, "Alpha", "x")
    assert tag_pattern(db, a, no_applies=["kaminos"]) is True


def test_the_same_target_on_both_sides_is_refused(db, home):
    a = _mk(db, "Alpha", "x")
    with pytest.raises(ValueError, match="both name: kobo"):
        tag_pattern(db, a, applies=["kobo"], no_applies=["Kobo"])


def test_tag_applies_canonicalises_through_the_alias(db, home):
    a = _mk(db, "Alpha", "x")
    tag_pattern(db, a, applies=["Prudence"])
    assert _names(db, "agents") == ["Alpha"]


# --- configured aliases ------------------------------------------------------


def test_a_configured_alias_resolves_in_a_body(db, home):
    _mk(db, "Alpha", "**Where to apply in prudence**: review.")
    backfill_graph(db)
    assert _names(db, "agents") == ["Alpha"]


def test_an_alias_to_an_unconfigured_target_resolves_to_nothing():
    assert (
        _apply_targets(
            "**Where to apply in prudence**: x", allowed={"techne"}, aliases={"prudence": "agents"}
        )
        == []
    )


def test_the_built_in_alias_still_applies_without_config():
    assert _apply_targets(
        "**Where to apply in limenal**: x", allowed={"liminal"}, aliases={"limenal": "liminal"}
    ) == ["liminal"]


def test_config_aliases_are_normalised_and_round_trip(home):
    from techne.config import Config

    (home / "config.yaml").write_text(
        "architect:\n  pattern_targets: [agents]\n  target_aliases: {' Prudence ': AGENTS, x: ''}\n"
    )
    cfg = Config.load()
    assert cfg.architect.target_aliases == {"prudence": "agents"}
    cfg.save()
    assert Config.load().architect.target_aliases == {"prudence": "agents"}


# --- schema ------------------------------------------------------------------


def test_v6_store_gains_origin_and_keeps_its_rows(tmp_path):
    db = tmp_path / "architect.db"
    conn = plain_connect(db)
    conn.executescript(
        """CREATE TABLE patterns (id TEXT PRIMARY KEY, name TEXT, source_url TEXT,
                                  body TEXT, status TEXT DEFAULT 'active');
           CREATE TABLE pattern_applies (pattern_id TEXT, target TEXT NOT NULL,
                                         PRIMARY KEY (pattern_id, target));
           INSERT INTO pattern_applies VALUES ('p1', 'techne');
           PRAGMA user_version = 6;"""
    )
    conn.commit()
    conn.close()

    architect._reconcile_relations(db)

    conn = plain_connect(db)
    try:
        # The ladder runs to the current version; this test owns only the v7 column.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION >= 8
        row = conn.execute("SELECT target, origin FROM pattern_applies").fetchone()
        assert (row[0], row[1]) == ("techne", "body")
    finally:
        conn.close()


# --- snapshot ----------------------------------------------------------------


def test_snapshot_moves_on_a_hand_set_target_but_not_on_backfill(db, home):
    a = _mk(db, "Alpha", "**Where to apply in techne**: here.")
    before = store_snapshot(db)
    backfill_graph(db)
    assert store_snapshot(db) == before
    tag_pattern(db, a, applies=["kobo"])
    assert store_snapshot(db) != before


# --- reverdict --reason ------------------------------------------------------


def _skip(db, url="http://x/repo", reason="judged against one project only"):
    upsert_investigation(
        db, url=url, surveyed_at="2026-07-01", verdict="skip-with-flag", skip_reason=reason
    )
    return url


def test_set_verdict_keeps_the_reason_by_default(db):
    url = _skip(db)
    assert set_verdict(db, url=url, verdict="patterns-only") is True
    row = get_investigation(db, url=url)
    assert (row["verdict"], row["skip_reason"]) == (
        "patterns-only",
        "judged against one project only",
    )


def test_set_verdict_replaces_or_clears_the_reason_and_keeps_the_date(db):
    url = _skip(db)
    set_verdict(db, url=url, verdict="skip-with-flag", reason="covered by [[X]]")
    assert get_investigation(db, url=url)["skip_reason"] == "covered by [[X]]"
    set_verdict(db, url=url, verdict="patterns-only", reason=None)
    row = get_investigation(db, url=url)
    assert row["skip_reason"] is None
    assert row["surveyed_at"].startswith("2026-07-01")


def test_reason_cites_evidence():
    assert reason_cites_evidence("covered by [[Some pattern]]")
    assert reason_cites_evidence("handled at src/tools/foo.ts:228-240")
    assert not reason_cites_evidence("not a control-plane fit")
    # The form the investigating guide asks for, with and without a line hint.
    assert reason_cites_evidence("covered at src/sched.ts::progressed")
    assert reason_cites_evidence("see techne/core/patterns.py::add_pattern:619")
    assert not reason_cites_evidence("the Foo::Bar idea does not apply")


def _cli(home, *args):
    from techne.cli import main

    return CliRunner().invoke(main, list(args))


@pytest.fixture()
def cli_db(home, db, monkeypatch):
    monkeypatch.setattr("techne.core.architect.architect_db", lambda _c: db)
    return db


def test_cli_reverdict_reason_flags(home, cli_db):
    url = _skip(cli_db)
    r = _cli(home, "architect", "reverdict", url, "--verdict", "patterns-only")
    assert r.exit_code == 0, r.output
    assert "still carries its skip reason" in r.output

    r = _cli(home, "architect", "reverdict", url, "--verdict", "patterns-only", "--clear-reason")
    assert r.exit_code == 0, r.output
    assert "still carries" not in r.output
    assert get_investigation(cli_db, url=url)["skip_reason"] is None

    r = _cli(home, "architect", "reverdict", url, "--verdict", "skip-with-flag", "--reason", "meh")
    assert r.exit_code == 0, r.output
    assert "cites no [[pattern]]" in r.output

    r = _cli(
        home, "architect", "reverdict", url, "--verdict", "skip", "--reason", "x", "--clear-reason"
    )
    assert r.exit_code != 0
    assert "mutually exclusive" in r.output


def test_cli_tag_applies_prints_the_result(home, cli_db):
    a = _mk(cli_db, "Alpha", "**Where to apply in techne**: here.")
    backfill_graph(cli_db)
    r = _cli(home, "architect", "tag", a, "--applies", "kobo", "--no-applies", "techne")
    assert r.exit_code == 0, r.output
    assert "applies: kobo (curated)" in r.output
    assert "excluded: techne" in r.output

    r = _cli(home, "architect", "tag", a, "--applies", "nope")
    assert r.exit_code == 1
    assert "not a configured target" in r.output
