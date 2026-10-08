"""Tests for the Librarian domain: documents store + search + GitHubIssuesSource.

All fixtures use tmp_path — the live colony db is never touched.
Vector assertions are gated on fastembed + sqlite_vec availability (Python 3.14
ships without fastembed wheels, so vector tests are skipped there).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from techne.core.database import connect, init_db
from techne.core.documents import (
    get_document,
    list_documents,
    search_documents,
    store_document,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_vectors() -> bool:
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401

        return True
    except ImportError:
        return False


vectors_available = pytest.mark.skipif(
    not _has_vectors(), reason="sqlite_vec + fastembed not installed"
)


@pytest.fixture()
def db_path(tmp_path) -> Path:
    # The documents schema lives in the Librarian domain now (db-per-domain #99),
    # not core SCHEMA_SQL — apply it so the documents store works on this db.
    from techne.core.librarian import LIBRARIAN_SCHEMA

    path = tmp_path / "library.db"
    conn = init_db(path)
    conn.executescript(LIBRARIAN_SCHEMA)
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Schema smoke tests
# ---------------------------------------------------------------------------


class TestDocumentsSchema:
    def test_documents_table_exists(self, db_path):
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_documents_fts_table_exists(self, db_path):
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='documents_fts'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_documents_triggers_exist(self, db_path):
        conn = connect(db_path)
        try:
            triggers = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
        finally:
            conn.close()
        assert "documents_ai" in triggers
        assert "documents_ad" in triggers
        assert "documents_au" in triggers


# ---------------------------------------------------------------------------
# store_document: insert + dedup + supersede
# ---------------------------------------------------------------------------


class TestStoreDocument:
    def test_store_returns_id_and_stored_true(self, db_path):
        doc_id, stored = store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/1",
            content="Fix the thing",
            title="Issue 1",
        )
        assert isinstance(doc_id, int)
        assert stored is True

    def test_store_round_trips(self, db_path):
        doc_id, _ = store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/2",
            content="Detailed body text",
            title="Issue 2",
            meta='{"state":"open"}',
        )
        row = get_document(db_path, doc_id)
        assert row is not None
        assert row["source_type"] == "github_issue"
        assert row["source_url"] == "https://github.com/acme/repo/issues/2"
        assert row["content"] == "Detailed body text"
        assert row["title"] == "Issue 2"
        assert row["meta"] == '{"state":"open"}'
        assert row["owned_by"] == "librarian"
        assert row["superseded_by"] is None

    def test_dedup_same_content_is_noop(self, db_path):
        url = "https://github.com/acme/repo/issues/3"
        id1, stored1 = store_document(
            db_path, source_type="github_issue", source_url=url, content="body"
        )
        id2, stored2 = store_document(
            db_path, source_type="github_issue", source_url=url, content="body"
        )
        assert stored1 is True
        assert stored2 is False
        assert id1 == id2  # same row returned

    def test_supersede_on_content_change(self, db_path):
        url = "https://github.com/acme/repo/issues/4"
        id1, _ = store_document(
            db_path, source_type="github_issue", source_url=url, content="original"
        )
        id2, stored = store_document(
            db_path, source_type="github_issue", source_url=url, content="updated body"
        )
        assert stored is True
        assert id2 != id1  # new row created

        old = get_document(db_path, id1)
        assert old["superseded_by"] == id2  # old row points to new

        new = get_document(db_path, id2)
        assert new["superseded_by"] is None  # new row is active
        assert new["content"] == "updated body"

    def test_only_one_active_row_per_source_url(self, db_path):
        """store_document enforces: only one active row per source_url.

        Uniqueness is maintained programmatically (SELECT + supersede-on-change)
        rather than a partial unique index — this test verifies that invariant.
        """
        url = "https://github.com/acme/repo/issues/5"
        store_document(db_path, source_type="github_issue", source_url=url, content="v1")
        store_document(db_path, source_type="github_issue", source_url=url, content="v2")
        store_document(db_path, source_type="github_issue", source_url=url, content="v3")

        conn = connect(db_path)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source_url=? AND superseded_by IS NULL",
                (url,),
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source_url=?", (url,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert active == 1  # only one active row
        assert total == 3  # three revisions in history

    def test_multiple_supersessions_chain(self, db_path):
        url = "https://github.com/acme/repo/issues/6"
        id1, _ = store_document(db_path, source_type="github_issue", source_url=url, content="v1")
        id2, _ = store_document(db_path, source_type="github_issue", source_url=url, content="v2")
        id3, _ = store_document(db_path, source_type="github_issue", source_url=url, content="v3")
        assert get_document(db_path, id1)["superseded_by"] == id2
        assert get_document(db_path, id2)["superseded_by"] == id3
        assert get_document(db_path, id3)["superseded_by"] is None


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------


class TestTitleDriftRepair:
    """techne#819 — a titling FIX must be able to reach rows already stored.

    Both dedup keys here are content-derived and neither includes the title, so
    re-indexing with a corrected adapter produces byte-identical content, lands in
    the unchanged branch, and the old title survives. That is how #816's 25
    `<local-command-caveat>Caveat: …` titles became unrepairable by the obvious
    remedy — while #817's note invited exactly that inference.
    """

    def test_a_corrected_title_lands_on_the_same_row(self, db_path):
        body = "the transcript body, byte-identical across both passes"
        first, _ = store_document(
            db_path,
            source_type="cc-transcript",
            source_url="cc:s1",
            title="<local-command-caveat>Caveat: The messages below",
            content=body,
        )
        stats: dict = {}
        second, stored = store_document(
            db_path,
            source_type="cc-transcript",
            source_url="cc:s1",
            title="why is recall empty",
            content=body,
            stats=stats,
        )
        assert second == first, "a repair must not mint a new revision"
        assert stored is False, (
            "no new row was written; widening `stored` here would make the "
            "caller's `unchanged` count wrong"
        )
        assert get_document(db_path, first)["title"] == "why is recall empty"
        assert stats == {"titles_repaired": 1}

    def test_an_unchanged_title_writes_nothing(self, db_path):
        """The repair must not turn a pure no-op path into a per-index write.

        THE ASSERTION USED TO BE `stats == {}` ALONE, WHICH CANNOT SEE A WRITE.
        Making the UPDATE unconditional — firing on every unchanged pass and
        bumping `updated_at` — while guarding only the stats increment left the
        whole suite green (audit instance 7). The docstring named a write that no
        assertion observed: one half pinned, the other free.
        """
        from techne.core.database import connect

        def _updated_at():
            conn = connect(db_path)
            try:
                row = conn.execute(
                    "SELECT updated_at FROM documents WHERE source_url = ?", ("cc:s2",)
                ).fetchone()
                return row["updated_at"] if row else None
            finally:
                conn.close()

        stats: dict = {}
        kwargs = dict(
            source_type="cc-transcript",
            source_url="cc:s2",
            title="stable title",
            content="stable body",
        )
        store_document(db_path, stats=stats, **kwargs)

        # A SENTINEL, NOT THE CLOCK. An earlier version of this test compared
        # `updated_at` across two back-to-back passes and could not fail:
        # `CURRENT_TIMESTAMP` is WHOLE-SECOND, the two calls are microseconds apart,
        # so a rewritten timestamp compares EQUAL to the original. Measured — at a
        # 0s gap the assertion passes under the unconditional-UPDATE mutation; only
        # at a >1s gap does it discriminate, which back-to-back calls never reach.
        # That was worse than the silence it replaced, because the docstring claimed
        # the gap was closed and a reader would trust it. Stamping an impossible
        # value makes any write visible regardless of clock resolution.
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE documents SET updated_at = ? WHERE source_url = ?",
                ("1999-01-01 00:00:00", "cc:s2"),
            )
            conn.commit()
        finally:
            conn.close()
        first_write = _updated_at()

        store_document(db_path, stats=stats, **kwargs)

        assert stats == {}, f"spurious repair on an unchanged title: {stats}"
        assert _updated_at() == first_write, (
            "the row was rewritten on an identical pass — a no-op path became a "
            "per-index write, and on the card side that also re-queues embedding"
        )

    def test_a_repair_is_counted_through_the_real_indexer(self, tmp_path):
        """`stats=` was severable at all three call sites with the suite green
        (audit instance 6): the only nonzero `titles_repaired` assertion called
        `store_document` DIRECTLY, and the `"titles_repaired": 0` keys elsewhere are
        satisfied by the counts-dict initializer whether the plumbing exists or not.

        So the count an operator reads after re-indexing to heal #816's titles —
        the one the CHANGELOG points them at — was pinned by nothing. This drives a
        real repair through `index_markdown_dir`, the indexer end of the wiring.
        """
        import json

        from techne.config import Config
        from techne.core.database import init_db
        from techne.core.librarian import librarian_db
        from techne.core.library import index_markdown_dir

        config = Config(db_path=tmp_path / "t.db", data_dir=tmp_path)
        init_db(config.db_path).close()
        root = tmp_path / "md"
        root.mkdir()
        (root / "note.md").write_text("# Original Heading\n\nbody text that will not change\n")

        first = index_markdown_dir(config, root, make_cards=False)
        assert first["titles_repaired"] == 0, first

        # same CONTENT hash is impossible if we edit the heading, so rename the title
        # the way a titling FIX would: rewrite the stored row's title out of band,
        # then re-index and watch the indexer repair it back.
        conn = __import__("techne.core.database", fromlist=["connect"]).connect(
            librarian_db(config)
        )
        try:
            conn.execute("UPDATE documents SET title = ?", ("<junk-wrapper> Caveat:",))
            conn.commit()
        finally:
            conn.close()

        second = index_markdown_dir(config, root, make_cards=False)
        assert second["titles_repaired"] == 1, (
            f"the indexer repaired a title without counting it: {json.dumps(second)}"
        )
        assert second["stored"] == 0 and second["unchanged"] == 1, second

    def test_a_content_change_still_supersedes(self, db_path):
        """The repair branch must not swallow a genuine revision."""
        first, _ = store_document(
            db_path, source_type="x", source_url="cc:s3", title="T1", content="body one"
        )
        second, stored = store_document(
            db_path, source_type="x", source_url="cc:s3", title="T2", content="body two"
        )
        assert stored is True
        assert second != first


class TestListDocuments:
    def test_list_returns_active_rows_only(self, db_path):
        url = "https://github.com/acme/repo/issues/7"
        store_document(db_path, source_type="github_issue", source_url=url, content="v1")
        store_document(db_path, source_type="github_issue", source_url=url, content="v2")
        rows = list_documents(db_path)
        urls = [r["source_url"] for r in rows]
        # Only one active row for this URL
        assert urls.count(url) == 1

    def test_list_filter_by_source_type(self, db_path):
        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/x/y/issues/1",
            content="issue content",
        )
        store_document(
            db_path,
            source_type="runbook",
            source_url="https://wiki.example.com/runbook-1",
            content="runbook content",
        )
        issues = list_documents(db_path, source_type="github_issue")
        assert all(r["source_type"] == "github_issue" for r in issues)
        assert len(issues) == 1

    def test_list_include_superseded(self, db_path):
        url = "https://github.com/acme/repo/issues/8"
        store_document(db_path, source_type="github_issue", source_url=url, content="v1")
        store_document(db_path, source_type="github_issue", source_url=url, content="v2")
        all_rows = list_documents(db_path, include_superseded=True)
        this_url_rows = [r for r in all_rows if r["source_url"] == url]
        assert len(this_url_rows) == 2


# ---------------------------------------------------------------------------
# FTS search over documents
# ---------------------------------------------------------------------------


class TestDocumentsFtsSearch:
    """Documents search is now the Librarian's domain search (search_documents),
    not core search — documents moved to librarian.db (db-per-domain #99)."""

    def test_fts_finds_document_by_content(self, db_path):
        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/10",
            content="The authentication flow is broken on Safari zymurgy",
            title="Safari auth bug",
        )
        results = search_documents(db_path, "zymurgy")
        assert len(results) >= 1
        assert "zymurgy" in results[0]["content"]

    def test_fts_finds_document_by_title(self, db_path):
        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/11",
            content="Body text here",
            title="Unique xylophone title",
        )
        assert len(search_documents(db_path, "xylophone")) >= 1

    def test_fts_blank_query_returns_empty(self, db_path):
        """#103: blank/whitespace must not crash (FTS5 MATCH '' is a syntax error)."""
        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/12",
            content="Body text here",
            title="A title",
        )
        assert search_documents(db_path, "") == []
        assert search_documents(db_path, "   ") == []
        # techne#143: a lone backslash sanitizes to "" and reached MATCH '' here — now guarded.
        assert search_documents(db_path, "\\") == []

    def test_fts_superseded_docs_excluded(self, db_path):
        url = "https://github.com/acme/repo/issues/13"
        store_document(
            db_path,
            source_type="github_issue",
            source_url=url,
            content="Old content mentioning zarquon exclusively",
            title="Old issue",
        )
        store_document(
            db_path,
            source_type="github_issue",
            source_url=url,
            content="New content entirely rewritten",
            title="Updated issue",
        )
        # 'zarquon' is only in the superseded row → excluded.
        assert search_documents(db_path, "zarquon") == []

    def test_fts_no_false_positives(self, db_path):
        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/14",
            content="Ordinary content",
        )
        assert search_documents(db_path, "xyzzy_nonexistent_42") == []


