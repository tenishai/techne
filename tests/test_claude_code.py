"""Tests for the Claude Code platform adapter (hook + MCP registration)."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from techne.config import Config, HookConfig, SyncConfig
from techne.platforms import claude_code
from techne.platforms.claude_code import hooks as _hooks_at_import

# conftest's autouse `_isolate_install_root` pins `_interpreter` to a fake path so
# registration-shaped tests don't trip the worktree guard on a host detail. Capture
# the GENUINE implementation here — module import runs before any fixture — so the
# tests that exercise interpreter validation itself (techne#458) can restore it.
_REAL_INTERPRETER = _hooks_at_import._interpreter


def _py_line(launcher_content: str) -> str:
    """The launcher's `PY=` assignment line, isolated.

    Asserting the interpreter appears ANYWHERE in the launcher does not protect the
    thing under test: `_heal_command()` embeds the interpreter in its remediation
    message too, so a launcher that pinned the WRONG interpreter in `PY=` still
    contains the right string elsewhere and the assertion passes. Mutation-verified
    by Prudence on PR #460 — reverting `_write_launcher` to `shlex.quote(sys.executable)`
    left both callers green, i.e. the exact bug being fixed sailed through.
    """
    for line in launcher_content.splitlines():
        if line.startswith("PY="):
            return line
    raise AssertionError(f"launcher has no PY= line:\n{launcher_content}")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect SETTINGS_PATH and MCP_CONFIG_PATH into a temp home.

    Patches both the implementation module (`claude_code.hooks`, where
    register_hooks actually reads from) and the package re-export
    (`claude_code`, which tests assert against).
    """
    from techne.platforms.claude_code import hooks as hooks_mod
    home = tmp_path / "home"
    home.mkdir()
    settings_path = home / ".claude" / "settings.json"
    mcp_path = home / ".claude.json"
    monkeypatch.setattr(hooks_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hooks_mod, "MCP_CONFIG_PATH", mcp_path)
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


class TestRegisterHooks:
    def test_groups_use_wrapped_matcher_shape(self, fake_home, cfg):
        # Regression: prior versions wrote {type, command} directly into the
        # event array. Claude Code's settings validator now requires the
        # wrapped shape {matcher, hooks: [{type, command}]}; /doctor flags
        # the flat shape with "Expected array, but received undefined".
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings["hooks"].items():
            assert isinstance(groups, list), event
            for group in groups:
                assert "matcher" in group, f"{event} group missing matcher"
                assert isinstance(group.get("hooks"), list), (
                    f"{event} group missing inner 'hooks' array"
                )
                for h in group["hooks"]:
                    assert h["type"] == "command"
                    # Every techne hook is either a `-m techne.hooks.<x>` module
                    # invocation or the standalone preflight guard script. Both are
                    # now routed through the launcher (techne#276).
                    assert (
                        " -m techne.hooks." in h["command"]
                        or "preflight_guard.py" in h["command"]
                    )

    def test_matcher_is_empty_string(self, fake_home, cfg):
        # techne wants every fire of every event, so matcher is "".
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings["hooks"].items():
            for group in groups:
                assert group["matcher"] == ""

    def test_hook_commands_route_through_launcher(self, fake_home, cfg):
        # After techne#276, hooks no longer invoke sys.executable directly —
        # they route through the generated POSIX launcher so a dead Homebrew
        # interpreter triggers auto-heal before any module is imported.
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        launcher = str(hooks_mod._launcher_path())
        for event, groups in settings["hooks"].items():
            for group in groups:
                for h in group["hooks"]:
                    cmd = h["command"]
                    assert launcher in cmd, (
                        f"{event} hook doesn't reference launcher: {cmd!r}"
                    )

    def test_hook_commands_pin_interpreter_via_launcher(self, fake_home, cfg, monkeypatch):
        # The launcher bakes in sys.executable so the right interpreter is used
        # even if `python` on PATH is something else or absent (replaces the
        # pre-#276 assertion that commands started with sys.executable directly).
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        launcher_content = hooks_mod._launcher_path().read_text()
        # Assert against the PY= LINE specifically: the heal message embeds the
        # interpreter too, so a whole-file search passes even when PY= is wrong.
        assert str(hooks_mod._interpreter()) in _py_line(launcher_content)

    def test_hook_commands_quote_spaces_in_launcher(self, fake_home, cfg, monkeypatch):
        # A space in sys.executable must be shell-quoted inside the launcher
        # file so exec "$PY" works correctly. The hook command itself references
        # the launcher path (which has no spaces), not the interpreter directly.
        from techne.platforms.claude_code import hooks as hooks_mod
        # Pin the SEAM, not the global: _interpreter() validates that its candidate
        # can import techne (techne#458), which a synthetic path never will. The
        # seam exists for exactly this — see its docstring.
        monkeypatch.setattr(
            hooks_mod, "_interpreter", lambda: Path("/Users/some user/python")
        )
        claude_code.register_hooks(cfg)
        launcher_content = hooks_mod._launcher_path().read_text()
        # shlex.quote wraps the spaced path in single quotes
        assert "'/Users/some user/python'" in launcher_content

    def test_post_tool_use_respects_config(self, fake_home, cfg):
        cfg.hooks.post_tool_use = False
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        assert "PostToolUse" not in settings["hooks"]

    def test_idempotent_no_duplicate_groups(self, fake_home, cfg):
        # Two inits in a row should leave exactly one techne group per event,
        # not two. (SessionStart's single group chains preflight + session_start
        # in one command, so it's still one group.)
        claude_code.register_hooks(cfg)
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings["hooks"].items():
            techne_groups = [g for g in groups if claude_code._group_contains_techne(g)]
            assert len(techne_groups) == 1, f"{event} has {len(techne_groups)} techne groups"

    def test_replaces_legacy_flat_shape(self, fake_home, cfg):
        # A user upgrading from pre-0.4.1 will have legacy flat-shape entries
        # in settings.json. register_hooks must clean those up, not duplicate.
        legacy = {
            "SessionStart": [
                {"type": "command", "command": "/old/python -m techne.hooks.session_start"}
            ],
            "PostToolUse": [
                {"type": "command", "command": "/old/python -m techne.hooks.post_tool_use"},
                {"type": "command", "command": "/usr/bin/true"},  # someone else's hook
            ],
        }
        path = claude_code.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": legacy}))

        claude_code.register_hooks(cfg)
        settings = json.loads(path.read_text())

        # Legacy techne flat entry gone, new wrapped single entry present:
        # SessionStart is one group whose command chains preflight + session_start.
        ss_groups = settings["hooks"]["SessionStart"]
        assert len(ss_groups) == 1
        assert "matcher" in ss_groups[0]
        # Foreign hook in PostToolUse preserved
        ptu_groups = settings["hooks"]["PostToolUse"]
        foreign = [
            g for g in ptu_groups
            if not claude_code._group_contains_techne(g)
        ]
        assert len(foreign) == 1
        assert foreign[0].get("command") == "/usr/bin/true"


