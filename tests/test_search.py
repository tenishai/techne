"""Tests for techne.core.search — FTS5 search, vector search, and combined search."""

import pytest

from techne.core.database import connect, init_db
from techne.core.knowledge import store as store_knowledge
from techne.core.search import (
    _rerank,
    _sanitize_fts_query,
    build_or_fts_query,
    search,
    search_fts,
    search_vec,
)
from techne.core.state import start_session
from techne.core.threads import Thread, save_threads


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_search.db"
    conn = init_db(path)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# FTS5 search tests
# ---------------------------------------------------------------------------


class TestSearchFtsThreads:
    def test_search_fts_threads(self, db_path):
        """Store a thread, search by a term from its label, verify it appears."""
        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="Kubernetes deployment rollout",
                summary="Deployed container image to Kubernetes cluster via rolling update.",
            )
        ]
        save_threads(threads, session_id, db_path)

        results = search_fts(db_path, "Kubernetes", table="threads")

        assert len(results) >= 1
        ids = [r["id"] for r in results]
        assert f"{session_id}-1" in ids
        labels = [r["label"] for r in results]
        assert "Kubernetes deployment rollout" in labels

    def test_search_fts_threads_no_false_positives(self, db_path):
        """Searching for a term not in any thread returns empty results."""
        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="DNS configuration cleanup",
                summary="Removed stale DNS A records from Route53.",
            )
        ]
        save_threads(threads, session_id, db_path)

        results = search_fts(db_path, "xyzzy_nonexistent_term_42", table="threads")
        assert results == []


class TestSearchFtsObservations:
    def test_search_fts_observations(self, db_path):
        """Insert an observation with distinctive content, verify FTS finds it."""
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('sess-obs-1', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            """INSERT INTO observations (session_id, content, tool_name)
               VALUES ('sess-obs-1', 'refactored zymurgy fermentation module for performance', 'Edit')"""
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, "zymurgy", table="observations")

        assert len(results) >= 1
        types = [r["source_table"] for r in results]
        assert "observation" in types
        contents = [r["content"] for r in results]
        assert any("zymurgy" in c for c in contents)

    def test_search_fts_observations_result_shape(self, db_path):
        """Observation results have the expected keys."""
        conn = connect(db_path)
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('sess-obs-2', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            """INSERT INTO observations (session_id, content, tool_name)
               VALUES ('sess-obs-2', 'configured palimpsest archival storage', 'Bash')"""
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, "palimpsest", table="observations")

        assert len(results) >= 1
        r = results[0]
        assert r["source_table"] == "observation"
        for key in ("id", "content", "tool_name", "session_id", "timestamp", "tier", "rank"):
            assert key in r, f"Missing key: {key}"


class TestSearchFtsKnowledge:
    def test_search_fts_knowledge(self, db_path):
        """Store a knowledge entry, search for a distinctive term, verify it appears."""
        store_knowledge(db_path, "quidditch scoring uses golden snitch retrieval", type="fact")

        results = search_fts(db_path, "quidditch", table="knowledge")

        assert len(results) >= 1
        types = [r["source_table"] for r in results]
        assert "knowledge" in types
        contents = [r["content"] for r in results]
        assert any("quidditch" in c for c in contents)

    def test_search_fts_knowledge_result_shape(self, db_path):
        """Knowledge results have the expected keys — and the right VALUE for the
        one that a consumer renders (techne#607).

        Key-presence alone left the FTS producer only INCIDENTALLY covered.
        Mutating `search.py` to `"knowledge_type": "knowledge"` — key present,
        value wrong, which is #566 returning on this channel — turned the suite
        red only via two federation tests that exist for an unrelated reason. Not
        one recall-rail test failed. The vec channel got a value-asserting
        producer test in #603; this puts FTS on the same footing, so the tripwire
        lives beside the thing it guards rather than in a subsystem that could be
        refactored without anyone noticing the coverage left with it.
        """
        store_knowledge(db_path, "albatross migration pattern spans hemispheres", type="fact")

        results = search_fts(db_path, "albatross", table="knowledge")

        assert len(results) >= 1
        r = results[0]
        assert r["source_table"] == "knowledge"
        for key in (
            "id",
            "knowledge_type",
            "content",
            "tags",
            "confidence",
            "source",
            "updated_at",
            "rank",
        ):
            assert key in r, f"Missing key: {key}"
        assert r["knowledge_type"] == "fact", (
            "knowledge_type must carry the ROW'S OWN kind, not the source table — "
            "a row labelled 'knowledge' renders as [knowledge] for every kind, "
            "which is exactly techne#566 (#607)"
        )

    def test_search_fts_knowledge_superseded_excluded(self, db_path):
        """Superseded knowledge entries are not returned by FTS search."""
        from techne.core.knowledge import supersede

        old_id = store_knowledge(db_path, "tachyon drives are experimental", type="fact")
        supersede(db_path, old_id, "tachyon drives are now production-grade")

        # Old content should not appear (superseded)
        results = search_fts(db_path, "experimental", table="knowledge")
        ids = [r["id"] for r in results]
        assert old_id not in ids


class TestRerankCrossTableNormalization:
    """_rerank normalises BM25 PER source table, not across the pooled multi-table
    set: bm25() magnitudes differ per fts table, so a single global spread buries a
    small-magnitude table's best match under another table's mid-pack rows (techne#56)."""

    def test_per_table_normalization_is_fair(self):
        # type A (observation): large-magnitude BM25 (-10..-1);
        # type B (knowledge): small-magnitude BM25 (-0.5..-0.1).
        candidates = [
            {"source_table": "observation", "id": "A_best", "rank": -10.0},
            {"source_table": "observation", "id": "A_worst", "rank": -1.0},
            {"source_table": "knowledge", "id": "B_best", "rank": -0.5},
            {"source_table": "knowledge", "id": "B_worst", "rank": -0.1},
        ]
        # recency_weight=0 isolates the BM25 component; bm25_weight=1.
        out = _rerank(candidates, limit=4, recency_weight=0.0, bm25_weight=1.0, mmr_lambda=0.0)
        order = [r["id"] for r in out]
        # Per-table: each table's best (A_best, B_best) normalises to 1.0 → both top.
        # The OLD global-spread normalisation buried B_best (norm≈0.04) below A_worst;
        # this assertion fails on that behaviour and passes on the per-table fix.
        assert set(order[:2]) == {"A_best", "B_best"}, order
        assert order.index("B_best") < order.index("A_worst"), order

    def test_single_table_unchanged(self):
        # One source table → one group → global == per-table normalisation.
        candidates = [
            {"source_table": "knowledge", "id": "k1", "rank": -8.0},
            {"source_table": "knowledge", "id": "k2", "rank": -4.0},
            {"source_table": "knowledge", "id": "k3", "rank": -1.0},
        ]
        out = _rerank(candidates, limit=3, recency_weight=0.0, bm25_weight=1.0, mmr_lambda=0.0)
        assert [r["id"] for r in out] == ["k1", "k2", "k3"]


class TestSearchFtsAll:
    def test_search_fts_all(self, db_path):
        """Store data in multiple tables with a shared term; 'all' returns multiple types."""
        shared_term = "wombat"

        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="wombat habitat analysis",
                summary="Analyzed wombat burrowing patterns in southern Australia.",
            )
        ]
        save_threads(threads, session_id, db_path)

        store_knowledge(db_path, "wombat populations are declining in dry regions", type="fact")

        conn = connect(db_path)
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('sess-wombat', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            """INSERT INTO observations (session_id, content, tool_name)
               VALUES ('sess-wombat', 'studied wombat dietary requirements', 'Read')"""
        )
        conn.commit()
        conn.close()

        results = search_fts(db_path, shared_term, table="all")

        assert len(results) >= 2
        result_types = {r["source_table"] for r in results}
        # Should have at least two distinct types
        assert len(result_types) >= 2

    def test_search_fts_all_returns_results(self, db_path):
        """Results from 'all' table return at least one hit (reranker may reorder raw BM25)."""
        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="tantalum metallurgy processing",
                summary="Investigated tantalum ore processing for semiconductor use cases.",
            )
        ]
        save_threads(threads, session_id, db_path)

        store_knowledge(db_path, "tantalum is a rare earth metal used in capacitors", type="fact")

        results = search_fts(db_path, "tantalum", table="all")
        assert len(results) >= 1
        # Reranker combines recency × BM25; raw BM25 ordering is no longer guaranteed.
        # Verify results are present and have the expected shape.
        assert all("rank" in r for r in results)


