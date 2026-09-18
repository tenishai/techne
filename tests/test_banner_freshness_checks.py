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


def test_codex_freshness_is_unobservable_without_a_recorded_head(tmp_path, monkeypatch):
    """A comparison that cannot be made is not a pass.

    An index written before this change has no indexed_head. Reporting LIVE
    there would be the false green the whole check exists to remove.
    """
    root = _mk_repo(tmp_path)
    (root / "src" / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    r = _verdict("codex:freshness", root, monkeypatch)
    assert r is not None
    assert r.verdict in (Verdict.DORMANT, Verdict.UNOBSERVABLE), r
    assert r.verdict is not Verdict.LIVE, "never LIVE without a comparison"


def test_outside_a_repo_neither_check_warns(tmp_path, monkeypatch):
    """No repo means nothing to be stale about; a warning there is pure noise."""
    monkeypatch.chdir(tmp_path)
    for c in run_checks(Config(), cheap_only=True):
        if c.name in ("codex:freshness", "cards:freshness"):
            assert c.verdict is not Verdict.STALE, c