class TestPreflightGuard:
    """The editable-install preflight guard is chained ahead of session_start in
    a single SessionStart command, and survives re-init (techne#261).

    The chaining (not two separate hooks) is load-bearing: Claude Code runs all
    hooks matching an event in parallel with no ordering guarantee, so two hooks
    would race — the guard must sequence before session_start within one command.
    """

    def _session_start_cmd(self):
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        ss = settings["hooks"]["SessionStart"]
        assert len(ss) == 1, "SessionStart must be a single chained group"
        assert len(ss[0]["hooks"]) == 1, "single command entry, not two"
        return ss[0]["hooks"][0]["command"]

    def test_preflight_sequenced_before_session_start(self, fake_home, cfg):
        # Both run in ONE command, guard first, joined by `;` (not `&&`, so a
        # guard failure can't suppress session_start).
        claude_code.register_hooks(cfg)
        cmd = self._session_start_cmd()
        assert "preflight_guard.py" in cmd
        assert "-m techne.hooks.session_start" in cmd
        assert " ; " in cmd, "commands must be sequenced, not two parallel hooks"
        assert cmd.index("preflight_guard.py") < cmd.index(
            "techne.hooks.session_start"
        ), "guard must run before session_start"

    def test_preflight_command_uses_launcher_and_abs_path(self, fake_home, cfg):
        # After techne#276, the preflight command routes through the launcher
        # (so a dead interpreter triggers heal before the guard runs). The guard
        # is still referenced by its absolute on-disk path — it cannot `import
        # techne` so -m is a chicken-and-egg trap; absolute path is the only option.
        from techne.platforms.claude_code import hooks as hooks_mod

        claude_code.register_hooks(cfg)
        cmd = self._session_start_cmd()
        # Command starts with /bin/sh <launcher>
        launcher = str(hooks_mod._launcher_path())
        assert cmd.startswith(f"/bin/sh {shlex.quote(launcher)}")
        # Absolute guard path still present
        assert str(hooks_mod.PREFLIGHT_GUARD) in cmd
        assert hooks_mod.PREFLIGHT_GUARD.is_absolute()

    def test_preflight_survives_reinit(self, fake_home, cfg):
        # register_hooks owns the preflight, so re-init strips-and-re-adds it
        # rather than dropping it (unlike a hand-wired hook, which it migrates).
        claude_code.register_hooks(cfg)
        claude_code.register_hooks(cfg)
        cmd = self._session_start_cmd()
        assert "preflight_guard.py" in cmd
        assert "-m techne.hooks.session_start" in cmd

    def test_migrates_hand_wired_preflight(self, fake_home, cfg):
        # A hand-wired preflight (command contains "techne") is recognized and
        # replaced by the packaged one — not duplicated.
        path = claude_code.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command",
             "command": "/old/py /home/x/.claude/techne_preflight.py"}]},
        ]}}))
        claude_code.register_hooks(cfg)
        cmd = self._session_start_cmd()  # asserts exactly one group
        assert "/old/py" not in cmd  # old hand-wired one replaced, not kept
        assert "preflight_guard.py" in cmd

    def test_refuses_worktree_install(self, fake_home, cfg, monkeypatch, tmp_path):
        # If techne is installed from a worktree, register_hooks must refuse —
        # baking that path in would just re-arm the -32000 footgun.
        from techne.platforms.claude_code import hooks as hooks_mod

        worktree = tmp_path / "repo" / ".claude" / "worktrees" / "agent-DEADBEEF"
        worktree.mkdir(parents=True)
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: worktree)
        with pytest.raises(RuntimeError, match="worktree"):
            claude_code.register_hooks(cfg)

    def test_is_worktree_path_detection(self):
        from techne.platforms.claude_code import hooks as hooks_mod

        assert hooks_mod._is_worktree_path(
            Path("/home/shaz/techne/.claude/worktrees/agent-abc/techne")
        )
        assert not hooks_mod._is_worktree_path(Path("/home/shaz/working/techne"))
        assert not hooks_mod._is_worktree_path(Path("/Users/x/working/claude/techne"))


