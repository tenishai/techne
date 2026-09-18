"""codex and arch-card freshness reach the SessionStart banner (techne#937).

THE INCIDENT. A session started on 30984e0 with cabal's index 5 days old and 60
source files changed since. Three symbols that exist in cabal source returned
ZERO hits from `codex search`. kobo was 31 files behind, daedalus 22, liminal
18. All silent, because no check looked at codex at all. A stale index does not
degrade gracefully -- it answers "we don't have that" with exactly the
confidence of a correct miss, on the one question codex exists to answer.

THE HARD PART, NAMED BY THE ISSUE UP FRONT. `run_checks(cheap_only=True)` skips
anything marked `@expensive`, whose stated bar is "subprocesses, filesystem
walks, or network". The obvious implementation of both checks is exactly that,
so it lands in `doctor` only and the banner stays silent -- delivering the
opposite of what was asked. Both checks here are cheap by construction, and
`test_both_checks_are_in_the_cheap_banner_set` is what keeps them that way.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


from techne.config import Config
from techne.core.checks import Verdict, run_checks


def _verdict(name, cwd, monkeypatch):
    monkeypatch.chdir(cwd)
    for c in run_checks(Config(), cheap_only=True):
        if c.name == name:
            return c
    return None


def test_both_checks_are_in_the_cheap_banner_set(monkeypatch):
    """THE DESIGN CONSTRAINT. If either becomes @expensive it vanishes from the
    banner, which is the failure this issue exists to prevent."""
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    names = {c.name for c in run_checks(Config(), cheap_only=True)}
    assert "codex:freshness" in names
    assert "cards:freshness" in names


def test_the_blob_sha_formula_matches_git_exactly():
    """cards:freshness is authoritative because this identity holds.

    The issue assumed the real comparison needed `git hash-object` and was
    therefore unaffordable in the banner. A blob sha is a formula, not a git
    feature, so the authoritative answer costs one read and one sha1.
    """
    target = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = target.read_bytes()
    mine = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324
    theirs = subprocess.run(
        ["git", "hash-object", str(target)],
        capture_output=True,
        text=True,
        cwd=target.parent,
    ).stdout.strip()
    assert mine == theirs, "the blob-sha identity no longer holds; cards:freshness is unsound"


def _mk_repo(tmp_path, name="proj"):
    root = tmp_path / name
    (root / "docs" / "arch").mkdir(parents=True)
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    return root


def _write_card(root, seam_rel, sha):
    (root / "docs" / "arch" / "c.md").write_text(
        f"---\ncard: arch-card:test\nanchor: {seam_rel} sha={sha}\n---\n# test card\n"
    )


def _blob(p):
    d = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest()  # noqa: S324


def test_cards_freshness_is_live_when_the_anchor_matches(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    _write_card(root, "src/m.py", _blob(seam))

    r = _verdict("cards:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.LIVE, r


def test_cards_freshness_goes_stale_when_the_seam_drifts(tmp_path, monkeypatch):
    """THE REGRESSION."""
    root = _mk_repo(tmp_path)
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    _write_card(root, "src/m.py", _blob(seam))
    seam.write_text("x = 2\n")  # drift

    r = _verdict("cards:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.STALE, r
    assert "src/m.py" in r.detail


def test_a_touched_but_unchanged_seam_stays_live(tmp_path, monkeypatch):
    """WHY THE MTIME VERSION WAS REJECTED, pinned as a test.

    An mtime comparison reported 12 of 19 anchors stale on this repo while the
    sha gate said all 19 were fresh, because `git checkout` rewrites mtimes.
    Content is what the card vouches for, not modification time.
    """
    import os
    import time

    root = _mk_repo(tmp_path)
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    _write_card(root, "src/m.py", _blob(seam))

    future = time.time() + 10_000
    os.utime(seam, (future, future))  # newer than the card, identical content

    r = _verdict("cards:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.LIVE, (
        "a touched-but-unchanged seam must not warn; that is the false-positive "
        f"class that made mtime unusable. got: {r}"
    )


def test_a_malformed_anchor_is_skipped_not_fatal(tmp_path, monkeypatch):
    """The banner and the pre-push gate keep DIFFERENT contracts, deliberately.

    scripts/check_arch_cards.py treats a malformed anchor as a hard failure so a
    typo cannot leave a seam unguarded. The banner skips it, because a banner
    that fails to render is worse than a banner missing one line.
    """
    root = _mk_repo(tmp_path)
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    (root / "docs" / "arch" / "c.md").write_text(
        f"---\nanchor: src/m.py sha=NOTAREALSHA\nanchor: src/m.py sha={_blob(seam)}\n---\n"
    )
    r = _verdict("cards:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.LIVE, r


def _indexed_repo(tmp_path, monkeypatch):
    """A real repo with a real codex index, returning (root, db_path)."""
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    root = _mk_repo(tmp_path, "indexed")
    (root / "src" / "m.py").write_text("def hello():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home"))
    cfg = Config()
    from techne.codex.source import RepoSource

    db = codex_db_for_repo(cfg, root)
    if db is None:
        pytest.skip("codex could not resolve a per-repo db in this environment")
    RepoSource(db, root).initial_walk()
    return root, db


def test_codex_freshness_is_live_at_the_indexed_head(tmp_path, monkeypatch):
    """THE LIVE PATH, which no test reached before."""
    root, _db = _indexed_repo(tmp_path, monkeypatch)
    r = _verdict("codex:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.LIVE, r


def test_codex_freshness_goes_stale_when_head_moves(tmp_path, monkeypatch):
    """THE STALE PATH. HEAD moving past the index is the whole predicate."""
    root, _db = _indexed_repo(tmp_path, monkeypatch)
    (root / "src" / "m.py").write_text("def hello():\n    return 2\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "move"],
        cwd=root,
        check=True,
    )
    r = _verdict("codex:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.STALE, r
    assert "HEAD is now" in r.detail


def test_codex_freshness_is_unobservable_when_the_index_predates_head_recording(
    tmp_path, monkeypatch
):
    """THE UNOBSERVABLE BRANCH, WHICH THE EARLIER TEST NEVER REACHED.

    It asserted `verdict in (DORMANT, UNOBSERVABLE)` against a repo with NO index
    at all, so it always took the DORMANT "no codex index yet" return and the
    branch it was named for was never executed. Reaching UNOBSERVABLE needs an
    index that EXISTS but carries no indexed_head -- exactly what every index
    written before techne#937 looks like.
    """
    from techne.core.codex import get_meta, set_meta

    root, db = _indexed_repo(tmp_path, monkeypatch)
    assert get_meta(db, "indexed_head"), "precondition: the index recorded a head"
    set_meta(db, "indexed_head", "")

    r = _verdict("codex:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.UNOBSERVABLE, r
    assert r.verdict is not Verdict.LIVE
    assert "cannot tell" in r.detail


def test_codex_freshness_is_dormant_without_any_index(tmp_path, monkeypatch):
    """Distinct from UNOBSERVABLE: nothing to be stale about yet."""
    root = _mk_repo(tmp_path, "unindexed")
    (root / "src" / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home2"))
    r = _verdict("codex:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.DORMANT, r


def test_cards_freshness_uses_the_worktree_not_the_primary(tmp_path, monkeypatch):
    """W1: a linked worktree was checked against the PRIMARY's cards and seams.

    primary_repo_root deliberately maps a worktree to its parent, which is right
    for codex and wrong for cards -- cards live in the checkout being EDITED. With
    the primary resolver, editing a seam in a worktree without bumping its card
    read LIVE, because the primary's cards were compared against the primary's
    seams. Worktree-per-session is the standing dev topology, so this was the
    common case, not an edge one.
    """
    root = _mk_repo(tmp_path, "primary")
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    _write_card(root, "src/m.py", _blob(seam))
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "HEAD"], cwd=root, check=True)
    # Drift ONLY inside the worktree; the primary stays clean.
    (wt / "src" / "m.py").write_text("x = 2\n")

    r = _verdict("cards:freshness", wt, monkeypatch)
    assert r is not None and r.verdict is Verdict.STALE, (
        "the worktree's drifted seam read clean, so the check resolved to the "
        f"primary checkout instead of the worktree. got: {r}"
    )


def test_a_missing_seam_is_reported_not_skipped(tmp_path, monkeypatch):
    """W3: the pre-push gate calls a missing seam a HARD failure; this skipped it.

    A card vouching for a file that has been renamed or deleted is exactly the
    drift the check exists to surface, and silently continuing let the banner read
    LIVE while the gate was red.
    """
    root = _mk_repo(tmp_path, "gone")
    seam = root / "src" / "m.py"
    seam.write_text("x = 1\n")
    _write_card(root, "src/m.py", _blob(seam))
    seam.unlink()

    r = _verdict("cards:freshness", root, monkeypatch)
    assert r is not None and r.verdict is Verdict.STALE, r
    assert "unreadable" in r.detail


def test_outside_a_repo_neither_check_warns(tmp_path, monkeypatch):
    """No repo means nothing to be stale about; a warning there is pure noise."""
    monkeypatch.chdir(tmp_path)
    for c in run_checks(Config(), cheap_only=True):
        if c.name in ("codex:freshness", "cards:freshness"):
            assert c.verdict is not Verdict.STALE, c