# ---------------------------------------------------------------------------
# Vector search (gated on fastembed availability)
# ---------------------------------------------------------------------------


class TestDocumentsVecSearch:
    @vectors_available
    @pytest.mark.xfail(
        strict=True,
        reason="There is no document vector search: backfill_vectors embeds threads, "
        "observations and knowledge only, and librarian.db has no documents_vec "
        "(search.py, the comment after the knowledge loop in search_vec). This was "
        "marked as issue 536 until that fix showed the test still fails. strict=True "
        "turns it red if document embedding is ever added, so the marker goes then.",
    )
    def test_backfill_and_vec_search(self, db_path):
        from techne.core.search import backfill_vectors, search_vec

        store_document(
            db_path,
            source_type="github_issue",
            source_url="https://github.com/acme/repo/issues/20",
            content="Database index optimization reduces query time significantly",
            title="Performance issue",
        )
        counts = backfill_vectors(db_path)
        assert counts.get("documents", 0) >= 1

        results = search_vec(db_path, "query performance", table="documents")
        assert len(results) >= 1
        assert results[0]["type"] == "document"


# ---------------------------------------------------------------------------
# GitHubIssuesSource
# ---------------------------------------------------------------------------

# Minimal fixture response mirroring `gh issue list --json` output.
_MOCK_ISSUES = [
    {
        "number": 1,
        "title": "Fix the login bug",
        "body": "The login page crashes on Safari.",
        "url": "https://github.com/acme/testrepo/issues/1",
        "state": "OPEN",
        "labels": [{"name": "bug"}],
        "updatedAt": "2024-01-15T10:00:00Z",
    },
    {
        "number": 2,
        "title": "Add dark mode",
        "body": "Users have requested dark mode support.",
        "url": "https://github.com/acme/testrepo/issues/2",
        "state": "CLOSED",
        "labels": [{"name": "enhancement"}, {"name": "ui"}],
        "updatedAt": "2024-01-16T12:00:00Z",
    },
]