class TestLauncher:
    """The generated POSIX launcher enables install-method-aware auto-heal
    when a Homebrew upgrade/removal kills the baked-in interpreter (techne#276).
    """

    def test_launcher_generated_executable_on_register(self, fake_home, cfg):
        # register_hooks must create the launcher before writing settings.json.
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()
        assert launcher.exists(), "launcher file not created"
        assert launcher.stat().st_mode & 0o111, "launcher must be executable"
        content = launcher.read_text()
        assert content.startswith("#!/bin/sh"), "must be a POSIX sh script"

    def test_launcher_bakes_interpreter(self, fake_home, cfg):
        # sys.executable is baked into the PY= assignment so the launcher pins
        # to the exact interpreter techne was installed under.
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        content = hooks_mod._launcher_path().read_text()
        # shlex.quote may or may not add quotes depending on the path; the resolved
        # interpreter must appear in the PY= LINE either way (not merely somewhere
        # in the file — the heal message would satisfy that vacuously).
        assert str(hooks_mod._interpreter()) in _py_line(content)

    def test_launcher_heal_command_present(self, fake_home, cfg):
        # A heal block is present (the [ -x ] || { ... } guard).
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        content = hooks_mod._launcher_path().read_text()
        assert '[ -x "$PY" ] || {' in content
        assert 'exec "$PY" "$@"' in content

    def test_launcher_path_in_hook_commands(self, fake_home, cfg):
        # Every registered hook command references the launcher, not sys.executable.
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        launcher = str(hooks_mod._launcher_path())
        for event, groups in settings["hooks"].items():
            for group in groups:
                for h in group["hooks"]:
                    assert launcher in h["command"], (
                        f"{event} hook missing launcher path: {h['command']!r}"
                    )

    def test_session_start_chains_both_through_launcher(self, fake_home, cfg):
        # Both the preflight guard and session_start sides of the `;`-chain
        # must route through the launcher — heal fires before either side runs.
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        launcher = str(hooks_mod._launcher_path())
        # Both halves of the chain reference the launcher
        assert cmd.count(launcher) == 2, (
            "both guard and session_start sides must use the launcher; "
            f"found {cmd.count(launcher)} reference(s) in: {cmd!r}"
        )
        assert " ; " in cmd

    def test_launcher_regenerated_on_reinit(self, fake_home, cfg):
        # re-running register_hooks must regenerate the launcher (keeps in sync
        # if sys.executable changes between installs).
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()
        assert launcher.exists()
        # Write a stale marker; re-init must overwrite it.
        launcher.write_text("stale")
        claude_code.register_hooks(cfg)
        content = launcher.read_text()
        assert content.startswith("#!/bin/sh"), "launcher not regenerated on re-init"

    def test_reinit_replaces_bare_pin_group(self, fake_home, cfg):
        # A pre-existing bare sys.executable-pin group (pre-#276 format) is
        # recognized by _group_contains_techne and replaced with the launcher form.
        path = claude_code.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command",
             "command": f"{sys.executable} -m techne.hooks.session_start"}]},
        ]}}))
        claude_code.register_hooks(cfg)
        settings = json.loads(path.read_text())
        ss_groups = settings["hooks"]["SessionStart"]
        assert len(ss_groups) == 1, "bare-pin group must be replaced, not kept"
        # New command uses the launcher
        cmd = ss_groups[0]["hooks"][0]["command"]
        assert "techne-hook.sh" in cmd

    def test_reinit_replaces_hand_wired_shim(self, fake_home, cfg):
        # A hand-wired /bin/sh shim (techne#276 validated this shim works; re-init
        # must replace it with the officially managed launcher, not duplicate it).
        path = claude_code.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command",
             "command": "/bin/sh /some/.claude/techne-hook.sh -m techne.hooks.session_start"}]},
        ]}}))
        claude_code.register_hooks(cfg)
        settings = json.loads(path.read_text())
        ss_groups = settings["hooks"]["SessionStart"]
        assert len(ss_groups) == 1, "shim group must be replaced, not duplicated"

    def test_unregister_removes_launcher_hooks(self, fake_home, cfg):
        # After unregister, no techne groups must remain in settings.json, and the
        # launcher file must be deleted so it doesn't orphan after uninstall.
        from techne.platforms.claude_code import hooks as hooks_mod
        # Provide a minimal MCP config so unregister's MCP cleanup path runs cleanly.
        mcp = hooks_mod.MCP_CONFIG_PATH
        mcp.parent.mkdir(parents=True, exist_ok=True)
        mcp.write_text(json.dumps({"mcpServers": {"techne": {"command": sys.executable}}}))

        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()
        assert launcher.exists(), "launcher must exist after register"
        claude_code.unregister(cfg)

        assert not launcher.exists(), "launcher must be removed by unregister"
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings.get("hooks", {}).items():
            techne_groups = [g for g in groups if claude_code._group_contains_techne(g)]
            assert not techne_groups, (
                f"{event} still has techne groups after unregister: {techne_groups}"
            )

    def test_launcher_pipx_heal_content(self, fake_home, cfg, monkeypatch):
        # When techne is installed under pipx, the baked heal command uses
        # `pipx reinstall --python "$(command -v python3)" techne` — explicitly
        # names python3 so a minor-version removal is survived as long as python3
        # and pipx are on PATH at heal time.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(
            hooks_mod,
            "_interpreter",
            lambda: Path("/home/user/.local/pipx/venvs/techne/bin/python"),
        )
        claude_code.register_hooks(cfg)
        content = hooks_mod._launcher_path().read_text()
        assert 'pipx reinstall --python "$(command -v python3)" techne' in content


class TestHealCommand:
    """_heal_command() picks the right heal strategy per install method (techne#276)."""

    def test_pipx_install(self, monkeypatch):
        # pipx path segment → heal via pipx with explicit --python so a minor-version
        # removal is survived (pipx's DEFAULT_PYTHON = its own sys.executable; by
        # naming python3 we decouple from whatever Python pipx itself runs under).
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(
            hooks_mod,
            "_interpreter",
            lambda: Path("/home/user/.local/pipx/venvs/techne/bin/python"),
        )
        assert hooks_mod._heal_command() == 'pipx reinstall --python "$(command -v python3)" techne'

    def test_system_non_venv(self, monkeypatch):
        # system install (prefix == base_prefix): pip reinstall with
        # --break-system-packages (same flag logic as preflight_guard), then PY
        # is reassigned so exec targets the freshly-resolved python3, not the
        # original dead interpreter (fixes the no-op heal bug from review #279).
        # Pin the interpreter AND its venv state: since techne#458 _heal_command
        # reads the state of the interpreter being BAKED IN, not of the process
        # running init. Without pinning, probing a synthetic path fails and
        # defaults to (False, ""), so this would pass for the wrong reason.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: Path("/usr/bin/python3.12"))
        monkeypatch.setattr(hooks_mod, "_venv_state", lambda _e: (False, "/usr"))
        cmd = hooks_mod._heal_command()
        assert "pip install" in cmd
        assert "--break-system-packages" in cmd
        assert "python3" in cmd
        # After install, PY must be re-pointed so exec uses the new interpreter.
        assert 'PY="$(command -v python3)"' in cmd

    def test_plain_venv_warns_not_heals(self, monkeypatch, tmp_path):
        # plain venv (prefix != base_prefix, no pipx): safe auto-rebuild is
        # impossible once the base interpreter is gone — emit remediation and
        # exit 1 instead of attempting a fragile auto-rebuild.
        from techne.platforms.claude_code import hooks as hooks_mod
        venv = tmp_path / "venv"
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: venv / "bin" / "python")
        monkeypatch.setattr(hooks_mod, "_venv_state", lambda _e: (True, str(venv)))
        cmd = hooks_mod._heal_command()
        # The remediation must name THIS venv — the bug was naming the venv of
        # whichever interpreter ran init (Prudence, PR #460).
        assert str(venv) in cmd
        assert "exit 1" in cmd
        # Must warn and exit — NOT auto-run pipx or pip as live commands.
        # (pipx/uv reinstall *text* may appear in the echoed help message, but
        # the command itself must start with `echo`, not be a live tool invocation.)
        assert cmd.startswith("echo ")
        assert not cmd.startswith("pipx reinstall")
        assert not cmd.startswith("python3 -m pip install")

    def test_pipx_detection_requires_path_segment(self, monkeypatch):
        # Only the `pipx/venvs/` PATH SEGMENT triggers pipx mode. A random
        # path that contains 'pipx' elsewhere must not false-positive.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(
            hooks_mod, "_interpreter", lambda: Path("/opt/pipx-tools/bin/python")
        )
        monkeypatch.setattr(hooks_mod, "_venv_state", lambda _e: (False, "/opt/pipx-tools"))
        cmd = hooks_mod._heal_command()
        # system path without pipx/venvs/ → pip path, not pipx reinstall
        assert cmd != "pipx reinstall techne"
        assert "pip install" in cmd


