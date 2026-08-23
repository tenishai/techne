"""The connection seam — one question, four domains (techne#552 part 4).

THE DESIGN DECISION THESE TESTS PIN IS A NEGATIVE ONE, so it is stated first: patterns
and codex are NOT recall rungs. #552 part 4 literally says "bring patterns and codex
through the same call", and this ships the binding instead, because

  * the siblings answer "same feel" on the BINDING axis — liminal's `Target` makes
    transport a field and never changes the call shape; cabal already ships patterns,
    code, documents and archivist as PEER handlers — and
  * measured retrieval would make those rungs noise: pattern FTS falls back to OR and
    returns metaphorically-titled misses for problem-language queries, and this repo's
    codex index was 83/249 files stale with a same-day symbol returning zero hits and
    45% of topic hits being test symbols.

`test_the_recall_ladder_is_unchanged` is the guard on that decision.
"""

from __future__ import annotations

import pytest

from techne.core.bindings import (
    BOUND_ABSENT,
    BOUND_LOCAL,
    BOUND_STORE,
    DOMAINS,
    all_bindings,
    domain_binding,
)


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    init_db(cfg.db_path)
    return cfg


def test_every_domain_answers_the_same_question_in_the_same_shape(config):
    """THE 'SAME FEEL' PROPERTY, ASSERTED RATHER THAN DESCRIBED. Four domains whose
    mechanisms differ completely — a config decision, two registry lookups and a
    per-repo index — must return one shape, or the caller has to know which one it got,
    which is precisely what the seam exists to remove."""
    reports = all_bindings(config)

    assert [r.domain for r in reports] == [name for name, _ in DOMAINS]
    for r in reports:
        assert r.binding in (BOUND_ABSENT, BOUND_LOCAL, BOUND_STORE), r
        assert isinstance(r.reachable, bool)
        assert r.detail.strip(), f"{r.domain} reported no operator-facing detail"
        assert set(r.as_dict()) == {"domain", "binding", "target", "reachable", "detail"}


def test_the_vocabulary_is_the_ladders_not_a_second_one(config):
    """A second vocabulary for the same three states is how two subsystems come to
    disagree about what "local" means."""
    from techne.core import tiers

    assert (BOUND_ABSENT, BOUND_LOCAL, BOUND_STORE) == (
        tiers.ARCHIVIST_ABSENT,
        tiers.ARCHIVIST_LOCAL,
        tiers.ARCHIVIST_STORE,
    )


def test_the_archivist_delegates_rather_than_re_deriving(config, monkeypatch):
    """techne#552 spent a PR making the archivist binding single-sourced after SIX call
    sites each held a private opinion. This module must not become the seventh."""
    called = {}

    def spy(cfg, **kw):
        called["yes"] = True
        return BOUND_ABSENT

    monkeypatch.setattr("techne.core.archivist.archivist_binding", spy)
    r = domain_binding(config, "archivist")

    assert called.get("yes"), "bindings.py re-derived the archivist binding itself"
    assert r.binding == BOUND_ABSENT


def test_a_named_store_reads_as_store_not_local(config, tmp_path):
    """The registry domains distinguish an operator-named dsn from the default path —
    the same distinction the archivist draws, so all four agree on what `store` means."""
    from techne.config import StoreSpec

    assert domain_binding(config, "architect").binding == BOUND_LOCAL

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "team.db"))
    r = domain_binding(config, "architect")
    assert r.binding == BOUND_STORE
    assert "team.db" in str(r.target)


def test_configured_and_unreachable_are_different_facts(config, tmp_path):
    """A binding can be set and the file absent. Collapsing those is the failure the
    recall ladder avoids by keeping `unconfigured` distinct from `empty`."""
    from techne.config import StoreSpec

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "nope.db"))
    r = domain_binding(config, "architect")

    assert r.binding == BOUND_STORE, "a named store is configured even when missing"
    assert r.reachable is False


def test_codex_reports_absent_rather_than_answering_about_another_repo(config, monkeypatch):
    """`absent` is the common HEALTHY state for codex in a way it is not for the other
    three: an unindexed repo has no codex. Resolving to some other repo's index would
    answer confidently about the wrong code, which is worse than saying no."""
    monkeypatch.setattr("techne.core.codex.resolve_repo_db_for_query", lambda *a, **k: None)

    r = domain_binding(config, "codex")
    assert r.binding == BOUND_ABSENT
    assert r.target is None
    assert "codex index" in r.detail


def test_a_resolver_that_throws_still_yields_a_row(config, monkeypatch):
    """The report's value is that it is COMPLETE — an operator comparing four domains
    needs four rows, one of which may say it could not be read.

    TARGETS THE OUTER HANDLER IN `all_bindings`, NOT AN INNER ONE. A first version
    monkeypatched inside `_codex`, which has its own `except` — so it exercised the
    inner catch, `all_bindings`' handler was never reached, and narrowing that handler
    to a different exception type left the suite green. A guard has to fail when the
    thing it names is removed, which means raising where nothing else will catch it.
    """
    import techne.core.bindings as B

    def explodes(_config):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(B, "DOMAINS", (("archivist", explodes), *B.DOMAINS[1:]))

    reports = all_bindings(config)

    assert len(reports) == 4, "a throwing resolver removed a row from the report"
    bad = next(r for r in reports if r.domain == "archivist")
    assert bad.binding == BOUND_ABSENT
    assert "RuntimeError" in bad.detail
    assert [r.domain for r in reports[1:]] == [n for n, _ in B.DOMAINS[1:]], (
        "the remaining domains were not reported after one resolver failed"
    )


def test_the_codex_resolver_absorbs_its_own_failure(config, monkeypatch):
    """The inner catch, tested separately from the outer one, because they are
    different guarantees: this one keeps a codex failure from becoming an absent-row,
    the other keeps any failure from becoming a missing row."""
    monkeypatch.setattr(
        "techne.core.codex.resolve_repo_db_for_query",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("index exploded")),
    )

    r = domain_binding(config, "codex")
    assert r.binding == BOUND_ABSENT
    assert "codex index" in r.detail


def test_an_unknown_domain_raises_rather_than_guessing(config):
    with pytest.raises(KeyError):
        domain_binding(config, "no-such-domain")


def test_the_recall_ladder_is_unchanged(config):
    """THE GUARD ON THE NEGATIVE DECISION. Part 4 ships the binding, NOT two new rungs.
    The ladder stays four rungs because measured retrieval would make patterns and codex
    noise on every recall — pattern FTS returns metaphorical misses for problem-language
    queries, and codex was 83/249 files stale with 45% of topic hits being test symbols.
    Open-sourcing freezes the response shape, so adding a rung is the expensive
    direction and adding a noisy one is worse than adding none.

    If a later change makes them answerable (techne#550 for patterns, codex freshness),
    this test is the thing to update deliberately rather than the thing that quietly
    stopped being true.
    """
    from techne.core.tiers import LADDER

    assert [r.name for r in LADDER] == [
        "memory",
        "archivist:local",
        "archivist:remote",
        "librarian",
    ], "a rung was added or renamed — see this test's docstring before updating it"


def test_the_cli_reports_every_domain(config, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    result = CliRunner().invoke(main, ["bindings"])

    assert result.exit_code == 0, result.output
    for name, _ in DOMAINS:
        assert name in result.output


def test_the_cli_marks_unreachable_domains(config, monkeypatch, tmp_path):
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import StoreSpec

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "gone.db"))
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    result = CliRunner().invoke(main, ["bindings"])

    assert "!" in result.output
    assert "not reachable" in result.output
