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
import os
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

    def test_git_as_a_directory_is_not_a_worktree(self, tmp_path):
        """A `.git` DIRECTORY is a primary checkout, whatever else is true."""
        p = tmp_path / "weird"
        p.mkdir()
        (p / ".git").mkdir()
        assert _is_worktree_path(p) is False

    def test_unreadable_marker_degrades_instead_of_raising(self, tmp_path):
        """Exercises the `except OSError` branch FOR REAL.

        The previous version of this test used a `.git` DIRECTORY, so `is_file()`
        was False and `read_text` was never reached — the except branch it claimed
        to cover was untested in both copies while the PR body asserted otherwise
        (Prudence, PR #435). Fourth time in two days I have written a test that
        asserts more than it proves; the fix is to make it actually reach the line.
        """
        p = tmp_path / "locked"
        p.mkdir()
        marker = p / ".git"
        marker.write_text("gitdir: /wherever/.git/worktrees/x\n")
        marker.chmod(0o000)
        try:
            if os.access(marker, os.R_OK):  # root, or a permissive filesystem
                pytest.skip("cannot make the marker unreadable in this environment")
            assert _is_worktree_path(p) is False  # degrades, does not raise
        finally:
            marker.chmod(0o644)


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


@pytest.fixture
def bare_repo_worktree(tmp_path):
    """A worktree of a BARE repo — the mainstream worktree workflow.

    Its marker reads `gitdir: /srv/proj.git/worktrees/<id>`: no `/.git/` segment
    anywhere. Any predicate that string-matches `/.git/worktrees/` misses it.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "f").write_text("x")
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "i", cwd=seed)

    bare = tmp_path / "srv.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    wt = tmp_path / "barewt"
    _git("worktree", "add", "-q", str(wt), cwd=bare)
    return bare, wt


class TestBareRepoAndNesting:
    def test_bare_repo_worktree_is_detected(self, bare_repo_worktree):
        """Prudence's finding #1, confirmed against real git before fixing.

        The first predicate matched `/.git/worktrees/` in the marker text, which
        silently assumes the git-dir is named `.git`. A bare repo's is not, so this
        returned False — missing the most common worktree setup there is.
        """
        _bare, wt = bare_repo_worktree
        assert "/.git/" not in (wt / ".git").read_text()  # the shape that fooled it
        assert _is_worktree_path(wt) is True

    def test_bare_repo_itself_is_not_a_worktree(self, bare_repo_worktree):
        bare, _wt = bare_repo_worktree
        assert _is_worktree_path(bare) is False

    def test_submodule_in_a_primary_checkout_is_not_refused(self, tmp_path):
        """The counterpart to the submodule-in-a-worktree case: a submodule of a
        NORMAL checkout is stable and must not be refused."""
        main = tmp_path / "main"
        main.mkdir()
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            _git(*a, cwd=main)
        (main / "f").write_text("x")
        _git("add", "-A", cwd=main)
        _git("commit", "-qm", "i", cwd=main)

        src = tmp_path / "src"
        src.mkdir()
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            _git(*a, cwd=src)
        (src / "s").write_text("y")
        _git("add", "-A", cwd=src)
        _git("commit", "-qm", "i", cwd=src)

        r = subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub"],
            cwd=main,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            pytest.skip(f"submodule add unsupported here: {r.stderr.strip()[:80]}")
        assert _is_worktree_path(main / "sub") is False


class TestAllBakedPathsAreGuarded:
    """Prudence's finding #2: the guard checked ONE directory while register_hooks
    bakes THREE prunable paths into machine-global config."""

    def test_interpreter_inside_a_worktree_is_refused(
        self, real_repo_and_worktree, fake_home, cfg, monkeypatch, tmp_path
    ):
        """The concrete gap: a NON-editable install into `<worktree>/.venv`.

        `_install_root()` then lands in site-packages — no `.git`, so the old guard
        passed — while `sys.executable` is still `<worktree>/.venv/bin/python`, and
        that path gets written into the launcher, the MCP command and the heal.
        """
        _repo, wt = real_repo_and_worktree
        clean = tmp_path / "site-packages" / "techne"
        clean.mkdir(parents=True)
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: clean)  # looks fine

        # Build the REAL venv shape: bin/python is a SYMLINK out to a base
        # interpreter. The earlier version of this test only named a path that was
        # never created, so `resolve()` appended the non-existent tail verbatim and
        # there was no symlink to follow — it passed against code where the check
        # was inert (Prudence, PR #435). Measured on the dev machine:
        #   <worktree>/.venv/bin/python -> ~/.local/share/uv/python/.../bin/python3.13
        base = tmp_path / "base-python" / "bin"
        base.mkdir(parents=True)
        real_py = base / "python3"
        real_py.write_text("#!/bin/sh\n")
        venv_bin = wt / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        venv_py = venv_bin / "python"
        os.symlink(real_py, venv_py)
        assert venv_py.resolve() == real_py.resolve()  # resolves OUT of the worktree
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: venv_py)

        with pytest.raises(RuntimeError, match="interpreter"):
            hooks_mod.register_hooks(cfg)
        assert not hooks_mod.SETTINGS_PATH.exists()

    def test_preflight_guard_inside_a_worktree_is_refused(
        self, real_repo_and_worktree, fake_home, cfg, monkeypatch, tmp_path
    ):
        """SessionStart bakes the guard's ABSOLUTE path, which is `__file__`-derived."""
        _repo, wt = real_repo_and_worktree
        clean = tmp_path / "sp" / "techne"
        clean.mkdir(parents=True)
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: clean)
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: clean / "bin" / "python")
        monkeypatch.setattr(hooks_mod, "PREFLIGHT_GUARD", wt / "techne" / "preflight_guard.py")

        with pytest.raises(RuntimeError, match="preflight guard"):
            hooks_mod.register_hooks(cfg)
