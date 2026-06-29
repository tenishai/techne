"""File-watch ingest — continuously observe a directory tree.

Operators run ``techne watch <dir>`` to keep a code corpus or docs corpus
fresh in the techne knowledge base. Each file create / modify lands as
an observation with ``tool_name="FileSync"``; a background resync task
catches any events the OS-level watcher missed.

This is the watch primitive that the future Codex persona uses to keep
its code graph current, and the Librarian filesystem-source adapter
uses to mirror local docs without polling.

Design notes:
  - ``watchdog`` is the cross-platform abstraction; pure-Python, no
    native deps required. Declared in the ``[watch]`` extra so techne
    core stays slim.
  - Per-path debounce collapses bursts of events (editor save-on-format,
    multi-file IDE refactor, ``git checkout``) into one ingest call per
    file per debounce window (default 500ms).
  - The resync loop runs periodically (default daily) doing a full
    dir-walk. Catches events the OS-level watcher dropped during high
    load, plus picks up files that existed before the watcher started.
  - The very first action a fresh watcher takes is a full walk, so
    pre-existing files don't sit invisible until the first edit.

Tests live in tests/test_watch.py — the matcher and debouncer have unit
coverage; the full event loop has a smoke test using watchdog's
PollingObserver against a tmp_path.
"""

from __future__ import annotations

import fnmatch
import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from techne.core.state import add_observation

log = logging.getLogger(__name__)


__all__ = [
    "WatchConfig",
    "Debouncer",
    "matches",
    "ingest_file",
    "run_full_walk",
    "watch",
]


@dataclass(slots=True)
class WatchConfig:
    """Operator config for a single watch invocation."""

    db_path: Path
    paths: list[Path]
    session_id: str
    include_globs: list[str] = field(default_factory=lambda: ["*"])
    exclude_globs: list[str] = field(default_factory=list)
    debounce_ms: int = 500
    resync_interval_seconds: int = 24 * 3600  # daily


# ---- glob matcher ---------------------------------------------------------


def matches(path: Path, includes: list[str], excludes: list[str]) -> bool:
    """Return True iff path passes the include + exclude glob filters.

    Includes: at least one pattern must match either the basename or the
    POSIX path. ``["*"]`` (the default) accepts everything.

    Excludes: no pattern may match. A typical exclude is
    ``["**/.git/**", "**/node_modules/**"]``.
    """
    name = path.name
    posix = path.as_posix()
    if not any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(posix, g) for g in includes):
        return False
    if any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(posix, g) for g in excludes):
        return False
    return True


# ---- debounce --------------------------------------------------------------


class Debouncer:
    """Per-key debounce — coalesces rapid events on the same key.

    Each call to ``schedule(key, payload)`` cancels any pending timer
    for ``key`` and schedules a new one ``delay_ms`` in the future. The
    callback fires with ``payload`` only when the timer elapses without
    being cancelled.

    Thread-safe — backed by stdlib ``threading.Timer`` and an internal
    ``threading.Lock``.
    """

    def __init__(self, callback: Callable[[Path], None], delay_ms: int) -> None:
        self._callback = callback
        self._delay = max(0.0, delay_ms / 1000.0)
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self, path: Path) -> None:
        key = str(path)
        with self._lock:
            existing = self._timers.get(key)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._delay, self._fire, args=(path,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, path: Path) -> None:
        with self._lock:
            self._timers.pop(str(path), None)
        try:
            self._callback(path)
        except Exception:  # noqa: BLE001
            log.exception("debounced_callback_failed path=%s", path)

    def flush(self) -> None:
        """Cancel every pending timer. Used at shutdown."""
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()


# ---- ingest ---------------------------------------------------------------


def ingest_file(db_path: Path, session_id: str, path: Path) -> bool:
    """Add a FileSync observation for one file. Returns True on success."""
    try:
        stat = path.stat()
    except OSError:
        return False
    content = f"path={path}\nsize={stat.st_size}\nmtime={stat.st_mtime:.0f}"
    try:
        add_observation(db_path, session_id, tool_name="FileSync", content=content)
    except Exception:  # noqa: BLE001
        log.exception("ingest_file_failed path=%s", path)
        return False
    return True


def run_full_walk(config: WatchConfig) -> int:
    """Walk every configured path; ingest matching files. Returns count."""
    count = 0
    for root in config.paths:
        if not root.exists():
            continue
        if root.is_file():
            if matches(root, config.include_globs, config.exclude_globs):
                if ingest_file(config.db_path, config.session_id, root):
                    count += 1
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if matches(p, config.include_globs, config.exclude_globs):
                if ingest_file(config.db_path, config.session_id, p):
                    count += 1
    return count


# ---- main event loop ------------------------------------------------------


def watch(
    config: WatchConfig,
    *,
    observer_cls=None,
    install_signal_handlers: bool = True,
) -> None:
    """Long-running watch. Returns on SIGTERM/SIGINT (or ``observer.stop()``).

    ``observer_cls`` lets tests inject ``watchdog.observers.polling.PollingObserver``
    or a mock; production uses ``watchdog.observers.Observer`` (auto-selects
    the platform-native impl).

    ``install_signal_handlers`` should be False when caller (e.g., a test)
    runs in a non-main thread where signal installation would raise.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError as exc:
        raise RuntimeError("watchdog is not installed; run `pip install techne[watch]`") from exc

    def ingest(path: Path) -> None:
        if matches(path, config.include_globs, config.exclude_globs):
            ingest_file(config.db_path, config.session_id, path)

    debouncer = Debouncer(ingest, config.debounce_ms)

    class _Handler(FileSystemEventHandler):
        def on_created(self, event) -> None:
            if not event.is_directory:
                debouncer.schedule(Path(event.src_path))

        def on_modified(self, event) -> None:
            if not event.is_directory:
                debouncer.schedule(Path(event.src_path))

    observer = (observer_cls or Observer)()
    for path in config.paths:
        if path.exists():
            target = path if path.is_dir() else path.parent
            observer.schedule(_Handler(), str(target), recursive=True)

    # Initial full walk so pre-existing files are captured before any edits.
    initial = run_full_walk(config)
    log.info("initial_walk path_count=%d files=%d", len(config.paths), initial)

    observer.start()

    stop_event = threading.Event()

    def _resync_loop() -> None:
        while not stop_event.wait(config.resync_interval_seconds):
            try:
                ingested = run_full_walk(config)
                log.info("resync_walk_completed files=%d", ingested)
            except Exception:  # noqa: BLE001
                log.exception("resync_walk_failed")

    resync_thread = threading.Thread(target=_resync_loop, daemon=True)
    resync_thread.start()

    def _shutdown(*_args) -> None:
        log.info("watcher_shutdown_requested")
        stop_event.set()
        debouncer.flush()
        observer.stop()

    if install_signal_handlers:
        try:
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)
        except ValueError:
            # Not the main thread; caller is responsible for stop signal.
            pass

    try:
        observer.join()
    finally:
        stop_event.set()
        debouncer.flush()
