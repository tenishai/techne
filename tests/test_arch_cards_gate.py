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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    # "seam file missing", not bare "missing": this fixture has no ARCHITECTURE.md,
    # and the manifest-missing failure supplies the word "missing" by itself — so the
    # loose assertion passed even if the branch it names never ran. (Prudence, #451.)
    assert "anchor seam file missing" in r.stdout


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
    # kind@no-cards-found must stay "coverage".
    assert "bump its recorded `anchor:` sha" not in r.stdout
    # The early return APPENDS rather than replaces: manifest problems collected
    # before it must survive. Mutating it back to a fresh list killed zero tests.
    assert "ARCHITECTURE.md is missing" in r.stdout, "manifest problems must not be discarded"


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
    # kind@manifest-missing must stay "coverage".
    assert "bump its recorded `anchor:` sha" not in r.stdout


def test_manifest_with_frontmatter_stripped_fails(tmp_path: Path) -> None:
    """The manifest is the one file exempt from the reverse check, so nothing was
    verifying IT. Delete it and you get a loud failure; strip four lines from its head
    and you got a clean bill of health — with its own `anchor:` silently unchecked.
    Same failure class the forward check already names, applied to the file exempt
    from it. (Prudence, PR #451.)"""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    orphan_sha = _seam(repo, "pkg/orphan.py", body="o = 1\n")
    _card(repo, "orphan.md", f"pkg/orphan.py sha={orphan_sha}")  # deliberately unindexed
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

    # Same file, same SEAMS index — frontmatter removed, so it no longer parses.
    body = (repo / "ARCHITECTURE.md").read_text().split("---\n")[-1]
    (repo / "ARCHITECTURE.md").write_text(body)

    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no longer parses as a card" in r.stdout
    # THE POINT OF THE SECOND `if`: the SEAMS block is still readable, so the index
    # cross-check must KEEP RUNNING. Asserting absence proved nothing — the previous
    # version of this test passed unchanged when the structure was mutated to `elif`
    # (0 tests failed), i.e. it documented a guarantee it could not enforce. Assert
    # PRESENCE of something only the still-running cross-check can say.
    # (Prudence, PR #451.)
    assert "not indexed" in r.stdout, "index cross-check must still run"
    assert "orphan.md" in r.stdout


def test_reformatted_index_blames_the_index_not_every_card(tmp_path: Path) -> None:
    """A SEAMS block rewritten as markdown links matches nothing. Reporting each card
    as unindexed would be N failures pointing at N innocent files; when NOTHING
    matches, the defect is one line of formatting. (Prudence, PR #451.)"""
    repo = _repo(tmp_path)
    for name in ("a", "b", "c"):
        sha = _seam(repo, f"pkg/{name}.py", body=f"{name} = 1\n")
        _card(repo, f"{name}.md", f"pkg/{name}.py sha={sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    (repo / "ARCHITECTURE.md").write_text(
        f"---\ncard: repo-card:test\nanchor: pkg/root.py sha={root_sha}\n---\n"
        "# repo\n\nSEAMS → card:\n"
        "- a  → [a](docs/arch/a.md)\n- b  → [b](docs/arch/b.md)\n- c  → [c](docs/arch/c.md)\n"
    )
    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "index entries matched" in r.stdout
    # kind@index-matched-nothing must stay "coverage": flipping it to "anchor" put
    # "the cards are not at fault" directly above "bump its recorded anchor sha".
    assert "bump its recorded `anchor:` sha" not in r.stdout
    assert r.stdout.count("not indexed") == 0, "must not blame the individual cards"
    assert "FAILED (1)" in r.stdout, "one index failure, not three card failures"


def test_anchor_trailer_only_prints_for_anchor_problems(tmp_path: Path) -> None:
    """The footer told the reader to bump an `anchor:` sha — printed underneath
    coverage failures that have nothing to do with anchors, contradicting the correct
    inline remediation directly above it. (Prudence, PR #451.)"""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    orphan_sha = _seam(repo, "pkg/orphan.py", body="o = 1\n")
    _card(repo, "orphan.md", f"pkg/orphan.py sha={orphan_sha}")  # unindexed
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "not indexed" in r.stdout
    assert "bump its recorded `anchor:` sha" not in r.stdout, (
        "anchor remediation must not follow a coverage-only failure"
    )


def test_anchor_trailer_still_prints_for_real_drift(tmp_path: Path) -> None:
    """Guard against over-gating: the trailer must survive for its actual case."""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/seam.py")
    _card(repo, "seam.md", f"pkg/seam.py sha={sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")
    (repo / "pkg" / "seam.py").write_text("x = 2  # drifted\n")

    r = _run(repo)
    assert r.returncode == 1
    assert "drifted" in r.stdout
    assert "bump its recorded `anchor:` sha" in r.stdout


def test_trailer_fires_for_a_card_with_no_anchor_line(tmp_path: Path) -> None:
    """UNDER-FIRE, and it was a live regression. `card has no 'anchor:' line` IS an
    anchor problem, was not in the old prose-grep's vocabulary, and is the only
    message in the script carrying no inline remediation — so gating it out left the
    reader with a bare line and nothing. (Prudence, PR #451.)"""
    repo = _repo(tmp_path)
    (repo / "docs" / "arch" / "seam.md").write_text("---\ncard: arch-card:t\n---\n# no anchor\n")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "no `anchor:` line" in r.stdout
    assert "bump its recorded `anchor:` sha" in r.stdout


