"""JsonlTranscriptSource — SourceAdapter for continuous Claude Code transcript indexing.

Composes three existing primitives:
  - JSONL parsing from techne.platforms.claude_code.transcript_import
  - OS-level change detection + debounce from techne.watch
  - Per-file byte-offset cursor from techne.core.extension_state

Observations emitted here are tagged in the content header:
    kind:transcript role:<role> session:<id> project:<dir>

Tags live in the content string (FTS5-indexed) rather than a separate column
because the observations table has no tags column. This matches the pattern
used by watch.py (path=, size=, mtime= headers).

Initial walk: scans all configured JSONL dirs, emits one observation per record.
Continuous watch: tails new lines on file modification; new files start at offset 0.
Idempotency: byte-offset cursors in extension_state prevent double-indexing.

Out of scope (deferred):
  - Cross-platform transcript adapters (openwebui, iPhone Claude.app)
  - Real-time mid-turn streaming
  - Embedding at ingest
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from techne.core.database import connect
from techne.core.extension_state import get_state, set_state
from techne.core.redaction import redact_credentials
from techne.hooks.post_tool_use import summarize
from techne.platforms.claude_code.transcript_import import (
    _content_blocks,
    _extract_user_text,
    _should_import,
    _tool_result_text,
)
from techne.sync.adapter import SourceAdapter  # noqa: F401 — re-exported for callers
from techne.watch import Debouncer, matches

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class JsonlTranscriptConfig:
    """Parsed config for one JsonlTranscriptSource instance."""

    def __init__(
        self,
        paths: list[str | Path],
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        debounce_ms: int = 500,
        adapter_name: str = "jsonl_transcript",
    ) -> None:
        self.paths = [Path(p).expanduser() for p in paths]
        self.include_globs = include_globs or ["*.jsonl"]
        self.exclude_globs = exclude_globs or []
        self.debounce_ms = debounce_ms
        self.adapter_name = adapter_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_from_path(jsonl_path: Path) -> str:
    """Derive project slug from the JSONL file's parent directory name."""
    return jsonl_path.parent.name


def _session_from_path(jsonl_path: Path) -> str:
    """Session ID from filename stem (fallback; record field is preferred)."""
    return jsonl_path.stem


def _tag_header(kind: str, role: str, session: str, project: str) -> str:
    return f"kind:{kind} role:{role} session:{session} project:{project}"


def _ingest_records_from_offset(
    db_path: Path,
    jsonl_path: Path,
    start_byte: int,
    adapter_name: str,
) -> tuple[int, int]:
    """Read new records from *jsonl_path* starting at *start_byte*.

    Emits observations into the DB. Returns (new_byte_offset, obs_count).
    Session and project are derived from path when not in the record.
    """
    session_id = _session_from_path(jsonl_path)
    project = _project_from_path(jsonl_path)

    obs_count = 0
    pending_tool_uses: dict[str, dict] = {}

    try:
        stat = jsonl_path.stat()
    except OSError:
        return start_byte, 0

    if stat.st_size <= start_byte:
        # No new bytes.
        return start_byte, 0

    conn = connect(db_path)
    try:
        # Ensure session row exists so observations can reference it.
        conn.execute(
            """INSERT OR IGNORE INTO sessions (id, project_path, platform)
               VALUES (?, ?, 'claude-code')""",
            (session_id, str(jsonl_path.parent)),
        )
        conn.commit()

        with jsonl_path.open("rb") as fh:
            fh.seek(start_byte)
            new_offset = start_byte

            for raw_line in fh:
                new_offset += len(raw_line)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                import json  # local to avoid top-level cost

                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Prefer record-level session/cwd when present.
                rec_sid = rec.get("sessionId")
                if rec_sid:
                    session_id = rec_sid
                rec_cwd = rec.get("cwd")
                if rec_cwd:
                    project = Path(rec_cwd).name

                rtype = rec.get("type")
                ts = rec.get("timestamp")
                blocks = _content_blocks(rec)

                if rtype == "assistant":
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            pending_tool_uses[b.get("id", "")] = {
                                "name": b.get("name", ""),
                                "input": b.get("input") or {},
                                "ts": ts,
                                "session_id": session_id,
                            }
                    text = _extract_user_text(blocks)
                    if text:
                        header = _tag_header("transcript", "assistant", session_id, project)
                        content = redact_credentials(f"{header}\n{text[:4000]}")
                        _ensure_session(conn, session_id, jsonl_path)
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at)
                               VALUES (?, ?, 'AssistantText', ?, 'hot', ?)""",
                            (session_id, ts, content, ts),
                        )
                        obs_count += 1

                elif rtype == "user":
                    for b in blocks:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_result":
                            tu = pending_tool_uses.pop(b.get("tool_use_id", ""), None)
                            if not tu:
                                continue
                            tool_name = tu["name"]
                            if not _should_import(tool_name):
                                continue
                            output = _tool_result_text(b.get("content"))
                            body = summarize(tool_name, tu["input"], output)
                            header = _tag_header(
                                "transcript", "tool_result", tu["session_id"], project
                            )
                            content = redact_credentials(f"{header}\n{body}")
                            _ensure_session(conn, tu["session_id"], jsonl_path)
                            event_ts = tu["ts"] or ts
                            conn.execute(
                                """INSERT INTO observations
                                   (session_id, timestamp, tool_name, content, tier, event_at)
                                   VALUES (?, ?, ?, ?, 'hot', ?)""",
                                (tu["session_id"], event_ts, tool_name, content, event_ts),
                            )
                            obs_count += 1

                    # User text (prompts)
                    text = _extract_user_text(blocks)
                    if text and not text.startswith("<"):
                        header = _tag_header("transcript", "user", session_id, project)
                        content = redact_credentials(f"{header}\n{text[:4000]}")
                        _ensure_session(conn, session_id, jsonl_path)
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at)
                               VALUES (?, ?, 'UserPrompt', ?, 'hot', ?)""",
                            (session_id, ts, content, ts),
                        )
                        obs_count += 1

        conn.commit()
        return new_offset, obs_count

    except Exception:  # noqa: BLE001
        log.exception("ingest_failed path=%s", jsonl_path)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return start_byte, 0
    finally:
        conn.close()


