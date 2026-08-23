"""The connection seam — one question, four domains (techne#552 part 4).

THE DESIGN DECISION THESE TESTS PIN IS A NEGATIVE ONE, so it is stated first: patterns
and codex are NOT recall rungs. #552 part 4 literally says "bring patterns and codex
through the same call", and this ships the binding instead, because

  * the siblings answer "same feel" on the BINDING axis — liminal's `Target` makes
    transport a field and never changes the call shape; cabal already ships patterns,
    code, documents and archivist as PEER handlers — and
  * measured retrieval would make those rungs noise: pattern FTS falls back to OR and
    returns metaphorically-titled misses for problem-language queries, and 45% of codex
    hits across six realistic topics were TEST symbols — structural, since FTS ranks
    English test names above identifiers. (Index staleness was measured too, but a
    reindex moves it; the dilution does not.)

`test_the_recall_ladder_is_unchanged` is the guard on that decision.
"""

from __future__ import annotations

import pytest

from techne.core.bindings import (
    BOUND_ABSENT,
    BOUND_LOCAL,
    BOUND_STORE,
    BOUND_UNAVAILABLE,
    DOMAINS,
    STATE_READABLE,
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
        assert r.state in (STATE_READABLE, BOUND_UNAVAILABLE), r
        assert isinstance(r.ok, bool)
        assert r.detail.strip(), f"{r.domain} reported no operator-facing detail"
        assert set(r.as_dict()) == {
            "domain",
            "binding",
            "state",
            "target",
            "ok",
            "detail",
        }


def test_the_vocabulary_is_the_ladders_not_a_second_one(config):
    """A second vocabulary for the same three states is how two subsystems come to
    disagree about what "local" means."""
    from techne.core import tiers

    assert (BOUND_ABSENT, BOUND_LOCAL, BOUND_STORE, BOUND_UNAVAILABLE) == (
        tiers.ARCHIVIST_ABSENT,
        tiers.ARCHIVIST_LOCAL,
        tiers.ARCHIVIST_STORE,
        tiers.STATE_UNAVAILABLE,
    ), "the fourth state was minted instead of borrowed from the ladder"


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

    shared = tmp_path / "team.db"
    shared.touch()  # a store an operator NAMED and that EXISTS — the healthy shared case
    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(shared))
    r = domain_binding(config, "architect")
    assert r.binding == BOUND_STORE
    assert r.ok is True
    assert "team.db" in str(r.target)


def test_a_named_store_that_is_missing_is_unavailable_not_absent(config, tmp_path):
    """FOUR STATES, NOT THREE PLUS A BOOL. `reachable` collapsed "absent by design",
    "bound and readable" and "bound and unreadable" into one flag, and the CLI then
    reported `3 domain(s) not reachable` on a HEALTHY fresh install. The fault case now
    has its own name, borrowed from the ladder rather than minted."""
    from techne.config import StoreSpec

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "nope.db"))
    r = domain_binding(config, "architect")

    assert r.state == BOUND_UNAVAILABLE
    assert r.binding == BOUND_STORE, (
        "the fault ERASED what the operator bound the domain to — the axis fusion "
        "Prudence flagged, where a --json consumer cannot learn it was store-bound"
    )
    assert r.ok is False
    assert "nope.db" in r.detail


def test_a_default_store_that_does_not_exist_yet_is_healthy(config):
    """librarian and architect create their dbs LAZILY on first write, so a fresh
    install has neither file and nothing is wrong. Flagging that is what made the CLI
    send an operator chasing four non-problems. (Prudence, #717.)"""
    for domain in ("librarian", "architect"):
        r = domain_binding(config, domain)
        assert r.binding == BOUND_LOCAL, domain
        assert r.ok is True, f"{domain} default db reported as a fault: {r.detail}"


def test_the_archivist_store_branch_reports_the_team_store_path(config, tmp_path):
    """PRUDENCE, #717: `_archivist` had THREE branches and the tests exercised two.
    The untested one is the only branch that returns a PATH an operator has to verify,
    and it is the branch a team install takes."""
    team = tmp_path / "team-archivist.db"
    team.touch()
    config.archivist_db_path = str(team)

    r = domain_binding(config, "archivist")
    assert r.binding == BOUND_STORE
    assert r.ok is True
    assert r.target == team
    assert str(team) in r.detail


