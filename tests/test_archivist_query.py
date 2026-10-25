"""Guarded read-only query surface over the team Archivist (techne#554).

EVERY GUARD HERE IS PINNED BY A TEST THAT FAILS IF THE GUARD IS REMOVED, not by one
that merely passes while it is present. The distinction is the whole lesson of the
last session: four fixes shipped with no test guarding them and 3,100 green tests said
nothing. Where a test's failure mode is subtle, the assertion message says what the
removal of the guard would mean.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core.archivist_query import (
    ALLOWED_TABLES,
    MAX_LIMIT,
    OUTCOME_NO_ROWS,
    OUTCOME_ROWS,
    OUTCOME_SCHEMA,
    OUTCOME_STORE_ABSENT,
    QueryDenied,
    query,
    readable_schema,
)


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    return Config.load()


@pytest.fixture()
def team(config, tmp_path):
    """A non-standalone instance with a populated Archivist.

    Rows go in through `store()` on the archivist db directly — this suite is about
    the READ path, and routing through the outbox would make every test here depend
    on the sink lane's behaviour as well as its own.
    """
    from techne.core.archivist import archivist_db
    from techne.core.knowledge import store

    config.standalone = False
    config.archivist_db_path = str(tmp_path / "shared-archivist.db")
    db = archivist_db(config)
    store(db, "cabal uses a CheckpointStore protocol", type="fact", confidence="high",
          source="team", provenance={"written_by": "poe@dimartin"})
    store(db, "we chose SQLite over Postgres for domain stores", type="decision",
          confidence="high", source="team")
    store(db, "raw observation content that must not leak", type="fact", source="team")
    return config


def _observation_in(config, text="a raw thread summary nobody distilled"):
    """Put a row in the raw tier so 'observations are denied' tests are about a
    table that HAS something to leak. A denial over an empty table would pass for
    the wrong reason."""
    from techne.core.archivist import archivist_db
    from techne.core.state import add_observation, ensure_session

    db = archivist_db(config)
    ensure_session(db, "src-sess")
    add_observation(db, "src-sess", "thread_summary", text)
    return db


# ---- the three outcomes are distinguishable (#554 constraint 1) --------------


def test_no_rows_is_not_store_absent_and_neither_is_schema(team):
    """The bug this whole outcome vocabulary exists to prevent: a caller cannot tell
    'we know nothing about that' from 'no team store is even bound here'."""
    answered = query(team, "SELECT id FROM knowledge WHERE content = 'nothing matches this'")
    assert answered["outcome"]["code"] == OUTCOME_NO_ROWS
    assert answered["rows"] == []

    found = query(team, "SELECT id FROM knowledge LIMIT 1")
    assert found["outcome"]["code"] == OUTCOME_ROWS

    unasked = query(team, None)
    assert unasked["outcome"]["code"] == OUTCOME_SCHEMA

    codes = {answered["outcome"]["code"], found["outcome"]["code"], unasked["outcome"]["code"]}
    assert len(codes) == 3, f"outcomes collapsed into each other: {codes}"


def test_standalone_reports_store_absent_not_empty(config):
    """A standalone instance has no team memory BY DESIGN. Reporting that as an empty
    result set says 'the team knows nothing', which is false and unfalsifiable."""
    assert config.standalone is True
    result = query(config, "SELECT id FROM knowledge")
    assert result["outcome"]["code"] == OUTCOME_STORE_ABSENT
    assert result["rows"] == []
    assert "not" in result["outcome"]["summary"].lower()


def test_absent_store_is_not_created_by_querying_it(config, tmp_path):
    """Resolving the path must not init the db. If it did, the FIRST query against a
    misconfigured instance would create an empty store and every query after it would
    report no-rows instead of store-absent — the failure would erase its own cause."""
    config.standalone = False
    path = tmp_path / "never-created.db"
    config.archivist_db_path = str(path)

    result = query(config, "SELECT id FROM knowledge")

    assert result["outcome"]["code"] == OUTCOME_STORE_ABSENT
    assert not path.exists(), "querying a missing Archivist created one"


# ---- the allow-list: distilled only ------------------------------------------


def test_raw_observations_are_denied(team):
    _observation_in(team)
    with pytest.raises(QueryDenied) as e:
        query(team, "SELECT content FROM observations")
    assert "observations" in str(e.value)


def test_raw_observations_are_denied_through_fts_too(team):
    """observations_fts is a second door to the same rows. Denying the base table and
    forgetting the index is the classic half-closed allow-list."""
    _observation_in(team)
    with pytest.raises(QueryDenied):
        query(team, "SELECT rowid FROM observations_fts WHERE observations_fts MATCH 'summary'")


def test_denied_table_reached_through_a_subselect_is_still_denied(team):
    """The reason this file does not parse SQL: the outer statement names only an
    allow-listed table, and a name-level check passes it."""
    _observation_in(team)
    with pytest.raises(QueryDenied):
        query(team, "SELECT id FROM knowledge WHERE id IN (SELECT id FROM observations)")


def test_a_recursive_cte_cannot_launder_a_denied_table(team):
    """The transient-table exemption (dbname is None) is the one place the allow-list
    is deliberately bypassed. This proves the bypass cannot be used to reach real data
    — if it could, the exemption would be a hole rather than a nuance."""
    _observation_in(team)
    with pytest.raises(QueryDenied):
        query(
            team,
            "WITH RECURSIVE leak(c) AS (SELECT content FROM observations) SELECT * FROM leak",
        )


def test_sessions_are_denied(team):
    """Not in the allow-list, and session ids are agent identity. Named explicitly so
    that widening the list is a deliberate act with a failing test attached."""
    with pytest.raises(QueryDenied):
        query(team, "SELECT id FROM sessions")


def test_allow_list_is_distilled_only(team):
    """A tripwire on the CONSTANT. Anything added to ALLOWED_TABLES that is not a
    knowledge table fails here, so widening the readable surface cannot happen as a
    side effect of an unrelated change."""
    assert all(t.startswith("knowledge") for t in ALLOWED_TABLES), (
        f"a non-knowledge table entered the allow-list: "
        f"{sorted(t for t in ALLOWED_TABLES if not t.startswith('knowledge'))}"
    )


# ---- the guard stack ---------------------------------------------------------


def test_attach_is_denied(team, config, tmp_path):
    """THE ONE TO KEEP. ?mode=ro does NOT prevent ATTACH, so without the authorizer
    this 'team store' surface reads the operator's personal db out through it.

    ASSERTS QueryDenied SPECIFICALLY, not "some exception". The first version of this
    test accepted `(QueryDenied, sqlite3.DatabaseError)` and passed while ATTACH was
    escaping as a bare `DatabaseError: not authorized` traceback — the guard worked,
    the REPORTING of it did not, and a test that accepts either could not tell. Found
    by running the CLI, not by the suite.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store

    init_db(config.db_path)
    store(config.db_path, "PERSONAL memory that must never egress here", type="fact")

    with pytest.raises(QueryDenied) as e:
        query(team, f"ATTACH DATABASE '{config.db_path}' AS personal")
    assert "ATTACH" in str(e.value)