def test_trailer_does_not_fire_on_a_card_whose_FILENAME_says_drifted(tmp_path: Path) -> None:
    """OVER-FIRE. The old gate grepped `why`, and three coverage messages interpolate
    repo data into it — so a card FILENAME could trigger the anchor trailer. Producer
    and consumer sharing a file bounds nothing when the matched text is user data."""
    repo = _repo(tmp_path)
    sha = _seam(repo, "pkg/a.py", body="a = 1\n")
    _card(repo, "drifted-clock.md", f"pkg/a.py sha={sha}")      # filename says "drifted"
    malformed_sha = _seam(repo, "pkg/b.py", body="b = 1\n")
    _card(repo, "malformed-input.md", f"pkg/b.py sha={malformed_sha}")
    root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
    # Index neither → coverage-only failures, whose messages embed those filenames.
    _repo_card(repo, "docs/arch/absent.md", anchor=f"pkg/root.py sha={root_sha}")

    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "not indexed" in r.stdout
    assert "bump its recorded `anchor:` sha" not in r.stdout, (
        "a card filename must not decide whether the anchor trailer prints"
    )


def test_trailer_fires_for_malformed_and_for_a_missing_seam_file(tmp_path: Path) -> None:
    """The old gate's `malformed` and `seam file missing` branches were entirely
    unconstrained — collapsing the predicate to `"drifted" in why` killed 0 tests."""
    for anchor, needle in (
        ("pkg/x.py sha=NOTAHASH", "malformed"),
        ("pkg/gone.py sha=" + "0" * 40, "anchor seam file missing"),
    ):
        repo = _repo(tmp_path / needle.replace(" ", "_"))
        _seam(repo, "pkg/x.py", body="x = 1\n")
        _card(repo, "seam.md", anchor)
        root_sha = _seam(repo, "pkg/root.py", body="r = 1\n")
        _repo_card(repo, "docs/arch/seam.md", anchor=f"pkg/root.py sha={root_sha}")

        r = _run(repo)
        assert r.returncode == 1, r.stdout
        assert needle in r.stdout
        assert "bump its recorded `anchor:` sha" in r.stdout, f"{needle}: trailer missing"


def test_environment_failure_is_exit_2_with_no_summary_or_trailer(tmp_path: Path) -> None:
    """The exit-2 contract had NO test: both `kind@env -> "anchor"` and dropping the
    `code == 1 and` guard killed zero tests. Exit 2 returns before any card is read,
    so an env problem can never mix with anchor problems — which is what makes the
    `env` kind safe redundancy rather than ambiguity. Pin the whole contract.
    (Prudence, PR #451.)"""
    not_a_repo = tmp_path / "plain-dir"
    (not_a_repo / "docs" / "arch").mkdir(parents=True)

    r = _run(not_a_repo)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not a git work tree" in r.stdout
    assert "ERROR" in r.stdout
    # exit 2 suppresses the card/anchor summary — there was nothing to count.
    assert "checked" not in r.stdout
    # ...and must never print anchor remediation for an environment failure.
    assert "bump its recorded `anchor:` sha" not in r.stdout


def _load_checker():
    """Import the gate as a module so its RETURN SHAPE can be asserted directly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_arch_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_failure_carries_the_env_kind(tmp_path: Path) -> None:
    """The two belts guarding the trailer on an env failure — `kind == "env"` and
    `code == 1 and ...` — are INDEPENDENTLY unobservable through stdout, because
    whichever survives suppresses the trailer on its own. A stdout test therefore
    cannot kill either mutant alone; only the double mutation shows.

    That is defence in depth working as intended, not a gap to design away — so pin
    each belt where it IS observable: the return shape. (Prudence, PR #451.)
    """
    mod = _load_checker()
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir(parents=True)

    code, problems, n_cards, n_anchors, _skipped = mod.check(not_a_repo)

    assert code == 2, "environment failure is exit 2, distinct from a card failure"
    assert problems and all(kind == "env" for _w, _y, kind in problems), problems
    assert not any(kind == "anchor" for _w, _y, kind in problems), (
        "an env failure must never be tagged anchor — exit 2 returns before any card "
        "is read, so it cannot legitimately mix with anchor problems"
    )
    assert (n_cards, n_anchors) == (0, 0)


def test_env_failure_prints_no_anchor_trailer_even_if_kind_is_lost(monkeypatch, capsys) -> None:
    """The `code == 1 and` belt IS individually observable — I claimed it wasn't.

    My reasoning was that the kind tag suppresses the trailer on its own, so no
    stdout test could isolate this guard. True end-to-end, false in general: inject a
    fake `check()` returning an exit-2 problem ALREADY MIS-TAGGED as "anchor", and
    the only thing left standing between that and the trailer is `code == 1`.

    Prudence wrote this test to disprove the comment I'd left saying it couldn't be
    written — in a PR whose thesis is that an unpinned fix walks back. A durable note
    asserting something is untestable will talk the next maintainer out of testing
    it, which is worse than the missing test. (PR #451.)
    """
    mod = _load_checker()
    fake = (2, [(Path("/nowhere"), "git executable not found on PATH", "anchor")], 0, 0, 0)
    monkeypatch.setattr(mod, "check", lambda repo: fake)
    monkeypatch.setattr(mod, "repo_root", lambda: Path("/nowhere"))

    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "ERROR" in out
    assert "bump its recorded `anchor:` sha" not in out
