"""SessionStart learns what a live server already knows (techne#696 part A)."""

from __future__ import annotations

import os

import pytest

from techne.core import runtime_drift as rd
from techne.core.checks import Verdict, check_runtime_torn


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    return tmp_path


def _watch():
    return rd.RuntimeWatch(baseline={"techne.core.x": 1.0}, marker_path=None, marker_at=None)


def test_the_check_is_cheap_and_registered_like_any_other():
    """THE WHOLE POINT OF PART A, and the thing #697 got wrong.

    `check_runtime_loaded` is `@expensive` because it interrogates OTHER processes —
    `ps` plus git. #697 called it from SessionStart anyway, reaching around `cheap_only`
    while the banner four lines above listed it as deferred. This check reads a file a
    server already wrote, so it must be an ordinary registered check. If it ever
    acquires `@expensive`, part A has regressed to #697.
    """
    from techne.core.checks import deferred_check_names, registered_checks

    names = [f.__name__ for f in registered_checks()]
    assert names.count("check_runtime_torn") == 1, "registered exactly once"
    assert "check_runtime_torn" not in deferred_check_names()


def test_the_expensive_checks_are_still_deferred():
    """Pins the damage I actually did while adding this: inserting between the stacked
    `@register_check` / `@expensive` decorators double-registered the new check AND
    silently stripped `check_runtime_loaded`'s registration, so it stopped running at
    all.

    "AND NOTHING ELSE COMPLAINED" IS RETRACTED — it was in this docstring, and pytest
    printed it directly above the failure of the very test it belongs to. The full suite
    fails FOUR tests on that accident, two of them pre-existing in test_runtime_loaded.py.
    The author had run one test file and generalised. See docs/arch/liveness.md."""
    from techne.core.checks import deferred_check_names, registered_checks

    names = [f.__name__ for f in registered_checks()]
    assert names.count("check_runtime_loaded") == 1
    # EXACT SET, not a superset. Prudence added a third `@expensive` check with its
    # registration stripped and got 120 passed — `>=` cannot notice an absence, which is
    # the very failure mode this test exists for.
    assert set(deferred_check_names()) == {"check_runtime_loaded", "check_runtime_revision"}


def test_silent_when_no_server_has_published(home):
    assert check_runtime_torn(None).verdict == Verdict.LIVE


def test_a_live_servers_verdict_is_surfaced_with_its_modules(home):
    """The gain over the ordering check sitting beside it: `runtime:loaded` can only say
    "a process started before the deployment landed" and says of itself that this is
    "ordering only… not a tear detector". This one names the modules."""
    rd.publish(
        _watch(), rd.Drift(torn=("techne.core.search", "techne.core.knowledge"), moved_at=1.0)
    )
    r = check_runtime_torn(None)
    assert r.verdict == Verdict.STALE
    assert "techne.core.search" in r.detail
    assert r.remedy


def test_a_dead_servers_verdict_is_reaped_not_reported(home):
    """A dead server's verdict is not a stale warning to soften — it is a FALSE one. The
    condition it described ended when the process did, and a banner that keeps naming it
    would be the noise this whole issue is about avoiding."""
    rd.publish(_watch(), rd.Drift(torn=("techne.core.x",), moved_at=1.0), pid=999999)
    assert check_runtime_torn(None).verdict == Verdict.LIVE
    assert not list((home / rd.STATE_DIRNAME).glob("*.json")), "the dead file must be reaped"


def test_a_healthy_server_publishes_nothing(home):
    """A file that exists means something is WRONG, not that a server ran. `_drift_notice`
    publishes only on a finding, so an empty directory is the healthy steady state rather
    than a state we have to interpret."""
    watch = _watch()
    rd.publish(watch, None)
    assert check_runtime_torn(None).verdict == Verdict.LIVE


def test_a_corrupt_state_file_is_reaped_rather_than_believed(home):
    """Never raise into SessionStart, and never guess at a half-written verdict."""
    d = home / rd.STATE_DIRNAME
    d.mkdir(parents=True)
    (d / "123.json").write_text("{not json")
    assert check_runtime_torn(None).verdict == Verdict.LIVE
    assert not list(d.glob("*.json"))


