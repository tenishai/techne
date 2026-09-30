"""#1124: the install-the-extra hint matches how techne was installed.

An editable install was told to `pipx install --force ...`, which put a second,
older, non-editable copy beside the checkout being developed.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.core import capabilities as caps
from techne.core.capabilities import install_command_for

GIT = '"techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.17.0"'


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