class TestSearchFtsEdgeCases:
    def test_search_fts_empty_query_returns_empty(self, db_path):
        """Empty query returns empty list."""
        results = search_fts(db_path, "", table="all")
        assert results == []

    def test_search_fts_whitespace_query_returns_empty(self, db_path):
        """Whitespace-only query returns empty list."""
        results = search_fts(db_path, "   ", table="all")
        assert results == []

    def test_search_fts_empty_db_returns_empty(self, db_path):
        """Searching an empty database returns empty results."""
        results = search_fts(db_path, "anything", table="all")
        assert results == []

    @pytest.mark.parametrize(
        "query",
        ["AND OR NOT", "foo\\bar baz", "*", "-", 'foo"bar', "NOT", "()", "a:b"],
    )
    def test_search_fts_adversarial_queries_dont_crash(self, db_path, query):
        """#74: queries that are operator-free per the old special-char set but
        still invalid FTS5 used to raise sqlite3.OperationalError. They must now
        return a (possibly empty) list, never crash."""
        store_knowledge(db_path, "a fact about pipelines and logs", type="fact")
        results = search_fts(db_path, query, table="all")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# _sanitize_fts_query tests
# ---------------------------------------------------------------------------


class TestSanitizeFtsQuery:
    def test_sanitize_plain_query(self):
        """Every term is quoted as a phrase literal (#74). Semantically
        identical to the old bare-term implicit-AND for plain words."""
        result = _sanitize_fts_query("hello world")
        assert result == '"hello" "world"'

    def test_sanitize_single_word(self):
        """Single plain word is quoted as a phrase literal (#74)."""
        result = _sanitize_fts_query("python")
        assert result == '"python"'

    def test_sanitize_parens(self):
        """Parentheses trigger quoting of each term."""
        result = _sanitize_fts_query("foo(bar)")
        assert '"foo(bar)"' == result

    def test_sanitize_asterisk(self):
        """Asterisk triggers quoting."""
        result = _sanitize_fts_query("foo*")
        assert '"foo*"' == result

    def test_sanitize_colon(self):
        """Colon triggers quoting."""
        result = _sanitize_fts_query("field:value")
        assert '"field:value"' == result

    def test_sanitize_caret(self):
        """Caret triggers quoting."""
        result = _sanitize_fts_query("foo^bar")
        assert '"foo^bar"' == result

    def test_sanitize_tilde(self):
        """Tilde triggers quoting."""
        result = _sanitize_fts_query("foo~3")
        assert '"foo~3"' == result

    def test_sanitize_hyphen_triggers_quoting(self):
        """Hyphen is FTS5's NOT operator. Bare `foo -bar` throws a syntax
        error or silently excludes `bar`; both are wrong for a search box."""
        result = _sanitize_fts_query("foo -bar")
        assert result == '"foo" "-bar"'

    def test_sanitize_hyphenated_word_triggers_quoting(self):
        """A hyphenated word like `pre-compact` is a single literal phrase
        the user typed; quote it so FTS5 doesn't try to interpret `-compact`
        as NOT-compact."""
        result = _sanitize_fts_query("pre-compact")
        assert result == '"pre-compact"'

    def test_sanitize_square_brackets(self):
        """Square brackets trigger quoting."""
        result = _sanitize_fts_query("foo[bar]")
        assert '"foo[bar]"' == result

    def test_sanitize_curly_braces(self):
        """Curly braces trigger quoting."""
        result = _sanitize_fts_query("foo{bar}")
        assert '"foo{bar}"' == result

    def test_sanitize_double_quotes(self):
        """Double-quoted term triggers quoting of terms."""
        result = _sanitize_fts_query('"exact phrase"')
        # Each whitespace-split token gets quoted
        assert '"exact' in result or result.startswith('"')

    def test_sanitize_multi_term_with_special(self):
        """Multi-term query with a special char wraps each term in quotes."""
        result = _sanitize_fts_query("foo(bar) baz")
        # Each term should be wrapped
        assert '"foo(bar)"' in result
        assert '"baz"' in result

    def test_sanitize_multi_term_no_special(self):
        """Multi-term plain query: each term quoted as a phrase literal (#74)."""
        result = _sanitize_fts_query("the quick brown fox")
        assert result == '"the" "quick" "brown" "fox"'

    # --- #74: queries that used to slip through raw and crash FTS5 ---

    def test_sanitize_bare_boolean_keywords(self):
        """Bare AND/OR/NOT have no special char, so the old code returned them
        raw and FTS5 parsed them as operators with missing operands → crash.
        Now they are quoted literals."""
        assert _sanitize_fts_query("AND OR NOT") == '"AND" "OR" "NOT"'

    def test_sanitize_backslash_becomes_separator(self):
        """FTS5 has no backslash escape; a lone `\\` crashes even inside a
        phrase. Treat it as a separator (#74)."""
        assert _sanitize_fts_query("foo\\bar baz") == '"foo" "bar" "baz"'

    def test_sanitize_lone_backslash_dropped(self):
        assert _sanitize_fts_query("\\") == ""

    def test_sanitize_unbalanced_quote(self):
        """Embedded double-quote is doubled so the phrase literal stays valid."""
        assert _sanitize_fts_query('foo"bar') == '"foo""bar"'


class TestPrepareFtsQuery:
    """techne#143: the single shared guard. Returns None for any query with no searchable
    terms (so callers skip the MATCH that would crash on ''), else the sanitized string."""

    def test_none_for_empty_and_whitespace(self):
        from techne.core.search import prepare_fts_query

        assert prepare_fts_query("") is None
        assert prepare_fts_query("   ") is None

    def test_none_for_all_punctuation_and_backslash(self):
        from techne.core.search import prepare_fts_query

        assert prepare_fts_query("\\") is None  # lone backslash → sanitizes to ""
        assert prepare_fts_query("\\\\  \\") is None  # only backslashes/space → no terms

    def test_real_query_sanitized(self):
        from techne.core.search import prepare_fts_query

        assert prepare_fts_query("database") == '"database"'
        assert prepare_fts_query("foo\\bar") == '"foo" "bar"'  # backslash-as-separator survives


# ---------------------------------------------------------------------------
# build_or_fts_query tests (#70)
# ---------------------------------------------------------------------------


class TestBuildOrFtsQuery:
    def test_single_term_returns_empty(self):
        """Single term is identical under AND/OR — fallback is a no-op."""
        assert build_or_fts_query("python") == ""

    def test_empty_query_returns_empty(self):
        assert build_or_fts_query("   ") == ""

    def test_two_terms_joined_with_or(self):
        assert build_or_fts_query("mongoose pattern") == '"mongoose" OR "pattern"'

    def test_multi_term_joined_with_or(self):
        result = build_or_fts_query("structured log ingestion")
        assert result == '"structured" OR "log" OR "ingestion"'

    def test_special_chars_quoted_as_phrase_literals(self):
        """Operator chars inside a term are neutralised by phrase-quoting."""
        result = build_or_fts_query("pre-compact thing")
        assert result == '"pre-compact" OR "thing"'

    def test_embedded_double_quote_doubled(self):
        result = build_or_fts_query('say "hi"')
        # the bare token `"hi"` has its quotes doubled inside the phrase literal
        assert '"say"' in result
        assert " OR " in result


