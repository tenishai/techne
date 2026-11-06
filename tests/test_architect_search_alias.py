"""Search aliases: a renamed project's old spelling still matches (issue #1345)."""

from __future__ import annotations

import pytest

from techne.core.architect import ARCHITECT_SCHEMA
from techne.core.patterns import (
    _expand_aliases,
    add_pattern,
    search_alias_groups,
    search_patterns,
    upsert_investigation,
)
from techne.core.stores import SqliteStore


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "techne-home"
    h.mkdir()
    (h / "config.yaml").write_text("architect:\n  pattern_targets: [techne]\n")
    monkeypatch.setenv("TECHNE_HOME", str(h))
    return h


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def _mk(db, name, body):
    url = f"http://x/{name}"
    inv = upsert_investigation(db, url=url, surveyed_at="2026-06-26", verdict="patterns-only")
    pid, _ = add_pattern(db, investigation_id=inv, name=name, source_url=url, body=body)
    return pid


def _ids(db, q):
    return {p["id"] for p in search_patterns(db, q)}


def test_either_spelling_finds_both(db, home):
    old = _mk(db, "Old", "the limenal gateway forwards identity")
    new = _mk(db, "New", "the liminal gateway forwards identity")
    other = _mk(db, "Other", "the cabal broker re-mints tokens")
    assert _ids(db, "liminal") == {old, new}
    assert _ids(db, "limenal") == {old, new}
    assert _ids(db, "Liminal gateway") == {old, new}
    assert other not in _ids(db, "liminal gateway")


def test_alias_survives_the_or_fallback(db, home):
    old = _mk(db, "Old", "limenal handlers load through entry points")
    # No row holds both words, so the AND pass is empty and the OR pass runs.
    assert old in _ids(db, "liminal zzzunmatched")


def test_a_query_without_an_alias_is_untouched():
    groups = {"limenal": ("liminal", "limenal"), "liminal": ("liminal", "limenal")}
    assert _expand_aliases('"cabal" "broker"', groups) == '"cabal" "broker"'
    assert _expand_aliases('"cabal" OR "broker"', groups) == '"cabal" OR "broker"'


def test_expansion_uses_an_explicit_joiner():
    groups = {"liminal": ("liminal", "limenal")}
    assert _expand_aliases('"liminal" "gateway"', groups) == (
        '("liminal" OR "limenal") AND "gateway"'
    )
    assert _expand_aliases('"liminal" OR "gateway"', groups) == (
        '("liminal" OR "limenal") OR "gateway"'
    )


def test_configured_aliases_add_to_the_built_in(home):
    (home / "config.yaml").write_text(
        "architect:\n  pattern_targets: [techne]\n  search_aliases: {Raven: Archivist}\n"
    )
    groups = search_alias_groups()
    assert groups["archivist"] == ("archivist", "raven")
    assert groups["raven"] == ("archivist", "raven")
    assert groups["liminal"] == ("limenal", "liminal")


def test_configured_alias_reaches_search(db, home):
    (home / "config.yaml").write_text(
        "architect:\n  pattern_targets: [techne]\n  search_aliases: {raven: archivist}\n"
    )
    old = _mk(db, "Old", "raven keeps the team's shared knowledge")
    assert _ids(db, "archivist") == {old}


def test_search_aliases_round_trip_through_config(home):
    from techne.config import Config

    (home / "config.yaml").write_text(
        "architect:\n  pattern_targets: [techne]\n  search_aliases: {' Raven ': Archivist}\n"
    )
    cfg = Config.load()
    assert cfg.architect.search_aliases == {"raven": "archivist"}
    assert cfg.architect.target_aliases == {}
    cfg.save()
    assert Config.load().architect.search_aliases == {"raven": "archivist"}