def test_writes_are_denied(team):
    """No write verbs on this path ever (#554 constraint 2). Both guards would have to
    fail for this to pass: the authorizer denies it, and the connection is ?mode=ro."""
    # MEASURED: `DROP TABLE knowledge` is refused as DELETE, not as DROP TABLE.
    # SQLite issues SQLITE_DELETE first (dropping a table deletes its rows) and
    # prepare aborts on the first denial, so SQLITE_DROP_TABLE is never reached. The
    # denial is still correct and still names a write verb — asserting the exact verb
    # per statement would be asserting SQLite's internal ordering, which is not our
    # invariant and would break on a version that reorders it.
    write_verbs = {"DELETE", "UPDATE", "INSERT", "DROP TABLE"}
    for sql in (
        "DELETE FROM knowledge",
        "UPDATE knowledge SET content = 'tampered'",
        "INSERT INTO knowledge (type, content) VALUES ('fact', 'injected')",
        "DROP TABLE knowledge",
    ):
        with pytest.raises(QueryDenied) as e:
            query(team, sql)
        assert any(v in str(e.value) for v in write_verbs), (
            f"denial for {sql!r} named no write verb at all: {e.value}"
        )

    surviving = query(team, "SELECT count(*) AS n FROM knowledge")
    assert surviving["rows"][0]["n"] == 3, "a write got through"