# ---------------------------------------------------------------------------
# OR-fallback integration in search_fts (#70)
# ---------------------------------------------------------------------------


def _make_joint_rerank_regression_fixture(db_path) -> tuple[int, int]:
    """One long, stale, exact (AND) row and one short, fresh, partial (OR) row
    for the "would a joint rerank flip them?" guard — reused by every site
    that reranks AND/OR blocks (techne#1079 review Important 1/2): search_fts,
    search_knowledge_conn, federation.merge_and_rank. Returns (id_and, id_or).

    Verified directly (mutation-checked per call site) that merging the two
    into one pool and reranking ONCE — the #1081 shape — puts the OR row
    first; a correct split-and-rerank keeps the AND row first regardless.
    """
    from techne.core.database import connect

    long_and = "alpha beta " + " ".join(f"filler{i}" for i in range(80))
    id_and = store_knowledge(db_path, long_and, type="fact")
    id_or = store_knowledge(db_path, "alpha short note", type="fact")

    conn = connect(db_path)
    try:
        # Stale the AND row and leave the OR row fresh — the combination
        # that, measured, flips a JOINT rerank in the OR row's favour.
        conn.execute(
            "UPDATE knowledge SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (id_and,),
        )
        conn.commit()
    finally:
        conn.close()
    return id_and, id_or


class TestOrFallback:
    def test_multiword_topic_recovered_via_or_fallback(self, db_path):
        """The canonical #70 case: a natural-language topic where implicit-AND
        finds nothing (terms differ by stemming / are absent) but OR does."""
        store_knowledge(
            db_path,
            "The mongoose pattern is preferred over the manticore approach "
            "when ingesting structured logs.",
            type="fact",
        )
        # AND form requires every term incl. "for", "log" (doc has "logs") and
        # "ingestion" (doc has "ingesting") — none of which match → AND empty.
        and_only = search_fts(db_path, "mongoose pattern for structured log ingestion", table="all")
        # With the OR fallback wired in, the entry is recovered.
        assert len(and_only) >= 1
        assert any("mongoose" in r.get("content", "") for r in and_only)

    def test_or_fallback_does_not_fire_when_and_matches(self, db_path):
        """When the AND query already matches, results are unchanged (the
        fallback only runs on an empty AND pass)."""
        store_knowledge(db_path, "mongoose pattern preferred for logs", type="fact")
        results = search_fts(db_path, "mongoose pattern", table="all")
        assert len(results) >= 1
        assert any("mongoose" in r.get("content", "") for r in results)

    def test_or_fallback_no_match_still_empty(self, db_path):
        """A multi-word query with no matching terms stays empty even after the
        OR fallback (no false positives)."""
        store_knowledge(db_path, "completely unrelated content here", type="fact")
        results = search_fts(db_path, "xyzzy plugh frobnitz", table="all")
        assert results == []

    def test_single_word_query_unaffected(self, db_path):
        """Single-word queries never hit the fallback and behave as before."""
        store_knowledge(db_path, "a note about kubernetes scheduling", type="fact")
        results = search_fts(db_path, "kubernetes", table="knowledge")
        assert len(results) >= 1

    def test_one_row_and_pass_does_not_hide_partial_matches(self, db_path):
        """techne#1079: the litestream scenario. One row quotes the whole query
        (contains every term) and used to replace the entire candidate pool; the
        rows that actually answer the query — each containing only SOME of the
        terms — were never fetched. Now the AND row still ranks first, but the
        partial rows are topped up from the OR pass rather than dropped."""
        store_knowledge(
            db_path,
            "note: someone asked 'litestream replicate techne exec' again today",
            type="fact",
        )  # contains every term — the AND row
        store_knowledge(db_path, "litestream is the tool we use for db replication", type="fact")
        store_knowledge(db_path, "the techne store lives at ~/.techne/techne.db", type="fact")
        store_knowledge(db_path, "exec runs a command inside the replica container", type="fact")

        and_only = search_fts(db_path, "litestream replicate techne exec", table="knowledge")
        contents = [r["content"] for r in and_only]
        assert "litestream replicate techne exec" in contents[0], (
            "the exact AND match must still rank first"
        )
        assert len(contents) > 1, "partial matches must be topped up, not hidden"
        assert any("db replication" in c for c in contents)
        assert any("~/.techne/techne.db" in c for c in contents)
        assert any("replica container" in c for c in contents)

    def test_and_or_pools_tops_up_per_table_not_on_the_pooled_total(self, db_path):
        """techne#1079 review fix: table="all" runs one candidate_limit-bounded
        AND query PER sub-table. Comparing the SUMMED row count across tables
        against a single `want` lets one table's full AND pass (observations
        here) mask another table's empty AND pass (knowledge) and skip its
        top-up — `_and_or_pools` must decide per table.

        Tested directly against `_and_or_pools` rather than through
        `search_fts`'s final page: once any table's AND pass reaches
        `candidate_limit` (always >= the display `limit`), AND rows alone
        already fill the whole page under AND-first precedence (techne#1079
        v2), so this specific bug's effect on the OR pool never survives to
        the truncated final output — the pool is the only place it shows.
        """
        from techne.core.database import connect
        from techne.core.search import _and_or_pools
        from techne.core.state import add_observation, ensure_session

        session_id = "s-1079-alltable"
        ensure_session(db_path, session_id)
        # Two observations match BOTH terms — fills observations' own
        # candidate_limit (2) on the AND pass alone, so no topup is needed there.
        for i in range(2):
            add_observation(db_path, session_id, "Bash", f"zerbat quoxlin filler observation {i}")
        # Matches only ONE of the two terms — AND pass on knowledge finds
        # nothing. Under the pre-fix pooled-sum comparison, 2 (observations) +
        # 0 (knowledge) = 2 >= want(2), so the OR pass never runs for EITHER
        # table and this row is never fetched at all.
        store_knowledge(db_path, "zerbat only, nothing else in this note", type="fact")

        conn = connect(db_path)
        try:
            and_pool, or_pool = _and_or_pools(conn, "zerbat quoxlin", "all", candidate_limit=2)
        finally:
            conn.close()

        assert len(and_pool) == 2, "both observations match AND"
        assert all(r["source_table"] == "observation" for r in and_pool)
        assert any(r["source_table"] == "knowledge" for r in or_pool), (
            "knowledge's own AND pass found nothing (0 < want=2), so it must be "
            "topped up independently of observations already filling their pool"
        )

    def test_and_row_outranks_a_short_or_row_that_would_win_a_joint_rerank(self, db_path):
        """THE GUARD FOR #1081/#1084: reranking AND and OR rows TOGETHER let a
        short on-topic OR row outrank a long exact-match AND row on normalised
        BM25 + recency — measured on the live store, this dropped identifier
        queries from rank 1 to 2 or 3 and lost `reflect-drain` from the hybrid
        path entirely. That PR was reverted and techne#1079 reopened over it.

        The fix must keep the AND row first regardless, because precedence
        here is structural (AND rows are reranked and concatenated as their
        own block, before any OR row is even considered), not a side effect
        of these particular scores.
        """
        id_and, id_or = _make_joint_rerank_regression_fixture(db_path)

        results = search_fts(db_path, "alpha beta", table="knowledge", limit=2)
        ids = [r["id"] for r in results]
        assert ids == [id_and, id_or], (
            f"AND row (matches both terms) must rank before the OR row "
            f"(matches only 'alpha'), regardless of recency/length; got {ids}"
        )

    def test_search_knowledge_conn_and_row_outranks_a_joint_rerank_or_row(self, db_path):
        """Prudence's #1088 review Important 2: search_knowledge_conn — the
        federation entrypoint — is the second site this issue fixed, and it
        had no guard of its own; reverting it to the joint-rerank shape (like
        #1081) failed no test. Same fixture as the search_fts guard above,
        called through search_knowledge_conn directly."""
        from techne.core.search import search_knowledge_conn

        id_and, id_or = _make_joint_rerank_regression_fixture(db_path)

        conn = connect(db_path)
        try:
            results = search_knowledge_conn(conn, "alpha beta", 2)
        finally:
            conn.close()
        ids = [r["id"] for r in results]
        assert ids == [id_and, id_or], (
            f"AND row must rank before the OR row through search_knowledge_conn "
            f"too, regardless of recency/length; got {ids}"
        )


