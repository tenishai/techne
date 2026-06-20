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
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
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
        assert active == 1   # only one active row
        assert total == 3    # three revisions in history

    def test_multiple_supersessions_chain(self, db_path):
        url = "https://github.com/acme/repo/issues/6"
        id1, _ = store_document(
            db_path, source_type="github_issue", source_url=url, content="v1"
        )
        id2, _ = store_document(
            db_path, source_type="github_issue", source_url=url, content="v2"
        )
        id3, _ = store_document(
            db_path, source_type="github_issue", source_url=url, content="v3"
        )
        assert get_document(db_path, id1)["superseded_by"] == id2
        assert get_document(db_path, id2)["superseded_by"] == id3
        assert get_document(db_path, id3)["superseded_by"] is None


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------


class TestListDocuments:
    def test_list_returns_active_rows_only(self, db_path):
        url = "https://github.com/acme/repo/issues/7"
        store_document(
            db_path, source_type="github_issue", source_url=url, content="v1"
        )
        store_document(
            db_path, source_type="github_issue", source_url=url, content="v2"
        )
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
        store_document(
            db_path, source_type="github_issue", source_url=url, content="v1"
        )
        store_document(
            db_path, source_type="github_issue", source_url=url, content="v2"
        )
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
def github_source(db_path):
    from techne.sync.github_issues import GitHubIssuesSource

    return GitHubIssuesSource(db_path=db_path, owner="acme", repo="testrepo")


class TestGitHubIssuesSource:
    def test_initial_walk_stores_documents(self, github_source, db_path):
        with patch(
            "techne.sync.github_issues.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            count = github_source.initial_walk()

        assert count == 2
        docs = list_documents(db_path, source_type="github_issue")
        assert len(docs) == 2

    def test_initial_walk_stores_correct_fields(self, github_source, db_path):
        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES[:1])
            mock_run.return_value.stderr = ""

            github_source.initial_walk()

        docs = list_documents(db_path)
        assert len(docs) == 1
        d = docs[0]
        assert d["source_type"] == "github_issue"
        assert d["source_url"] == "https://github.com/acme/testrepo/issues/1"

        full = get_document(db_path, d["id"])
        assert full["content"] == "The login page crashes on Safari."
        assert full["title"] == "Fix the login bug"

        meta = json.loads(full["meta"])
        assert meta["state"] == "OPEN"
        assert "bug" in meta["labels"]
        assert meta["number"] == 1

    def test_initial_walk_advances_cursor(self, github_source, db_path):
        from techne.core.database import connect as _connect
        from techne.core.extension_state import get_state

        with patch("techne.sync.github_issues.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(_MOCK_ISSUES)
            mock_run.return_value.stderr = ""

            github_source.initial_walk()

        conn = _connect(db_path)
        try:
            state = get_state(conn, "github_issues:acme/testrepo")
        finally:
            conn.close()

        assert state is not None
        # Cursor should be the max updatedAt among the two mock issues.
        assert state["last_updated_at"] == "2024-01-16T12:00:00Z"

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

    def test_initial_walk_supersedes_on_body_change(self, github_source, db_path):
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


def test_search_documents_sanitizes_fts_query(db_path):
    # hyphenated/operator-ish terms must not crash FTS5 (regression).
    store_document(
        db_path, source_type="docs", source_url="docs:x.md",
        content="the db-per-domain design splits stores", title="db-per-domain",
    )
    hits = search_documents(db_path, "db-per-domain stores")
    assert len(hits) == 1
