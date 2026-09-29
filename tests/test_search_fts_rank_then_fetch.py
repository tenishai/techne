"""techne#1092 (rank FTS matches before fetching rows) and techne#1093 (the OR pass
drops terms matching over half the table).

Both are latency changes that must not move a page. These tests pin the SQL shape,
the filter-before-LIMIT rule for knowledge, the fallbacks that keep the page exact,
and the df prune's scope (OR pass only, never empties the query).
"""

import pytest

from techne.core.database import connect, init_db
from techne.core.knowledge import store as store_knowledge
from techne.core.search import (
    _and_or_pools,
    _drop_clamped_or_terms,
    _fts_match_rows,
    fts_and_or_split,
)
from techne.core.state import add_observation, ensure_session

SESSION = "s-1092"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "rank_then_fetch.db"
    init_db(path).close()
    ensure_session(path, SESSION)
    return path


def _traced(db_path):
    """A connection that records every SQL statement it runs."""
    conn = connect(db_path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return conn, statements


def _search_sql(statements):
    """Our own statements: the trace also carries FTS5's internal shadow-table reads."""
    return [s for s in statements if "bm25(" in s or "json_each" in s]


def _single_statement_page(conn, fts, base, key, match, limit, filter_sql=""):
    """The pre-#1092 shape: join, sort, LIMIT in one statement. The reference."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            f"SELECT b.id, bm25({fts}) AS rank FROM {fts} f JOIN {base} b ON b.{key} = f.rowid "
            f"WHERE {fts} MATCH ?{filter_sql} ORDER BY rank LIMIT ?",
            (match, limit),
        )
    ]


# ---------------------------------------------------------------------------
# techne#1092: rank first, then fetch
# ---------------------------------------------------------------------------


def test_observations_rank_on_the_fts_table_alone_then_fetch_by_id(db_path):
    for i in range(6):
        add_observation(db_path, SESSION, "Bash", f"zorblat entry {i} " + "zorblat " * i)
    conn, statements = _traced(db_path)
    try:
        rows = _fts_match_rows(conn, '"zorblat"', "observations", 3)
        conn.set_trace_callback(None)
        reference = _single_statement_page(
            conn, "observations_fts", "observations", "id", '"zorblat"', 3
        )
    finally:
        conn.close()

    rank_sql, fetch_sql = _search_sql(statements)
    assert "JOIN" not in rank_sql and "content" not in rank_sql, (
        "the ranking step must sort (rowid, bm25) from the FTS table alone, not base rows"
    )
    assert "ORDER BY rank LIMIT" in rank_sql
    assert "json_each" in fetch_sql and "ORDER BY" not in fetch_sql
    assert [(r["id"], r["rank"]) for r in rows] == reference, (
        "same rows, same order, same bm25 values as the single-statement query"
    )


def test_knowledge_filters_apply_before_the_limit(db_path):
    """The three best-scoring rows are superseded, archived and uncertain. With
    candidate_limit=3, a filter applied AFTER the LIMIT would leave nothing; the
    page must be the three best rows that pass the filter, found without falling
    back to the single-statement query."""
    strong = [
        store_knowledge(db_path, f"quillfeather quillfeather quillfeather strong {i}")
        for i in range(3)
    ]
    weak = [
        store_knowledge(
            db_path, f"quillfeather weak note {i} with a longer body of unrelated words here"
        )
        for i in range(4)
    ]
    conn = connect(db_path)
    conn.execute("UPDATE knowledge SET superseded_by = ? WHERE id = ?", (weak[3], strong[0]))
    conn.execute("UPDATE knowledge SET status = 'archived' WHERE id = ?", (strong[1],))
    conn.execute("UPDATE knowledge SET status = 'uncertain' WHERE id = ?", (strong[2],))
    conn.commit()
    conn.close()

    conn, statements = _traced(db_path)
    try:
        rows = _fts_match_rows(conn, '"quillfeather"', "knowledge", 3, include_archived=False)
        conn.set_trace_callback(None)
        reference = _single_statement_page(
            conn,
            "knowledge_fts",
            "knowledge",
            "id",
            '"quillfeather"',
            3,
            " AND b.superseded_by IS NULL AND b.status != 'archived' AND b.status != 'uncertain'",
        )
    finally:
        conn.close()

    assert len(rows) == 3, "a filter applied after the LIMIT returns a short page"
    assert not {r["id"] for r in rows} & set(strong)
    assert [(r["id"], r["rank"]) for r in rows] == reference
    assert len(_search_sql(statements)) == 2, (
        "rank + fetch only: the filtered page must not need the single-statement fallback"
    )


def test_a_row_missing_at_fetch_time_falls_back_to_the_single_statement_query(db_path):
    """An FTS rowid with no base row (index drift, or a delete between the two
    statements) must not shorten the page: that table re-runs the joined query."""
    for i in range(4):
        add_observation(db_path, SESSION, "Bash", f"glimmerwick row {i}")
    conn = connect(db_path)
    # An FTS entry whose base row does not exist, scoring above every real row.
    conn.execute(
        "INSERT INTO observations_fts(rowid, content, tags, kind) VALUES (?, ?, '', '')",
        (999_999, "glimmerwick " * 20),
    )
    conn.commit()
    try:
        rows = _fts_match_rows(conn, '"glimmerwick"', "observations", 3)
        reference = _single_statement_page(
            conn, "observations_fts", "observations", "id", '"glimmerwick"', 3
        )
    finally:
        conn.close()
    assert len(rows) == 3
    assert [(r["id"], r["rank"]) for r in rows] == reference


# ---------------------------------------------------------------------------
# techne#1093: drop OR terms matching over half the table
# ---------------------------------------------------------------------------


@pytest.fixture
def df_db(db_path):
    """Ten knowledge rows: `crowdword` in 6 (>50%), `halfword` in exactly 5
    (=50%), `rareword` in 1."""
    for i in range(10):
        words = [f"filler{i}"]
        if i < 6:
            words.append("crowdword")
        if i < 5:
            words.append("halfword")
        if i == 9:
            words.append("rareword")
        store_knowledge(db_path, " ".join(words))
    return db_path


def test_prune_drops_a_term_over_half_and_keeps_one_at_exactly_half(df_db):
    conn = connect(df_db)
    try:
        pruned = _drop_clamped_or_terms(
            conn, "knowledge_fts", '"crowdword" OR "halfword" OR "rareword"'
        )
    finally:
        conn.close()
    assert pruned == '"halfword" OR "rareword"'


def test_prune_never_empties_the_query(df_db):
    conn = connect(df_db)
    try:
        # Both terms are over half: dropping them would leave nothing to MATCH.
        assert (
            _drop_clamped_or_terms(conn, "knowledge_fts", '"crowdword" OR "crowdword"')
            == '"crowdword" OR "crowdword"'
        )
    finally:
        conn.close()


def test_pruned_page_is_topped_up_by_the_full_query_when_short(df_db):
    """`rareword` matches one row; the rows matching only `crowdword` would have
    filled the rest of a 4-row page under the full OR query, so a short pruned
    result must re-run it."""
    conn = connect(df_db)
    try:
        full = _fts_match_rows(conn, '"crowdword" OR "rareword"', "knowledge", 4)
        pruned = _fts_match_rows(
            conn, '"crowdword" OR "rareword"', "knowledge", 4, drop_clamped_or_terms=True
        )
    finally:
        conn.close()
    assert len(full) == 4
    assert [r["id"] for r in pruned] == [r["id"] for r in full]


def test_a_boundary_tie_decided_by_a_clamped_term_re_runs_the_full_query(db_path):
    """Two rows tie on every kept term; only the second also holds the >50% term,
    which puts it ahead under the full query by a clamped ~1e-6. With one slot,
    the pruned query would break the tie by rowid and seat the wrong row, so the
    tie at the LIMIT boundary must send the table back to the full query."""
    first = store_knowledge(db_path, "rareword alpha zeta")
    second = store_knowledge(db_path, "rareword crowdword zeta")
    for i in range(5):
        store_knowledge(db_path, f"crowdword filler{i} padding")
    for i in range(3):
        store_knowledge(db_path, f"other{i} filler padding")
    conn = connect(db_path)
    try:
        full = _fts_match_rows(conn, '"crowdword" OR "rareword"', "knowledge", 1)
        pruned = _fts_match_rows(
            conn, '"crowdword" OR "rareword"', "knowledge", 1, drop_clamped_or_terms=True
        )
    finally:
        conn.close()
    assert [r["id"] for r in full] == [second], "fixture: the clamped term breaks the tie"
    assert first < second
    assert [(r["id"], r["rank"]) for r in pruned] == [(r["id"], r["rank"]) for r in full]


def test_the_and_pass_never_drops_a_term(df_db):
    """Only the OR pass prunes. `crowdword rareword` has no AND match, so the OR
    pass runs: it probes df and drops `crowdword`, while the AND MATCH before it
    carried both terms and ran before any probe."""
    conn, statements = _traced(df_db)
    try:
        and_pool, or_pool = _and_or_pools(conn, "crowdword rareword", "knowledge", 1)
    finally:
        conn.close()
    assert and_pool == []
    assert [r["content"] for r in or_pool] == ["filler9 rareword"]
    ours = [s for s in statements if "bm25(" in s or "count(*) FROM (SELECT 1" in s]
    assert 'MATCH \'"crowdword" "rareword"\'' in ours[0], "AND MATCH keeps every term"
    assert "count(*) FROM (SELECT 1" in ours[1], "the OR pass probes df after the AND pass"
    or_rank = [s for s in ours if "bm25(" in s][1]
    assert "MATCH '\"rareword\"'" in or_rank, "the OR pass dropped the >50% term"


def test_fts_and_or_split_sends_only_the_or_query_to_run_or():
    calls: list[tuple[str, str]] = []

    def run(match):
        calls.append(("run", match))
        return [{"id": 1}]

    def run_or(match):
        calls.append(("run_or", match))
        return [{"id": 2}]

    and_rows, or_rows = fts_and_or_split("alpha beta", run, want=3, run_or=run_or)
    assert calls == [("run", '"alpha" "beta"'), ("run_or", '"alpha" OR "beta"')]
    assert and_rows == [{"id": 1}] and or_rows == [{"id": 2}]