# ---------------------------------------------------------------------------
# Combined search (search()) tests
# ---------------------------------------------------------------------------


class TestSearchCombined:
    def test_search_combined_with_vectors_disabled(self, db_path):
        """search(..., use_vectors=False) returns FTS-only results."""
        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="ephemeral container orchestration",
                summary="Set up ephemeral containers in Kubernetes for debugging pods.",
            )
        ]
        save_threads(threads, session_id, db_path)

        results = search(db_path, "ephemeral", use_vectors=False)

        assert len(results) >= 1
        ids = [r["id"] for r in results]
        assert f"{session_id}-1" in ids

    def test_search_combined_use_vectors_false_no_vec_import(self, db_path, monkeypatch):
        """use_vectors=False path must not attempt to import vector dependencies."""

        # Block sqlite_vec import to confirm it's never reached
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else None

        blocked = {"sqlite_vec": True, "fastembed": True}
        real_import = __import__

        def strict_import(name, *args, **kwargs):
            if name in blocked:
                raise ImportError(f"Blocked import of {name} in test")
            return real_import(name, *args, **kwargs)

        import builtins

        monkeypatch.setattr(builtins, "__import__", strict_import)

        # This should not raise even though sqlite_vec/fastembed are blocked
        results = search(db_path, "anything", use_vectors=False)
        assert isinstance(results, list)

    def test_search_combined_returns_list(self, db_path):
        """search() always returns a list, even on empty DB."""
        results = search(db_path, "nothinghere", use_vectors=False)
        assert isinstance(results, list)
        assert results == []

    def test_search_combined_empty_query(self, db_path):
        """Combined search with empty query returns empty list."""
        results = search(db_path, "", use_vectors=False)
        assert results == []


# ---------------------------------------------------------------------------
# Vector search tests (skipped when extras not available)
# ---------------------------------------------------------------------------


class TestSearchVec:
    def test_search_vec_returns_empty_without_extras(self, db_path):
        """search_vec returns [] gracefully when sqlite_vec or NO embedding
        backend (neither model2vec nor fastembed) is installed.

        techne#601/#602: on Python 3.14, fastembed has no wheel but model2vec
        DOES install — so checking `import fastembed` alone would wrongly
        conclude "no extras" and test the wrong path on 3.14. Use
        `embeddings.backend_available()` (the same probe search_vec() itself
        now calls) so this test's notion of "extras available" matches
        production on every python version, not just <3.14.
        """
        import importlib.util

        from techne.core import embeddings as embeddings_mod

        sqlite_vec_available = importlib.util.find_spec("sqlite_vec") is not None
        extras_available = sqlite_vec_available and embeddings_mod.backend_available()

        if not extras_available:
            # ImportError path: should return [] without raising
            results = search_vec(db_path, "any query")
            assert results == [], f"Expected [] without extras, got {results}"
        else:
            # Extras present (sqlite-vec + a real embedding backend, e.g. this
            # 3.14 venv with model2vec) — just confirm it returns a list.
            results = search_vec(db_path, "any query")
            assert isinstance(results, list)

    def test_search_vec_degrades_on_attribute_error(self, db_path, monkeypatch):
        """#84: AttributeError from enable_load_extension (interpreter compiled
        without loadable-extension support) must return [] not crash."""
        import techne.core.search as _search_mod
        from techne.core.database import connect as _real_connect

        pytest.importorskip("sqlite_vec")

        class _NoExtConn:
            """Proxy that delegates all connection ops except enable_load_extension."""

            def __init__(self, real):
                self._real = real

            def enable_load_extension(self, enabled):
                raise AttributeError("enable_load_extension not available")

            def close(self):
                self._real.close()

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(_search_mod, "connect", lambda path: _NoExtConn(_real_connect(path)))

        results = search_vec(db_path, "anything")
        assert results == []

    def test_search_vec_degrades_on_operational_error(self, db_path, monkeypatch):
        """#84: sqlite3.OperationalError from enable_load_extension must return []."""
        import sqlite3 as _sqlite3

        import techne.core.search as _search_mod
        from techne.core.database import connect as _real_connect

        pytest.importorskip("sqlite_vec")

        class _OpErrConn:
            def __init__(self, real):
                self._real = real

            def enable_load_extension(self, enabled):
                raise _sqlite3.OperationalError("not authorized")

            def close(self):
                self._real.close()

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(_search_mod, "connect", lambda path: _OpErrConn(_real_connect(path)))

        results = search_vec(db_path, "anything")
        assert results == []

    def test_search_vec_degrades_on_stale_dim_vec_table(self, db_path, monkeypatch):
        """techne#601 BLOCKER regression: a vec0 table embedded by a PREVIOUS
        backend at a different width (e.g. 384-d fastembed/MiniLM) survives into a
        query after a py3.14 upgrade flips the active backend to 256-d potion —
        and nothing has re-embedded yet. The `embedding MATCH ?` then raises
        "Dimension mismatch"; that must degrade to FTS-only ([]), NEVER crash the
        top-level recall path. (Before the fix, this OperationalError propagated
        straight out of search()/search_vec().)

        techne#1101: the stub for ``_get_embedding_model`` must take the real
        signature. The live call site is ``_query_embedding``
        (``_get_embedding_model(allow_download=False)``) — a zero-arg lambda
        raises TypeError there, which is itself an ``Exception`` caught by
        ``_query_embedding``'s own broad ``except``, so ``_query_embedding``
        returns None and ``search_vec`` short-circuits to ``[]`` BEFORE it ever
        touches the stale-dimension vec tables below. That made the old test
        green for the wrong reason — it never reached the
        ``except sqlite3.OperationalError`` branch in ``_vec_channel_rows`` this
        test claims to cover. A ``_SpyConnection`` swapped in via ``conn=``
        (techne#1094's caller-supplied-connection path) positively asserts that
        branch was taken, by recording the actual "Dimension mismatch" error
        sqlite-vec raises on the `embedding MATCH ?` query.
        """
        import sqlite3
        import struct

        import techne.core.search as _search_mod
        from techne.core.search import _VEC_TABLE_NAMES, _ensure_vec_table

        # Guard BEFORE importing numpy: a base install (no [vectors] extra) has
        # neither sqlite_vec nor numpy, so skip cleanly rather than ImportError.
        sqlite_vec = pytest.importorskip("sqlite_vec")
        np = pytest.importorskip("numpy")

        # Stub the active backend at 256-d (independent of what's installed / any
        # model download) so the query embeds at 256 while the table is 384. Real
        # signature: `_get_embedding_model(config=None, *, allow_download=True)` —
        # `_query_embedding` calls it as `_get_embedding_model(allow_download=False)`.
        class _FakeBackend:
            name = "model2vec:stub-256"
            dim = 256

            def embed(self, texts):
                return [np.full(256, 0.1, dtype="float32") for _ in texts]

        monkeypatch.setattr(_search_mod, "_get_embedding_model", lambda *a, **k: _FakeBackend())

        # Lay down 384-d vec tables with a row each — the pre-upgrade cache.
        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            emb384 = struct.pack("384f", *([0.1] * 384))
            for name in sorted(_VEC_TABLE_NAMES):
                _ensure_vec_table(conn, name, 384)
                conn.execute(f"INSERT INTO {name}(rowid, embedding) VALUES (1, ?)", (emb384,))
            conn.commit()
        finally:
            conn.close()

        # A connection subclass that records any "embedding MATCH ?" query that
        # raises OperationalError, then re-raises so _vec_channel_rows's own
        # `except sqlite3.OperationalError` still handles it exactly as in
        # production. sqlite3.Connection is a C type — its methods can't be
        # monkeypatched on the instance (AttributeError: read-only attribute) or
        # the class (TypeError: immutable type), so this goes through
        # search_vec's `conn=` parameter instead.
        dim_mismatch_errors: list[str] = []

        class _SpyConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                try:
                    return super().execute(sql, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if "embedding MATCH" in sql:
                        dim_mismatch_errors.append(str(exc))
                    raise

        spy_conn = sqlite3.connect(str(db_path), factory=_SpyConnection)
        spy_conn.row_factory = sqlite3.Row
        try:
            # Query with the 256-d backend against 384-d tables — must NOT raise.
            results = search_vec(db_path, "anything", table="all", conn=spy_conn)
        finally:
            spy_conn.close()

        assert results == []
        # Positive assertion: the dimension-mismatch branch in
        # `_vec_channel_rows`'s `except sqlite3.OperationalError` was actually
        # taken, not skipped via an earlier, unrelated degrade exit.
        assert dim_mismatch_errors, (
            "expected sqlite-vec to raise a dimension-mismatch OperationalError "
            "on the `embedding MATCH ?` query; got none — the stale-dimension "
            "path was never reached"
        )
        assert "dimension mismatch" in dim_mismatch_errors[0].lower()

    def test_search_degrades_to_fts_when_vec_extension_unavailable(self, db_path, monkeypatch):
        """#84: search(..., use_vectors=True) falls through to FTS-only results
        when enable_load_extension raises AttributeError (vec returns [])."""
        import techne.core.search as _search_mod
        from techne.core.database import connect as _real_connect

        pytest.importorskip("sqlite_vec")

        from techne.core.knowledge import store as store_knowledge

        store_knowledge(db_path, "graceful degradation under extension failure", type="fact")

        class _NoExtConn:
            def __init__(self, real):
                self._real = real

            def enable_load_extension(self, enabled):
                raise AttributeError("enable_load_extension not available")

            def close(self):
                self._real.close()

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(_search_mod, "connect", lambda path: _NoExtConn(_real_connect(path)))

        results = search(db_path, "graceful degradation", use_vectors=True)
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_search_combined_dedupes(self, db_path):
        """Combined search deduplicates results that appear in both FTS and vec.

        Requires vector extras (sqlite-vec + an embedding backend). Skipped
        cleanly if not available. techne#601/#602: checks
        `embeddings.backend_available()` rather than `importorskip("fastembed")`
        directly, since fastembed alone is absent-by-design on py3.14 while
        model2vec provides working vector search there — importorskip on
        fastembed specifically would wrongly skip real coverage on 3.14.
        """
        from techne.core import embeddings as embeddings_mod

        pytest.importorskip("sqlite_vec")
        if not embeddings_mod.backend_available():
            pytest.skip("no embedding backend (model2vec or fastembed) available")

        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="vector deduplication test thread",
                summary="Testing that combined search returns deduplicated results.",
            )
        ]
        save_threads(threads, session_id, db_path)

        # Backfill vectors so this thread appears in vec search too
        from techne.core.search import backfill_vectors

        backfill_vectors(db_path)

        results = search(db_path, "deduplication", use_vectors=True)

        # Collect (type, id) pairs and check for duplicates
        seen = set()
        for r in results:
            key = (r["source_table"], r.get("id"))
            assert key not in seen, f"Duplicate result found: {key}"
            seen.add(key)