class TestRegisterMCP:
    def test_mcp_command_uses_sys_executable(self, fake_home, cfg):
        # Regression: previously "python", same problem as hooks. Now the VALIDATED
        # interpreter (techne#458) — a poisoned pin kills the MCP server with -32000
        # exactly as it kills the hooks (techne#578).
        from techne.platforms.claude_code import hooks as hooks_mod
        claude_code.register_mcp(cfg)
        config = json.loads(claude_code.MCP_CONFIG_PATH.read_text())
        techne_server = config["mcpServers"]["techne"]
        assert techne_server["command"] == str(hooks_mod._interpreter())
        assert techne_server["args"] == ["-m", "techne.mcp_server"]


class TestConfigSafety:
    """techne#166: register_* must never destroy a user's real Claude Code config.

    The pre-#166 bug: _load_json treated an existing-but-corrupt file as "no file"
    (returned None), so register_hooks started from {} and OVERWROTE settings.json
    with only techne's hooks — wiping every other hook, permission, and setting the
    user had. And _save_json wrote in place, so a crash mid-write left the file
    truncated, which the next load would then treat as empty too.
    """

    def test_corrupt_settings_raises_and_preserves_file(self, fake_home, cfg):
        # An existing-but-unparseable settings.json must make register_hooks RAISE,
        # never silently start from {} and clobber the user's real settings.
        from techne.platforms.claude_code import hooks as hooks_mod

        path = hooks_mod.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        corrupt = '{"hooks": {"SessionStart": [  <<< not json'
        path.write_text(corrupt)

        with pytest.raises(RuntimeError, match="could not be read/parsed"):
            claude_code.register_hooks(cfg)

        # The bytes on disk are exactly what we wrote — nothing was overwritten.
        assert path.read_text() == corrupt

    def test_register_preserves_foreign_settings(self, fake_home, cfg):
        # Non-hook top-level keys and foreign hooks must survive a re-register.
        from techne.platforms.claude_code import hooks as hooks_mod

        path = hooks_mod.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}
                ]
            },
        }))

        claude_code.register_hooks(cfg)
        settings = json.loads(path.read_text())

        # Foreign setting preserved verbatim.
        assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
        # Foreign hook preserved.
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/true"
        # techne hooks added.
        assert "SessionStart" in settings["hooks"]

    def test_save_is_atomic_no_tmp_left_behind(self, fake_home, cfg):
        # After a successful save the sibling .tmp file must be gone (os.replace
        # consumed it) and the result must be valid JSON.
        claude_code.register_hooks(cfg)
        path = claude_code.SETTINGS_PATH
        leaked = list(path.parent.glob(path.name + ".tmp*"))
        assert not leaked, f"atomic-write temp file(s) leaked: {leaked}"
        json.loads(path.read_text())  # parses cleanly

    def test_unregister_corrupt_settings_still_cleans_mcp(self, fake_home, cfg):
        # techne#166 nit: uninstall must be resilient — a corrupt settings.json
        # warns and presses on so the MCP-config cleanup still runs (otherwise a
        # corrupt settings file strands the user, unable to remove techne at all).
        from techne.platforms.claude_code import hooks as hooks_mod

        settings_path = hooks_mod.SETTINGS_PATH
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{corrupt not json")

        mcp_path = hooks_mod.MCP_CONFIG_PATH
        mcp_path.write_text(json.dumps({"mcpServers": {"techne": {"command": "x"}, "other": {}}}))

        # Must NOT raise — degrade past the unreadable settings, clean the MCP half.
        claude_code.unregister(cfg)

        mcp = json.loads(mcp_path.read_text())
        assert "techne" not in mcp["mcpServers"]
        assert "other" in mcp["mcpServers"]  # foreign server preserved
        # The corrupt settings file is left untouched for the user to hand-fix.
        assert settings_path.read_text() == "{corrupt not json"

    def test_save_json_roundtrip(self, tmp_path):
        from techne.platforms.claude_code import hooks as hooks_mod

        target = tmp_path / "sub" / "x.json"
        hooks_mod._save_json(target, {"a": 1, "b": [2, 3]})
        assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}
        # _load_json returns the same dict; a missing file returns None.
        assert hooks_mod._load_json(target) == {"a": 1, "b": [2, 3]}
        assert hooks_mod._load_json(tmp_path / "nope.json") is None


