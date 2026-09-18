"""--version can identify a build installed from a sha (techne#965).

`techne --version` reported 0.16.1 on a build installed from e6dec9a carrying
seven merged PRs, two schema migrations and new CLI surface. We tell external
consumers to install from a sha, so this is the one surface that has to answer
"which build am I on" for them -- and it could not. Grip identified their build
by probing for features instead.

`__version__` names the last RELEASE because it resolves through installed
distribution metadata. The revision is the finer identifier, and for an
INSTALLED build it was reported as None, because `_revision_info` reads git and
an installed build has no checkout.

PEP 610 closes that: `pip install "techne @ git+...@<sha>"` writes
`direct_url.json` into the dist-info carrying `vcs_info.commit_id` -- the
RESOLVED sha, not just the ref asked for. Confirmed against a real install:

    {"url": "...", "vcs_info": {"commit_id": "21813c16ed8be...",
     "requested_revision": "21813c1...", "vcs": "git"}}
"""

from __future__ import annotations

import json

import pytest

from techne.cli import _version_string
from techne.core.health import _installed_revision, _revision_info


class _Dist:
    def __init__(self, payload):
        self._payload = payload

    def read_text(self, name):
        assert name == "direct_url.json"
        return self._payload


def _patch_dist(monkeypatch, payload):
    monkeypatch.setattr("importlib.metadata.distribution", lambda _n: _Dist(payload), raising=True)


def test_a_vcs_install_reports_its_commit(monkeypatch):
    """THE REGRESSION. The shape pip actually writes."""
    _patch_dist(
        monkeypatch,
        json.dumps(
            {
                "url": "https://github.com/tenishai/techne.git",
                "vcs_info": {
                    "commit_id": "21813c16ed8be444dd85814de77a785db62f911a",
                    "requested_revision": "21813c16ed8be444dd85814de77a785db62f911a",
                    "vcs": "git",
                },
            }
        ),
    )
    assert _installed_revision() == "21813c1"


def test_the_resolved_commit_wins_over_the_requested_ref(monkeypatch):
    """A tag or branch install must report the SHA it resolved to, not the name.

    `requested_revision` can be "main" or "v0.16.1", which answers a different
    question -- what was asked for, not what is running.
    """
    _patch_dist(
        monkeypatch,
        json.dumps(
            {
                "url": "https://github.com/tenishai/techne.git",
                "vcs_info": {
                    "commit_id": "abcdef1234567890abcdef1234567890abcdef12",
                    "requested_revision": "main",
                    "vcs": "git",
                },
            }
        ),
    )
    assert _installed_revision() == "abcdef1"


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"dir_info": {"editable": True}, "url": "file:///x"}),  # editable
        json.dumps({"url": "https://files.pythonhosted.org/x.whl"}),  # a wheel
        json.dumps({"url": "x", "vcs_info": {"vcs": "git"}}),  # no commit_id
        "",  # empty metadata
        "{not json",  # malformed
    ],
)
def test_absent_provenance_is_none_not_an_error(monkeypatch, payload):
    """Unknowable is reported as unknowable, and never raises into a health read."""
    _patch_dist(monkeypatch, payload)
    assert _installed_revision() is None


def test_a_missing_distribution_is_none(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def _boom(_n):
        raise PackageNotFoundError("techne")

    monkeypatch.setattr("importlib.metadata.distribution", _boom, raising=True)
    assert _installed_revision() is None


def test_installed_build_reports_revision_without_claiming_currency(monkeypatch, tmp_path):
    """Naming the commit must NOT flip `drifted` away from None.

    The installed branch deliberately reports drifted=None because currency
    cannot be checked locally. Knowing what you were built from says nothing
    about whether something newer exists, and a False here would let a consumer
    draw the reassuring conclusion the banner explicitly refuses to draw.
    """
    monkeypatch.setattr("techne.core.health._installed_revision", lambda: "deadbee")
    info = _revision_info(tmp_path / "site-packages" / "techne")

    assert info["build"] == "installed"
    assert info["revision"] == "deadbee"
    assert info["drifted"] is None, "a revision must not be read as a currency claim"
    assert info["remote_revision"] is None


def test_version_string_carries_the_revision(monkeypatch):
    monkeypatch.setattr(
        "techne.core.health._revision_info",
        lambda _p: {"build": "installed", "revision": "21813c1"},
    )
    out = _version_string()
    assert "21813c1" in out and "+" in out, out
    # THE HEADLINE TRADE, PINNED (Prudence, #971). The ` (installed)` suffix is the
    # whole reason the string is deliberately not PEP 440, and the docstring shouts
    # about it -- but removing it left the FULL suite green, 4220 passed. The most
    # plausible future change here is someone making the string PEP 440-legal, which
    # is the one change that docstring exists to forbid. A prose warning with no test
    # behind it is the weakest possible guard.
    assert "(installed)" in out, (
        "the build word is gone. It is what makes this string deliberately "
        "non-PEP-440; if that trade is being reversed, reverse it on purpose."
    )


def test_version_string_survives_an_unresolvable_build(monkeypatch):
    """--version must never fail, whatever the health read does."""

    def _boom(_p):
        raise RuntimeError("no")

    monkeypatch.setattr("techne.core.health._revision_info", _boom)
    assert _version_string()


def test_the_banner_names_the_commit_for_an_installed_build():
    """THE PRE-PASS FINDING: the PR claimed the banner gained this and it had not.

    `format_banner`'s installed branch built its note from a literal, so a sha
    install's banner read exactly as before. The banner is the surface an external
    consumer actually reads, which is the whole point of techne#965.
    """
    from techne.core.health import format_banner

    payload = {
        "build": "installed",
        "revision": "abc1234",
        "version": "0.16.1",
        "drifted": None,
        "remote_revision": None,
        "branch": None,
        "remote_ref_age_days": None,
        "db_path": "/tmp/x.db",
        "db_ok": True,
        "observations": 0,
        "threads": 0,
        "knowledge": 0,
        "sessions": 0,
        "identity": 0,
        "outbox_pending": 0,
        "active_threads": 0,
    }
    assert "abc1234" in format_banner(payload)
    assert "abc1234" not in format_banner({**payload, "revision": None})


def test_a_degraded_version_does_not_grow_a_second_local_label():
    """`0.0.0+unknown` already carries a local label; appending another is illegal.

    packaging.version rejects `0.0.0+unknown+21813c1`.
    """
    import techne.cli as cli_mod

    original = cli_mod.__version__
    try:
        cli_mod.__version__ = "0.0.0+unknown"
        cli_mod.__dict__["__version__"] = "0.0.0+unknown"
        out = cli_mod._version_string()
    finally:
        cli_mod.__dict__["__version__"] = original
    assert "+unknown+" not in out, f"two local labels concatenated: {out!r}"


@pytest.mark.parametrize(
    "commit,expected",
    [
        ("21813c16ed8be444dd85814de77a785db62f911a", "21813c1"),
        ("main", None),
        ("v0.16.1", None),
        ("abc", None),
        (12345678, None),
        (None, None),
    ],
)
def test_a_non_sha_commit_id_is_not_dressed_up_as_one(monkeypatch, commit, expected):
    """`str(commit)[:7]` printed "main" and "1234567" as if they were shas.

    pip writes a hash per PEP 610, so a non-hex value means the metadata is not
    what we think it is. Report unknown rather than something that reads like a
    commit.
    """
    payload = json.dumps({"url": "x", "vcs_info": {"commit_id": commit}})
    _patch_dist(monkeypatch, payload)
    assert _installed_revision() == expected