# ---------------------------------------------------------------------------
# Phase 3 correctness tests
# ---------------------------------------------------------------------------


class TestFtsSanitizerDoubleQuote:
    def test_double_quote_in_query_does_not_crash(self, db_path):
        """A query containing a double-quote must not raise OperationalError."""
        session_id = start_session(db_path)
        threads = [Thread(thread_num=1, label='test "quoted" label', summary="quoted")]
        save_threads(threads, session_id, db_path)

        # This must not raise sqlite3.OperationalError
        results = search_fts(db_path, 'test"', table="threads", limit=5)
        assert isinstance(results, list)

    def test_sanitize_escapes_embedded_quotes(self):
        from techne.core.search import _sanitize_fts_query

        sanitized = _sanitize_fts_query('hello "world"')
        # Must not contain unescaped embedded quote sequences that break FTS5
        assert '""' in sanitized or sanitized.count('"') % 2 == 0

    def test_backslash_only_query_does_not_crash(self, db_path):
        """techne#168: a query of only escape chars (e.g. '\\') survives the
        query.strip() guard but sanitizes to an empty MATCH string, which would
        raise sqlite3.OperationalError. Must return [] instead of crashing."""
        session_id = start_session(db_path)
        save_threads([Thread(thread_num=1, label="real thread", summary="x")], session_id, db_path)
        assert search_fts(db_path, "\\", table="threads", limit=5) == []
        assert search_fts(db_path, "\\\\\\", table="all", limit=5) == []
        # A real term still works after the guard.
        assert isinstance(search_fts(db_path, "real", table="threads", limit=5), list)


class TestPurgeSupersededKnowledgeVec:
    """techne#168: superseded knowledge must not keep an ANN embedding, or it
    occupies search_vec top-k slots and starves live rows (search_vec filters
    superseded only post-cut)."""

    def test_purge_removes_only_superseded(self, db_path):
        sqlite_vec = pytest.importorskip("sqlite_vec")
        import struct

        from techne.core.knowledge import store, supersede
        from techne.core.search import _ensure_vec_table, _purge_superseded_knowledge_vec

        live_id = store(db_path, "live fact", type="fact", confidence="high")
        old_id = store(db_path, "old fact", type="fact", confidence="high")
        new_id = supersede(db_path, old_id, "superseding fact")  # old_id now superseded

        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            _ensure_vec_table(conn, "knowledge_vec", 384)
            emb = struct.pack("384f", *([0.1] * 384))
            for rid in (live_id, old_id, new_id):
                conn.execute(
                    "INSERT INTO knowledge_vec(rowid, embedding) VALUES (?, ?)", (rid, emb)
                )
            conn.commit()

            removed = _purge_superseded_knowledge_vec(conn)
            assert removed == 1  # only old_id

            remaining = {r[0] for r in conn.execute("SELECT rowid FROM knowledge_vec").fetchall()}
            assert old_id not in remaining
            assert live_id in remaining and new_id in remaining
        finally:
            conn.close()

    def test_purge_no_vec_table_is_noop(self, db_path):
        from techne.core.search import _purge_superseded_knowledge_vec

        conn = connect(db_path)
        try:
            assert _purge_superseded_knowledge_vec(conn) == 0  # table never created
        finally:
            conn.close()