def test_it_reaches_the_banner_without_any_banner_code(home, tmp_path):
    """Part A's actual claim: this needed no new banner block. `collect_health` already
    runs `run_checks(cheap_only=True)` and `format_banner` already renders a liveness row
    for any verdict that is not live/dormant. #697 added a bespoke block AND an
    `@expensive` call to achieve less."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.health import collect_health, format_banner

    cfg = Config.load()
    cfg.db_path = tmp_path / "t.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path)
    rd.publish(_watch(), rd.Drift(torn=("techne.core.search",), moved_at=1.0))

    health = collect_health(cfg)
    assert health["checks_worst"] == "stale"
    assert [c for c in health["checks"] if c["name"] == "runtime:torn"]
    assert "runtime:torn" in format_banner(health)


def test_publishing_never_raises_on_an_unwritable_home(monkeypatch, tmp_path):
    """Failing to publish a diagnostic must not affect serving.

    The home is made unwritable by pointing it INSIDE a regular file, so `mkdir` raises
    NotADirectoryError — a real OS error from the real call path. (A null byte in the
    env var was the first attempt and it raised in `monkeypatch.setenv` itself, testing
    pytest rather than `publish`.)
    """
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    monkeypatch.setenv("TECHNE_HOME", str(blocker / "under_a_file"))
    rd.publish(_watch(), rd.Drift(torn=("x",), moved_at=1.0))  # must not raise
    assert check_runtime_torn(None).verdict == Verdict.LIVE  # and reading is safe too


def test_the_server_publishes_only_on_a_finding(home, monkeypatch):
    """Pins the SERVER's decision, not `live_reports`' filtering.

    `test_a_healthy_server_publishes_nothing` calls `publish(watch, None)` directly, so
    it passes whether or not `_drift_notice` guards the call — mutating the guard to
    publish unconditionally left all nine green. The distinction matters because the
    docstring claims a file existing means something is WRONG rather than "a server ran";
    unconditional publishing would litter one file per process forever and make that
    sentence false.
    """
    import techne.mcp_server as ms

    monkeypatch.setattr(rd, "poll", lambda *a, **k: None)  # a healthy server
    ms._RUNTIME_WATCH = _watch()
    try:
        assert ms._drift_notice(None) is None
        assert not (home / rd.STATE_DIRNAME).exists(), (
            "a healthy server must write nothing — the presence of a state file is the "
            "signal, so writing one unconditionally destroys it"
        )
    finally:
        ms._RUNTIME_WATCH = None


# --- untrusted input: these files are written by ANOTHER process ----------------


def _write(home, name, payload):
    import json

    d = home / rd.STATE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return d


def test_a_fractional_pid_is_rejected_not_truncated(home):
    """`int(1.5)` is 1, and pid 1 (init/launchd) ALWAYS exists — so a state file claiming
    pid 1.5 read as a live server forever and produced a permanent false STALE banner.
    Measured before the fix. Reject rather than coerce."""
    _write(home, "frac", {"pid": 1.5, "torn": ["x"]})
    assert rd.live_reports() == []


def test_pid_zero_is_refused_explicitly(home):
    """`os.kill(0, sig)` addresses the caller's ENTIRE PROCESS GROUP, not one process.
    With signal 0 that is only an existence probe and harmless — but a guard that relies
    on the signal number staying 0 is a guard waiting for someone to reuse the helper."""
    killed = []
    real = os.kill
    os.kill = lambda p, s: (killed.append(p), real(p, s))[1]
    try:
        _write(home, "zero", {"pid": 0, "torn": ["x"]})
        assert rd.live_reports() == []
    finally:
        os.kill = real
    assert 0 not in killed, "pid 0 must never reach os.kill"


def test_one_malformed_file_cannot_disable_the_whole_check(home):
    """THE WORST OF THE THREE, and the reason this section exists.

    `os.kill(2**63, 0)` raises OverflowError, which is NOT an OSError. It escaped
    `_alive`, hit `live_reports`' outer handler, and returned early — so ONE junk file
    left a genuinely torn server unreported and every later file unexamined and unreaped.
    A detector that a single bad file switches off is exactly the shape #696 exists to
    avoid, reproduced inside the fix for it.

    The real report is written LAST alphabetically so it is only reachable if the scan
    survives everything before it.
    """
    for name, payload in {
        "a_corrupt": "{{{not json",
        "b_huge": {"pid": 2**63, "torn": ["x"]},
        "c_string": {"pid": "rm -rf /", "torn": ["x"]},
        "d_negative": {"pid": -1, "torn": ["x"]},
        "e_null": {"pid": None, "torn": ["x"]},
        "f_notdict": [1, 2, 3],
        "g_bool": {"pid": True, "torn": ["x"]},
    }.items():
        _write(home, name, payload)
    _write(home, "zzz_real", {"pid": os.getpid(), "torn": ["techne.core.search"]})

    reports = rd.live_reports()
    assert [r["pid"] for r in reports] == [os.getpid()], "the real report must survive"
    assert reports[0]["torn"] == ["techne.core.search"]


def test_torn_entries_that_are_not_strings_cannot_poison_the_check(home):
    """Prudence r1, Important 2 — the half my own hardening missed.

    `torn: [1, 2]` is a non-empty list, so a shape check passes it, and then
    `", ".join(...)` raises TypeError inside `check_runtime_torn` — OUTSIDE
    `live_reports`' guard. The result is a permanent ⚠️ UNKNOWN row that the check's
    own stated remedy does not clear: the never-green shape the liveness card forbids.
    """
    from techne.core.checks import check_runtime_torn

    _write(home, "poison", {"pid": os.getpid(), "torn": [1, 2]})
    r = check_runtime_torn(None)  # must not raise
    assert r.verdict == Verdict.LIVE

    # ...and a genuine report alongside it still works.
    _write(home, "zzz_good", {"pid": os.getpid(), "torn": ["techne.core.search"]})
    assert check_runtime_torn(None).verdict == Verdict.STALE


def test_a_fifo_cannot_block_the_scan(home):
    """Prudence r2, Important 3 — the CLASS, not another instance.

    Every other guard here asks what is IN the file. None asked what the file IS. A FIFO
    named `*.json` blocks `read_text()` indefinitely; the resulting `HookTimeout` is a
    RuntimeError, so the per-file handler swallows it while `setitimer` — one-shot — has
    already spent the SessionStart budget. She measured 6.0s against a 2.0s budget with
    `bounded_hook` reporting "300% — approaching timeout".
    """
    import os as _os

    d = home / rd.STATE_DIRNAME
    d.mkdir(parents=True)
    _os.mkfifo(d / "blocker.json")
    _write(home, "zzz_real", {"pid": os.getpid(), "torn": ["techne.core.search"]})

    reports = rd.live_reports()  # must not block
    assert [r["pid"] for r in reports] == [os.getpid()]


def test_a_directory_named_like_a_state_file_is_skipped(home):
    """Same guard, second shape — `read_text()` on a directory raises IsADirectoryError,
    which the per-file handler would then try to `unlink`, failing again."""
    d = home / rd.STATE_DIRNAME
    d.mkdir(parents=True)
    (d / "adir.json").mkdir()
    _write(home, "zzz_real", {"pid": os.getpid(), "torn": ["techne.core.search"]})
    assert [r["pid"] for r in rd.live_reports()] == [os.getpid()]
    assert (d / "adir.json").is_dir(), "a directory must be skipped, never unlinked"


def test_a_fresh_tmp_file_is_not_reaped_mid_publish(home):
    """Prudence r2, Important 2 — introduced by a NIT I applied, not by the hardening.

    `publish` writes `.{pid}.json.tmp` then renames. A scan landing in that window
    deleted the tmp; `replace` then raised, `publish`'s blanket except swallowed it, and
    the report was LOST — permanently, because `poll` records `_reported` before
    returning, so that deployment is never announced again. Two servers plus a
    SessionStart is ordinary operation.
    """
    d = home / rd.STATE_DIRNAME
    d.mkdir(parents=True)
    fresh = d / ".12345.json.tmp"
    fresh.write_text("{}")

    rd.live_reports()
    assert fresh.exists(), "a tmp mid-write must survive a concurrent scan"

    # ...and a genuinely old one is still reaped.
    os.utime(fresh, (0, 0))
    rd.live_reports()
    assert not fresh.exists()


def test_publish_survives_a_concurrent_scan(home):
    """The end-to-end form of the same race, driven through the real functions rather
    than an imitation of them."""
    watch = _watch()
    rd.live_reports()  # scanner runs first, directory may not exist yet
    rd.publish(watch, rd.Drift(torn=("techne.core.search",), moved_at=1.0))
    rd.live_reports()
    reports = rd.live_reports()
    assert [r["torn"] for r in reports] == [["techne.core.search"]]