def test_a_named_archivist_store_that_is_missing_is_unavailable(config, tmp_path):
    config.archivist_db_path = str(tmp_path / "never-created.db")

    r = domain_binding(config, "archivist")
    assert r.state == BOUND_UNAVAILABLE
    assert r.binding == BOUND_STORE
    assert "never-created.db" in r.detail


def test_the_346_misconfiguration_surfaces_here_with_its_own_message(config):
    """THE #346 GUARD, REACHED THROUGH THIS SEAM. Pointing archivist_db_path at the
    personal db makes team intake pollute personal memory, so `_resolve_archivist_path`
    RAISES. `bindings` deliberately does not catch it locally — `all_bindings` turns it
    into an `unavailable` row carrying the verbatim message, because `techne bindings`
    is the one command an operator would run to diagnose exactly this, and a row saying
    only "unavailable" would send them looking for a missing file that isn't the
    problem."""
    config.archivist_db_path = str(config.db_path)

    row = next(r for r in all_bindings(config) if r.domain == "archivist")
    assert row.state == BOUND_UNAVAILABLE
    assert row.binding is None, (
        "a resolver that died before determining the binding must say so with None, "
        "not guess a value the operator would then act on"
    )
    assert row.ok is False
    assert "techne#346" in row.detail, f"the guard's own text was swallowed: {row.detail}"
    assert "must not be the personal techne db" in row.detail


def test_a_connection_bound_archivist_reports_the_connection_not_a_derived_path(config, tmp_path):
    """PRUDENCE, #717: `archivist_binding` answers `store` for TWO topologies —
    `archivist_db_path` and a `connections` entry — and `_archivist` delegated the
    binding while re-deriving the PATH, which only knew the first. A team install bound
    by connection fell through to the default `<data_dir>/archivist.db`, which it has no
    reason to have, so this command reported `unavailable` for a store that was present
    and working.

    Recall never saw it (`mcp_server` setdefaults, so the operator's entry wins), which
    is exactly what made it invisible: the divergence lived only in the surface built to
    diagnose bindings."""
    from techne.config import FederationConnectionSpec

    team = tmp_path / "team.db"
    team.touch()
    config.connections = {
        "archivist": FederationConnectionSpec(role="team", db_path=str(team), label="acme team")
    }

    r = domain_binding(config, "archivist")

    assert r.binding == BOUND_STORE
    assert r.ok is True, f"a present, working team store reported as a fault: {r.detail}"
    assert r.target == team, "the connection's path was re-derived instead of read"
    assert "acme team" in r.detail


def test_a_relative_connection_path_resolves_the_way_federation_resolves_it(
    config, tmp_path, monkeypatch
):
    """techne#332 resolves a relative db_path against `techne_home()` so a committed
    config.yaml stays portable. Checking `exists()` on the raw string would invent a
    NEW false `unavailable` for precisely that case."""
    from techne.config import FederationConnectionSpec

    (tmp_path / "rel-team.db").touch()  # tmp_path IS techne_home here (see fixture)
    config.connections = {"archivist": FederationConnectionSpec(role="team", db_path="rel-team.db")}

    r = domain_binding(config, "archivist")

    assert r.ok is True, f"a relative connection path was misread as missing: {r.detail}"
    assert r.target == tmp_path / "rel-team.db"


def test_a_connection_naming_a_missing_store_is_unavailable(config, tmp_path):
    from techne.config import FederationConnectionSpec

    config.connections = {
        "archivist": FederationConnectionSpec(
            role="team", db_path=str(tmp_path / "gone.db"), label="acme team"
        )
    }

    r = domain_binding(config, "archivist")
    assert r.state == BOUND_UNAVAILABLE
    assert r.binding == BOUND_STORE
    assert "gone.db" in r.detail


def test_a_connection_with_no_db_path_is_a_fault_not_a_green_row(config):
    """PRUDENCE, #717 r3 — THE SAME DIVERGENCE ONE BRANCH ALONG. `config.py` coerces a
    missing `db_path` to `""`, and `_resolve_conn_db_path("")` resolves to
    `techne_home()` — a DIRECTORY, which always exists. So `.exists()` reported
    `store | ok=True | team store ... at <a directory>` while federation, handed the
    same spec, died with `disk I/O error` and skipped past it.

    Two surfaces disagreeing about one config is precisely the defect this PR exists to
    remove; I reintroduced it in the branch that fixed the first instance."""
    from techne.config import FederationConnectionSpec

    config.connections = {
        "archivist": FederationConnectionSpec(role="team", db_path="", label="acme team")
    }

    r = domain_binding(config, "archivist")

    assert r.ok is False, f"a connection with no db_path reported healthy: {r.detail}"
    assert r.state == BOUND_UNAVAILABLE
    assert "db_path" in r.detail, "the detail did not name the operator's actual mistake"


