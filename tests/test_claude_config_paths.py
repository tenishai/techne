"""Tests for CLAUDE_CONFIG_DIR-aware host path resolution (techne#113)."""

import os
from pathlib import Path

import pytest

from techne.platforms.claude_code.paths import (
    _git_project_root,
    claude_config_dir,
    claude_json_path,
    claude_projects_dir,
)


def test_unset_uses_default_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_config_dir() == Path.home() / ".claude"
    assert claude_json_path() == Path.home() / ".claude.json"
    assert claude_projects_dir() == Path.home() / ".claude" / "projects"


def test_env_relocates_config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "agents" / "rook" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert claude_config_dir() == cfg
    assert claude_projects_dir() == cfg / "projects"


def test_env_json_is_sibling_with_suffix(monkeypatch, tmp_path):
    """${CLAUDE_CONFIG_DIR}.json is a sibling of the dir, not a file inside it."""
    cfg = tmp_path / "agents" / "rook" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert claude_json_path() == tmp_path / "agents" / "rook" / "claude.json"


def test_env_expanduser(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/agents/rook/claude")
    assert claude_config_dir() == Path.home() / "agents" / "rook" / "claude"
    assert claude_json_path() == Path.home() / "agents" / "rook" / "claude.json"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 directory")
def test_git_root_walk_skips_an_unreadable_ancestor(tmp_path):
    """techne#1149: a cwd below a directory the user cannot read must not raise.

    `locked` is mode 000, so stat()ing anything beneath it, `locked/inner/.git`
    included, raises PermissionError. The walk must skip those levels and still
    find the repo above them."""
    locked = tmp_path / "locked"
    inner = locked / "inner"
    inner.mkdir(parents=True)
    (tmp_path / ".git").mkdir()  # a repo above the unreadable level
    locked.chmod(0)
    try:
        assert _git_project_root(inner) == tmp_path.resolve()
    finally:
        locked.chmod(0o755)