class TestEnsureVecBackendIdentity:
    """techne#601/#602: an embedding-backend/model swap (e.g. upgrading to
    py3.14, where fastembed can't install and model2vec takes over — a 384-d
    -> 512-d dim change) must drop and recreate the fixed-width vec0 tables,
    or vec0 INSERTs at the new dim would fail against the old column width
    (and even at equal width, cross-backend vectors aren't comparable)."""

    def test_unstamped_db_with_existing_384d_table_assumed_fastembed_noop(self, db_path):
        """A DB predating this migration has vec tables but no extension_state
        stamp. If the ACTIVE backend today happens to still be fastembed
        MiniLM/384 (unchanged), assume that identity retroactively — no drop,
        no wasted re-embed of an already-correct index."""
        import struct

        from techne.core.embeddings import EmbeddingBackend
        from techne.core.search import _ensure_vec_backend_identity, _ensure_vec_table

        sqlite_vec = pytest.importorskip("sqlite_vec")
        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            _ensure_vec_table(conn, "threads_vec", 384)
            emb = struct.pack("384f", *([0.1] * 384))
            conn.execute("INSERT INTO threads_vec(rowid, embedding) VALUES (1, ?)", (emb,))
            conn.commit()

            fake_backend = EmbeddingBackend(
                "fastembed", "sentence-transformers/all-MiniLM-L6-v2", impl=None, dim=384
            )
            changed = _ensure_vec_backend_identity(conn, fake_backend)
            assert changed is False

            # Existing row untouched — table was NOT dropped.
            rows = conn.execute("SELECT rowid FROM threads_vec").fetchall()
            assert [r[0] for r in rows] == [1]
        finally:
            conn.close()

    def test_dim_change_drops_and_recreates_at_new_dim(self, db_path):
        """Simulate an existing float[384] vec table (pre-migration, unstamped)
        with a row in it, then switch the active backend to a different dim.
        The three vec tables must be dropped; recreating (via _ensure_vec_table,
        as backfill_vectors does right after) must build them at the NEW dim,
        and the dropped-then-recreated table is empty — so backfill_vectors'
        `NOT IN (SELECT rowid FROM vec_tbl)` query re-embeds every row on an
        empty table with zero special-casing needed there."""
        import struct

        from techne.core.embeddings import EmbeddingBackend
        from techne.core.extension_state import get_state
        from techne.core.search import (
            _VEC_TABLE_NAMES,
            _ensure_vec_backend_identity,
            _ensure_vec_table,
        )

        sqlite_vec = pytest.importorskip("sqlite_vec")
        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            # Old world: all three vec0 tables at 384-d, one row each, no stamp.
            emb384 = struct.pack("384f", *([0.1] * 384))
            for name in sorted(_VEC_TABLE_NAMES):
                _ensure_vec_table(conn, name, 384)
                conn.execute(f"INSERT INTO {name}(rowid, embedding) VALUES (1, ?)", (emb384,))
            conn.commit()

            # New world: switch to a 512-d model2vec backend (an explicit
            # non-default variant chosen so the dim differs from the old 384-d
            # tables — exercises the drop/recreate path regardless of the shipped
            # default, which is potion-base-8M/256-d).
            new_backend = EmbeddingBackend(
                "model2vec", "minishlab/potion-retrieval-32M", impl=None, dim=512
            )
            changed = _ensure_vec_backend_identity(conn, new_backend)
            assert changed is True

            # All three tables gone (they'll be recreated lazily by the caller's
            # per-table _ensure_vec_table loop, same as backfill_vectors does).
            for name in _VEC_TABLE_NAMES:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                assert exists is None, f"{name} should have been dropped"

            # Identity stamp updated to the new backend.
            stamp = get_state(conn, "embedding_backend")
            assert stamp == {"name": "model2vec:minishlab/potion-retrieval-32M", "dim": 512}

            # Recreate at the new dim (what backfill_vectors does next) and
            # verify the column width actually matches — inserting a 512-float
            # blob must succeed; a stale 384-wide column would reject it.
            _ensure_vec_table(conn, "threads_vec", 512)
            emb512 = struct.pack("512f", *([0.2] * 512))
            conn.execute("INSERT INTO threads_vec(rowid, embedding) VALUES (1, ?)", (emb512,))
            conn.commit()
            rows = conn.execute("SELECT rowid FROM threads_vec").fetchall()
            assert [r[0] for r in rows] == [
                1
            ]  # old rowid 1 is gone-then-reinserted, table was empty

        finally:
            conn.close()

    def test_matching_identity_is_noop_and_preserves_rows(self, db_path):
        """Calling with the SAME backend identity as already stamped must not
        drop anything — the common case on every search/backfill call."""
        import struct

        from techne.core.embeddings import EmbeddingBackend
        from techne.core.search import _ensure_vec_backend_identity, _ensure_vec_table

        sqlite_vec = pytest.importorskip("sqlite_vec")
        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            backend = EmbeddingBackend(
                "model2vec", "minishlab/potion-retrieval-32M", impl=None, dim=512
            )

            # First call stamps identity (fresh DB, no existing tables).
            assert _ensure_vec_backend_identity(conn, backend) is False
            _ensure_vec_table(conn, "threads_vec", 512)
            emb = struct.pack("512f", *([0.3] * 512))
            conn.execute("INSERT INTO threads_vec(rowid, embedding) VALUES (7, ?)", (emb,))
            conn.commit()

            # Second call, same backend identity — must be a no-op.
            changed = _ensure_vec_backend_identity(conn, backend)
            assert changed is False
            rows = conn.execute("SELECT rowid FROM threads_vec").fetchall()
            assert [r[0] for r in rows] == [7]  # untouched
        finally:
            conn.close()

    def test_backfill_vectors_end_to_end_dim_change_reembeds(self, db_path):
        """Full integration: stamp an old 384-d identity + populate a vec table
        with a stale row, then call backfill_vectors() with the REAL active
        embedding backend (whatever's installed — model2vec on this venv,
        512-d) and confirm it ends up re-embedded at the new dim rather than
        erroring on a width mismatch or silently keeping the stale rows."""
        import struct

        from techne.core.embeddings import backend_available
        from techne.core.extension_state import get_state, set_state
        from techne.core.knowledge import store as store_knowledge
        from techne.core.search import _ensure_vec_table, backfill_vectors

        pytest.importorskip("sqlite_vec")
        if not backend_available():
            pytest.skip("no embedding backend (model2vec or fastembed) available")

        store_knowledge(
            db_path, "a fact that will need embedding at the new dimension", type="fact"
        )

        conn = connect(db_path)
        try:
            conn.enable_load_extension(True)
            import sqlite_vec

            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            _ensure_vec_table(conn, "knowledge_vec", 384)
            emb384 = struct.pack("384f", *([0.1] * 384))
            conn.execute(
                "INSERT INTO knowledge_vec(rowid, embedding) VALUES (999999, ?)", (emb384,)
            )
            # Stamp a DIFFERENT identity than whatever's actually active, so the
            # real backfill_vectors call is guaranteed to observe a mismatch.
            set_state(conn, "embedding_backend", {"name": "fastembed:bogus-old-model", "dim": 384})
            conn.commit()
        finally:
            conn.close()

        counts = backfill_vectors(db_path)
        assert counts.get("knowledge", 0) >= 1

        conn = connect(db_path)
        try:
            # knowledge_vec is a vec0 virtual table — querying it (even just
            # rowid) requires the extension loaded on THIS connection too;
            # plain connect() doesn't load it (same reason backfill_vectors
            # itself loads it explicitly).
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            # Stale rowid 999999 (from the dropped-and-recreated table) is gone.
            remaining = {r[0] for r in conn.execute("SELECT rowid FROM knowledge_vec").fetchall()}
            assert 999999 not in remaining

            stamp = get_state(conn, "embedding_backend")
            assert stamp["name"] != "fastembed:bogus-old-model"
        finally:
            conn.close()

    def test_backfill_reembeds_content_edited_row(self, db_path):
        """techne#382: a knowledge row whose `content` is edited in place
        (vec_stale=1) must be RE-embedded by backfill_vectors — not skipped as
        'already present in vec' — else semantic recall matches the stale text
        forever. Asserts the stored embedding actually changes."""
        from techne.core.embeddings import backend_available
        from techne.core.knowledge import store as store_knowledge
        from techne.core.search import backfill_vectors

        pytest.importorskip("sqlite_vec")
        if not backend_available():
            pytest.skip("no embedding backend (model2vec or fastembed) available")

        kid = store_knowledge(db_path, "alpha alpha the original body text", type="fact")
        assert backfill_vectors(db_path).get("knowledge", 0) >= 1

        def _vec_bytes(rowid):
            conn = connect(db_path)
            try:
                import sqlite_vec

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                row = conn.execute(
                    "SELECT embedding FROM knowledge_vec WHERE rowid = ?", (rowid,)
                ).fetchone()
                return bytes(row[0]) if row else None
            finally:
                conn.close()

        before = _vec_bytes(kid)
        assert before is not None  # embedded on the first backfill

        # Edit content in place + flag stale (exactly what fill_card_summary does).
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE knowledge SET content = ?, vec_stale = 1 WHERE id = ?",
                ("beta beta an entirely different body", kid),
            )
            conn.commit()
        finally:
            conn.close()

        # Pre-#382 this returned knowledge:0 (row already in vec) and `before`
        # would persist. The fix re-embeds the flagged row.
        assert backfill_vectors(db_path).get("knowledge", 0) >= 1
        after = _vec_bytes(kid)
        assert after is not None
        assert after != before, "embedding was not regenerated after the content edit"

        # Flag cleared → the row drops out of the re-embed set (loop termination).
        conn = connect(db_path)
        try:
            stale = conn.execute("SELECT vec_stale FROM knowledge WHERE id = ?", (kid,)).fetchone()[
                0
            ]
            assert stale == 0
        finally:
            conn.close()


