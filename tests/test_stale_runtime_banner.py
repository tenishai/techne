"""SessionStart tells you when a live process is holding pre-pull modules (techne#696)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from techne.core.checks import Verdict
from techne.hooks.session_start import _stale_runtime_notice


def _fake_check(verdict, detail="d", evidence="e"):
    return lambda config: SimpleNamespace(verdict=verdict, detail=detail, evidence=evidence)


@pytest.fixture
def patched(monkeypatch):
    def _apply(result_or_exc):
        import techne.core.checks as checks

        if isinstance(result_or_exc, Exception):

            def boom(config):
                raise result_or_exc

            monkeypatch.setattr(checks, "check_runtime_loaded", boom)
        else:
            monkeypatch.setattr(checks, "check_runtime_loaded", result_or_exc)

    return _apply


def test_stale_is_reported(patched):
    """The whole point: `check_runtime_loaded` has detected this since #491 and nothing
    consumed it — a detector wired to nothing (#468)."""
    patched(_fake_check(Verdict.STALE, detail="1 of 1 started before", evidence="pid 1"))
    notice = _stale_runtime_notice(object())
    assert notice is not None
    assert "1 of 1 started before" in notice
    assert "pid 1" in notice
    assert "Restart" in notice


@pytest.mark.parametrize(
    "verdict", [Verdict.LIVE, Verdict.DORMANT, Verdict.UNKNOWN, Verdict.UNOBSERVABLE]
)
def test_everything_that_is_not_stale_is_silent(patched, verdict):
    """THE LOAD-BEARING ASSERTION, and the one most likely to be lost in a refactor.

    #562 measured 74.5% of SessionStart's all-time budget going to sessions that produced
    zero observations; `capabilities.invitation` fires once per version change for the
    same reason. A line that shows up when nothing is wrong teaches the reader to skip
    the line, which costs more than never having added it — the signal it destroys is the
    stale one this exists to deliver.
    """
    patched(_fake_check(verdict))
    assert _stale_runtime_notice(object()) is None


def test_the_checks_own_caveat_survives_verbatim(patched):
    """The check says ORDERING, not tear detection, and is careful about it. Paraphrasing
    that into "your session is running old code" would put the claim-stronger-than-the-
    code defect in the banner, which is the class this repo keeps catching in prose."""
    detail = "ordering only: ... this is not a tear detector."
    patched(_fake_check(Verdict.STALE, detail=detail))
    assert detail in _stale_runtime_notice(object())


def test_a_failing_check_costs_nothing(patched):
    """A diagnostic that can take down SessionStart is worse than no diagnostic. Both
    this function and its caller swallow — belt and braces, because this runs on every
    session on the machine."""
    patched(RuntimeError("ps exploded"))
    assert _stale_runtime_notice(object()) is None
