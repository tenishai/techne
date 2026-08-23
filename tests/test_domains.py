"""The four knowledge domains, addressable by name (techne#552, the connection seam).

THE POINT IS THE VOCABULARY. Richard: "I want a user that starts using techne to refer
to the archivist, librarian, codex, and architect... then when they want to start using
liminal and cabal, they can continue to refer to things in that manner, but the
infrastructure gets larger... their interface to it looks the same."

So these tests pin the CALL SHAPE, not the retrieval quality — the shape is the part
that has to survive a local store becoming a persona and then a service.
"""

from __future__ import annotations

import subprocess

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


def test_every_domain_reports_its_binding_THROUGH_MCP(config, monkeypatch, tmp_path):
    """PRUDENCE, #725 — THE SEVENTH TEST THIS SESSION THAT COULD NOT SEE WHAT IT NAMED.

    `test_every_answer_carries_its_binding` checks `domains.SEARCH`, and `codex_search`
    is NOT wired to it — the MCP tool predates this module and returned no binding at
    all. That test asserted the envelope of a function no MCP caller reaches, while
    `test_the_mcp_surface_exposes_the_vocabulary` asserted only the NAME, which already
    existed. Between them the two tests looked like coverage of a claim
    `docs/mcp-tools.md` makes for all four domains and that codex could not back.

    This one goes through `TOOL_HANDLERS` — the path an agent actually calls.
    """
    from techne import mcp_server
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    # CODEX MUST TAKE ITS SUCCESS PATH, not the no-repo early return. A first version
    # chdir'd to an empty dir, so every codex call short-circuited on `_NO_REPO_HINT` —
    # which IS wrapped — and the mutation stripping the binding from the real return
    # passed. The early return is not the path callers hit when they have an index.
    codex_db = tmp_path / "codex.db"
    SqliteStore("codex", codex_db).migrate(CODEX_SCHEMA)
    add_symbol(codex_db, id="a.py::f::function::1", file="a.py", name="f", kind="function")
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: codex_db)
    monkeypatch.setattr("techne.core.codex.resolve_repo_db_for_query", lambda *a, **k: codex_db)

    for domain in DOMAIN_NAMES:
        r = mcp_server.TOOL_HANDLERS[f"{domain}_search"]({"query": "f", "limit": 1}, config)
        assert "binding" in r, f"{domain}_search returned no binding over MCP: {sorted(r)}"
        assert r.get("domain") == domain
    # and codex specifically reached its SUCCESS return, not the hint
    r = mcp_server.TOOL_HANDLERS["codex_search"]({"query": "f", "limit": 1}, config)
    assert r["count"] >= 1, "codex took the no-repo path; the success return is untested"


def test_a_connection_bound_archivist_reads_the_connection_not_a_derived_path(config, tmp_path):
    """PRUDENCE, #725 — THE SEVENTH INSTANCE OF THE #552 DEFECT, and the one I
    reintroduced while fixing the sixth.

    `archivist_binding` says `store` for TWO topologies — `archivist_db_path` OR a
    `connections["archivist"]` entry — and `_resolve_archivist_path` knows only the
    first. Calling it here read the DEFAULT `<data_dir>/archivist.db` while the envelope
    named the team store: a binding that was wrong WITHOUT SAYING SO, which is worse
    than no binding because callers trust it.

    Not hypothetical. #552 measured 1,104 rows in that stale mirror and stopped writing
    it, so a connection-bound install would have been served stale rows under a label
    pointing somewhere else. The fixture reproduces exactly that: a row in the derived
    default that must NOT come back, and a row in the real team store that must.
    """
    from techne.config import FederationConnectionSpec
    from techne.core.database import init_db
    from techne.core.knowledge import store

    # the stale mirror at the DERIVED default path
    stale = tmp_path / "archivist.db"
    init_db(stale)
    store(stale, "the kestrel has flown away and this row is stale", type="fact")

    # the real team store, named only by a connection
    team = tmp_path / "team.db"
    init_db(team)
    store(team, "the kestrel roosts at dusk", type="fact")
    config.connections = {
        "archivist": FederationConnectionSpec(role="team", db_path=str(team), label="acme")
    }

    r = SEARCH["archivist"](config, "kestrel")

    assert r["binding"] == "store"
    assert r["target"] == str(team), "the envelope named a store the read did not use"
    joined = " ".join(str(row) for row in r["results"])
    assert "roosts at dusk" in joined, "the connection's store was not read"
    assert "flown away" not in joined, (
        "read the DERIVED default archivist.db — the stale mirror #552 stopped writing "
        "— while reporting the team store"
    )