class TestLauncherExecution:
    """Execute the generated launcher as a real shell script to verify the heal logic
    composes and fires correctly (not just string-assertion coverage).

    Strategy: monkeypatch sys.executable / sys.prefix to a dead path, generate the
    launcher, then run it under /bin/sh with controlled PATH (shims replace real tools
    so no actual install runs). Each shim writes a log line for later assertion.
    """

    @staticmethod
    def _shim(bin_dir: Path, name: str, body: str, log: Path) -> None:
        """Write an executable shim that appends 'CALLED: <name> <args>' to *log*."""
        s = bin_dir / name
        s.write_text(
            "#!/bin/sh\n"
            f'echo "CALLED: {name} $@" >> {shlex.quote(str(log))}\n'
            + body + "\n"
        )
        s.chmod(0o755)

    def test_plain_venv_exits_with_remediation(self, fake_home, cfg, monkeypatch, tmp_path):
        """Plain-venv launcher with a dead interpreter: exit 1, remediation on stderr,
        no install invoked."""
        from techne.platforms.claude_code import hooks as hooks_mod

        dead_py = tmp_path / "venv" / "bin" / "python"
        monkeypatch.setattr(sys, "executable", str(dead_py))
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: dead_py)
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        monkeypatch.setattr(sys, "base_prefix", "/usr/local")

        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()

        # Minimal PATH — no python3/pip so any accidental install attempt would fail
        # (but heal is echo+exit 1 so it won't try).
        env = {"PATH": "/bin:/usr/bin", "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/sh", str(launcher), "dummy_arg"],
            capture_output=True, text=True, env=env,
        )

        assert result.returncode == 1, (
            f"expected exit 1, got {result.returncode}; stderr={result.stderr!r}"
        )
        assert "interpreter dead" in result.stderr, (
            f"remediation message missing from stderr: {result.stderr!r}"
        )
        assert "recreate the venv" in result.stderr

    def test_system_heal_fires_python3_and_reppoints_exec(
        self, fake_home, cfg, monkeypatch, tmp_path
    ):
        """System-install launcher with a dead interpreter: heal calls python3 for install,
        reassigns PY, then exec targets the re-pointed python3 (not the dead one)."""
        from techne.platforms.claude_code import hooks as hooks_mod

        dead_py = tmp_path / "usr" / "bin" / "python3.11"
        monkeypatch.setattr(sys, "executable", str(dead_py))
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: dead_py)
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()

        # Shims: python3 records every invocation, exits 0.
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        log = tmp_path / "python3_calls.log"
        self._shim(shim_dir, "python3", "exit 0", log)

        env = {
            "PATH": str(shim_dir) + ":/bin:/usr/bin",
            "HOME": str(tmp_path),
        }
        # sentinel_arg is passed to the launcher; after the heal and PY re-point,
        # exec should forward it to our python3 shim.
        result = subprocess.run(
            ["/bin/sh", str(launcher), "sentinel_arg"],
            capture_output=True, text=True, env=env,
        )

        assert log.exists(), "python3 shim was never called — heal did not fire"
        calls = log.read_text()

        # First call: the install step (-m pip install ...)
        assert "-m pip install" in calls, (
            f"expected install call in log; got:\n{calls}"
        )
        # Second call: exec re-points to python3 and forwards the sentinel arg.
        assert "sentinel_arg" in calls, (
            f"expected exec to re-point to python3 and forward args; got:\n{calls}"
        )

    def test_pipx_heal_invokes_pipx_with_python_flag(
        self, fake_home, cfg, monkeypatch, tmp_path
    ):
        """pipx-install launcher with a dead interpreter: heal calls `pipx reinstall
        --python <python3-path> techne` (shim logs the call for assertion)."""
        from techne.platforms.claude_code import hooks as hooks_mod

        monkeypatch.setattr(
            hooks_mod,
            "_interpreter",
            lambda: Path("/home/user/.local/pipx/venvs/techne/bin/python"),
        )
        monkeypatch.setattr(sys, "prefix", "/home/user/.local/pipx/venvs/techne")
        monkeypatch.setattr(sys, "base_prefix", "/usr/local")

        claude_code.register_hooks(cfg)
        launcher = hooks_mod._launcher_path()

        # Shims: pipx records args, exits 0; python3 records args (used for --python
        # resolution via `command -v python3`), exits 0.
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        log = tmp_path / "calls.log"
        self._shim(shim_dir, "pipx", "exit 0", log)
        self._shim(shim_dir, "python3", "exit 0", log)

        env = {
            "PATH": str(shim_dir) + ":/bin:/usr/bin",
            "HOME": str(tmp_path),
        }
        # PY (/home/user/.local/pipx/venvs/techne/bin/python) doesn't exist, so
        # exec after heal will fail (127). We don't assert on exit code — only that
        # the pipx shim was called with the right arguments.
        subprocess.run(
            ["/bin/sh", str(launcher), "dummy_arg"],
            capture_output=True, text=True, env=env,
        )

        assert log.exists(), "pipx shim was never called — heal did not fire"
        calls = log.read_text()

        assert "CALLED: pipx" in calls, (
            f"expected pipx to be invoked; got:\n{calls}"
        )
        assert "reinstall" in calls, f"expected 'reinstall' in pipx call; got:\n{calls}"
        assert "techne" in calls, f"expected 'techne' in pipx call; got:\n{calls}"
        # --python flag must be present (verified against pipx CLI reference: reinstall
        # accepts --python to override DEFAULT_PYTHON = sys.executable).
        assert "--python" in calls, (
            f"expected '--python' flag in pipx call (robustness against removed base "
            f"python); got:\n{calls}"
        )


class TestHookDrift:
    """techne#287: check_hook_drift surfaces config↔settings.json mismatch.

    The silent-no-op failure mode: a hooks.* flag is enabled in config.yaml but
    `techne init` was never re-run, so the hook is absent from settings.json and
    never fires — with no other diagnostic. check_hook_drift reports it (and the
    reverse: a techne hook still wired for a flag that's since been turned off).
    """

    def test_clean_after_register_is_no_drift(self, fake_home, cfg):
        # In-sync config and settings must report nothing.
        cfg.hooks = HookConfig(post_tool_use=True, codex_grep_assist=True)
        claude_code.register_hooks(cfg)
        assert claude_code.check_hook_drift(cfg) == []

    def test_missing_pretooluse_when_grep_flag_flipped_on(self, fake_home, cfg):
        # The reported incident: init ran with grep-assist OFF, user later flips the
        # flag ON in config without re-running init → PreToolUse never registered.
        cfg.hooks = HookConfig(post_tool_use=True, codex_grep_assist=False)
        claude_code.register_hooks(cfg)  # writes always-on + PostToolUse, no PreToolUse

        cfg.hooks = HookConfig(post_tool_use=True, codex_grep_assist=True)
        drift = claude_code.check_hook_drift(cfg)
        assert any("PreToolUse/Grep" in w and "NOT registered" in w for w in drift), drift
        # The always-on hooks are all present — only PreToolUse drifts.
        assert not any("SessionStart" in w for w in drift), drift

    def test_userpromptsubmit_registered_when_recall_flag_on(self, fake_home, cfg):
        # per_turn_recall ON → UserPromptSubmit wired and drift-clean (techne#109/#277).
        cfg.hooks = HookConfig(post_tool_use=True, per_turn_recall=True)
        claude_code.register_hooks(cfg)
        assert claude_code.check_hook_drift(cfg) == []

    def test_missing_userpromptsubmit_when_recall_flag_flipped_on(self, fake_home, cfg):
        # Same silent-no-op incident shape as grep-assist: the flag is flipped ON in
        # config without a re-init → UserPromptSubmit never registered.
        cfg.hooks = HookConfig(post_tool_use=True, per_turn_recall=False)
        claude_code.register_hooks(cfg)

        cfg.hooks = HookConfig(post_tool_use=True, per_turn_recall=True)
        drift = claude_code.check_hook_drift(cfg)
        assert any("UserPromptSubmit" in w and "NOT registered" in w for w in drift), drift
        assert not any("SessionStart" in w for w in drift), drift

    def test_orphaned_posttooluse_when_flag_flipped_off(self, fake_home, cfg):
        # Reverse drift: PostToolUse was registered, then the flag was turned off in
        # config without re-init → the techne hook keeps firing until reconciled.
        cfg.hooks = HookConfig(post_tool_use=True)
        claude_code.register_hooks(cfg)

        cfg.hooks = HookConfig(post_tool_use=False)
        drift = claude_code.check_hook_drift(cfg)
        assert any("PostToolUse" in w and "not enabled in config" in w for w in drift), drift

    def test_no_false_positive_on_foreign_hooks(self, fake_home, cfg):
        # A user's own PreToolUse/Bash hook must never be read as a techne group,
        # so with grep-assist off it is neither "missing" nor "orphaned".
        from techne.platforms.claude_code import hooks as hooks_mod

        path = hooks_mod.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}
                ]
            }
        }))
        cfg.hooks = HookConfig(post_tool_use=True, codex_grep_assist=False)
        claude_code.register_hooks(cfg)  # preserves the foreign hook
        assert claude_code.check_hook_drift(cfg) == []

    def test_missing_when_settings_file_absent(self, fake_home, cfg):
        # No settings.json at all + flags on → everything expected is reported missing.
        cfg.hooks = HookConfig(post_tool_use=True, codex_grep_assist=True)
        drift = claude_code.check_hook_drift(cfg)
        assert any("SessionStart" in w and "NOT registered" in w for w in drift), drift
        assert any("PreToolUse/Grep" in w and "NOT registered" in w for w in drift), drift

    def test_corrupt_settings_returns_empty(self, fake_home, cfg):
        # An unparseable settings.json is already reported loudly by register/unregister;
        # drift must not double-report (or crash) on it.
        from techne.platforms.claude_code import hooks as hooks_mod

        path = hooks_mod.SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt not json")
        assert claude_code.check_hook_drift(cfg) == []


