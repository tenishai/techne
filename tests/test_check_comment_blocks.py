"""`scripts/check_comment_blocks.sh`'s CI (base-ref) mode (techne#1071).

Direct subprocess tests, not only the YAML-introspection tests in
`test_ci_workflow_triggers.py` — those pin that CI CALLS the script correctly;
these pin that the script itself BEHAVES correctly once called, in particular
on the failure mode Prudence's PR #1120 review found: an unresolvable base ref
made every `git diff` below print `fatal: ambiguous argument` to stderr, the
empty file list read as "nothing to check", and the gate exited 0 — a silent
pass, which is this issue's whole point one door further in than the no-arg
case the original tests covered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_comment_blocks.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.sh").write_text("#!/bin/bash\necho hi\n")
    _git(tmp_path, "add", "f.sh")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=repo, capture_output=True, text=True
    )


class TestBaseRefMustResolve:
    def test_unresolvable_base_ref_fails_loudly_not_silently(self, repo):
        """THE REGRESSION THIS TEST KILLS: before the `git rev-parse --verify`
        guard, a bad ref made `git diff` fail internally, the empty result read
        as "nothing to check", and this returned 0 — PASSED-GREEN having
        checked nothing (Prudence, PR #1120 review, Important 1)."""
        result = _run(repo, "no/such/ref")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "does not resolve" in result.stderr

    def test_a_resolvable_base_ref_with_nothing_added_passes(self, repo):
        result = _run(repo, "HEAD")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_resolvable_base_ref_still_catches_a_long_block(self, repo):
        """The base-ref mode is not JUST permissive about bad refs — it still
        does its actual job on a good one."""
        lines = ["#!/bin/bash"] + [f"# line {i}" for i in range(13)] + ["echo hi\n"]
        (repo / "g.sh").write_text("\n".join(lines))
        _git(repo, "add", "g.sh")
        _git(repo, "commit", "-q", "-m", "add a long block")

        result = _run(repo, "HEAD~1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "g.sh" in result.stderr

    def test_no_arg_mode_is_unaffected(self, repo):
        """The pre-commit (staged-diff) shape must not change: nothing staged,
        no base-ref argument, still a clean pass."""
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
