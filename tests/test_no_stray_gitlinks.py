"""A committed gitlink (mode 160000) reaching main, made falsifiable (#834).

A stray `git worktree add --detach --force "$BASE_WT" origin/main`, run with `BASE_WT`
unset, collapsed to the two-argument form and created a worktree at `./origin/main`
inside the repo. `git add -A` enrolled it as a gitlink and it reached a PR branch,
caught only by reading `git push` output.

This repo carries no submodules, so any mode-160000 entry is a mistake by definition —
there is no legitimate case this test must special-case. It runs in the DEFAULT suite
(unlike the CI-only `arch-cards` step that inspired it) so the mistake surfaces on the
next local `pytest`, not only at push time or in a CI log nobody blocks on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_no_committed_gitlinks() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-s"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    gitlinks = [line for line in out.splitlines() if line.startswith("160000")]
    assert gitlinks == [], (
        "stray gitlink(s) (mode 160000) committed — this repo has no submodules, so "
        f"this is a mistake, likely a `git worktree add` with a missing path argument: "
        f"{gitlinks}"
    )
