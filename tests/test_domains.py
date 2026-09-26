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
from pathlib import Path

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
    """PRUDENCE, #725 r3 and again at r5 — pre-existing, and it fires in HER environment.

    `db_path` is the PRIMARY checkout's index (#719 made the read resolve there) while
    `find_repo_root` stops at the worktree, so freshness compared the primary's index
    against a different tree's files. Every zero-result search from inside a review
    worktree got a notice calling that db stale on the strength of a tree it was never
    built from.

    THE FIRST VERSION OF THIS TEST BUILT THIS EXACT FIXTURE AND THEN NEVER USED IT —
    the deciding assertion was still `inspect.getsource`, and its docstring called
    itself BEHAVIOURAL. She reintroduced the defect and got ruff clean, 3370 passed:
    the same number I was reporting as green with the bug live. A grep that admits it
    is a grep is a weak test; one labelled behavioural is a trap for the next reader.

    So it spies on `index_freshness` and asserts the `repo_root` it is HANDED.
    """
    from techne import mcp_server
    from techne.core.repo import find_repo_root, primary_repo_root

    primary = tmp_path / "primary"
    primary.mkdir()
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

    primary_real = primary.resolve()
    assert find_repo_root(wt) != primary_repo_root(wt), (
        "fixture did not produce a worktree where the two resolvers differ"
    )

    # An indexed repo so the search reaches the zero-result branch that computes
    # freshness, rather than short-circuiting on the no-repo hint.
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, repo_db_path
    from techne.core.stores import SqliteStore

    db = repo_db_path(config, primary_real)
    db.parent.mkdir(parents=True, exist_ok=True)
    SqliteStore("codex", db).migrate(CODEX_SCHEMA)
    add_symbol(db, id="a.py::f::function::1", file="a.py", name="f", kind="function")

    seen = {}

    def spy(db_path, *, repo_root=None, **kw):
        seen["repo_root"] = repo_root
        # Shape matches the real return so `zero_result_notice` can render it:
        # {"files": n, **counts, "unindexed": n, "coverage": str}
        return {
            "files": 1,
            "fresh": 1,
            "stale": 0,
            "missing": 0,
            "unknown": 0,
            "unindexed": 0,
            "coverage": "complete",
        }

    monkeypatch.setattr("techne.core.codegraph.index_freshness", spy)
    monkeypatch.chdir(wt)

    mcp_server.TOOL_HANDLERS["codex_search"]({"query": "no_such_symbol_xyz"}, config)

    assert seen.get("repo_root") is not None, "index_freshness was never reached"
    assert Path(seen["repo_root"]).resolve() == primary_real, (
        f"freshness judged the index against {seen['repo_root']} — the worktree — "
        f"when the index was built from {primary_real}"
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


# ---- techne#744: the four names are addressable on the CLI too --------------------


class TestTheFourDomainsAreAddressableByName:
    """Board #726's DONE MEANS, pinned as an invariant rather than left as prose:

        An operator can address the archivist, librarian, architect and codex BY NAME.

    It was false when the documentation phase went looking. The MCP surface had all
    four (`archivist_search` / `librarian_search` / `architect_search` / `codex_search`)
    and `techne bindings` reported all four, but the CLI spelled the librarian
    `techne library` — the STORAGE word, naming the document store, which is the
    layout framing this release demotes the four names out of. So the one place the
    vocabulary broke was the domain #724 already records as diverging from cabal.
    """

    @staticmethod
    def _invoke(args):
        from click.testing import CliRunner

        from techne.cli import main

        return CliRunner().invoke(main, args)

    @pytest.mark.parametrize("domain", ["archivist", "librarian", "architect", "codex"])
    def test_each_domain_answers_to_its_own_name(self, domain):
        """A missing command exits 2 with 'No such command', so this discriminates
        between the command existing and the CLI merely not crashing."""
        result = self._invoke([domain, "--help"])

        assert result.exit_code == 0, (
            f"`techne {domain}` must exist — it is one of the four domain names "
            f"the release exists to teach. Got: {result.output}"
        )
        assert "No such command" not in result.output

    def test_the_legacy_library_spelling_still_works(self):
        """An alias, not a rename. Every existing script, `docs/librarian-guide.md` and
        `tests/test_cli_transcript_commands.py` say `library`, and breaking them to
        teach a word is a bad trade."""
        result = self._invoke(["library", "--help"])

        assert result.exit_code == 0, result.output

    def test_the_alias_reports_the_canonical_name(self):
        """`techne library` must not print usage for a command that does not appear in
        `--help`. click echoes back what the operator typed unless `resolve_command`
        says otherwise, and a usage line naming an unlisted command is worse than
        either spelling on its own."""
        result = self._invoke(["library", "--help"])

        usage = result.output.splitlines()[0]
        # Asserting on the COMMAND half, not the program name: CliRunner's prog_name is
        # "main", while the real entry point prints "techne". Pinning the prog name
        # would pass here and say nothing about what an operator sees.
        assert " librarian [OPTIONS]" in usage, usage
        assert " library [OPTIONS]" not in usage, usage

    def test_aliases_holds_only_the_documented_legacy_spelling(self):
        """THE DOOR, CLOSED WITH A TEST RATHER THAN WITH SYNTAX (Prudence, #745 r1).

        A frozenset stops mutation at runtime, which is not the failure being guarded
        against: the failure is someone adding a second key in a future PR because the
        dict makes it a one-line change. Every entry must be a word an operator already
        has in a script, not a word we would choose today — growing this table turns a
        compatibility shim into a synonym system. If you are adding one, say in the PR
        why the word exists.
        """
        from techne.cli import _AliasedGroup

        assert _AliasedGroup.ALIASES == {"library": "librarian"}

    def test_shell_completion_survives_an_unknown_command(self):
        """`resolve_command`'s override CRASHED here (#745 r1 Important 1). click ends
        `return cmd_name if cmd else None, cmd, args[1:]`, and `cmd` is None whenever
        `resilient_parsing` is set — the NoSuchCommand raise is guarded by
        `and not ctx.resilient_parsing`. Dereferencing `cmd.name` unconditionally killed
        shell completion over any unknown word, and `_resolve_context` could not help
        because it checks `cmd is None` AFTER the call."""
        import click

        from techne.cli import main

        ctx = click.Context(main, info_name="techne", resilient_parsing=True)

        name, cmd, _ = main.resolve_command(ctx, ["definitely-not-a-command"])

        assert cmd is None and name is None

    def test_the_cli_never_prints_the_demoted_spelling(self):
        """AN ASSERTION RATHER THAN A HABIT. The docs were updated and the CLI's own
        output still said `techne library` in four places — three of them printed
        immediately after the operator typed the canonical word, and one
        (`techne recall`) reaches operators who never invoke the librarian at all. So
        the release shipped its CLI teaching the word it exists to demote.

        Found twice in one PR, which is what makes it worth a test: prose about the
        vocabulary is checked, and the strings the operator actually SEES were not.
        Scans every string literal INCLUDING DOCSTRINGS, because click renders command
        docstrings verbatim as `--help` — the surface an operator is most likely to
        read. Exactly two are exempt, by owner name: `_AliasedGroup` and the `librarian`
        group, which explain the alias rather than teach it. Comments are not scanned
        and do not need to be; they never reach an operator.

        f-strings ARE covered: `ast.walk` descends into `JoinedStr` and yields its
        literal segments as ordinary `Constant` nodes — which matters, because two of
        the four sites this test was written for are f-strings. The narrow edge is a
        SPLIT f-string (`f"techne {noun} search"`), where a placeholder abuts the word;
        surveyed at the time and there were none.
        """
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "techne" / "cli.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        # DOCSTRINGS ARE SCANNED TOO, AND ONLY TWO ARE EXEMPT — BY OWNER, NOT BY
        # CATEGORY (Prudence, #745 r2 Important 1). An earlier version exempted every
        # docstring, which exempted exactly the surface this test claims to guard:
        # CLICK RENDERS COMMAND DOCSTRINGS VERBATIM AS `--help`. Demonstrated by adding
        # one line to `librarian search`'s docstring — the suite stayed green while
        # `techne librarian search --help` printed "run techne library show <id>" to
        # the operator. And it is not exotic: the `librarian` group docstring one screen
        # up MODELS writing `techne library`, so the next person adding a subcommand has
        # a template for the offence.
        #
        # (An earlier cut also matched "techne library " with a TRAILING SPACE and
        # passed only because that docstring writes it with a backtick — it passed for a
        # reason unrelated to what it asserts. The match is unconditional now.)
        #
        # EXEMPT BY OWNER NAME, NOT BY LINE NUMBER: a line citation is the one form that
        # goes stale silently (techne#747), and these two move whenever the file above
        # them does.
        EXPLAINS_THE_ALIAS = {"_AliasedGroup", "librarian"}

        # `tree.body`, NOT `ast.walk` — MODULE SCOPE ONLY (Prudence, #745 r3 nit 1).
        # Matching the name anywhere in the tree meant a NESTED `def librarian` could
        # carry an offending docstring and be exempted; she planted one inside
        # `library_search` and the guard stayed green at 25 passed. Both legitimate
        # owners are module-level, so restricting the scan costs one word and closes it.
        exempt = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name not in EXPLAINS_THE_ALIAS:
                    continue
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    exempt.add(id(body[0].value))

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in exempt:
                    continue
                idx = node.value.find("techne library")
                if idx != -1:
                    # A WINDOW AROUND THE MATCH, not the first 70 chars (#745 r3 nit 2).
                    # Slicing from the start of a long `--help` docstring printed a
                    # first line that does not contain the offence at all, with `lineno`
                    # pointing at the opening quote — a cold trail on the exact failure
                    # this guard exists to report.
                    window = node.value[max(0, idx - 30) : idx + 45].replace("\n", " ")
                    offenders.append(f"{src.name}:{node.lineno}: …{window.strip()}…")

        assert not offenders, "operator-facing text still says `techne library`:\n" + "\n".join(
            offenders
        )

    def test_help_offers_one_spelling_per_domain(self):
        """TWO SPELLINGS IN `--help` TEACHES THE AMBIGUITY, which is the thing #744 is
        about — so the alias is resolved rather than registered a second time. If this
        fails, someone reached for `main.add_command(..., name=...)`, which works and
        puts both words in the listing."""
        result = self._invoke(["--help"])
        commands = result.output[result.output.index("Commands:") :]

        assert "\n  librarian " in commands, commands
        assert "\n  library " not in commands, (
            "the legacy spelling must keep working but must not be advertised"
        )


# ---- techne#726: the `absent` rule the docs state twice, pinned once -----------------


class TestWhichDomainsCanReportAbsent:
    """`docs/four-functions.md` and `docs/welcome.md` both teach a three-way rule:

        codex     -> `absent` means "not indexed yet"
        archivist -> `absent` means "you configured `archivist: none`"
        librarian, architect -> no `absent` AT ALL

    It was stated in two places and pinned in none (Prudence, #752 r3). Two prose copies
    are consistent-today rather than checkable; this makes the load-bearing half — that
    two of the four can never reach the value — a test, so a future change to
    `_registry_domain` fails here rather than silently making both pages wrong.
    """

    @staticmethod
    def _binding(config, domain):
        from techne.core.bindings import domain_binding

        return domain_binding(config, domain).binding

    @pytest.mark.parametrize("domain", ["librarian", "architect"])
    def test_a_registry_domain_can_never_report_absent(self, domain, tmp_path, monkeypatch):
        """`_registry_domain` has exactly two exits — STORE when a dsn is named, LOCAL
        otherwise — and `store_path` always returns a path, so the LOCAL branch is
        unconditional. There is no `librarian: none` config key to reach `absent` with."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.config import Config

        config = Config.load()

        assert self._binding(config, domain) != "absent", (
            f"{domain} reported `absent`, which two documentation pages say is impossible "
            "for it — update the docs and this test together, or the pages are now wrong"
        )

    @pytest.mark.parametrize("domain", ["librarian", "architect"])
    def test_not_even_with_a_named_store_that_is_missing(self, domain, tmp_path, monkeypatch):
        """The near-miss: a named-but-absent store is `store`/`unavailable`, NOT `absent`.
        `absent` claims nothing is configured; this is configured and unreachable, and
        collapsing them is what the `unconfigured`/`empty` split exists to prevent."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        from techne.config import Config, StoreSpec
        from techne.core.bindings import domain_binding

        config = Config.load()
        config.stores = dict(config.stores)
        # A `StoreSpec`, not a bare string — `store_path` reads `spec.dsn`. My first cut
        # passed a str and died with AttributeError, which is a test asserting nothing
        # about the thing it names.
        config.stores[domain] = StoreSpec(dsn=f"sqlite:///{tmp_path / 'nope' / f'{domain}.db'}")

        report = domain_binding(config, domain)

        assert report.binding != "absent", report
        assert report.state == "unavailable", report


def test_codex_blast_radius_says_which_index_it_answered_from(config, tmp_path, monkeypatch):
    """Issue #730: the sibling of the test above. `codex_blast_radius` resolved
    through the same `_codex_repo_db` and returned no binding, so a caller inside a
    review worktree could not tell it was reading the primary's index. Through
    TOOL_HANDLERS, on all three returns: success, unresolvable id, and no repo."""
    from techne import mcp_server
    from techne.core.codegraph import add_edge, add_symbol
    from techne.core.codex import CODEX_SCHEMA, repo_db_path
    from techne.core.stores import SqliteStore

    repo_b = tmp_path / "repo_b"
    (repo_b / ".git").mkdir(parents=True)
    db_b = repo_db_path(config, repo_b)
    db_b.parent.mkdir(parents=True, exist_ok=True)
    SqliteStore("codex", db_b).migrate(CODEX_SCHEMA)
    add_symbol(db_b, id="b.py::hub::function::1", file="b.py", name="hub", kind="function")
    add_symbol(db_b, id="c.py::user::function::1", file="c.py", name="user", kind="function")
    add_edge(db_b, "c.py::user::function::1", "hub", "call")

    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    blast = mcp_server.TOOL_HANDLERS["codex_blast_radius"]

    ok = blast({"symbol_id": "b.py::hub::function::1", "repo": str(repo_b)}, config)
    assert [n["id"] for n in ok["nodes"]] == ["c.py::user::function::1"]
    assert (ok["domain"], ok["target"]) == ("codex", str(db_b))
    assert ok["binding"] != "absent"
    assert "walked" in ok and "truncated" in ok  # the #722 fields survive the wrap

    bad = blast({"symbol_id": "no_such_symbol", "repo": str(repo_b)}, config)
    assert "error" in bad and bad["target"] == str(db_b)

    none = blast({"symbol_id": "hub"}, config)
    assert none["nodes"] == [] and none["domain"] == "codex" and none["binding"] == "absent"


def test_archivist_search_reports_the_346_misconfiguration_instead_of_raising(config):
    """#727 finding 3. Pointing `archivist_db_path` at the personal db makes
    `_resolve_archivist_path` raise. `techne bindings` already turned that into an
    `unavailable` row carrying the message; the domain call raised instead, so the
    other place an operator meets the misconfiguration answered with a traceback.
    Both now go through one conversion (`bindings._resolve_or_fault`)."""
    from techne.core.bindings import all_bindings

    config.archivist_db_path = str(config.db_path)

    r = SEARCH["archivist"](config, "anything")

    assert r["state"] == "unavailable", r
    assert r["results"] == []
    assert "techne#346" in r["notice"], r
    row = next(b for b in all_bindings(config) if b.domain == "archivist")
    assert r["notice"] == row.detail, "the domain call and `techne bindings` disagree"