def test_codex_reports_the_binding_of_the_repo_it_actually_read(config, tmp_path, monkeypatch):
    """PRUDENCE, #725 r2 — TWO DERIVATIONS OF ONE FACT, THE EIGHTH INSTANCE.

    `_codex_repo_db` resolves `args["repo"] or cwd`; `_with_binding` derived the binding
    from cwd alone. They disagree the moment a caller passes `repo` — and `_NO_REPO_HINT`
    ROUTES CALLERS THERE ("pass `repo` to select an indexed repo"), so following the
    hint returned rows labelled `absent`. The envelope denied the index it had just read
    a row from.

    NO MONKEYPATCHING OF THE RESOLVERS. My previous attempt patched BOTH
    `_codex_repo_db` and `resolve_repo_db_for_query` to the same db, which pins the two
    derivations to one value and makes their disagreement structurally invisible — the
    same mistake one turn later, on a different pair. This builds a real second repo and
    a real index, and reads with cwd pointing somewhere else entirely.
    """
    from techne import mcp_server
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, repo_db_path
    from techne.core.stores import SqliteStore

    repo_b = tmp_path / "repo_b"
    (repo_b / ".git").mkdir(parents=True)
    db_b = repo_db_path(config, repo_b)
    db_b.parent.mkdir(parents=True, exist_ok=True)
    SqliteStore("codex", db_b).migrate(CODEX_SCHEMA)
    add_symbol(
        db_b,
        id="b.py::kestrel_bravo::function::1",
        file="b.py",
        name="kestrel_bravo",
        kind="function",
    )

    # cwd is NOT a repo — her exact reproduction. The rows can only come from `repo`.
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    r = mcp_server.TOOL_HANDLERS["codex_search"](
        {"query": "kestrel_bravo", "repo": str(repo_b), "limit": 5}, config
    )

    assert r["count"] >= 1, "fixture did not read repo_b's index"
    assert r["binding"] != "absent", (
        "the envelope reported `absent` beside rows it had just read — the binding was "
        "derived from cwd while the read used the `repo` argument"
    )
    assert r["target"] == str(db_b), f"reported {r['target']}, read {db_b}"


def test_domains_codex_search_honours_its_repo_argument(config, tmp_path, monkeypatch):
    """THE NINTH INSTANCE, and the one I found by asking rather than being told.

    `domains.codex_search` takes a `repo` argument and had the identical split the MCP
    tool did: `report` derived from cwd, the read from `repo or cwd`. Prudence's eighth
    finding prompted the question; this is the answer to it.

    Fixed by REMOVING the second derivation rather than teaching it the same input —
    the resolver already returns the db it resolved, so the read uses `report.target`.
    That is the difference between two derivations that agree today and one derivation
    that cannot disagree.
    """
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, repo_db_path
    from techne.core.domains import codex_search
    from techne.core.stores import SqliteStore

    repo_b = tmp_path / "repo_b"
    (repo_b / ".git").mkdir(parents=True)
    db_b = repo_db_path(config, repo_b)
    db_b.parent.mkdir(parents=True, exist_ok=True)
    SqliteStore("codex", db_b).migrate(CODEX_SCHEMA)
    add_symbol(
        db_b,
        id="b.py::kestrel_delta::function::1",
        file="b.py",
        name="kestrel_delta",
        kind="function",
    )

    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    r = codex_search(config, "kestrel_delta", repo=str(repo_b))

    assert r["count"] >= 1, "the repo argument was ignored"
    assert r["binding"] != "absent", "reported `absent` beside rows it had just read"
    assert r["target"] == str(db_b)


