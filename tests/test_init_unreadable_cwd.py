"""`techne init` and the memory commands under an unreadable cwd (techne#1153).

On macOS, getcwd() fails with EACCES when an ancestor of the cwd has no read
permission, so the first Path.cwd() call raises. Linux returns the path
regardless, so the CI runners only reach these branches through the monkeypatched
tests; the real reproduction runs on macOS alone.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config


def _invoke(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


def _cwd_raises(monkeypatch):
    def _raise(cls):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "cwd", classmethod(_raise))


@pytest.mark.skipif(sys.platform != "darwin", reason="only macOS getcwd() fails here")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode 000")
def test_init_under_mode_000_parent_exits_cleanly(tmp_path, monkeypatch):
    """The issue's reproduction: chdir into a child, then lock its parent."""
    child = tmp_path / "locked" / "child"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    (tmp_path / "locked").chmod(0o000)
    try:
        with pytest.raises(OSError):
            Path.cwd()  # the precondition: without it this test proves nothing
        result = _invoke(["init", "--platform", "standalone", "--no-vectors"])
    finally:
        (tmp_path / "locked").chmod(0o755)
    assert result.exit_code == 0, result.output
    assert "working directory can't be read" in result.output


def test_init_treats_unreadable_cwd_as_no_project(monkeypatch):
    _cwd_raises(monkeypatch)
    result = _invoke(["init", "--platform", "standalone", "--no-vectors"])
    assert result.exit_code == 0, result.output
    assert "working directory can't be read" in result.output
    assert "detection skipped" in result.output
    assert "no CC curated memory detected" not in result.output
    cfg = Config.load()
    assert cfg.posture.mode == "observe"
    assert cfg.posture.cc_memory_detected is False


@pytest.mark.parametrize(
    "args", [["memory", "doctor"], ["memory", "doctor", "--claims"], ["memory", "sync"]]
)
def test_memory_commands_refuse_unreadable_cwd(monkeypatch, args):
    assert _invoke(["init", "--platform", "standalone", "--no-vectors"]).exit_code == 0
    _cwd_raises(monkeypatch)
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 1, result.output
    assert "working directory can't be read" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