@pytest.fixture()
def gh_config(tmp_path):
    """A Config whose core techne db holds the github cursor; the registry resolves
    librarian.db beside it for documents (db-per-domain #102)."""
    from techne.config import Config

    path = tmp_path / "techne.db"
    init_db(path).close()
    return Config(db_path=path, data_dir=tmp_path)


@pytest.fixture()
def techne_db(gh_config) -> Path:
    """The core techne db path (cursor lives here in extension_state)."""
    return gh_config.db_path


@pytest.fixture()
def lib_db(gh_config) -> Path:
    """The librarian.db the github source writes documents into."""
    from techne.core.librarian import librarian_db

    return librarian_db(gh_config)


@pytest.fixture()
def github_source(gh_config):
    from techne.sync.github_issues import GitHubIssuesSource

    return GitHubIssuesSource(gh_config, owner="acme", repo="testrepo")


class TestGitHubIssuesSource:
    def test_initial_walk_stores_documents(self, github_source, lib_db):
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            count = github_source.initial_walk()

        assert count == 2
        docs = list_documents(lib_db, source_type="github_issue")
        assert len(docs) == 2

    def test_documents_land_in_librarian_not_core(self, github_source, techne_db, lib_db):
        """#101 regression: docs go to librarian.db; the core db gets no documents
        table (no core-pollution of a domain db, and cards resolve via librarian.db).

        Probe with raw sqlite3 — the core `connect()` would itself create the tables
        we're asserting are absent."""
        import sqlite3

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""
            github_source.initial_walk()

        def _has_table(path, name):
            raw = sqlite3.connect(str(path))
            try:
                return (
                    raw.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()
                    is not None
                )
            finally:
                raw.close()

        # core db never grew a documents table (domain content stays out of core)
        assert not _has_table(techne_db, "documents")
        # librarian.db never grew core tables (no substrate/domain entanglement)
        assert not _has_table(lib_db, "observations")
        assert _has_table(lib_db, "documents")
        assert len(list_documents(lib_db, source_type="github_issue")) == 2

    def test_initial_walk_stores_correct_fields(self, github_source, lib_db):
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES[:1])
            mock_run.return_value.stderr = ""

            github_source.initial_walk()

        docs = list_documents(lib_db)
        assert len(docs) == 1
        d = docs[0]
        assert d["source_type"] == "github_issue"
        assert d["source_url"] == "https://github.com/acme/testrepo/issues/1"

        full = get_document(lib_db, d["id"])
        assert full["content"] == "The login page crashes on Safari."
        assert full["title"] == "Fix the login bug"

        meta = json.loads(full["meta"])
        assert meta["state"] == "OPEN"
        assert "bug" in meta["labels"]
        assert meta["number"] == 1

    def test_initial_walk_advances_cursor(self, github_source, techne_db):
        from techne.core.database import connect as _connect
        from techne.core.extension_state import get_state

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            github_source.initial_walk()

        conn = _connect(techne_db)  # cursor lives in the core db (extension_state)
        try:
            state = get_state(conn, "github_issues:acme/testrepo")
        finally:
            conn.close()

        assert state is not None
        # Cursor should be the max updatedAt among the two mock issues.
        assert state["last_updated_at"] == "2024-01-16T12:00:00Z"

    def test_second_walk_is_incremental_with_search_cursor(self, github_source, techne_db):
        """techne#170: the saved cursor is now USED. The first walk fetches everything;
        the second restricts the gh fetch to `--search updated:>=<cursor>` instead of
        re-fetching the whole repo (the cursor used to be saved but never read)."""
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            github_source.initial_walk()
            first_cmd = list(mock_run.call_args.args[0])
            github_source.initial_walk()
            second_cmd = list(mock_run.call_args.args[0])

        assert "--search" not in first_cmd, "first walk should be a full fetch"
        assert "--search" in second_cmd, "second walk should be incremental"
        i = second_cmd.index("--search")
        assert second_cmd[i + 1] == "updated:>=2024-01-16T12:00:00Z"

    def test_initial_walk_idempotent(self, github_source, db_path):
        """Re-walking with the same data stores nothing new (all dedup no-ops)."""
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            count_first = github_source.initial_walk()
            count_second = github_source.initial_walk()

        assert count_first == 2
        assert count_second == 0  # no content changed → no new rows

    def test_initial_walk_supersedes_on_body_change(self, github_source, lib_db):
        """If issue body changes between walks, old row is superseded."""
        first_walk_issues = [
            {
                **_MOCK_ISSUES[0],
                "body": "Original body.",
            }
        ]
        second_walk_issues = [
            {
                **_MOCK_ISSUES[0],
                "body": "Updated body — fix is more complex.",
                "updatedAt": "2024-01-20T09:00:00Z",
            }
        ]

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(first_walk_issues)
            mock_run.return_value.stderr = ""
            github_source.initial_walk()

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(second_walk_issues)
            mock_run.return_value.stderr = ""
            count = github_source.initial_walk()

        assert count == 1  # one supersession happened

        url = _MOCK_ISSUES[0]["url"]
        conn = connect(lib_db)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source_url=? AND superseded_by IS NULL",
                (url,),
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source_url=?", (url,)
            ).fetchone()[0]
        finally:
            conn.close()

        assert active == 1  # one active row
        assert total == 2  # one superseded + one active

    def test_initial_walk_empty_repo(self, github_source):
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            mock_run.return_value.stderr = ""

            count = github_source.initial_walk()

        assert count == 0

    def test_initial_walk_gh_failure_raises(self, github_source):
        import subprocess as _subprocess

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.side_effect = _subprocess.CalledProcessError(
                1, "gh", stderr="authentication required"
            )
            with pytest.raises(RuntimeError, match="gh issue list failed"):
                github_source.initial_walk()

    def test_start_stop_watch_are_noop(self, github_source):
        """start_watch / stop_watch must not raise (they're documented no-ops)."""
        github_source.start_watch()
        github_source.stop_watch()

    def test_health_returns_bool(self, github_source):
        """health() returns a bool regardless of gh status."""
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert github_source.health() is True

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert github_source.health() is False

    def test_health_returns_false_when_gh_missing(self, github_source):
        with patch(
            "techne.sync.github_issues.subprocess.run",
            side_effect=FileNotFoundError("gh not found"),
        ):
            assert github_source.health() is False

    def test_gh_command_structure(self, github_source):
        """Verify the gh CLI call uses the expected flags."""
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            mock_run.return_value.stderr = ""
            github_source.initial_walk()

        called_cmd = mock_run.call_args[0][0]
        assert "gh" in called_cmd
        assert "--repo" in called_cmd
        assert "acme/testrepo" in called_cmd
        assert "--state" in called_cmd
        assert "all" in called_cmd
        assert "--json" in called_cmd

    def test_issue_body_redacted_at_store(self, github_source, lib_db):
        """#244: issue body must be redacted before landing in documents.content
        (techne#244). GitHub calls store_document directly, bypassing library.index_document's
        redaction wrapper — redact_credentials(body) was added to close that gap.

        Uses the Google API key pattern from #243 to provide a high-confidence
        signal that goes through the new code path.
        """
        issue_with_secret = [
            {
                "number": 99,
                "title": "Update Maps integration",
                "body": (
                    "The Maps widget broke after rotating the key. "
                    "New key: AIzaSyAbcdefghijklmnopqrstuvwxyz1234567"
                ),
                "url": "https://github.com/acme/testrepo/issues/99",
                "state": "OPEN",
                "labels": [],
                "updatedAt": "2024-03-01T00:00:00Z",
            }
        ]
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(issue_with_secret)
            mock_run.return_value.stderr = ""
            github_source.initial_walk()

        docs = list_documents(lib_db, source_type="github_issue")
        assert len(docs) == 1
        stored_content = get_document(lib_db, docs[0]["id"])["content"]
        assert "AIzaSyAbcdefghijklmnopqrstuvwxyz1234567" not in stored_content
        assert "<redacted:google-api-key>" in stored_content


def test_search_documents_sanitizes_fts_query(db_path):
    # hyphenated/operator-ish terms must not crash FTS5 (regression).
    store_document(
        db_path,
        source_type="docs",
        source_url="docs:x.md",
        content="the db-per-domain design splits stores",
        title="db-per-domain",
    )
    hits = search_documents(db_path, "db-per-domain stores")
    assert len(hits) == 1