def _ensure_session(conn, session_id: str, jsonl_path: Path) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO sessions (id, project_path, platform)
           VALUES (?, ?, 'claude-code')""",
        (session_id, str(jsonl_path.parent)),
    )


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class JsonlTranscriptSource:
    """SourceAdapter that indexes Claude Code JSONL transcripts.

    Satisfies the SourceAdapter Protocol defined above.
    """

    def __init__(self, db_path: Path, config: JsonlTranscriptConfig) -> None:
        self._db_path = db_path
        self._cfg = config
        self._debouncer: Debouncer | None = None
        self._observer = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Cursor helpers (extension_state)
    # ------------------------------------------------------------------

    def _load_cursors(self) -> dict[str, dict]:
        """Return {file_path: {offset, inode}} dict from extension_state."""
        conn = connect(self._db_path)
        try:
            state = get_state(conn, self._cfg.adapter_name) or {}
            return state.get("cursors", {})
        finally:
            conn.close()

    def _save_cursors(self, cursors: dict[str, dict]) -> None:
        conn = connect(self._db_path)
        try:
            state = get_state(conn, self._cfg.adapter_name) or {}
            state["cursors"] = cursors
            set_state(conn, self._cfg.adapter_name, state)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Core ingest
    # ------------------------------------------------------------------

    def _ingest_file(self, path: Path, cursors: dict) -> tuple[dict, int]:
        """Ingest new records from *path*, updating *cursors* in place.

        Returns (updated_cursors, obs_count).
        Handles rotation: if inode changed, restart from byte 0.
        """
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return cursors, 0

        cur = cursors.get(key, {})
        saved_inode = cur.get("inode")
        saved_offset = cur.get("offset", 0)

        # Rotation: new inode means the file was replaced (new session).
        if saved_inode is not None and saved_inode != st.st_ino:
            log.info(
                "jsonl_rotated path=%s old_inode=%d new_inode=%d", path, saved_inode, st.st_ino
            )
            saved_offset = 0

        new_offset, count = _ingest_records_from_offset(
            self._db_path, path, saved_offset, self._cfg.adapter_name
        )
        cursors[key] = {"offset": new_offset, "inode": st.st_ino}
        return cursors, count

    def _walk_and_ingest(self) -> int:
        """Scan all configured paths; ingest new records. Returns total obs count."""
        cursors = self._load_cursors()
        total = 0
        for root in self._cfg.paths:
            if not root.exists():
                continue
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                if not matches(p, self._cfg.include_globs, self._cfg.exclude_globs):
                    continue
                cursors, count = self._ingest_file(p, cursors)
                total += count
                if count:
                    log.debug("jsonl_ingest path=%s obs=%d", p, count)
        self._save_cursors(cursors)
        return total

    # ------------------------------------------------------------------
    # SourceAdapter Protocol
    # ------------------------------------------------------------------

    def initial_walk(self) -> int:
        """Scan all configured JSONL dirs; emit observations for new records."""
        count = self._walk_and_ingest()
        log.info("jsonl_initial_walk obs=%d", count)
        return count

    def start_watch(self) -> None:
        """Start OS-level watcher. Non-blocking."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise RuntimeError(
                "watchdog is not installed; run `pip install techne[watch]`"
            ) from exc

        def _on_change(path: Path) -> None:
            with self._lock:
                cursors = self._load_cursors()
                cursors, count = self._ingest_file(path, cursors)
                self._save_cursors(cursors)
                if count:
                    log.info("jsonl_watch_ingest path=%s obs=%d", path, count)

        self._debouncer = Debouncer(_on_change, self._cfg.debounce_ms)
        debouncer = self._debouncer
        include = self._cfg.include_globs
        exclude = self._cfg.exclude_globs

        class _Handler(FileSystemEventHandler):
            def on_created(self, event) -> None:
                if not event.is_directory:
                    p = Path(event.src_path)
                    if matches(p, include, exclude):
                        debouncer.schedule(p)

            def on_modified(self, event) -> None:
                if not event.is_directory:
                    p = Path(event.src_path)
                    if matches(p, include, exclude):
                        debouncer.schedule(p)

        observer = Observer()
        for root in self._cfg.paths:
            if root.exists():
                observer.schedule(_Handler(), str(root), recursive=True)
        self._observer = observer
        observer.start()
        log.info("jsonl_watch_started paths=%d", len(self._cfg.paths))

    def stop_watch(self) -> None:
        if self._debouncer:
            self._debouncer.flush()
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        log.info("jsonl_watch_stopped")

    def health(self) -> bool:
        return any(p.exists() for p in self._cfg.paths)