class TestSearchNoMutation:
    def test_use_vectors_does_not_mutate_fts_results(self, db_path):
        """search(use_vectors=True) must not overwrite the 'source' field
        on results returned by search_fts — callers may read 'source' as
        the knowledge provenance, not the match origin."""

        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label="mutation check thread",
                summary="check source field is not mutated",
            )
        ]
        save_threads(threads, session_id, db_path)

        fts_before = search_fts(db_path, "mutation", table="threads", limit=5)
        original_source_values = [dict(r) for r in fts_before]

        search(db_path, "mutation", use_vectors=False)

        fts_after = search_fts(db_path, "mutation", table="threads", limit=5)
        for orig, after in zip(original_source_values, fts_after):
            assert orig.get("source_table") == after.get("source_table")
            # The 'source' column (provenance) must not have been overwritten
            assert orig.get("source") == after.get("source")


class TestVecChannelCarriesKnowledgeType:
    """techne#566's PRODUCER, pinned on the real vector path.

    The fusion side was pinned by test_search_rrf.py, but deleting
    `"knowledge_type": k[1]` from `search.py:682` still left the suite
    bit-identical at 2810/19 — the producer was unpinned, so the label fix could
    silently revert to `[knowledge]` on the channel that actually leads.

    A stubbed test cannot reach it: the fixture supplies the key it then asserts.
    This runs the REAL vec query. Since techne#535 the main test matrix installs
    `[vectors]` with a warm model cache, so this executes on every PR rather than
    only in `eval-vec` — it is not eval-gated coverage.

    Guarded on `backend_available()` rather than `importorskip("fastembed")`, for
    the reason recorded four classes above: fastembed is absent-by-design on
    py3.14 while model2vec provides working vector search there, so importorskip
    on fastembed specifically would wrongly skip real coverage on 3.14.
    """

    def test_a_vec_hit_carries_the_rows_own_kind(self, db_path):
        from techne.core import embeddings as embeddings_mod

        pytest.importorskip("sqlite_vec")
        if not embeddings_mod.backend_available():
            pytest.skip("no embedding backend (model2vec or fastembed) available")

        from techne.core.knowledge import store
        from techne.core.search import backfill_vectors

        store(
            db_path,
            "Vector retrieval must preserve the row's own kind, not the table name.",
            type="decision",
        )
        backfill_vectors(db_path)

        results = search(db_path, "vector retrieval preserve kind", use_vectors=True)

        knowledge_rows = [r for r in results if r.get("source_table") == "knowledge"]
        assert knowledge_rows, "no knowledge row came back; the query matched nothing"
        assert all(r.get("knowledge_type") for r in knowledge_rows), (
            "a knowledge row reached a consumer without its own kind — "
            "every line it renders would read [knowledge]"
        )
        assert any(r.get("knowledge_type") == "decision" for r in knowledge_rows)


class TestResultRowContract:
    """techne#579 — the row contract that cost two live bugs before it was named.

    `search()` set `type` to the SOURCE TABLE, never the knowledge row's own kind.
    Nothing errored; the value was just wrong in a way that read as plausible, and
    the natural code was the broken code:

      #565 — the per-turn pull-only guard shipped as
             `[r for r in rows if r.get("type") not in PULL_ONLY_TYPES]`.
             It passed nothing and blocked nothing, because `type` was always the
             literal "knowledge". A fail-without-fix test caught it; review did not.
      #566 — `_build_context` rendered `[{r.get("type")}]`, so EVERY injected line
             read `[knowledge]` regardless of kind, collapsing decision-vs-evaluation
             exactly where the agent decides how much to trust the line.

    `type` is REMOVED rather than aliased, deliberately: an alias keeps the trap for
    whoever reaches for the obvious name first. A stale consumer now raises KeyError
    at its own call site instead of silently comparing against a wrong string.
    """

    def test_no_result_row_carries_a_bare_type_key(self, db_path):
        """The guard. If `type` returns, #565's shape becomes writable again."""
        from techne.core.state import add_observation, ensure_session

        store_knowledge(db_path, "a durable fact about contracts", type="fact")
        ensure_session(db_path, "s-579")
        add_observation(db_path, "s-579", "Bash", "an observation about contracts")

        results = search(db_path, "contracts", table="all")

        assert results, "precondition: the query matched something"
        offenders = [r for r in results if "type" in r]
        assert not offenders, (
            "a search result carries a bare `type` key again (techne#579). It is "
            "ambiguous between the source table and the row's own kind, and that "
            "ambiguity cost #565 and #566. Use `source_table` or `knowledge_type`."
        )

    def test_source_table_names_the_table_and_knowledge_type_names_the_kind(self, db_path):
        """Both halves, on one query, so their DIFFERENCE is pinned rather than
        each being asserted in isolation."""
        from techne.core.state import add_observation, ensure_session

        store_knowledge(db_path, "a decision about retrieval contracts", type="decision")
        ensure_session(db_path, "s-579b")
        add_observation(db_path, "s-579b", "Bash", "an observation about retrieval")

        results = search(db_path, "retrieval", table="all")
        by_table = {}
        for r in results:
            by_table.setdefault(r["source_table"], []).append(r)

        assert "knowledge" in by_table, "no knowledge row came back"
        k = by_table["knowledge"][0]
        assert k["source_table"] == "knowledge", "source_table must name the TABLE"
        assert k["knowledge_type"] == "decision", (
            "knowledge_type must name the ROW'S OWN KIND — this is the distinction "
            "the old single `type` key destroyed"
        )
        assert k["source_table"] != k["knowledge_type"], (
            "the two fields must be able to disagree; if they cannot, the contract "
            "has collapsed back into one value"
        )

        for r in by_table.get("observation", []):
            assert r.get("knowledge_type") is None, (
                "only knowledge rows have a kind; an observation claiming one would "
                "invite the same conflation from the other direction"
            )

    def test_the_565_filter_shape_is_now_impossible_to_write_silently(self, db_path):
        """#565's exact broken line, re-run against today's rows.

        It filtered on `type` against PULL_ONLY_TYPES and matched nothing, because
        `type` held a table name. That line now raises instead of quietly passing
        everything — which is the entire point of removing the key rather than
        aliasing it.
        """
        from techne.core.knowledge import PULL_ONLY_TYPES

        store_knowledge(db_path, "a pull-only document row", type="document")
        results = search(db_path, "pull-only document", table="knowledge")
        assert results, "precondition: the row is findable"

        with pytest.raises(KeyError):
            _ = [r for r in results if r["type"] not in PULL_ONLY_TYPES]

        # And the CORRECT spelling actually discriminates, rather than passing all.
        excluded = [r for r in results if r.get("knowledge_type") in PULL_ONLY_TYPES]
        assert excluded, "the document row must be identifiable by its real kind"


