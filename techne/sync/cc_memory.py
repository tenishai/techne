"""CCMemorySource — SourceAdapter that drains CC's curated memory files into techne.db.

Walks ``~/.claude/projects/<slug>/memory/*.md`` via ``find_curated_memory_files`` and
ingests each one via ``ingest_memory_file`` (content-hash deduped, superseding on change).

This is the persistent drain side of techne#107 PR2.  The raw tool-call LEDGER
(observations table) is populated by the ``post_tool_use`` hook -- a separate pipeline;
this adapter provides ONLY the ``curated`` band.

Poll loop (start_watch / stop_watch) is a documented no-op for v1 -- the same pattern
as GitHubIssuesSource.  A real watch would call initial_walk() on an interval.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from techne._log import get_logger
from techne.config import Config
from techne.core.curated import ingest_memory_file
from techne.core.memory_doctor import find_curated_memory_files
from techne.platforms.claude_code.paths import memory_project_slug

log = get_logger(__name__)


class CCMemorySource:
    """SourceAdapter that drains CC curated memory files into the ``curated_memory`` table.

    Provides the ``curated`` band only -- the raw deed-ledger is the ``post_tool_use``
    hook pipeline and is NOT this adapter's responsibility.

    Usage::

        src = CCMemorySource(config, cwd=Path("/work/myproject"))
        count = src.initial_walk()  # stored (new/changed) file count

    Args:
        config: The techne Config.  Documents are written to ``config.db_path``.
        cwd:    Project root to resolve the CC slug.  Defaults to the current working
                directory at walk time when None (mirrors find_curated_memory_files).
    """

    provides: ClassVar[frozenset[str]] = frozenset({"curated"})

    def __init__(self, config: Config, cwd: Path | str | None = None) -> None:
        self._config = config
        self._cwd = Path(cwd) if cwd else None

    # ------------------------------------------------------------------
    # SourceAdapter Protocol
    # ------------------------------------------------------------------

    def initial_walk(self) -> int:
        """Discover and ingest all curated memory files for the project.

        Iterates ``find_curated_memory_files(self._cwd)``, calls
        ``ingest_memory_file`` for each, and returns the count of rows
        STORED (new inserts + supersessions).  Re-running on unchanged
        files returns 0 (all content-hash no-ops).
        """
        cwd = self._cwd or Path.cwd()
        slug = memory_project_slug(cwd)
        files = find_curated_memory_files(cwd)

        if not files:
            log.info("cc_memory_walk_empty", cwd=str(cwd), slug=slug)
            return 0

        stored = 0
        for path in files:
            _id, was_stored = ingest_memory_file(
                self._config.db_path,
                path,
                slug=slug,
                provenance="disk_walk",
            )
            if was_stored:
                stored += 1

        log.info(
            "cc_memory_walk_done",
            cwd=str(cwd),
            slug=slug,
            found=len(files),
            stored=stored,
        )
        return stored

    def start_watch(self) -> None:
        """No-op for v1.

        A real implementation would start a poll loop calling initial_walk()
        on a configurable interval to pick up memory edits between sessions.
        Deferred until the memory sync scheduler is built.
        """
        log.info("cc_memory_start_watch_noop", cwd=str(self._cwd or Path.cwd()))

    def stop_watch(self) -> None:
        """No-op for v1 (symmetric with start_watch)."""
        log.info("cc_memory_stop_watch_noop", cwd=str(self._cwd or Path.cwd()))

    def health(self) -> bool:
        """Return True iff the project's memory dir exists and is non-empty.

        Note: slug resolution is path-based -- if the cwd does not map to a CC
        project directory the slug will be wrong and health() will return False
        even though the raw path might exist.  That is the correct behavior:
        no slug -> no curated memory to drain.
        """
        cwd = self._cwd or Path.cwd()
        files = find_curated_memory_files(cwd)
        return len(files) > 0
