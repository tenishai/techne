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
    all. The banner's deferred count dropped from 2 to 1 and nothing else complained."""
    from techne.core.checks import deferred_check_names, registered_checks

    names = [f.__name__ for f in registered_checks()]
    assert names.count("check_runtime_loaded") == 1
    assert set(deferred_check_names()) >= {"check_runtime_loaded", "check_runtime_revision"}


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