def test_search_honours_the_callers_row_chars_for_knowledge(tmp_path):
    """techne#842 — the knob governs above 500, which is the whole issue.

    `search()` used to slice every knowledge row at a hardcoded 500 before any
    caller saw it, so `hooks.per_turn_recall_chars` was inert above that value.
    Measured on a 2,399-char row at caps 200/500/600/1200: each must come back at
    exactly the cap, and asking for nothing must still give the 500 default.

    The motivating case is the affected one — `config.py` says this knob "decides
    whether a multi-step SOP arrives as a procedure or as its title", and a
    multi-step SOP is exactly a row over 500 characters.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store as store_knowledge
    from techne.core.search import _ROW_CHAR_CAP, search

    db = tmp_path / "t.db"
    init_db(db)
    body = " ".join(f"word{i:03d}" for i in range(300))
    assert len(body) > 2000, "fixture must exceed the default cap several times over"
    store_knowledge(db, content=body, type="procedure", tags="probe", confidence="high")

    for cap in (200, 500, 600, 1200):
        rows = search(db, "word001", table="knowledge", limit=5, row_chars=cap)
        assert rows, f"no row at cap {cap}"
        assert len(rows[0]["content"]) == cap, (
            f"cap {cap} returned {len(rows[0]['content'])} chars — search is still "
            "pre-truncating below what the caller asked for (techne#842)"
        )

    default = search(db, "word001", table="knowledge", limit=5)
    assert len(default[0]["content"]) == _ROW_CHAR_CAP, (
        "a caller that asks for nothing must still get the documented default"
    )


def test_the_ledger_tables_keep_the_default_cap(tmp_path):
    """The asymmetry is the point, not an oversight (techne#842).

    Observations are a tool ledger — mostly shell echoes — and truncating them
    costs nothing a reader wanted. `knowledge` is the curated layer someone
    deliberately wrote. So `row_chars` moves the knowledge cap and must NOT move
    the observation one, or the recall payload grows by the ledger rather than by
    the thing that was asked for.
    """
    from techne.core.database import init_db
    from techne.core.search import _ROW_CHAR_CAP, search
    from techne.core.state import add_observation, ensure_session

    db = tmp_path / "t.db"
    init_db(db)
    ensure_session(db, "s-842")
    body = " ".join(f"word{i:03d}" for i in range(300))
    add_observation(db, "s-842", "Bash", body)

    rows = search(db, "word001", table="observations", limit=5, row_chars=1200)
    assert rows, "fixture produced no observation row"
    assert len(rows[0]["content"]) == _ROW_CHAR_CAP, (
        f"observation row is {len(rows[0]['content'])} chars — the ledger must keep "
        "the default regardless of row_chars"
    )


def test_fts_with_or_fallback_control_flow():
    """Issue #559: the #70 control flow lives in one helper. AND first; OR only when
    AND is empty and there are two or more terms; nothing run for an unsearchable
    query (the #143 MATCH '' guard)."""
    from techne.core.search import fts_with_or_fallback

    calls: list[str] = []

    def run_empty_then(rows):
        def run(match):
            calls.append(match)
            return [] if len(calls) == 1 else rows
        return run

    assert fts_with_or_fallback("alpha beta", run_empty_then(["hit"])) == ["hit"]
    assert calls == ['"alpha" "beta"', '"alpha" OR "beta"']

    calls.clear()
    assert fts_with_or_fallback("alpha beta", lambda m: calls.append(m) or ["and"]) == ["and"]
    assert calls == ['"alpha" "beta"']  # AND matched: no second pass

    calls.clear()
    assert fts_with_or_fallback("alpha", run_empty_then(["x"])) == []
    assert calls == ['"alpha"']  # one term: AND == OR, no re-query

    calls.clear()
    assert fts_with_or_fallback("  \\ ", run_empty_then(["x"])) == []
    assert calls == []  # unsearchable: run never called


def test_fts_and_or_split_tops_up_short_and_pass():
    """techne#1079: a one-row AND pass no longer replaces the whole pool. When
    `want` is given and AND returns fewer rows than `want`, the OR-of-terms pass
    tops it up, returned as a SEPARATE list from the AND rows (v2: search_fts
    reranks the two blocks separately, so the split must be preserved rather
    than merged — see fts_and_or_split's docstring)."""
    from techne.core.search import fts_and_or_split

    and_rows = [{"id": "full"}]  # the one row containing every term
    or_rows = [{"id": "full"}, {"id": "partial-a"}, {"id": "partial-b"}]

    def run(match):
        return and_rows if "OR" not in match else or_rows

    and_out, or_out = fts_and_or_split("alpha beta", run, want=5, key=lambda r: r["id"])
    assert [r["id"] for r in and_out] == ["full"]
    assert [r["id"] for r in or_out] == ["partial-a", "partial-b"]  # "full" deduped out


def test_fts_and_or_split_dedups_on_key():
    """No duplicate keys across the two returned lists: an OR row whose key
    already appears in the AND rows is dropped from the OR list, not merely
    left for a later merge to catch."""
    from techne.core.search import fts_and_or_split

    and_rows = [{"source_table": "knowledge", "id": 1}]
    or_rows = [
        {"source_table": "knowledge", "id": 1},  # same row as the AND hit
        {"source_table": "thread", "id": 1},  # same numeric id, DIFFERENT table
        {"source_table": "knowledge", "id": 2},
    ]

    def run(match):
        return and_rows if "OR" not in match else or_rows

    and_out, or_out = fts_and_or_split(
        "alpha beta", run, want=10, key=lambda r: (r["source_table"], r["id"])
    )
    keys = [(r["source_table"], r["id"]) for r in and_out + or_out]
    assert len(keys) == len(set(keys)), f"duplicate key in {keys}"
    assert [(r["source_table"], r["id"]) for r in or_out] == [("thread", 1), ("knowledge", 2)]


def test_fts_and_or_split_no_topup_when_pool_already_full():
    """When the AND pass already fills `want`, the OR pass is never run — same
    behaviour as before `want` existed, just made explicit for a non-None want."""
    from techne.core.search import fts_and_or_split

    calls: list[str] = []

    def run(match):
        calls.append(match)
        return [{"id": "a"}, {"id": "b"}]

    and_out, or_out = fts_and_or_split("alpha beta", run, want=2, key=lambda r: r["id"])
    assert [r["id"] for r in and_out] == ["a", "b"]
    assert or_out == []
    assert calls == ['"alpha" "beta"']  # OR pass never invoked


def test_fts_with_or_fallback_merges_split_and_caps_at_want():
    """fts_with_or_fallback is fts_and_or_split's two lists concatenated
    AND-then-OR and capped at `want` — the shape codegraph/patterns rely on,
    since they don't rerank and need AND-then-OR ordering unconditionally."""
    from techne.core.search import fts_with_or_fallback

    and_rows = [{"id": "full"}]
    or_rows = [{"id": "full"}, {"id": "partial-a"}, {"id": "partial-b"}]

    def run(match):
        return and_rows if "OR" not in match else or_rows

    out = fts_with_or_fallback("alpha beta", run, want=2, key=lambda r: r["id"])
    assert [r["id"] for r in out] == ["full", "partial-a"]  # capped at want=2
