"""A codex query from inside a `git worktree` must resolve to the PRIMARY index.

tenishai/agents#53. The reviewer runs in a throwaway worktree (techne#740) and the
codex is indexed against the shared checkout, so `find_repo_root` stopped at the
worktree — a path with no db — and every query answered "no codex index for the active
repo". Measured across 340 posted reviews, the impact pass ran in 6% of them: her
persona correctly says to fall back to grep when codex returns nothing, so the fallback
fired every time. The tool was pointed at the wrong path.

REAL WORKTREES, NOT MOCKS. The bug is entirely in git's on-disk layout — a linked
worktree's `.git` is a FILE, and the `commondir` indirection is what has to be followed
— so a fake would test the fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from techne.core.repo import find_repo_root, primary_repo_root


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def primary(tmp_path):
    """A real repo with one commit, so `git worktree add` has something to point at."""
    root = tmp_path / "primary"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture()
def worktree(primary, tmp_path):
    wt = tmp_path / "linked-worktree"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=primary)
    return wt


def test_a_linked_worktree_resolves_to_the_primary(primary, worktree):
    """THE BUG, DIRECTLY. `find_repo_root` is correct about what it answers and wrong
    for this question."""
    assert find_repo_root(worktree) == worktree, "premise changed: git layout differs"
    assert primary_repo_root(worktree) == primary


def test_the_primary_still_resolves_to_itself(primary):
    """The common case must not move. A primary checkout's `.git` is a directory and
    the function returns before touching any of the worktree indirection."""
    assert primary_repo_root(primary) == primary


def test_a_file_inside_a_worktree_resolves_too(primary, worktree):
    assert primary_repo_root(worktree / "a.py") == primary


def test_a_path_in_no_repo_is_still_None(tmp_path):
    assert primary_repo_root(tmp_path / "not-a-repo") is None


def test_a_primary_whose_path_contains_worktrees_is_not_confused(tmp_path):
    """`coord.py` warns from experience against splitting the git-dir on `/worktrees/`.

    THE FIRST VERSION OF THIS TEST WAS DECORATION and a mutation caught it: it used a
    PRIMARY repo under such a path, which returns at the `.git`-is-a-directory branch
    and never reaches any splitting at all. Substituting the naive heuristic passed all
    seven tests. The hazard needs a LINKED WORKTREE whose PRIMARY lives under the
    poisoned path — that is the next test; this one keeps the trivial case honest."""
    root = tmp_path / "worktrees" / "myrepo"
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    assert primary_repo_root(root) == root, "a path containing '/worktrees/' was misread"


def test_a_worktree_whose_primary_lives_under_a_worktrees_path(tmp_path):
    """THE ACTUAL HAZARD. Primary at `<tmp>/worktrees/myrepo`, queried from a linked
    worktree. Splitting the git-dir on the FIRST `/worktrees/` yields `<tmp>` and then
    its parent — nowhere near the repo. Following git's `commondir` file is exact."""
    root = tmp_path / "worktrees" / "myrepo"
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "a.py").write_text("x = 1\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    wt = tmp_path / "wt-poisoned"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=root)

    assert primary_repo_root(wt) == root, (
        "resolved via a '/worktrees/' string split rather than git's commondir file"
    )


def test_an_undecodable_commondir_falls_back_rather_than_raising(primary, worktree):
    """The `ValueError` arm, which a mutation showed was unexercised. Invalid UTF-8 in
    `commondir` raises UnicodeDecodeError — a ValueError subclass — and narrowing the
    handler to OSError alone would let it escape into a caller whose contract is
    "return None or a path", never "raise"."""
    pointer = (worktree / ".git").read_text(encoding="utf-8").strip()
    git_dir = Path(pointer.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    (git_dir / "commondir").write_bytes(b"\xff\xfe not utf-8\n")

    assert primary_repo_root(worktree) == worktree


def test_a_malformed_git_pointer_falls_back_rather_than_raising(primary, worktree):
    """FAIL SAFE. Any unreadable piece returns the worktree's own root — today's
    behaviour — so this can only improve resolution, never break a working install."""
    (worktree / ".git").write_text("this is not a gitdir pointer\n")
    assert primary_repo_root(worktree) == worktree


def test_the_read_path_uses_it_and_the_write_path_does_not(primary, worktree, tmp_path):
    """THE SCOPE OF THE FIX, PINNED. Following the primary on the WRITE path would let
    an edit inside a PR worktree write that branch's symbols into the base graph other
    sessions are querying — so `reindex_edited_file` deliberately still uses
    `find_repo_root`."""
    import inspect

    from techne.core import codex

    read_src = inspect.getsource(codex.resolve_repo_db_for_query)
    write_src = inspect.getsource(codex.reindex_edited_file)

    assert "primary_repo_root(" in read_src
    assert "primary_repo_root(" not in write_src, (
        "the write path followed the worktree to the primary — a PR branch's symbols "
        "would land in the base graph"
    )


def test_a_query_from_a_worktree_finds_the_primarys_index(primary, worktree, tmp_path, monkeypatch):
    """END TO END, THROUGH THE REAL ENTRY POINT — because the scope test above is
    introspection, and source-reading proves a call site exists, not that the behaviour
    is right. This is the reviewer's actual experience: index the shared checkout, query
    from the throwaway worktree, and get the index rather than `None`."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home"))
    from techne.config import Config
    from techne.core.codex import repo_db_path, resolve_repo_db_for_query

    cfg = Config.load()
    db = repo_db_path(cfg, primary)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()  # the primary is indexed; the worktree never is

    assert resolve_repo_db_for_query(cfg, worktree) == db, (
        "a query from inside a worktree missed the primary's index — the agents#53 bug"
    )
    assert resolve_repo_db_for_query(cfg, primary) == db, "the primary case regressed"


def test_an_unindexed_repo_still_answers_None_from_a_worktree(
    primary, worktree, tmp_path, monkeypatch
):
    """The fix must not invent an index. `None` is how a read consumer learns to
    no-op rather than create an empty db."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home2"))
    from techne.config import Config
    from techne.core.codex import resolve_repo_db_for_query

    assert resolve_repo_db_for_query(Config.load(), worktree) is None
