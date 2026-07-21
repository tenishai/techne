"""Exit-code contract for scripts/check_arch_cards.py (the architecture-card gate).

Runs the checker as a subprocess against a throwaway git repo via ARCH_CARDS_ROOT, so
the contract is pinned before the script is copied into other repos. Stdlib-only, like
the checker itself — no techne import needed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_arch_cards.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ARCH_CARDS_ROOT": str(repo)}
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "arch").mkdir(parents=True)
    return tmp_path


def _seam(repo: Path, rel: str, body: str = "x = 1\n") -> str:
    """Write a seam file and return its current git blob sha."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return _git(repo, "hash-object", rel).stdout.strip()


def _card(repo: Path, name: str, *anchors: str, card_id: str = "arch-card:test") -> None:
    lines = "\n".join(f"anchor: {a}" for a in anchors)
    (repo / "docs" / "arch" / name).write_text(
        f"---\ncard: {card_id}\n{lines}\n---\n# {name}\nbody\n"
    )


def test_fresh_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fresh" in r.stdout


def test_drift_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    (repo / "pkg" / "seam.py").write_text("x = 2  # changed\n")  # working-tree edit
    r = _run(repo)
    assert r.returncode == 1
    assert "drifted" in r.stdout


def test_malformed_anchor_fails_even_with_a_valid_sibling(tmp_path: Path) -> None:
    """Regression: a bad sha on a multi-anchor card must FAIL, not hide behind the
    good anchor (the silent false-green Prudence caught on PR #384)."""
    repo = _repo(tmp_path)
    good = _seam(repo, "pkg/a.py")
    _seam(repo, "pkg/b.py", body="y = 1\n")
    _card(repo, "seam.md", f"pkg/a.py sha={good}", "pkg/b.py sha=NOTAHASH")
    r = _run(repo)
    assert r.returncode == 1
    assert "malformed" in r.stdout


def test_missing_seam_file_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _card(repo, "seam.md", "pkg/gone.py sha=" + "0" * 40)
    r = _run(repo)
    assert r.returncode == 1
    assert "missing" in r.stdout


def test_stray_non_card_md_is_skipped_not_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    (repo / "docs" / "arch" / "NOTES.md").write_text("# just notes, no frontmatter\n")
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped 1" in r.stdout


def test_no_cards_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "arch" / "NOTES.md").write_text("# not a card\n")
    r = _run(repo)
    assert r.returncode == 1
    assert "no architecture cards" in r.stdout