def test_schema_introspection_through_the_query_path_is_denied(team):
    """sqlite_master and the pragma_* table-valued functions enumerate the WHOLE db,
    including the tables the allow-list exists to hide."""
    for sql in (
        "SELECT name FROM sqlite_master",
        "SELECT * FROM pragma_table_info('observations')",
        "SELECT * FROM pragma_database_list",
    ):
        with pytest.raises((QueryDenied, sqlite3.DatabaseError)):
            query(team, sql)


def test_bare_pragma_statements_are_denied(team):
    """A SEPARATE test from the pragma_* functions above, because they take DIFFERENT
    branches: the table-valued functions are denied as reads of a `pragma_<name>`
    table, while these hit the SQLITE_PRAGMA branch. Mutation proved the distinction
    is not academic — allowing every pragma left all of the above green while
    `PRAGMA table_list` happily enumerated the tables the allow-list exists to hide.
    """
    for sql in ("PRAGMA table_list", "PRAGMA table_info(observations)", "PRAGMA database_list"):
        with pytest.raises(QueryDenied):
            query(team, sql)


def test_the_one_allowed_pragma_is_the_only_one(team):
    """_PRAGMA_ALLOWED is a set of exactly one for a measured reason (FTS5's vtab
    constructor). A tripwire so it cannot grow quietly."""
    from techne.core.archivist_query import _PRAGMA_ALLOWED

    assert _PRAGMA_ALLOWED == {"data_version"}, f"the pragma allow-list grew: {_PRAGMA_ALLOWED}"


def test_multiple_statements_are_refused(team):
    with pytest.raises(QueryDenied) as e:
        query(team, "SELECT 1; SELECT 2")
    assert "one" in str(e.value).lower()


def test_a_runaway_query_is_stopped(team):
    """READ-ONLY DOES NOT MEAN CHEAP. This runs unbounded on a connection that cannot
    write a byte; the progress handler is the only thing that stops it. Measured at
    ~0.64s against the real 25MB store."""
    with pytest.raises(QueryDenied) as e:
        query(
            team,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<200000000) "
            "SELECT count(*) FROM c",
        )
    assert "budget" in str(e.value).lower()


def test_the_store_is_byte_identical_after_a_query(team):
    """The sovereignty guarantee stated in federation's read-only opener, asserted
    rather than trusted: team MEMORY is never modified by reading it. Compares db
    CONTENT — the -wal/-shm sidecars are SQLite's read coordination and carry none."""
    from techne.core.archivist import _resolve_archivist_path

    path = _resolve_archivist_path(team)
    before = path.read_bytes()
    query(team, "SELECT id, content FROM knowledge")
    assert path.read_bytes() == before, "reading the Archivist modified it"


# ---- legitimate queries still work -------------------------------------------


def test_fts_match_works(team):
    """FTS5 issues PRAGMA data_version and reads its shadow tables during the vtab
    constructor. A blanket PRAGMA deny breaks every MATCH query with 'vtable
    constructor failed', which reads as bad SQL rather than as a broken guard — so
    this test is what tells the next person which of the two it is."""
    result = query(
        team,
        "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'CheckpointStore'",
    )
    assert result["outcome"]["code"] == OUTCOME_ROWS
    assert result["row_count"] >= 1


def test_a_plain_cte_and_a_join_work(team):
    result = query(
        team,
        "WITH recent AS (SELECT id, type, content FROM knowledge WHERE type = 'decision') "
        "SELECT r.id, r.content FROM recent r ORDER BY r.id",
    )
    assert result["outcome"]["code"] == OUTCOME_ROWS
    assert "we chose SQLite" in result["rows"][0]["content"]


