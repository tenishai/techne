"""The worktree guard must detect a REAL git worktree, not a path convention (techne#434).

`register_hooks` already refused to register from a worktree — the guard just could
not see most worktrees. `_is_worktree_path` matched only paths under
`.claude/worktrees/` (the harness's own directory), so a manual `git worktree add`
anywhere else passed straight through, got baked into ~/.claude/settings.json and the
launcher, and took every hook on the machine down when the worktree was pruned. Third
occurrence of techne#578's class; the first two were fixed in memory, never in code.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from techne.config import Config, HookConfig, SyncConfig
from techne.platforms import claude_code
from techne.platforms.claude_code import hooks as hooks_mod
from techne.platforms.claude_code import preflight_guard as guard_mod
from techne.platforms.claude_code.hooks import _is_worktree_path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    from techne.platforms.claude_code import hooks as hm

    home = tmp_path / "home"
    home.mkdir()
    settings_path = home / ".claude" / "settings.json"
    mcp_path = home / ".claude.json"
    monkeypatch.setattr(hm, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hm, "MCP_CONFIG_PATH", mcp_path)
    monkeypatch.setattr(claude_code, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(claude_code, "MCP_CONFIG_PATH", mcp_path)
    return home


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = tmp_path / "techne.db"
    c.data_dir = tmp_path
    c.platform = "claude-code"
    c.hooks = HookConfig(post_tool_use=True)
    c.sync = SyncConfig(adapter="noop")
    return c


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def real_repo_and_worktree(tmp_path):
    """A genuine git repo plus a genuine linked worktree, made by git itself.

    Deliberately NOT a hand-faked `.git` file: the whole defect was trusting an
    assumption about what worktrees look like, so the fixture asks git to make one.
    """
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    # Placed OUTSIDE any `.claude/worktrees/` directory — the case the old guard missed.
    wt = tmp_path / "somewhere-else" / "wt-434"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    return repo, wt


class TestDetection:
    def test_detects_a_real_worktree_outside_the_claude_convention(self, real_repo_and_worktree):
        """THE regression. This returned False before #434 and is how the outage happened."""
        _repo, wt = real_repo_and_worktree
        assert ".claude" not in wt.parts  # the old heuristic had nothing to match on
        assert _is_worktree_path(wt) is True

    def test_canonical_checkout_is_not_a_worktree(self, real_repo_and_worktree):
        repo, _wt = real_repo_and_worktree
        assert (repo / ".git").is_dir()
        assert _is_worktree_path(repo) is False

    def test_submodule_marker_is_not_treated_as_a_worktree(self, tmp_path):
        """A submodule also uses a `.git` FILE, but points at `.git/modules/<name>`.

        Submodule checkouts are stable and must not be refused — only the
        `worktrees` path segment means "this can be pruned out from under you".
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        assert _is_worktree_path(sub) is False

    def test_claude_worktrees_convention_still_matches(self, tmp_path):
        """The old path heuristic is retained as a fallback for when the marker is
        missing or unreadable — it must not have been dropped in the rewrite."""
        p = tmp_path / ".claude" / "worktrees" / "techne" / "abc"
        p.mkdir(parents=True)
        assert _is_worktree_path(p) is True

    def test_plain_directory_is_not_a_worktree(self, tmp_path):
        assert _is_worktree_path(tmp_path) is False

    def test_unreadable_marker_does_not_raise(self, tmp_path):
        """The guard runs on the preflight hot path; it must degrade, never crash."""
        p = tmp_path / "weird"
        p.mkdir()
        (p / ".git").mkdir()  # a directory where a file might be expected
        assert _is_worktree_path(p) is False


class TestRegisterHooksRefuses:
    def test_register_hooks_refuses_from_a_real_worktree(
        self, real_repo_and_worktree, fake_home, cfg, monkeypatch
    ):
        """The outcome that actually matters: the machine-global write never happens."""
        _repo, wt = real_repo_and_worktree
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: wt)

        with pytest.raises(RuntimeError, match="worktree"):
            hooks_mod.register_hooks(cfg)

        # And nothing was written — not settings.json, not the launcher.
        assert not hooks_mod.SETTINGS_PATH.exists()
        assert not hooks_mod._launcher_path().exists()

    def test_register_hooks_succeeds_from_a_canonical_checkout(
        self, real_repo_and_worktree, fake_home, cfg, monkeypatch
    ):
        """Guard must not be so broad it blocks the normal install."""
        repo, _wt = real_repo_and_worktree
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: repo)

        hooks_mod.register_hooks(cfg)
        assert hooks_mod.SETTINGS_PATH.exists()

    def test_suite_can_run_from_a_worktree(self, fake_home, cfg):
        """Developing techne IN a worktree must stay possible.

        conftest's autouse `_isolate_install_root` pins `_install_root` to a plain
        temp dir, so a registration-shaped test behaves the same whether the
        developer works in a clone or a worktree. Without it, making the guard
        actually detect worktrees broke 35 tests — the guard firing on the worktree
        the suite itself runs in.

        The fix belongs in the harness, not the invariant: an earlier attempt let
        the guard stand down whenever SETTINGS_PATH was redirected, which passed
        the suite by silently weakening the guard in every sandboxed context.
        """
        hooks_mod.register_hooks(cfg)  # must NOT raise, even from a worktree checkout
        assert hooks_mod.SETTINGS_PATH.exists()


def _logic_of(fn) -> str:
    """The function's body with the docstring stripped, normalized.

    Compares behaviour, not prose: the two copies carry deliberately different
    docstrings explaining their own context.
    """
    src = Path(fn.__code__.co_filename).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn.__name__:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]  # drop the docstring
            return "\n".join(ast.dump(n) for n in body)
    raise AssertionError(f"{fn.__name__} not found in {src[:80]}")


def test_the_two_copies_have_not_drifted():
    """`preflight_guard` cannot import from techne — it runs as a bare script before
    techne is known to be importable — so `_is_worktree_path` is duplicated on
    purpose. This test is the thing that keeps the duplication honest.

    Not hypothetical: the copies HAD already drifted before #434 — the hooks copy
    called `.resolve()` and the guard copy did not, so a relative path could evade
    even the old check in one of them.
    """
    assert _logic_of(hooks_mod._is_worktree_path) == _logic_of(guard_mod._is_worktree_path)


class TestDetectionEdgeCases:
    """Cases I could not reason my way to confidently, so I built them with real git.

    Each was a live question on the PR: could a legitimate non-worktree checkout
    match the predicate, and does the submodule discriminator actually hold?
    """

    def test_separate_git_dir_clone_is_not_a_worktree(self, tmp_path):
        """`git clone --separate-git-dir` also produces a `.git` FILE.

        The predicate must key on the `/worktrees/` segment, not merely on "`.git`
        is a file", or every detached-gitdir clone would be refused.
        """
        origin = tmp_path / "origin"
        origin.mkdir()
        _git("init", "-q", cwd=origin)
        _git("config", "user.email", "t@t", cwd=origin)
        _git("config", "user.name", "t", cwd=origin)
        (origin / "f").write_text("x")
        _git("add", "-A", cwd=origin)
        _git("commit", "-qm", "i", cwd=origin)

        clone = tmp_path / "clone"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                f"--separate-git-dir={tmp_path / 'gd'}",
                str(origin),
                str(clone),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert (clone / ".git").is_file()  # the shape that could false-positive
        assert _is_worktree_path(clone) is False

    def test_submodule_inside_a_worktree_IS_flagged(self, real_repo_and_worktree, tmp_path):
        """True here is CORRECT, not a false positive.

        A submodule checked out inside a linked worktree has a gitdir under
        `<main>/.git/worktrees/<wt>/modules/<name>` — so pruning the worktree takes
        the submodule's git dir with it. Installing from there is exactly as
        fragile as installing from the worktree itself.
        """
        _repo, wt = real_repo_and_worktree
        sub_src = tmp_path / "subsrc"
        sub_src.mkdir()
        _git("init", "-q", cwd=sub_src)
        _git("config", "user.email", "t@t", cwd=sub_src)
        _git("config", "user.name", "t", cwd=sub_src)
        (sub_src / "s").write_text("y")
        _git("add", "-A", cwd=sub_src)
        _git("commit", "-qm", "i", cwd=sub_src)

        r = subprocess.run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(sub_src),
                "sub",
            ],
            cwd=wt,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:  # some git builds refuse file:// submodules outright
            pytest.skip(f"submodule add unsupported here: {r.stderr.strip()[:80]}")

        marker = (wt / "sub" / ".git").read_text()
        assert "worktrees" in marker  # the shape under test
        assert _is_worktree_path(wt / "sub") is True

    def test_a_directory_named_worktrees_is_not_enough(self, tmp_path):
        """The path heuristic requires `.claude/worktrees/`, so an unrelated
        directory called `worktrees` must not trip the guard."""
        p = tmp_path / "worktrees" / "inner"
        p.mkdir(parents=True)
        assert _is_worktree_path(p) is False