class TestInterpreterValidation:
    """techne#458: register_hooks must never bake in an interpreter that cannot
    import techne. Baking a bare sys.executable poisoned this machine's hooks three
    times (#574/#576/#584/#587); the launcher's own guard can't catch it because it
    tests `[ -x "$PY" ]` — a poisoned interpreter exists and is executable.
    """

    @pytest.fixture(autouse=True)
    def _real_interpreter_and_clean_cache(self, monkeypatch):
        """These tests exercise `_interpreter` itself, so restore the real one over
        conftest's `_isolate_install_root` stub, and keep the probe memo clean so a
        cached verdict from a sibling test can't decide the outcome."""
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(hooks_mod, "_interpreter", _REAL_INTERPRETER)
        hooks_mod._IMPORT_PROBE.clear()
        yield
        hooks_mod._IMPORT_PROBE.clear()

    @staticmethod
    def _fake_python(path: Path, body: str) -> Path:
        path.write_text(body)
        path.chmod(0o755)
        return path

    def test_probe_uses_a_neutral_cwd(self, tmp_path, monkeypatch):
        """THE regression. On 2026-08-02 the poisoning interpreter imported techne
        SUCCESSFULLY — because init ran inside the repo checkout, where `techne/` is
        importable from cwd. A probe inheriting cwd would have passed it and pinned a
        Homebrew python with no techne installed. The neutral cwd is what makes this
        check real rather than decorative.
        """
        from techne.platforms.claude_code import hooks as hooks_mod

        # An interpreter that "has techne" only when cwd happens to contain it.
        py = self._fake_python(
            tmp_path / "cwd-sensitive-python",
            "#!/bin/sh\n[ -f ./techne/__init__.py ] && exit 0\nexit 1\n",
        )
        repo = tmp_path / "repo" / "techne"
        repo.mkdir(parents=True)
        (repo / "__init__.py").write_text("")
        monkeypatch.chdir(tmp_path / "repo")

        # Control: from this cwd it "works", which is exactly why the bug was silent.
        assert subprocess.run([str(py)], cwd=str(tmp_path / "repo")).returncode == 0
        # The probe must NOT be fooled by it.
        assert hooks_mod._can_import_techne(py) is False

    def test_falls_back_to_console_script_interpreter(self, tmp_path, monkeypatch):
        """The repair, not just the detection: when the running interpreter can't
        import techne, the installed entry point almost always can."""
        from techne.platforms.claude_code import hooks as hooks_mod

        bad = self._fake_python(tmp_path / "brew-python", "#!/bin/sh\nexit 1\n")
        good = self._fake_python(tmp_path / "pipx-python", "#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(sys, "executable", str(bad))
        monkeypatch.setattr(hooks_mod, "_console_script_interpreter", lambda: good)

        assert hooks_mod._interpreter() == good

    def test_raises_rather_than_pinning_a_broken_interpreter(self, tmp_path, monkeypatch):
        """A failed `techne init` is visible and fixable in one command. A silently
        poisoned pin takes down every hook with no diagnostic and has cost hours,
        three times. So this fails loudly instead of writing something broken."""
        from techne.platforms.claude_code import hooks as hooks_mod

        bad = self._fake_python(tmp_path / "brew-python", "#!/bin/sh\nexit 1\n")
        monkeypatch.setattr(sys, "executable", str(bad))
        monkeypatch.setattr(hooks_mod, "_console_script_interpreter", lambda: None)

        with pytest.raises(RuntimeError, match="cannot determine a stable interpreter"):
            hooks_mod._interpreter()

    def test_register_hooks_refuses_to_write_a_poisoned_pin(
        self, fake_home, cfg, tmp_path, monkeypatch
    ):
        """End-to-end: the whole point is that nothing lands in settings.json."""
        from techne.platforms.claude_code import hooks as hooks_mod

        bad = self._fake_python(tmp_path / "brew-python", "#!/bin/sh\nexit 1\n")
        monkeypatch.setattr(sys, "executable", str(bad))
        monkeypatch.setattr(hooks_mod, "_console_script_interpreter", lambda: None)

        with pytest.raises(RuntimeError):
            claude_code.register_hooks(cfg)
        assert not hooks_mod._launcher_path().exists(), "launcher must not be written"

    def test_console_script_interpreter_reads_the_shebang(self, tmp_path, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod

        script = tmp_path / "techne"
        script.write_text("#!/opt/venvs/techne/bin/python -E\n# console script\n")
        script.chmod(0o755)
        monkeypatch.setattr(hooks_mod.shutil, "which", lambda _n: str(script))

        assert hooks_mod._console_script_interpreter() == Path(
            "/opt/venvs/techne/bin/python"
        )

    def test_console_script_interpreter_tolerates_a_non_script(self, tmp_path, monkeypatch):
        """A Windows launcher .exe has no shebang — return None, never raise."""
        from techne.platforms.claude_code import hooks as hooks_mod

        binary = tmp_path / "techne.exe"
        binary.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")
        monkeypatch.setattr(hooks_mod.shutil, "which", lambda _n: str(binary))

        assert hooks_mod._console_script_interpreter() is None


class TestProbeMaskingChannels:
    """techne#458 / PR #460: neutralizing `cwd` is not enough. PYTHONPATH is the same
    masking channel in a different coat — `PYTHONPATH=. python3 -m techne.cli init`
    would otherwise reproduce the original bug straight through the new guard.
    (Prudence measured it: probe(/usr/bin/python3) False bare, True under
    PYTHONPATH=<checkout>.)
    """

    @pytest.fixture(autouse=True)
    def _real_interpreter_and_clean_cache(self, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(hooks_mod, "_interpreter", _REAL_INTERPRETER)
        hooks_mod._IMPORT_PROBE.clear()
        yield
        hooks_mod._IMPORT_PROBE.clear()

    def test_probe_env_strips_pythonpath_entries_inside_the_install_root(self, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod

        root = hooks_mod._install_root()
        innocent = "/opt/some/other/lib"
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(root), innocent]))

        env = hooks_mod._probe_env()
        assert str(root) not in env.get("PYTHONPATH", ""), "masking entry must be stripped"
        assert innocent in env["PYTHONPATH"], "unrelated entries must survive"

    def test_probe_env_strips_a_relative_entry_resolving_into_the_root(
        self, monkeypatch, tmp_path
    ):
        """`PYTHONPATH=.` is the realistic shape — it resolves against cwd, which in
        the incident IS the checkout."""
        from techne.platforms.claude_code import hooks as hooks_mod

        root = hooks_mod._install_root()
        monkeypatch.chdir(root)
        monkeypatch.setenv("PYTHONPATH", ".")

        assert "PYTHONPATH" not in hooks_mod._probe_env()

    def test_pythonpath_cannot_mask_a_broken_interpreter(self, monkeypatch, tmp_path):
        """End-to-end: an interpreter that only 'works' when PYTHONPATH is set must
        still probe False, because the probe strips the masking entry."""
        from techne.platforms.claude_code import hooks as hooks_mod

        py = tmp_path / "pythonpath-sensitive-python"
        py.write_text('#!/bin/sh\n[ -n "$PYTHONPATH" ] && exit 0\nexit 1\n')
        py.chmod(0o755)

        monkeypatch.setenv("PYTHONPATH", str(hooks_mod._install_root()))
        assert hooks_mod._can_import_techne(py) is False, (
            "PYTHONPATH pointing into the install root must not make a broken "
            "interpreter look importable"
        )

        # Control: an UNRELATED PYTHONPATH is preserved, so the same fake passes —
        # proving the probe strips selectively rather than blanket-clearing the env.
        hooks_mod._IMPORT_PROBE.clear()
        monkeypatch.setenv("PYTHONPATH", "/opt/some/other/lib")
        assert hooks_mod._can_import_techne(py) is True

    def test_substituting_a_different_install_is_announced(self, tmp_path, monkeypatch, capsys):
        """Wiring ~/.claude to an interpreter the operator did not choose is the right
        repair, but it must not be silent (Prudence, PR #460)."""
        from techne.platforms.claude_code import hooks as hooks_mod

        bad = tmp_path / "brew-python"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        good = tmp_path / "pipx-python"
        good.write_text("#!/bin/sh\nexit 0\n")
        good.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bad))
        monkeypatch.setattr(hooks_mod, "_console_script_interpreter", lambda: good)

        assert hooks_mod._interpreter() == good
        err = capsys.readouterr().err
        assert "cannot import techne" in err
        assert str(good) in err