def test_aggregates_work(team):
    result = query(team, "SELECT type, count(*) AS n FROM knowledge GROUP BY type")
    counts = {r["type"]: r["n"] for r in result["rows"]}
    assert counts == {"fact": 2, "decision": 1}


def test_provenance_columns_are_queryable(team):
    """#554 and #553 are halves of one thing: the query surface is what makes writer
    provenance answerable. If written_by were unreadable here, #553 would have added a
    column nobody can ask about."""
    result = query(team, "SELECT written_by FROM knowledge WHERE written_by = 'poe@dimartin'")
    assert result["row_count"] == 1


# ---- response shape ----------------------------------------------------------


def test_truncation_is_observed_not_inferred(team):
    """len(rows) == limit is ambiguous between 'exactly that many' and 'more,
    withheld'. A caller that guesses wrong concludes it has the whole answer."""
    exact = query(team, "SELECT id FROM knowledge ORDER BY id", limit=3)
    assert exact["row_count"] == 3
    assert exact["truncated"] is False, "3 of exactly 3 rows reported as truncated"

    cut = query(team, "SELECT id FROM knowledge ORDER BY id", limit=2)
    assert cut["row_count"] == 2
    assert cut["truncated"] is True, "rows were withheld and the caller was not told"


def test_limit_is_capped(team):
    """A caller cannot raise the ceiling by asking.

    NEEDS MORE ROWS THAN THE CAP TO MEAN ANYTHING. The first version of this test
    asserted `row_count <= MAX_LIMIT` against the 3-row fixture and passed whether or
    not the cap existed — the same do-nothing shape as the registry test in #701.
    Caught by mutation, not by review.
    """
    from techne.core.archivist import _resolve_archivist_path
    from techne.core.stores import plain_connect

    conn = plain_connect(_resolve_archivist_path(team))
    try:
        conn.executemany(
            "INSERT INTO knowledge (type, content, source) VALUES ('fact', ?, 'team')",
            [(f"bulk row {i}",) for i in range(MAX_LIMIT + 50)],
        )
        conn.commit()
    finally:
        conn.close()

    result = query(team, "SELECT id FROM knowledge", limit=MAX_LIMIT * 100)

    assert result["row_count"] == MAX_LIMIT, (
        f"asked for {MAX_LIMIT * 100} rows and got {result['row_count']} — "
        "the ceiling is raisable by asking"
    )
    assert result["truncated"] is True


def test_columns_are_reported(team):
    result = query(team, "SELECT id, type FROM knowledge LIMIT 1")
    assert result["columns"] == ["id", "type"]


def test_schema_lists_the_public_surface_only(team):
    """The FTS shadow tables are readable (they must be) but are NOT part of the
    advertised interface — describing them invites queries against an unstable
    internal encoding."""
    report = readable_schema(team)
    assert set(report["tables"]) == {"knowledge", "knowledge_deps", "knowledge_fts"}
    assert "written_by" in report["tables"]["knowledge"]
    assert not any(t.endswith(("_data", "_idx", "_docsize", "_config")) for t in report["tables"])


def test_schema_on_a_standalone_instance_says_so(config):
    report = readable_schema(config)
    assert report["outcome"]["code"] == OUTCOME_STORE_ABSENT
    assert report["tables"] == {}


# ---- the MCP seam ------------------------------------------------------------


def test_mcp_tool_returns_a_denial_as_a_result(team):
    """A denial has to reach the agent as the SURFACE DESCRIPTION, not as a bare
    'Error: not authorized'. 'You may read these three tables' is the useful half."""
    from techne.mcp_server import _techne_archivist_query

    _observation_in(team)
    result = _techne_archivist_query({"sql": "SELECT content FROM observations"}, team)
    assert result["outcome"]["code"] == "denied"
    assert "knowledge" in result["outcome"]["summary"]
    assert result["rows"] == []


