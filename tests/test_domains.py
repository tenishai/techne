"""The four knowledge domains, addressable by name (techne#552, the connection seam).

THE POINT IS THE VOCABULARY. Richard: "I want a user that starts using techne to refer
to the archivist, librarian, codex, and architect... then when they want to start using
liminal and cabal, they can continue to refer to things in that manner, but the
infrastructure gets larger... their interface to it looks the same."

So these tests pin the CALL SHAPE, not the retrieval quality — the shape is the part
that has to survive a local store becoming a persona and then a service.
"""

from __future__ import annotations

import pytest

from techne.core.domains import DOMAIN_NAMES, SEARCH


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    init_db(cfg.db_path)
    return cfg


def test_all_four_domains_are_addressable_by_name(config):
    """The whole feature in one assertion: the four words the operator learns are the
    four words that work. Before this, the librarian had NO tool at all and the
    architect answered only to `pattern_search` — the mechanism, not the function."""
    assert set(SEARCH) == set(DOMAIN_NAMES) == {"archivist", "librarian", "architect", "codex"}


def test_every_domain_answers_in_one_shape(config):
    """FOUR DOMAINS, ONE ENVELOPE. Four shapes would put the burden of knowing which one
    you got back onto the caller, which is the burden the seam exists to remove."""
    for name in DOMAIN_NAMES:
        r = SEARCH[name](config, "anything", limit=5)
        assert set(r) >= {"domain", "binding", "state", "target", "count", "results"}, name
        assert r["domain"] == name
        assert isinstance(r["results"], list)
        assert r["count"] == len(r["results"])


def test_every_answer_carries_its_binding(config):
    """THE BINDING IS THE PART THAT GROWS. The same call says `local` today and would
    say `store` — or whatever cabal resolves it to — without the caller changing. An
    answer that did not say which would make the escalation invisible, and the operator
    would have to know out-of-band which infrastructure served them."""
    from techne.core.bindings import BOUND_ABSENT, BOUND_LOCAL, BOUND_STORE

    for name in DOMAIN_NAMES:
        r = SEARCH[name](config, "anything", limit=5)
        assert r["binding"] in (BOUND_ABSENT, BOUND_LOCAL, BOUND_STORE), (name, r["binding"])


def test_an_unreadable_domain_gives_a_reason_not_an_empty_list(config, tmp_path):
    """An empty result and an unreadable store are DIFFERENT FACTS. Collapsing them is
    what the ladder's `unconfigured`/`empty` split exists to prevent, and a domain call
    must not reintroduce it."""
    from techne.config import StoreSpec

    config.stores["architect"] = StoreSpec(adapter="sqlite", dsn=str(tmp_path / "gone.db"))
    r = SEARCH["architect"](config, "anything")

    assert r["results"] == []
    assert r.get("notice"), "an unreadable store returned an empty list with no reason"
    assert "gone.db" in r["notice"]


def test_the_archivist_asks_the_resolver_rather_than_re_deriving(config, tmp_path):
    """techne#552 spent a PR collapsing six private opinions about the archivist binding
    into one resolver. This must not become the eighth.

    BEHAVIOURAL, NOT A SPY. A first version monkeypatched the resolver and asserted it
    was called — which passed even with the call replaced by an inline derivation,
    because the ENVELOPE looks the binding up too and the spy fired from there. The
    assertion could not tell which code path had asked.

    So: put the two answers in CONFLICT. `archivist_db_path` is set (an inline
    derivation would read `store`) while the resolver is made to say `local`. Only code
    that actually asks the resolver reads this instance's own db.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.core.tiers import ARCHIVIST_LOCAL

    # The row exists ONLY in this instance's own db. The team store is real but empty.
    store(config.db_path, "the kestrel roosts at dusk", type="fact", tags="birds")
    team = tmp_path / "team.db"
    init_db(team)
    config.archivist_db_path = str(team)

    monkey = pytest.MonkeyPatch()
    monkey.setattr("techne.core.archivist.archivist_binding", lambda cfg, **kw: ARCHIVIST_LOCAL)
    try:
        r = SEARCH["archivist"](config, "kestrel")
    finally:
        monkey.undo()

    # ASSERT ON WHICH STORE WAS READ, not on the reported target. A previous version
    # asserted `r["target"]`, which comes from the ENVELOPE's own lookup — also
    # patched — so it read `local` no matter which store the search actually opened.
    assert r["count"] >= 1, (
        "the archivist re-derived its binding from `archivist_db_path` instead of "
        "asking the resolver, and searched the team store the resolver said not to use"
    )


def test_a_local_archivist_reads_this_instances_own_knowledge(config):
    """`local` means the call RESOLVES to knowledge this instance already has — not a
    copy, not a second read. techne#552 measured the archivist db as a 98.9% mirror and
    stopped reading it; addressing the archivist by name must not undo that."""
    from techne.core.knowledge import store

    store(config.db_path, "the kestrel roosts at dusk", type="fact", tags="birds")
    r = SEARCH["archivist"](config, "kestrel")

    assert r["binding"] == "local"
    assert r["target"] == str(config.db_path), "a local archivist opened something else"
    assert r["count"] >= 1


def test_recall_is_unchanged_and_remains_the_default(config):
    """THE GUARD ON THE NEGATIVE DECISION. Addressing a domain directly is for when you
    already know which function you want; escalating across four rungs is still the
    right answer to "what do we know about X". Adding these calls must not have quietly
    turned the ladder into something else."""
    from techne.core.tiers import LADDER

    assert [r.name for r in LADDER] == [
        "memory",
        "archivist:local",
        "archivist:remote",
        "librarian",
    ], "the ladder changed — see this test's docstring before updating it"


def test_the_mcp_surface_exposes_the_vocabulary(config):
    """The names have to be reachable from where an agent actually calls them. The
    librarian's absence here was asserted by a test until this change
    (`test_librarian_flagged_not_mcp_exposed`)."""
    from techne import mcp_server

    names = {t["name"] for t in mcp_server.TOOLS}
    for domain in DOMAIN_NAMES:
        assert f"{domain}_search" in names, f"{domain} is not addressable over MCP"
        assert f"{domain}_search" in mcp_server.TOOL_HANDLERS, f"{domain}_search has no handler"
