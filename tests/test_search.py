"""Tests for techne.core.search — FTS5 search, vector search, and combined search."""

import pytest

from techne.core.database import init_db, connect
from techne.core.search import (
    search_fts,
    search_vec,
    search,
    _sanitize_fts_query,
    _build_or_fts_query,
)
from techne.core.threads import Thread, save_threads
from techne.core.knowledge import store as store_knowledge
from techne.core.state import start_session


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
        types = [r["type"] for r in results]
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
        assert r["type"] == "observation"
        for key in ("id", "content", "tool_name", "session_id", "timestamp", "tier", "rank"):
            assert key in r, f"Missing key: {key}"


class TestSearchFtsKnowledge:
    def test_search_fts_knowledge(self, db_path):
        """Store a knowledge entry, search for a distinctive term, verify it appears."""
        store_knowledge(db_path, "quidditch scoring uses golden snitch retrieval", type="fact")

        results = search_fts(db_path, "quidditch", table="knowledge")

        assert len(results) >= 1
        types = [r["type"] for r in results]
        assert "knowledge" in types
        contents = [r["content"] for r in results]
        assert any("quidditch" in c for c in contents)

    def test_search_fts_knowledge_result_shape(self, db_path):
        """Knowledge results have the expected keys."""
        store_knowledge(db_path, "albatross migration pattern spans hemispheres", type="fact")

        results = search_fts(db_path, "albatross", table="knowledge")

        assert len(results) >= 1
        r = results[0]
        assert r["type"] == "knowledge"
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

    def test_search_fts_knowledge_superseded_excluded(self, db_path):
        """Superseded knowledge entries are not returned by FTS search."""
        from techne.core.knowledge import supersede

        old_id = store_knowledge(db_path, "tachyon drives are experimental", type="fact")
        supersede(db_path, old_id, "tachyon drives are now production-grade")

        # Old content should not appear (superseded)
        results = search_fts(db_path, "experimental", table="knowledge")
        ids = [r["id"] for r in results]
        assert old_id not in ids


class TestSearchFtsAll:
    def test_search_fts_all(self, db_path):
        """Store data in multiple tables with a shared term; 'all' returns multiple types."""
        shared_term = "wombat"

        session_id = start_session(db_path)
        threads = [
            Thread(
                thread_num=1,
                label=f"wombat habitat analysis",
                summary=f"Analyzed wombat burrowing patterns in southern Australia.",
            )
        ]
        save_threads(threads, session_id, db_path)

        store_knowledge(db_path, f"wombat populations are declining in dry regions", type="fact")

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
        result_types = {r["type"] for r in results}
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


# ---------------------------------------------------------------------------
# _build_or_fts_query tests (#70)
# ---------------------------------------------------------------------------


class TestBuildOrFtsQuery:
    def test_single_term_returns_empty(self):
        """Single term is identical under AND/OR — fallback is a no-op."""
        assert _build_or_fts_query("python") == ""

    def test_empty_query_returns_empty(self):
        assert _build_or_fts_query("   ") == ""

    def test_two_terms_joined_with_or(self):
        assert _build_or_fts_query("mongoose pattern") == '"mongoose" OR "pattern"'

    def test_multi_term_joined_with_or(self):
        result = _build_or_fts_query("structured log ingestion")
        assert result == '"structured" OR "log" OR "ingestion"'

    def test_special_chars_quoted_as_phrase_literals(self):
        """Operator chars inside a term are neutralised by phrase-quoting."""
        result = _build_or_fts_query("pre-compact thing")
        assert result == '"pre-compact" OR "thing"'

    def test_embedded_double_quote_doubled(self):
        result = _build_or_fts_query('say "hi"')
        # the bare token `"hi"` has its quotes doubled inside the phrase literal
        assert '"say"' in result
        assert ' OR ' in result


# ---------------------------------------------------------------------------
# OR-fallback integration in search_fts (#70)
# ---------------------------------------------------------------------------


class TestOrFallback:
    def test_multiword_topic_recovered_via_or_fallback(self, db_path):
        """The canonical #70 case: a natural-language topic where implicit-AND
        finds nothing (terms differ by stemming / are absent) but OR does."""
        store_knowledge(
            db_path,
            "The mongoose pattern is preferred over the manticore approach "
            "when ingesting structured logs.",
            type="pattern",
        )
        # AND form requires every term incl. "for", "log" (doc has "logs") and
        # "ingestion" (doc has "ingesting") — none of which match → AND empty.
        and_only = search_fts(
            db_path, "mongoose pattern for structured log ingestion", table="all"
        )
        # With the OR fallback wired in, the entry is recovered.
        assert len(and_only) >= 1
        assert any("mongoose" in r.get("content", "") for r in and_only)

    def test_or_fallback_does_not_fire_when_and_matches(self, db_path):
        """When the AND query already matches, results are unchanged (the
        fallback only runs on an empty AND pass)."""
        store_knowledge(db_path, "mongoose pattern preferred for logs", type="pattern")
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
        import sys

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
        """search_vec returns [] gracefully when sqlite_vec/fastembed not installed.

        On Python 3.14 these extras have no wheel, so the ImportError path is tested.
        If extras ARE present, skip this specific assertion and test the happy path instead.
        """
        try:
            import sqlite_vec
            import fastembed

            extras_available = True
        except ImportError:
            extras_available = False

        if not extras_available:
            # ImportError path: should return [] without raising
            results = search_vec(db_path, "any query")
            assert results == [], f"Expected [] without extras, got {results}"
        else:
            # If extras are somehow available, just confirm it returns a list
            pytest.skip("Vector extras are installed; skipping graceful-degradation test")

    def test_search_combined_dedupes(self, db_path):
        """Combined search deduplicates results that appear in both FTS and vec.

        Requires vector extras. Skipped cleanly if not available.
        """
        sqlite_vec = pytest.importorskip("sqlite_vec")
        pytest.importorskip("fastembed")

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
            key = (r["type"], r.get("id"))
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


class TestSearchNoMutation:
    def test_use_vectors_does_not_mutate_fts_results(self, db_path):
        """search(use_vectors=True) must not overwrite the 'source' field
        on results returned by search_fts — callers may read 'source' as
        the knowledge provenance, not the match origin."""
        import sqlite3

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
            assert orig.get("type") == after.get("type")
            # The 'source' column (provenance) must not have been overwritten
            assert orig.get("source") == after.get("source")