def test_mcp_tool_with_no_sql_returns_the_schema(team):
    from techne.mcp_server import _techne_archivist_query

    result = _techne_archivist_query({}, team)
    assert result["outcome"]["code"] == OUTCOME_SCHEMA
    assert "knowledge" in result["tables"]


def test_mcp_tool_is_registered(team):
    from techne.mcp_server import TOOL_HANDLERS, TOOLS

    assert "techne_archivist_query" in TOOL_HANDLERS
    declared = {t["name"] for t in TOOLS}
    assert "techne_archivist_query" in declared, "handler exists but tools/list never offers it"


# ---- the CLI seam ------------------------------------------------------------


def test_cli_separates_a_denial_from_a_bad_query(team, monkeypatch):
    """Three exit codes, because they are three different things to a script AND to a
    person: 0 ran, 2 the surface refused you (learn it, re-ask), 1 your SQL is wrong.
    Collapsing 1 and 2 tells an operator to rewrite a query that was never the
    problem. Also pins that neither path prints a traceback — a stack dump describes
    OUR call stack when the mistake was in their query."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: team)
    runner = CliRunner()

    ok = runner.invoke(main, ["archivist", "query", "SELECT id FROM knowledge LIMIT 1"])
    assert ok.exit_code == 0, ok.output

    denied = runner.invoke(main, ["archivist", "query", "SELECT content FROM observations"])
    assert denied.exit_code == 2
    assert "denied:" in denied.output
    assert "Traceback" not in denied.output

    bad = runner.invoke(main, ["archivist", "query", "SELECT nope FROM knowledge"])
    assert bad.exit_code == 1
    assert "error:" in bad.output
    assert "Traceback" not in bad.output


def test_cli_always_prints_the_outcome_even_with_zero_rows(team, monkeypatch):
    """A silent empty table IS the 'no results reads as nothing exists' failure."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: team)
    result = CliRunner().invoke(
        main, ["archivist", "query", "SELECT id FROM knowledge WHERE content = 'nope'"]
    )
    assert result.exit_code == 0
    assert result.output.strip(), "zero rows printed nothing at all"
    assert "matched nothing" in result.output


# ---- what Prudence found in #707 ---------------------------------------------


def test_a_blob_result_does_not_break_the_response(team):
    """A BLOB IS REACHABLE ON THE PERMITTED SURFACE, so this is not defensive coding.
    `knowledge_fts_data.block` is a BLOB and that table is in ALLOWED_TABLES, so this
    is an ALLOWED query that ran successfully and then had its rows discarded: the MCP
    dispatcher serialises with `json.dumps` and no `default=`, `bytes` raised
    TypeError, and the caller got `Error: Object of type bytes is not JSON
    serializable` — a third failure category in a surface whose contract has two, and
    one that reads as a server bug. Nothing in the original 31 tests selected a BLOB.
    """
    result = query(team, "SELECT randomblob(8) AS b")

    assert result["outcome"]["code"] == OUTCOME_ROWS
    assert result["rows"][0]["b"] == "<blob, 8 bytes>"


def test_the_allow_listed_blob_table_is_actually_queryable(team):
    """The specific reachable path, not a synthetic one: a permitted table whose column
    is a BLOB must survive the whole round trip."""
    result = query(team, "SELECT * FROM knowledge_fts_data LIMIT 1")

    assert result["outcome"]["code"] == OUTCOME_ROWS
    assert any(str(v).startswith("<blob,") for v in result["rows"][0].values())


