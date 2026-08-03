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


def _repo_card(repo: Path, *indexed: str, anchor: str) -> None:
    """Write ARCHITECTURE.md carrying a `SEAMS → card:` index — the manifest."""
    rows = "\n".join(f"- name  pkg/x.py  (desc)  → {rel}" for rel in indexed)
    (repo / "ARCHITECTURE.md").write_text(
        f"---\ncard: repo-card:test\nanchor: {anchor}\n---\n"
        f"# repo\n\nSEAMS → card:\n{rows}\n"
    )


def test_fresh_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    # Every seam card must be indexed in the manifest — a card absent from it is
    # unprotected, since deleting it later would pass silently (Prudence, PR #451).
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")
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
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")
    (repo / "docs" / "arch" / "NOTES.md").write_text("# just notes, no frontmatter\n")
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped 1" in r.stdout


def test_deleted_card_still_indexed_fails(tmp_path: Path) -> None:
    """The glob cannot see a DELETED card — it just yields one fewer file and exits 0,
    so coverage shrinks silently. The ARCHITECTURE.md manifest cross-check closes that
    false-green (Prudence, PR #451)."""
    repo = _repo(tmp_path)
    seam_sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={seam_sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

    assert _run(repo).returncode == 0, "baseline must be green before deleting"

    (repo / "docs" / "arch" / "seam.md").unlink()  # delete the card, keep the index line
    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "indexed in ARCHITECTURE.md" in r.stdout


def test_removing_card_and_its_index_line_together_passes(tmp_path: Path) -> None:
    """Retiring a seam is legitimate: drop the card AND its manifest line and the gate
    stays green — the check must not become a ratchet that forbids deleting anything."""
    repo = _repo(tmp_path)
    keep_sha = _seam(repo, "pkg/keep.py")
    _card(repo, "keep.md", f"pkg/keep.py sha={keep_sha}")
    gone_sha = _seam(repo, "pkg/gone.py", body="g = 1\n")
    _card(repo, "gone.md", f"pkg/gone.py sha={gone_sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(
        repo, "docs/arch/keep.md", "docs/arch/gone.md", anchor=f"pkg/root.py sha={root_sha}"
    )
    assert _run(repo).returncode == 0, "baseline must be green"

    (repo / "docs" / "arch" / "gone.md").unlink()
    _repo_card(repo, "docs/arch/keep.md", anchor=f"pkg/root.py sha={root_sha}")
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_cards_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "arch" / "NOTES.md").write_text("# not a card\n")
    r = _run(repo)
    assert r.returncode == 1
    assert "no architecture cards" in r.stdout


def test_card_absent_from_the_index_fails(tmp_path: Path) -> None:
    """The check must run BOTH directions. Forward-only left a hole exactly where it
    hurts most: a card that was never indexed can be deleted for exit 0 — and new
    cards are the ones least likely to have been wired into the index carefully.
    (Prudence, PR #451.)"""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    orphan_sha = _seam(repo, "pkg/orphan.py", body="o = 1\n")
    _card(repo, "orphan.md", f"pkg/orphan.py sha={orphan_sha}")  # never indexed
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "not indexed" in r.stdout
    assert "orphan.md" in r.stdout


def test_deleting_the_manifest_itself_fails(tmp_path: Path) -> None:
    """ARCHITECTURE.md IS the coverage check; without it every seam card is
    unverifiable, so its absence must not read as 'nothing to check'."""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")
    assert _run(repo).returncode == 0, "baseline must be green"

    (repo / "ARCHITECTURE.md").unlink()
    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ARCHITECTURE.md is missing" in r.stdout