def test_a_directory_where_a_store_should_be_is_not_a_store(config, tmp_path):
    """The general form of the bug above: `.exists()` is true for a directory. Fixed at
    the shared `_present` helper rather than in the one branch that surfaced it, so the
    named-store and default-store paths cannot drift back."""
    from techne.config import StoreSpec

    d = tmp_path / "architect.db"
    d.mkdir()  # a directory sitting exactly where the db should be
    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(d))

    r = domain_binding(config, "architect")
    assert r.ok is False, "a DIRECTORY was accepted as a store because .exists() is true"


def test_a_codex_fault_is_unavailable_not_absent(config, monkeypatch):
    """PRUDENCE, #717: `resolve_repo_db_for_query` RETURNS None for both healthy
    absences — no repo, and a repo with no index — so nothing that reaches its `except`
    is healthy. Reporting `absent` + ok=True told the operator "no index for the active
    repo", sending them to `techne codex index` for a fault that command does not fix."""
    monkeypatch.setattr(
        "techne.core.codex.resolve_repo_db_for_query",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("legacy migration failed")),
    )

    r = domain_binding(config, "codex")

    assert r.state == BOUND_UNAVAILABLE, "a real fault was reported as a healthy absence"
    assert r.binding is None, "the resolver never determined a binding; it must not invent one"
    assert r.ok is False
    assert "legacy migration failed" in r.detail, "the fault's message was discarded"
    assert "codex index" not in r.detail, (
        "still advising `techne codex index`, which does not fix a resolver fault"
    )


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
    assert bad.state == BOUND_UNAVAILABLE, (
        "a failed resolver reported `absent`, indistinguishable from a domain that is "
        "deliberately unconfigured — a --json consumer could not tell them apart"
    )
    assert bad.binding is None
    assert "RuntimeError" in bad.detail
    assert "resolver exploded" in bad.detail, "the exception MESSAGE was discarded"
    assert [r.domain for r in reports[1:]] == [n for n, _ in B.DOMAINS[1:]], (
        "the remaining domains were not reported after one resolver failed"
    )


def test_an_unknown_domain_raises_rather_than_guessing(config):
    with pytest.raises(KeyError):
        domain_binding(config, "no-such-domain")


def test_the_recall_ladder_is_unchanged(config):
    """THE GUARD ON THE NEGATIVE DECISION. Part 4 ships the binding, NOT two new rungs.
    The ladder stays four rungs because measured retrieval would make patterns and codex
    noise on every recall — pattern FTS returns metaphorical misses for problem-language
    queries, and 45% of codex topic hits were test symbols, which is structural rather
    than a staleness snapshot.
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


def test_the_cli_marks_the_broken_domain_and_only_it(config, monkeypatch, tmp_path):
    """PRUDENCE, #717: the first version of this test asserted `"!" in output` and
    `"not reachable" in output` — and PASSED with the StoreSpec line, the entire setup
    the test is named after, deleted. Both substrings appeared in output the broken
    store had nothing to do with. A guard that survives the removal of its own premise
    is measuring the wrong thing.

    So it now asserts on the ARCHITECT ROW specifically, and that the other three are
    NOT flagged — which is also the regression this exact bug produced (every domain
    marked on a healthy box)."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import StoreSpec

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "gone.db"))
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    result = CliRunner().invoke(main, ["bindings"])

    assert result.exit_code == 0, result.output
    # column 0 is the mark (" " or "!"), column 1 onward is the row
    rows = {
        ln[1:].split()[0]: ln
        for ln in result.output.splitlines()
        if ln[:1] in (" ", "!") and ln[1:].strip()
    }
    assert {"archivist", "librarian", "architect", "codex"} <= set(rows), rows

    assert rows["architect"][:1] == "!", f"the missing store was not flagged: {rows['architect']}"
    assert "gone.db" in rows["architect"], "the flag did not name the unreadable file"
    for healthy in ("archivist", "librarian", "codex"):
        assert rows[healthy][:1] == " ", f"{healthy} flagged on a healthy box: {rows[healthy]}"
    assert "1 domain(s) configured but unreadable" in result.output, result.output
