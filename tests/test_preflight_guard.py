"""Unit tests for the standalone editable-install preflight guard (techne#261).

These exercise the guard's decision logic — worktree detection, the venv-gated
`--break-system-packages` flag, the no-op-on-non-editable-install path, and the
heal/refuse branches of main() — without ever invoking real pip. subprocess.run
and _healthy are monkeypatched so the branching is deterministic.
"""
from pathlib import Path

from techne.platforms.claude_code import preflight_guard as pg


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestWorktreeDetection:
    def test_detects_claude_worktree(self):
        assert pg._is_worktree_path(
            Path("/home/shaz/techne/.claude/worktrees/agent-abc/techne")
        )

    def test_ignores_canonical_checkout(self):
        assert not pg._is_worktree_path(Path("/Users/x/working/claude/techne"))
        assert not pg._is_worktree_path(Path("/home/shaz/working/techne"))


class TestPipCommand:
    def test_omits_break_system_packages_in_venv(self, monkeypatch):
        # In a venv (sys.prefix != sys.base_prefix) the flag is unnecessary and
        # some venv pips reject it — must be omitted.
        captured = {}
        monkeypatch.setattr(pg.subprocess, "run",
                            lambda cmd, **kw: captured.setdefault("cmd", cmd) or _FakeProc())
        monkeypatch.setattr(pg.sys, "prefix", "/some/venv")
        monkeypatch.setattr(pg.sys, "base_prefix", "/usr")
        pg._pip_install_editable(Path("/repo"))
        assert "--break-system-packages" not in captured["cmd"]
        assert captured["cmd"][:4] == [pg.sys.executable, "-m", "pip", "install"]
        assert captured["cmd"][-1] == "/repo"

    def test_includes_break_system_packages_on_system_python(self, monkeypatch):
        # System python (PEP 668, prefix == base_prefix) rejects a bare install;
        # the flag is required — e.g. Ubuntu /usr/bin/python3, modern Homebrew.
        captured = {}
        monkeypatch.setattr(pg.subprocess, "run",
                            lambda cmd, **kw: captured.setdefault("cmd", cmd) or _FakeProc())
        monkeypatch.setattr(pg.sys, "prefix", "/usr")
        monkeypatch.setattr(pg.sys, "base_prefix", "/usr")
        pg._pip_install_editable(Path("/repo"))
        assert "--break-system-packages" in captured["cmd"]


class TestMain:
    def _no_pip(self, monkeypatch):
        """Install a pip stub that fails the test if the heal path is taken."""
        calls = []
        monkeypatch.setattr(pg, "_pip_install_editable",
                            lambda target: calls.append(target) or _FakeProc())
        return calls

    def test_noop_when_not_a_source_checkout(self, monkeypatch, tmp_path):
        # No pyproject.toml beside the guard => techne was installed normally,
        # there is no editable pointer to heal. Must do nothing (and not pip).
        monkeypatch.setattr(pg, "CANONICAL", tmp_path)  # no pyproject.toml
        calls = self._no_pip(monkeypatch)
        assert pg.main() == 0
        assert calls == []

    def test_refuses_to_heal_from_worktree(self, monkeypatch, tmp_path, capsys):
        # A guard running from a worktree must warn, not reinstall (that would
        # just re-arm the footgun).
        wt = tmp_path / ".claude" / "worktrees" / "agent-DEAD"
        wt.mkdir(parents=True)
        (wt / "pyproject.toml").write_text("")
        monkeypatch.setattr(pg, "CANONICAL", wt)
        calls = self._no_pip(monkeypatch)
        assert pg.main() == 0
        assert calls == []
        assert "worktree" in capsys.readouterr().err

    def test_healthy_is_silent_and_skips_pip(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(pg, "CANONICAL", tmp_path)
        monkeypatch.setattr(pg, "_healthy", lambda: (True, tmp_path / "techne"))
        calls = self._no_pip(monkeypatch)
        assert pg.main() == 0
        assert calls == []
        assert capsys.readouterr().err == ""  # silent when healthy

    def test_heals_when_pointer_stale(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(pg, "CANONICAL", tmp_path)
        # Don't pollute the real sys.path with the re-verify insert.
        monkeypatch.setattr(pg.sys, "path", list(pg.sys.path))
        # Unhealthy on first probe, healthy after the (faked) reinstall.
        states = iter([(False, None), (True, tmp_path / "techne")])
        monkeypatch.setattr(pg, "_healthy", lambda: next(states))
        calls = []
        monkeypatch.setattr(pg, "_pip_install_editable",
                            lambda target: calls.append(target) or _FakeProc(returncode=0))
        assert pg.main() == 0
        assert calls == [tmp_path]  # reinstalled from the canonical repo
        assert "repaired" in capsys.readouterr().err

    def test_reports_failure_when_heal_does_not_take(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(pg, "CANONICAL", tmp_path)
        monkeypatch.setattr(pg.sys, "path", list(pg.sys.path))
        monkeypatch.setattr(pg, "_healthy", lambda: (False, None))  # never recovers
        monkeypatch.setattr(pg, "_pip_install_editable",
                            lambda target: _FakeProc(returncode=1, stderr="boom"))
        assert pg.main() == 0  # never fails the hook chain
        assert "repair FAILED" in capsys.readouterr().err

    def test_canonical_is_repo_root(self):
        # The guard self-locates: CANONICAL is the repo root (parents[3]), i.e.
        # the dir that contains the `techne` package next to this guard file.
        assert (pg.CANONICAL / "techne" / "platforms" / "claude_code"
                / "preflight_guard.py").exists()
