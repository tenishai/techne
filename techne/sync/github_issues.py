"""GitHubIssuesSource — SourceAdapter that ingests GitHub issues into the Librarian.

Fetches issues via the `gh` CLI (must be authenticated) and stores each one
as a `documents` row (`source_type='github_issue'`).

db-per-domain (techne#99/#101): document rows live in **librarian.db**, reached
via `librarian_db(techne_db_path)` — the same store transcripts, markdown, and
`resolve_card` use, so GitHub-issue docs resolve through cards. The adapter takes
the *core* techne db path and derives librarian.db beside it; it does NOT
core-initialize the domain db (that pollution was the #101 bug).

Cursor (last-fetched updatedAt timestamp) is persisted in `extension_state`
under the key ``github_issues:<owner>/<repo>`` so re-walks are incremental.
`extension_state` is a core table → the cursor lives in the core techne db
(legitimate sync-cursor state), not in the document domain db.

Dedup: `source_url` (the issue HTML URL) is the dedup key. Re-walking an
already-indexed issue with unchanged body is a no-op; body changes trigger
supersession of the prior row.

Out of scope / deferred:
  - Poll loop (start_watch / stop_watch) — currently documented no-ops.
    A real watch would call initial_walk() on a configurable interval.
  - Comments ingestion (issue body only for MVP).
  - PR ingestion (separate source_type='github_pr' adapter).
  - Chunking long bodies into document_chunks rows for per-chunk vectors.
  - load_source_adapter() factory + entry-point registration.
  - CLI commands (techne library ingest github ...).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from techne._log import get_logger
from techne.config import Config
from techne.core.database import connect
from techne.core.documents import store_document
from techne.core.extension_state import get_state, set_state
from techne.core.librarian import librarian_db
from techne.core.redaction import redact_credentials

log = get_logger(__name__)

_STATE_KEY_PREFIX = "github_issues"
_DEFAULT_LIMIT = 500


class GitHubIssuesSource:
    """SourceAdapter that fetches GitHub issues into a library db.

    Usage::

        src = GitHubIssuesSource(config, owner="owner", repo="repo")
        count = src.initial_walk()  # docs land in the registry's librarian.db

    Args:
        config:   The techne Config. Documents are written to the registry-resolved
                  librarian.db (techne#102); the cursor (extension_state) is written
                  to the core techne db (`config.db_path`).
        owner:    GitHub organisation or user name.
        repo:     Repository name.
        limit:    Maximum issues to fetch per walk (default 500). Increase for
                  large repos; the gh CLI streams paginated requests internally.
    """

    def __init__(
        self,
        config: Config,
        owner: str,
        repo: str,
        limit: int = _DEFAULT_LIMIT,
    ) -> None:
        self._config = config
        self._owner = owner
        self._repo = repo
        self._limit = limit
        self._state_key = f"{_STATE_KEY_PREFIX}:{owner}/{repo}"

    @property
    def _library_db(self) -> Path:
        """The registry-resolved librarian.db — where document rows live
        (db-per-domain #99/#102), the same store cards resolve through. Accessing it
        migrates librarian.db on first use; no core schema is applied there."""
        return librarian_db(self._config)

    # ------------------------------------------------------------------
    # Cursor helpers
    # ------------------------------------------------------------------

    def _load_cursor(self) -> str | None:
        """Return the stored last-updatedAt cursor (ISO string) or None."""
        conn = connect(self._config.db_path)
        try:
            state = get_state(conn, self._state_key) or {}
            return state.get("last_updated_at")
        finally:
            conn.close()

    def _save_cursor(self, last_updated_at: str) -> None:
        conn = connect(self._config.db_path)
        try:
            state = get_state(conn, self._state_key) or {}
            state["last_updated_at"] = last_updated_at
            set_state(conn, self._state_key, state)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch_issues(self, since: str | None = None) -> list[dict]:
        """Run `gh issue list` and return parsed JSON rows.

        When *since* (an ISO updatedAt cursor) is given, restrict the fetch to
        issues updated at/after it via `--search "updated:>=<since>"` — the
        incremental path (techne#170: the cursor used to be saved but never read,
        so every walk re-fetched the entire repo). `>=` (not `>`) re-fetches the
        boundary issue, which dedups to a no-op, rather than risk missing another
        issue updated in the same second.

        Uses subprocess so tests can mock it without touching the network.
        Raises RuntimeError on non-zero exit or JSON parse failure.
        """
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{self._owner}/{self._repo}",
            "--state",
            "all",
            "--json",
            "number,title,body,url,state,labels,updatedAt",
            "--limit",
            str(self._limit),
        ]
        if since:
            cmd += ["--search", f"updated:>={since}"]
        log.debug("gh_issue_list_cmd", cmd=cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"gh issue list failed for {self._owner}/{self._repo}: {exc.stderr.strip()}"
            ) from exc

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"gh issue list returned invalid JSON for {self._owner}/{self._repo}"
            ) from exc

    # ------------------------------------------------------------------
    # SourceAdapter Protocol
    # ------------------------------------------------------------------

    def initial_walk(self) -> int:
        """Fetch all issues, store new/changed ones, advance cursor.

        Returns the count of document rows stored (new inserts + supersessions).
        Re-running on an unchanged repo returns 0 (all dedup no-ops).
        """
        # Incremental: only fetch issues updated since the saved cursor (techne#170).
        # First walk (no cursor) fetches everything; later walks fetch the delta.
        since = self._load_cursor()
        issues = self._fetch_issues(since=since)
        if not issues:
            log.info("github_issues_walk_empty", repo=f"{self._owner}/{self._repo}", since=since)
            return 0

        # librarian.db is migrated on first access (no core schema applied) —
        # resolve it once so store_document writes document rows there.
        library_db = self._library_db

        stored = 0
        max_updated_at: str | None = None

        for issue in issues:
            url: str = issue.get("url", "")
            title: str = issue.get("title", "")
            body: str = issue.get("body") or ""
            state: str = issue.get("state", "")
            labels: list[dict] = issue.get("labels", [])
            updated_at: str = issue.get("updatedAt", "")

            # Build meta as a compact JSON string — source-specific fields
            # that don't fit core document columns.
            label_names = [lbl.get("name", "") for lbl in labels]
            meta = json.dumps(
                {
                    "state": state,
                    "labels": label_names,
                    "number": issue.get("number"),
                },
                separators=(",", ":"),
            )

            _id, was_stored = store_document(
                library_db,
                source_type="github_issue",
                source_url=url,
                content=redact_credentials(body),
                title=title,
                meta=meta,
            )
            if was_stored:
                stored += 1

            # Track the max updatedAt across all fetched issues (not just stored
            # ones) so the cursor advances even when all docs were unchanged.
            if updated_at and (max_updated_at is None or updated_at > max_updated_at):
                max_updated_at = updated_at

        if max_updated_at:
            self._save_cursor(max_updated_at)

        log.info(
            "github_issues_walk_done",
            repo=f"{self._owner}/{self._repo}",
            fetched=len(issues),
            stored=stored,
            cursor=max_updated_at,
        )
        return stored

    def start_watch(self) -> None:
        """No-op for MVP.

        A real implementation would start a poll loop calling initial_walk()
        on a configurable interval (e.g. every 30 minutes). Deferred until
        the library ingest scheduler is built.
        """
        log.info(
            "github_issues_start_watch_noop",
            repo=f"{self._owner}/{self._repo}",
        )

    def stop_watch(self) -> None:
        """No-op for MVP (symmetric with start_watch)."""
        log.info(
            "github_issues_stop_watch_noop",
            repo=f"{self._owner}/{self._repo}",
        )

    def health(self) -> bool:
        """Probe gh CLI authentication status.

        Returns True iff `gh auth status` exits with code 0.
        """
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except (FileNotFoundError, OSError):
            # gh not installed
            return False