class TestVenvStateFollowsTheChosenInterpreter:
    """_heal_command must describe the interpreter being BAKED IN, not the one that
    ran init — they can now differ, and mixing them names the wrong venv as dead."""

    def test_venv_state_of_the_running_interpreter_uses_sys(self, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod
        hooks_mod._VENV_PROBE.clear()
        in_venv, prefix = hooks_mod._venv_state(Path(sys.executable))
        assert in_venv == (sys.prefix != sys.base_prefix)
        assert prefix == sys.prefix

    def test_heal_message_names_the_chosen_venv_not_ours(self, monkeypatch, tmp_path):
        from techne.platforms.claude_code import hooks as hooks_mod
        chosen = tmp_path / "their-venv"
        monkeypatch.setattr(hooks_mod, "_interpreter", lambda: chosen / "bin" / "python")
        monkeypatch.setattr(hooks_mod, "_venv_state", lambda _e: (True, str(chosen)))
        # Our own prefix is something else entirely; it must not leak into the message.
        monkeypatch.setattr(sys, "prefix", "/our/unrelated/venv")

        cmd = hooks_mod._heal_command()
        assert str(chosen) in cmd
        assert "/our/unrelated/venv" not in cmd


class TestNamespacePackageChannel:
    """PR #460 round 2: `import techne` and `-m techne.hooks.session_start` are not
    the same claim under PEP 420. Prudence measured three routes where the probe
    returned True while the actual hook command returned rc=1 — including one with
    no env trickery at all (an empty `techne/` left in site-packages by a partial
    uninstall). _probe_env had closed the incident SHAPE, not the CHANNEL.
    """

    @pytest.fixture(autouse=True)
    def _real_interpreter_and_clean_cache(self, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(hooks_mod, "_interpreter", _REAL_INTERPRETER)
        hooks_mod._IMPORT_PROBE.clear()
        yield
        hooks_mod._IMPORT_PROBE.clear()

    def test_probe_asserts_the_module_the_hooks_actually_run(self, tmp_path, monkeypatch):
        """An interpreter that can satisfy `import techne` but NOT the hook module
        must be rejected — that is the empty-namespace-dir shape."""
        from techne.platforms.claude_code import hooks as hooks_mod

        py = tmp_path / "namespace-only-python"
        # Succeeds for the weak claim, fails for the real one.
        # NOTE $2, not $3: argv is (-c, <stmt>), so the statement is $2. An earlier
        # cut read $3, which is always empty — so it exited 1 for BOTH the weak and
        # the real claim and the test passed under mutation without testing anything.
        py.write_text(
            '#!/bin/sh\n'
            'case "$2" in\n'
            '  "import techne") exit 0 ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        py.chmod(0o755)
        assert hooks_mod._can_import_techne(py) is False, (
            "probing only `import techne` lets a namespace-package shell through"
        )

    def test_pythonpath_parent_of_the_install_root_is_stripped(self, monkeypatch, tmp_path):
        """The real install root is literally named `techne`, so PYTHONPATH=<parent>
        makes that directory importable as a PEP 420 namespace package. Stripping
        only entries UNDER the root kept it. Modelled explicitly here — conftest pins
        _install_root to a temp dir NOT named techne, which hides the mechanism."""
        from techne.platforms.claude_code import hooks as hooks_mod

        parent = tmp_path / "tenishai"
        root = parent / "techne"        # repo root, literally named `techne`
        root.mkdir(parents=True)
        monkeypatch.setattr(hooks_mod, "_install_root", lambda: root)
        monkeypatch.setenv("PYTHONPATH", str(parent))

        assert "PYTHONPATH" not in hooks_mod._probe_env(), (
            "the parent of a root named `techne` supplies a namespace package"
        )

    def test_a_second_checkout_on_pythonpath_is_stripped(self, monkeypatch, tmp_path):
        """Nowhere near this root, but it still supplies techne."""
        from techne.platforms.claude_code import hooks as hooks_mod

        other = tmp_path / "other-checkout"
        (other / "techne").mkdir(parents=True)
        (other / "techne" / "__init__.py").write_text("")
        monkeypatch.setenv("PYTHONPATH", str(other))
        assert "PYTHONPATH" not in hooks_mod._probe_env()

    def test_an_unrelated_entry_is_still_kept(self, monkeypatch, tmp_path):
        """Guard against over-stripping — the whole reason for not using -E."""
        from techne.platforms.claude_code import hooks as hooks_mod

        innocent = tmp_path / "unrelated-lib"
        innocent.mkdir()
        monkeypatch.setenv("PYTHONPATH", str(innocent))
        assert hooks_mod._probe_env()["PYTHONPATH"] == str(innocent)


class TestVenvStateSubprocessPath:
    """PR #460 round 2: `if key == sys.executable:` -> `if True:` left all 57 tests
    green. Every existing test either hit the short-circuit or monkeypatched
    _venv_state away, so the subprocess branch — the one that exists BECAUSE the
    chosen interpreter can differ from ours — was never exercised."""

    def test_queries_the_other_interpreter_rather_than_assuming_ours(self, tmp_path):
        from techne.platforms.claude_code import hooks as hooks_mod

        hooks_mod._VENV_PROBE.clear()
        fake = tmp_path / "other-python"
        # Reports a venv whose prefix is deliberately unlike ours.
        fake.write_text('#!/bin/sh\necho 1\necho /somewhere/else/venv\n')
        fake.chmod(0o755)

        in_venv, prefix = hooks_mod._venv_state(fake)
        assert in_venv is True
        assert prefix == "/somewhere/else/venv"
        assert prefix != sys.prefix, "must not have answered from OUR interpreter"
        hooks_mod._VENV_PROBE.clear()

    def test_a_failing_interpreter_degrades_to_not_a_venv(self, tmp_path):
        from techne.platforms.claude_code import hooks as hooks_mod

        hooks_mod._VENV_PROBE.clear()
        broken = tmp_path / "broken-python"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        assert hooks_mod._venv_state(broken) == (False, "")
        hooks_mod._VENV_PROBE.clear()


class TestBakedInModulesStaysInSync:
    """PR #460 round 3: the probe must cover EVERY module baked into a registered
    command. That list lived in four places; drift between them is how this class of
    bug recurs, so _BAKED_IN_MODULES is the single source and this test is the thing
    that keeps it honest."""

    def test_every_registered_module_is_in_the_probe_list(self, fake_home, cfg):
        from techne.platforms.claude_code import hooks as hooks_mod

        cfg.hooks = HookConfig(
            post_tool_use=True, codex_grep_assist=True, per_turn_recall=True
        )
        claude_code.register_hooks(cfg)
        claude_code.register_mcp(cfg)

        blob = claude_code.SETTINGS_PATH.read_text() + claude_code.MCP_CONFIG_PATH.read_text()
        registered = set(re.findall(r"techne\.(?:hooks\.[a-z_]+|mcp_server)", blob))
        # EQUALITY, not subset. The subset form only caught "registered but not
        # probed". The other direction is worse: because the probe is an
        # all-or-nothing `import a,b,c`, ONE stale or misspelled name makes
        # _can_import_techne False for EVERY interpreter — a machine-wide `techne
        # init` outage. Prudence inserted `techne.hooks.sesion_start`: all eight real
        # modules imported rc=0, the union returned rc=1, and the suite reported 66
        # passed. Shipping green. (PR #460.)
        assert registered == set(hooks_mod._BAKED_IN_MODULES), (
            f"probed-but-not-registered (typo/stale?): "
            f"{sorted(set(hooks_mod._BAKED_IN_MODULES) - registered)}\n"
            f"registered-but-not-probed: {sorted(registered - set(hooks_mod._BAKED_IN_MODULES))}"
        )

    def test_probe_rejects_an_interpreter_missing_any_one_module(self, tmp_path, monkeypatch):
        """The PyYAML shape: four of the eight import fine, four die. A probe that
        asserts one representative module returns True while hooks are dead."""
        from techne.platforms.claude_code import hooks as hooks_mod

        hooks_mod._IMPORT_PROBE.clear()
        py = tmp_path / "partial-python"
        # Satisfies session_start alone; fails the union.
        py.write_text(
            '#!/bin/sh\n'
            'case "$2" in\n'
            '  "import techne.hooks.session_start") exit 0 ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        py.chmod(0o755)
        assert hooks_mod._can_import_techne(py) is False
        hooks_mod._IMPORT_PROBE.clear()


class TestPythonpathOnlyInstallIsDiagnosed:
    """PR #460 round 3: `pip install --target=DIR` + exported PYTHONPATH supplies
    techne AND its deps from one entry, so the strict strip reports it unusable. We
    still REFUSE it — hooks run in Claude Code's environment, so a PYTHONPATH-only
    install is fragile in the same way the masked one is broken — but a bare
    'cannot determine a stable interpreter' is baffling when `import techne` plainly
    works in the operator's shell. Diagnose the actual situation."""

    @pytest.fixture(autouse=True)
    def _real_interpreter(self, monkeypatch):
        """conftest's _isolate_install_root stubs _interpreter module-wide; this class
        exercises the real one. Without this the test passes vacuously — the stub
        returns a path and never probes, so it can never raise."""
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(hooks_mod, "_interpreter", _REAL_INTERPRETER)
        hooks_mod._IMPORT_PROBE.clear()
        yield
        hooks_mod._IMPORT_PROBE.clear()

    def test_raise_names_the_pythonpath_only_case(self, tmp_path, monkeypatch):
        from techne.platforms.claude_code import hooks as hooks_mod

        hooks_mod._IMPORT_PROBE.clear()
        supplier = tmp_path / "target-dir"
        (supplier / "techne").mkdir(parents=True)
        py = tmp_path / "pythonpath-only-python"
        py.write_text('#!/bin/sh\n[ -n "$PYTHONPATH" ] && exit 0\nexit 1\n')
        py.chmod(0o755)

        monkeypatch.setattr(sys, "executable", str(py))
        monkeypatch.setattr(hooks_mod, "_console_script_interpreter", lambda: None)
        monkeypatch.setenv("PYTHONPATH", str(supplier))

        with pytest.raises(RuntimeError, match="importable only via PYTHONPATH"):
            hooks_mod._interpreter()
        hooks_mod._IMPORT_PROBE.clear()
