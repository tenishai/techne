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


def test_version_string_survives_an_unresolvable_build(monkeypatch):
    """--version must never fail, whatever the health read does."""

    def _boom(_p):
        raise RuntimeError("no")

    monkeypatch.setattr("techne.core.health._revision_info", _boom)
    assert _version_string()
