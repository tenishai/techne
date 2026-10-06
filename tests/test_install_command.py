"""#1124: the install-the-extra hint matches how techne was installed.

An editable install was told to `pipx install --force ...`, which put a second,
older, non-editable copy beside the checkout being developed.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.core import capabilities as caps
from techne.core.capabilities import install_command_for

GIT = '"tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.17.0"'


def _cmd(direct_url=None, prefix="/usr", base_prefix="/usr", executable="/usr/bin/python3"):
    return install_command_for(
        "0.17.0",
        direct_url=direct_url,
        prefix=prefix,
        base_prefix=base_prefix,
        executable=executable,
    )


def test_editable_names_the_checkout_and_its_python():
    du = {"url": "file:///home/u/src/my%20techne", "dir_info": {"editable": True}}
    out = _cmd(du, prefix="/home/u/src/my techne/.venv", executable="/home/u/src/v/bin/python")
    assert out == (
        "cd '/home/u/src/my techne' && "
        '/home/u/src/v/bin/python -m pip install -e ".[recommended]"'
    )


def test_a_non_editable_local_dir_is_not_treated_as_editable():
    du = {"url": "file:///home/u/src/techne", "dir_info": {}}
    out = _cmd(du, prefix="/home/u/venv", executable="/home/u/venv/bin/python")
    assert out == f"/home/u/venv/bin/python -m pip install {GIT}"


def test_pipx_keeps_the_pipx_form():
    out = _cmd(
        {"url": "https://github.com/tenishai/techne.git", "vcs_info": {"commit_id": "a" * 40}},
        prefix="/home/u/.local/share/pipx/venvs/techne",
    )
    assert out == f"pipx install --force {GIT}"


def test_plain_venv_uses_that_venvs_pip_and_the_git_url():
    out = _cmd(prefix="/home/u/venv", executable="/home/u/venv/bin/python")
    assert out == f"/home/u/venv/bin/python -m pip install {GIT}"


def test_uv_tool_uses_uv():
    out = _cmd(prefix="/home/u/.local/share/uv/tools/techne")
    assert out == f"uv tool install --force {GIT}"


def test_no_venv_falls_back_to_pipx():
    assert _cmd() == f"pipx install --force {GIT}"


def test_editable_dir_reads_pep610():
    assert caps._editable_dir(None) is None
    assert caps._editable_dir({"url": "file:///x", "dir_info": {"editable": False}}) is None
    assert caps._editable_dir({"url": "https://x", "dir_info": {"editable": True}}) is None
    assert caps._editable_dir({"url": "file:///x/y", "dir_info": {"editable": True}}) == "/x/y"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
    return tmp_path


def test_init_gives_an_editable_install_the_editable_command(isolated, monkeypatch):
    """The issue's reproduction, end to end through `init`."""
    monkeypatch.setattr(
        caps,
        "_install_context",
        lambda: {
            "direct_url": {"url": "file:///work/techne", "dir_info": {"editable": True}},
            "prefix": "/work/techne/.venv",
            "base_prefix": "/usr",
            "executable": "/work/techne/.venv/bin/python",
        },
    )
    result = CliRunner().invoke(main, ["init", "--platform", "standalone"])
    assert result.exit_code == 0, result.output
    want = 'cd /work/techne && /work/techne/.venv/bin/python -m pip install -e ".[recommended]"'
    assert want in result.output
    assert "pipx" not in result.output


# --- techne#1128: the REAL _install_context()/_direct_url(), not the pinned seam ---
#
# Every test above (and every hint test elsewhere in the suite) runs against
# conftest's `_pin_install_context`, which replaces `_install_context` wholesale —
# so `_direct_url()`'s own read of `direct_url.json` never runs at all. A mutant
# that read "direct-url.json" instead passed all of them. These tests opt out of
# the pin (`real_install_context`, see conftest.py) and fake only the layer below
# `_direct_url()`: `importlib.metadata.distribution`.


class _FakeDistribution:
    """Stands in for `importlib.metadata.distribution` (see `techne._distribution`).

    Real `Distribution.read_text(filename)` returns None for a dist-info file that
    isn't there; this mirrors that instead of raising, so a caller that asks for
    the wrong filename (the #1128 mutation) sees the same "not present" shape a
    real install would give it.
    """

    def __init__(self, files: dict[str, str | None]):
        self._files = files

    def read_text(self, filename: str) -> str | None:
        return self._files.get(filename)


@pytest.mark.real_install_context
def test_the_real_direct_url_reads_an_editable_payload(monkeypatch):
    payload = json.dumps({"url": "file:///work/techne", "dir_info": {"editable": True}})
    fake_dist = _FakeDistribution({"direct_url.json": payload})
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: fake_dist)
    monkeypatch.setattr("sys.prefix", "/work/techne/.venv")
    monkeypatch.setattr("sys.base_prefix", "/usr")
    monkeypatch.setattr("sys.executable", "/work/techne/.venv/bin/python")

    ctx = caps._install_context()
    assert ctx["direct_url"] == {"url": "file:///work/techne", "dir_info": {"editable": True}}

    out = install_command_for("0.17.0", **ctx)
    assert out == (
        'cd /work/techne && /work/techne/.venv/bin/python -m pip install -e ".[recommended]"'
    )


@pytest.mark.real_install_context
def test_the_real_direct_url_reads_a_non_editable_payload(monkeypatch):
    payload = json.dumps(
        {"url": "https://github.com/tenishai/techne.git", "vcs_info": {"commit_id": "a" * 40}}
    )
    fake_dist = _FakeDistribution({"direct_url.json": payload})
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: fake_dist)
    monkeypatch.setattr("sys.prefix", "/home/u/venv")
    monkeypatch.setattr("sys.base_prefix", "/usr")
    monkeypatch.setattr("sys.executable", "/home/u/venv/bin/python")

    ctx = caps._install_context()
    assert not (ctx["direct_url"].get("dir_info") or {}).get("editable")

    out = install_command_for("0.17.0", **ctx)
    assert out == f"/home/u/venv/bin/python -m pip install {GIT}"


@pytest.mark.real_install_context
def test_a_uv_created_venvs_editable_install_uses_uv_pip(monkeypatch):
    """Optional half of #1128: a uv venv has no pip, so `-m pip install -e` fails."""
    payload = json.dumps({"url": "file:///work/techne", "dir_info": {"editable": True}})
    fake_dist = _FakeDistribution({"direct_url.json": payload, "INSTALLER": "uv\n"})
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: fake_dist)
    monkeypatch.setattr("sys.prefix", "/work/techne/.venv")
    monkeypatch.setattr("sys.base_prefix", "/usr")
    monkeypatch.setattr("sys.executable", "/work/techne/.venv/bin/python")

    ctx = caps._install_context()
    assert ctx["installer"] == "uv"

    out = install_command_for("0.17.0", **ctx)
    assert out == (
        'cd /work/techne && uv pip install --python /work/techne/.venv/bin/python '
        '-e ".[recommended]"'
    )