def test_the_mcp_tool_serialises_a_blob_row(team):
    """END TO END THROUGH THE REAL DISPATCHER, because that is where it broke — the
    seam is fine in isolation and the failure only appears once json.dumps runs."""
    import json

    from techne.mcp_server import handle_request

    response = handle_request(
        {"id": 1, "method": "tools/call",
         "params": {"name": "techne_archivist_query",
                    "arguments": {"sql": "SELECT randomblob(4) AS b"}}},
        team,
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["rows"][0]["b"] == "<blob, 4 bytes>"


def test_a_non_numeric_limit_is_a_denial_not_a_stack_trace(team):
    """The inputSchema declares `integer` but nothing in this server validates against
    it — the schema is documentation to the client, not a gate. int()'s own message
    describes OUR internals rather than the caller's mistake."""
    with pytest.raises(QueryDenied) as e:
        query(team, "SELECT id FROM knowledge", limit="abc")
    assert "whole number" in str(e.value)


def test_a_misconfigured_archivist_path_does_not_traceback_out_of_the_cli(
    team, monkeypatch, tmp_path
):
    """techne#346: archivist_db_path pointing at the PERSONAL db raises ValueError.
    The message is right; the packaging was not — click let it out, so the command
    printed nothing to stdout and dumped a traceback to stderr. CliRunner intercepts
    the exception, which is exactly why the suite could not see this and a terminal
    could. Asserted on BOTH subcommands.
    """
    from click.testing import CliRunner

    from techne.cli import main

    team.archivist_db_path = str(team.db_path)  # the misconfiguration itself
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: team)
    runner = CliRunner()

    for argv in (["archivist", "query", "SELECT 1"], ["archivist", "schema"]):
        result = runner.invoke(main, argv)
        assert result.exit_code == 1, f"{argv}: {result.output}"
        assert "error:" in result.output, f"{argv} printed nothing to stdout"
        assert "Traceback" not in result.output


def test_an_unmapped_authorizer_action_is_denied(team):
    """THE DEFAULT BRANCH, tested by BEHAVIOUR rather than by source shape.

    Prudence's point: the load-bearing property is that an action code this module has
    never heard of is refused, so a future SQLite cannot be waved through — and only
    two tests currently depend on it. This asserts it directly: hand the callback an
    action code that does not exist and require DENY.
    """
    from techne.core.archivist_query import _authorizer

    denied, ops = [], []
    callback = _authorizer(denied, ops)

    assert callback(9999, "whatever", None, "main", None) == sqlite3.SQLITE_DENY
    assert ops, "an unknown action was denied but recorded nothing to tell the caller"


def test_a_non_finite_float_does_not_leave_as_invalid_json(team):
    """`json.dumps` emits bare `Infinity` without raising, so NOTHING announces this —
    unlike the BLOB case, which at least errored. The token is not valid JSON (RFC
    8259) and a conformant client rejects the whole response; Python's own `json.loads`
    is lenient, which is precisely why a naive Python-side assertion would miss it.
    Parsed here with `parse_constant` set to raise, so the test sees what a strict
    client sees. (Prudence, #707 round 2.)
    """
    import json

    from techne.mcp_server import handle_request

    response = handle_request(
        {"id": 1, "method": "tools/call",
         "params": {"name": "techne_archivist_query", "arguments": {"sql": "SELECT 1e400 AS a"}}},
        team,
    )
    text = response["result"]["content"][0]["text"]

    def reject(token):
        raise ValueError(f"{token} is not valid JSON (RFC 8259)")

    payload = json.loads(text, parse_constant=reject)
    assert payload["rows"][0]["a"] == "inf"


# ---- techne#1256: one query cannot take hundreds of MB --------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT length(zeroblob(50000000))",  # the issue's reproduction, ~590 MB before
        "SELECT length(replace(hex(zeroblob(1500000)), '0', '00'))",
        "SELECT length(randomblob(50000000))",
    ],
)
def test_a_value_past_the_size_limit_is_refused_before_it_is_built(team, sql):
    with pytest.raises(QueryDenied, match="byte limit"):
        query(team, sql)


def test_a_value_past_the_size_limit_is_refused_when_returned(team):
    from techne.core.archivist_query import MAX_VALUE_BYTES

    big = MAX_VALUE_BYTES // 2 + 1  # hex() doubles it past the limit
    with pytest.raises(QueryDenied, match="byte limit"):
        query(team, f"SELECT hex(printf('%{big}s', 'x')) FROM knowledge LIMIT 1")


def test_ordinary_functions_still_work(team):
    result = query(team, "SELECT length(content), upper(substr(content, 1, 3)) FROM knowledge")
    assert result["outcome"]["code"] == OUTCOME_ROWS
