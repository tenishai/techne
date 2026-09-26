"""Tests for the file-watch ingest pipeline (techne#12).

Unit-level tests cover the glob matcher, the Debouncer, and the
ingest_file / run_full_walk pieces — all of those work without
filesystem events.

The full event loop is exercised in test_watch_full_loop using
watchdog's PollingObserver against a tmp_path. PollingObserver is
deterministic enough for tests (no OS-level notification race) and
ships in the watchdog wheel by default.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from techne.core.database import init_db
from techne.core.state import get_observations, start_session
from techne.watch import (
    Debouncer,
    WatchConfig,
    ingest_file,
    matches,
    run_full_walk,
)

# ---- matches() ------------------------------------------------------------


class TestMatches:
    def test_default_includes_accepts_everything(self) -> None:
        assert matches(Path("foo.py"), ["*"], [])

    def test_basename_glob(self) -> None:
        assert matches(Path("foo.py"), ["*.py"], [])
        assert not matches(Path("foo.py"), ["*.md"], [])

    def test_path_glob_matches_via_full_posix(self) -> None:
        # Path globs use POSIX-style separators
        assert matches(Path("src/auth/jwt.py"), ["**/auth/**"], [])

    def test_exclude_overrides_include(self) -> None:
        # fnmatch matches against full POSIX path — `**/.git/**` requires
        # something before `.git/`, which is what rglob produces in practice
        # (e.g. /repo/.git/index). The test_run_full_walk_ingests_matching_files
        # test confirms the realistic file-tree walk path.
        assert not matches(Path("/repo/.git/index"), ["*"], ["**/.git/**"])

    def test_exclude_basename(self) -> None:
        assert not matches(Path("README.md"), ["*"], ["README.md"])

    def test_multiple_includes_any_match(self) -> None:
        assert matches(Path("foo.py"), ["*.py", "*.md"], [])
        assert matches(Path("foo.md"), ["*.py", "*.md"], [])


# ---- Debouncer ------------------------------------------------------------


class _FakeTimer:
    """A threading.Timer stand-in that only moves when a test says so (techne#593).

    Same call shape as threading.Timer so the seam cannot silently diverge:
    constructed with (function, args), then `start()` / `cancel()` / `elapse()`.
    """

    def __init__(self, fn, args) -> None:
        self.fn = fn
        self.args = args
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def elapse(self) -> None:
        """Fire as a real timer would when its interval expires."""
        if not self.cancelled:
            self.fn(*self.args)


def _record(bucket, timer):
    bucket.append(timer)
    return timer


class TestDebouncer:
    def test_fires_after_delay(self) -> None:
        seen: list[Path] = []
        d = Debouncer(lambda p: seen.append(p), delay_ms=50)
        d.schedule(Path("/tmp/x"))
        # Initially no fire
        assert seen == []
        # Wait for the timer to elapse
        time.sleep(0.1)
        assert seen == [Path("/tmp/x")]

    def test_coalesces_rapid_schedules_on_same_key(self) -> None:
        """Three schedule() calls in the window → callback fires ONCE.

        DETERMINISTIC SINCE techne#593. This asserted a wall-clock race: three
        calls separated by 10ms sleeps coalesce only if every sleep returns
        inside the 50ms window — a 5x margin, unguarded, where falsifying the
        PRECONDITION is indistinguishable from the Debouncer genuinely failing to
        coalesce. That is the house rule "a guard's precondition is a way to
        switch the guard off", in a test rather than in production code.

        Observed, not theorised: this failed on one CI runner during the
        2026-08-16 fleet migration — asserted one event, saw two — and passed on
        the other two at the same commit. The fleet move did not fix it, it
        only stopped triggering it.

        Widening the delay was rejected: it buys headroom, keeps the race, and
        re-tunes a number against one machine (#543). A fake timer removes time
        from the assertion entirely.
        """
        seen: list[Path] = []
        fired: list[_FakeTimer] = []
        d = Debouncer(
            lambda p: seen.append(p),
            delay_ms=50,
            timer_factory=lambda interval, fn, args: _record(fired, _FakeTimer(fn, args)),
        )

        d.schedule(Path("/tmp/x"))
        d.schedule(Path("/tmp/x"))  # cancel + reschedule
        d.schedule(Path("/tmp/x"))

        assert [t.cancelled for t in fired] == [True, True, False], (
            "each schedule() must cancel the pending timer for that key"
        )
        assert seen == [], "nothing fires until a timer elapses"

        fired[-1].elapse()  # only the surviving timer runs
        assert seen == [Path("/tmp/x")]

    def test_coalescing_still_holds_against_the_real_clock(self) -> None:
        """A smoke test over the REAL threading.Timer, so the fake cannot drift
        from it (techne#593). Deliberately generous — it exists to catch a seam
        that stopped matching production, not to pin the coalescing window."""
        # WINDOW SET BY SCHEDULING NOISE, NOT BY AMBITION. At `delay_ms=50` this required
        # three CONSECUTIVE STATEMENTS to complete within 50ms of each other, and a loaded
        # runner under `-n 4` does not guarantee that — it failed on 3.12 while 3.13 passed,
        # on a PR whose diff was AGENTS.md and one unrelated test file. Measured: coalescing
        # holds at gaps of 0/10/30ms and breaks at 60ms, so the old margin sat right at the
        # noise floor.
        #
        # Widening costs about a second and loses nothing this test is for: the docstring
        # says it exists to catch a seam that stopped matching production, NOT to pin the
        # coalescing window. Coalescing genuinely broken still fires three times and still
        # fails, which is verified rather than assumed.
        seen: list[Path] = []
        d = Debouncer(lambda p: seen.append(p), delay_ms=500)
        d.schedule(Path("/tmp/x"))
        d.schedule(Path("/tmp/x"))
        d.schedule(Path("/tmp/x"))
        time.sleep(1.5)
        assert seen == [Path("/tmp/x")]

    def test_different_keys_fire_independently(self) -> None:
        seen: list[Path] = []
        d = Debouncer(lambda p: seen.append(p), delay_ms=50)
        d.schedule(Path("/tmp/x"))
        d.schedule(Path("/tmp/y"))
        time.sleep(0.1)
        assert set(seen) == {Path("/tmp/x"), Path("/tmp/y")}

    def test_flush_cancels_pending(self) -> None:
        seen: list[Path] = []
        d = Debouncer(lambda p: seen.append(p), delay_ms=200)
        d.schedule(Path("/tmp/x"))
        d.flush()
        time.sleep(0.3)  # well past the delay
        assert seen == []

    def test_callback_exception_does_not_break_debouncer(self) -> None:
        """A bad callback must not propagate or leave the debouncer broken."""

        def boom(_p):
            raise RuntimeError("kaboom")

        d = Debouncer(boom, delay_ms=10)
        d.schedule(Path("/tmp/x"))  # should not raise
        time.sleep(0.05)
        # Debouncer should still work on subsequent calls
        seen: list[Path] = []
        d2 = Debouncer(lambda p: seen.append(p), delay_ms=10)
        d2.schedule(Path("/tmp/y"))
        time.sleep(0.05)
        assert seen == [Path("/tmp/y")]


# ---- ingest_file + run_full_walk -----------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "watch_test.db"
    init_db(p).close()
    return p


@pytest.fixture
def session_id(db_path: Path) -> str:
    return start_session(db_path, project_path="/test", agent_identity="watch-test")


def test_ingest_file_adds_observation(tmp_path, db_path, session_id) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("contents")
    ok = ingest_file(db_path, session_id, target)
    assert ok is True

    obs = get_observations(db_path, session_id)
    assert len(obs) == 1
    assert obs[0]["tool_name"] == "FileSync"
    assert f"path={target}" in obs[0]["content"]
    assert "size=" in obs[0]["content"]
    assert "mtime=" in obs[0]["content"]


def test_ingest_file_returns_false_for_missing_file(tmp_path, db_path, session_id) -> None:
    target = tmp_path / "does-not-exist"
    ok = ingest_file(db_path, session_id, target)
    assert ok is False
    assert get_observations(db_path, session_id) == []


def test_run_full_walk_ingests_matching_files(tmp_path, db_path, session_id) -> None:
    # Walk a subdirectory this test OWNS, not `tmp_path` itself. The autouse
    # fixtures in conftest.py carve `_host` / `_install_root` out of every
    # test's `tmp_path`, so a recursive walk rooted at `tmp_path` would traverse
    # them. They are empty today, which is why this passed either way — but
    # `_isolate_install_root` already pins PREFLIGHT_GUARD to
    # `_install_root/preflight_guard.py`, so the day anything creates that file
    # the count below silently becomes 3. Rooting the walk here makes the
    # invariant structural again instead of contingent (techne#466 review).
    #
    # Layout:
    #   tmp_path/src/
    #     keep.py
    #     skip.txt
    #     nested/keep.py
    #     .git/index           (excluded via glob)
    root = tmp_path / "src"
    root.mkdir()
    (root / "keep.py").write_text("a")
    (root / "skip.txt").write_text("b")
    (root / "nested").mkdir()
    (root / "nested" / "keep.py").write_text("c")
    (root / ".git").mkdir()
    (root / ".git" / "index").write_text("d")

    config = WatchConfig(
        db_path=db_path,
        paths=[root],
        session_id=session_id,
        include_globs=["*.py"],
        exclude_globs=["**/.git/**"],
    )
    count = run_full_walk(config)
    assert count == 2  # keep.py + nested/keep.py
    obs = get_observations(db_path, session_id)
    assert {o["tool_name"] for o in obs} == {"FileSync"}
    paths_seen = {
        line.split("=", 1)[1]
        for o in obs
        for line in o["content"].splitlines()
        if line.startswith("path=")
    }
    assert any("nested/keep.py" in p for p in paths_seen)
    assert not any(".git" in p for p in paths_seen)


def test_run_full_walk_handles_missing_root_silently(tmp_path, db_path, session_id) -> None:
    config = WatchConfig(
        db_path=db_path,
        paths=[tmp_path / "does-not-exist"],
        session_id=session_id,
    )
    assert run_full_walk(config) == 0


def test_run_full_walk_accepts_single_file_path(tmp_path, db_path, session_id) -> None:
    target = tmp_path / "single.md"
    target.write_text("hi")
    config = WatchConfig(
        db_path=db_path,
        paths=[target],
        session_id=session_id,
        include_globs=["*.md"],
    )
    assert run_full_walk(config) == 1


# ---- full event-loop smoke test ------------------------------------------


def test_watch_loop_with_polling_observer(tmp_path, db_path, session_id) -> None:
    """End-to-end smoke: spin up the watcher in a background thread,
    create a file, wait for the debounced ingest to land in the DB."""
    pytest.importorskip("watchdog.observers")
    polling = pytest.importorskip("watchdog.observers.polling")

    from techne.watch import watch

    config = WatchConfig(
        db_path=db_path,
        paths=[tmp_path],
        session_id=session_id,
        include_globs=["*.txt"],
        debounce_ms=50,
        resync_interval_seconds=3600,
    )

    stop_called = threading.Event()

    def runner() -> None:
        try:
            watch(
                config,
                observer_cls=polling.PollingObserver,
                install_signal_handlers=False,
            )
        finally:
            stop_called.set()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # Give the watcher a moment to do its initial full walk and arm watchers.
    time.sleep(0.2)

    target = tmp_path / "hello.txt"
    target.write_text("from watcher test")

    # Poll the DB until the observation lands or timeout.
    deadline = time.time() + 4.0
    while time.time() < deadline:
        obs = get_observations(db_path, session_id)
        if any(o["tool_name"] == "FileSync" and "hello.txt" in o["content"] for o in obs):
            break
        time.sleep(0.05)
    else:
        pytest.fail("file event did not produce an observation within deadline")

    # Shut down the observer cleanly.
    # We need to access the observer from inside watch() to stop it; instead
    # we rely on the test exiting (daemon thread). For clean shutdown, the
    # watch() public API takes signal handlers — we passed install=False so
    # signals are not wired. The polling observer will be GC'd with the thread.


# ---- EX_CONFIG exit contract (techne#498) ---------------------------------
#
# A missing `[watch]` extra is a configuration error, not a transient fault.
# Before #498 it surfaced as a generic exit 1, so a systemd unit with
# Restart=on-failure respawned it every 5s forever. These assert BOTH halves
# of the contract: the process emits EX_CONFIG, and the shipped unit tells the
# supervisor not to respawn on it.


def test_watch_raises_startup_config_error_when_watchdog_missing(
    monkeypatch, tmp_path, db_path, session_id
) -> None:
    """The missing-extra path must be distinguishable at the exit boundary.

    Guards the regression directly: a bare RuntimeError here is
    indistinguishable from a runtime fault, which is what produced the
    crash-loop.
    """
    import sys

    from techne.exit_codes import StartupConfigError

    # None in sys.modules makes `from watchdog.events import ...` raise
    # ImportError, simulating an install without the [watch] extra without
    # having to uninstall anything.
    monkeypatch.setitem(sys.modules, "watchdog.events", None)

    from techne.watch import watch

    config = WatchConfig(
        db_path=db_path,
        paths=[tmp_path],
        session_id=session_id,
        include_globs=["*.txt"],
        debounce_ms=50,
        resync_interval_seconds=3600,
    )

    with pytest.raises(StartupConfigError, match=r"pip install techne\[watch\]"):
        watch(config, install_signal_handlers=False)


# ---- inotify limits are EX_CONFIG too (techne#529) -------------------------
#
# watchdog 6.0 raises OSError(ENOSPC|EMFILE) from Inotify._raise_error when a
# host inotify sysctl is exhausted. That surfaces at observer.start() here (the
# emitter creates its inotify fd and watches when it starts), or at schedule()
# on an already-running observer. A restart allocates nothing new, so both must
# take the EX_CONFIG path rather than the generic one systemd respawns forever.


def _failing_observer(where: str, err: OSError):
    class _Obs:
        def schedule(self, *_a, **_k):
            if where == "schedule":
                raise err

        def start(self):
            if where == "start":
                raise err

    return _Obs


@pytest.mark.parametrize("where", ["schedule", "start"])
@pytest.mark.parametrize(
    ("err", "sysctl"),
    [
        (OSError(28, "inotify watch limit reached"), "fs.inotify.max_user_watches"),
        (OSError(24, "inotify instance limit reached"), "fs.inotify.max_user_instances"),
    ],
)
def test_inotify_exhaustion_raises_startup_config_error(
    monkeypatch, tmp_path, db_path, session_id, where, err, sysctl
) -> None:
    import errno
    import sys

    from techne.exit_codes import StartupConfigError
    from techne.watch import watch

    assert err.errno in (errno.ENOSPC, errno.EMFILE)
    monkeypatch.setattr(sys, "platform", "linux")
    config = WatchConfig(db_path=db_path, paths=[tmp_path], session_id=session_id)
    with pytest.raises(StartupConfigError, match=rf"{sysctl}=\d+.*restarting will not help"):
        watch(config, observer_cls=_failing_observer(where, err), install_signal_handlers=False)


def test_other_oserrors_and_non_linux_are_not_reclassified(
    monkeypatch, tmp_path, db_path, session_id
) -> None:
    """Only the two inotify limits, and only on Linux: EACCES is not a sysctl, and
    EMFILE from kqueue on BSD is the process fd limit, not an inotify knob."""
    import sys

    from techne.exit_codes import StartupConfigError
    from techne.watch import watch

    config = WatchConfig(db_path=db_path, paths=[tmp_path], session_id=session_id)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(PermissionError):
        watch(
            config,
            observer_cls=_failing_observer("start", PermissionError(13, "denied")),
            install_signal_handlers=False,
        )
    monkeypatch.setattr(sys, "platform", "freebsd14")
    with pytest.raises(OSError) as info:
        watch(
            config,
            observer_cls=_failing_observer("start", OSError(24, "too many open files")),
            install_signal_handlers=False,
        )
    assert not isinstance(info.value, StartupConfigError)


def test_startup_config_error_stays_a_runtime_error() -> None:
    """The compatibility half of the change.

    StartupConfigError was introduced under an existing `except RuntimeError`
    handler. If it ever stops subclassing RuntimeError, callers that only
    catch RuntimeError start leaking it — silently, since nothing else asserts
    the relationship.
    """
    from techne.exit_codes import StartupConfigError

    assert issubclass(StartupConfigError, RuntimeError)


def test_watch_cmd_exits_ex_config_on_startup_config_error(monkeypatch) -> None:
    """`techne watch` must exit 78, not 1, so the supervisor can act on it."""
    from click.testing import CliRunner

    import techne.watch as watch_mod
    from techne.cli import main
    from techne.config import Config
    from techne.core.database import init_db
    from techne.exit_codes import EX_CONFIG, StartupConfigError

    init_db(Config.load().db_path)

    def _boom(*_args, **_kwargs):
        raise StartupConfigError("watchdog is not installed; run `pip install techne[watch]`")

    monkeypatch.setattr(watch_mod, "watch", _boom)

    result = CliRunner().invoke(main, ["watch", "."])

    assert result.exit_code == EX_CONFIG, result.output
    assert "watchdog is not installed" in result.output


def _active_directives(body: str, section: str = "Service") -> dict[str, set[str]]:
    """Directives systemd would actually ACT ON in `section`, mapped to values.

    A substring search over the raw file is NOT this, and neither is a
    section-blind scan. Four states where the unit is disarmed but a looser
    check still passes, all found by review on this PR:

    * `#RestartPreventExitStatus=78` — inert to systemd, identical to `in`.
    * `"...=7" in "...=78"` — a substring cannot tell one exit code from
      another that merely starts with it.
    * the directive sitting in `[Unit]` or `[Install]` — `Restart*` are
      `[Service]`-only per systemd.service(5); elsewhere systemd logs
      "Unknown key name … ignoring" and respawns exactly as before.
    * a later `RestartPreventExitStatus=` — assigning the empty string RESETS
      the list and voids every prior assignment.

    Repetition follows systemd.syntax(7): a repeated option OVERRIDES the
    previous setting UNLESS the option is list-valued, in which case values are
    appended. Unioning everything was wrong for `Restart=`, which takes a single
    enum — appending `Restart=no` after `Restart=on-failure` left the stale
    `on-failure` in the set and passed, while systemd reads `no` and the unit
    never restarts at all, making RestartPreventExitStatus decorative. The
    empty-value reset is correct for both kinds.

    Still a test helper, not a systemd parser: it does not handle line
    continuations, quoting, or `.d/` drop-in directories. Indented directives
    ARE accepted, because systemd accepts them.
    """
    #: Directives this helper is asserted against that systemd treats as lists.
    #: Extend when a new list-valued directive is asserted on; anything absent
    #: is single-valued and last-assignment-wins.
    list_valued = {"RestartPreventExitStatus"}
    out: dict[str, set[str]] = {}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line[0] == "[":
            current = line.strip("[]").strip()
            continue
        if current != section:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not value.strip():
            out[key] = set()
        elif key in list_valued:
            out.setdefault(key, set()).update(value.split())
        else:
            out[key] = set(value.split())
    return out


def test_shipped_unit_refuses_to_respawn_on_ex_config() -> None:
    """The unit half of the contract, derived AND parsed like systemd reads it.

    Deriving the value from techne/exit_codes.py stops the unit's 78 becoming a
    second copy of the constant. Deriving alone was not enough: the assertion
    has to MATCH the way systemd matches, or the derivation binds nothing.

    Fails if the directive is deleted, commented out, given a different code,
    moved out of [Service] into a section where systemd ignores it, voided by a
    later empty-value reset, or if EX_CONFIG changes without the unit following.
    An indented directive still passes, because systemd accepts those.
    """
    from techne.exit_codes import EX_CONFIG

    unit = Path(__file__).resolve().parents[1] / "deploy" / "techne-watch.service"
    directives = _active_directives(unit.read_text())

    assert str(EX_CONFIG) in directives.get("RestartPreventExitStatus", set()), (
        "the shipped unit does not prevent restart on the code the app exits with; "
        "one half of the contract without the other still crash-loops"
    )
    # The setting is inert unless the unit actually restarts on failure.
    assert "on-failure" in directives.get("Restart", set())


def test_real_process_exits_ex_config(tmp_path) -> None:
    """The claim the PR body made, now actually shipped as a test.

    Every other test here reads `CliRunner().invoke(...).exit_code`, which is
    click's own accounting rather than an OS process status. Nothing asserted
    that the process really LEAVES 78 — which is the only number systemd ever
    sees. Reviewed as "believed correct by reading, not by running"; this runs
    it. `watchdog.events` is poisoned in the child so the missing-extra path is
    reached without uninstalling anything.
    """
    import os
    import subprocess
    import sys

    from techne.exit_codes import EX_CONFIG

    env = {**os.environ, "TECHNE_HOME": str(tmp_path)}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['watchdog.events'] = None;"
            " from techne.cli import main; main()",
            "watch",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        # PINNED: `python -c` puts cwd at sys.path[0], so an inherited cwd
        # decides WHICH copy of techne this proves. From the repo root that is
        # the working tree; from anywhere else it is whatever is importable.
        cwd=Path(__file__).resolve().parents[1],
    )

    assert proc.returncode == EX_CONFIG, (proc.returncode, proc.stderr[-2000:])
    assert "watchdog is not installed" in proc.stderr