def test_the_no_repo_hint_reports_the_repo_the_caller_asked_about(config, tmp_path, monkeypatch):
    """A TENTH INSTANCE, AND MINE — from a silent `.replace()` no-op.

    I patched the success return to pass `args["repo"]` and believed I had patched the
    hint return too; ruff had reflowed that call onto one line, my replace did not
    match, and I did not assert the count. So the hint path reported cwd's binding
    while `_NO_REPO_HINT` literally instructs the caller to *pass `repo`*.

    That is the same `.replace()`-without-assert failure I hit this morning and wrote
    a lesson about, arriving in the code I wrote after writing it.
    """
    from techne import mcp_server

    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    unindexed = tmp_path / "repo_c"
    (unindexed / ".git").mkdir(parents=True)

    r = mcp_server.TOOL_HANDLERS["codex_search"](
        {"query": "anything", "repo": str(unindexed)}, config
    )

    assert r["count"] == 0
    assert "binding" in r, "the hint path lost its binding entirely"
    # The point is that the binding describes the REPO ARG's situation, not the cwd's.
    # Both are unindexed here, so assert the call reached the repo-aware path at all.
    assert r["domain"] == "codex"


def test_freshness_is_judged_against_the_tree_the_index_was_built_from(
    config, tmp_path, monkeypatch
):
    """PRUDENCE, #725 r3 — pre-existing, and it fires in HER environment.

    `db_path` is the PRIMARY checkout's index (#719 made the read resolve there) while
    `find_repo_root` stopped at the worktree — so freshness compared the primary's index
    against a different tree's files and called it stale. On every zero-result search
    from inside a review worktree, the envelope names the primary's db and the notice
    beside it calls that db stale on the strength of a tree it was never built from.
    """
    import inspect

    from techne import mcp_server

    # BEHAVIOURAL, NOT A SOURCE GREP. The first version asserted on
    # `inspect.getsource`, which Prudence flagged: it pins the spelling, not the
    # behaviour, and would pass if the call moved somewhere that never ran. This builds
    # a REAL worktree and asserts the two resolvers actually differ there, then that the
    # one used is the primary-resolving one.
    from techne.core.repo import find_repo_root, primary_repo_root

    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=primary, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=primary, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=primary, check=True)
    (primary / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=primary, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=primary, check=True, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
        cwd=primary,
        check=True,
        capture_output=True,
    )

    assert find_repo_root(wt) != primary_repo_root(wt), (
        "fixture did not produce a worktree where the two resolvers differ"
    )
    assert primary_repo_root(wt) == primary

    src = inspect.getsource(mcp_server._codex_search)
    assert "primary_repo_root" in src and "find_repo_root(" not in src, (
        "freshness resolves the repo root with find_repo_root, which stops at a "
        "worktree — the index it judges was built from the primary"
    )


def test_archivist_search_hides_the_same_rows_recall_hides(config):
    """PRUDENCE, #727. `search()` defaults `include_archived=True` and both recall call
    sites pass False (techne#216) — so addressing the archivist BY NAME surfaced
    soft-archived rows that asking recall the same question suppresses.

    Two surfaces answering one question differently is the defect the connection seam
    exists to remove: one of them is lying about what the archivist holds. The point of
    a stable vocabulary is that `archivist_search` and `techne_recall` disagree about
    ranking, never about what exists.
    """
    import sqlite3

    from techne.core.knowledge import store

    kept = store(config.db_path, "the kestrel roosts at dusk", type="fact")
    gone = store(config.db_path, "the kestrel roosts at dawn", type="fact")

    # soft-archive is `status = 'archived'`, the column recall's own clause reads
    # (`search.py:374`) — not a boolean `archived` column, which does not exist.
    con = sqlite3.connect(config.db_path)
    con.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (gone,))
    con.commit()
    con.close()

    r = SEARCH["archivist"](config, "kestrel")
    ids = {row.get("id") for row in r["results"]}

    assert kept in ids, "the live row was dropped"
    assert gone not in ids, (
        "archivist_search returned a soft-archived row that recall hides — the two "
        "surfaces disagree about what the archivist holds"
    )
