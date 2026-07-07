"""Tests for the Claude Code platform adapter (hook + MCP registration)."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from techne.config import Config, HookConfig, SyncConfig
from techne.platforms import claude_code


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
        # sys.executable must appear in the PY= line (possibly quoted by shlex)
        assert sys.executable in launcher_content

    def test_hook_commands_quote_spaces_in_launcher(self, fake_home, cfg, monkeypatch):
        # A space in sys.executable must be shell-quoted inside the launcher
        # file so exec "$PY" works correctly. The hook command itself references
        # the launcher path (which has no spaces), not the interpreter directly.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(sys, "executable", "/Users/some user/python")
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
        assert cmd.startswith(f"/bin/sh {shlex.quote(launcher)}") or launcher in cmd
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
        # shlex.quote may or may not add quotes depending on the path; the raw
        # executable string must appear either way.
        assert sys.executable in content

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
        # After unregister, no techne groups must remain in settings.json.
        from techne.platforms.claude_code import hooks as hooks_mod
        # Provide a minimal MCP config so unregister's MCP cleanup path runs cleanly.
        mcp = hooks_mod.MCP_CONFIG_PATH
        mcp.parent.mkdir(parents=True, exist_ok=True)
        mcp.write_text(json.dumps({"mcpServers": {"techne": {"command": sys.executable}}}))

        claude_code.register_hooks(cfg)
        claude_code.unregister(cfg)

        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings.get("hooks", {}).items():
            techne_groups = [g for g in groups if claude_code._group_contains_techne(g)]
            assert not techne_groups, (
                f"{event} still has techne groups after unregister: {techne_groups}"
            )

    def test_launcher_pipx_heal_content(self, fake_home, cfg, monkeypatch):
        # When techne is installed under pipx, the baked heal command is
        # `pipx reinstall techne` — uses pipx's own bundled python so it survives
        # removal of the base python.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(
            sys, "executable", "/home/user/.local/pipx/venvs/techne/bin/python"
        )
        claude_code.register_hooks(cfg)
        content = hooks_mod._launcher_path().read_text()
        assert "pipx reinstall techne" in content


class TestHealCommand:
    """_heal_command() picks the right heal strategy per install method (techne#276)."""

    def test_pipx_install(self, monkeypatch):
        # pipx path segment → safe auto-heal via pipx's own bundled python.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(
            sys, "executable", "/home/user/.local/pipx/venvs/techne/bin/python"
        )
        assert hooks_mod._heal_command() == "pipx reinstall techne"

    def test_system_non_venv(self, monkeypatch):
        # system install (prefix == base_prefix): pip reinstall with
        # --break-system-packages (same flag logic as preflight_guard).
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3.12")
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        cmd = hooks_mod._heal_command()
        assert "pip install" in cmd
        assert "--break-system-packages" in cmd
        assert "python3" in cmd

    def test_plain_venv_warns_not_heals(self, monkeypatch, tmp_path):
        # plain venv (prefix != base_prefix, no pipx): safe auto-rebuild is
        # impossible once the base interpreter is gone — emit remediation and
        # exit 1 instead of attempting a fragile auto-rebuild.
        from techne.platforms.claude_code import hooks as hooks_mod
        venv = tmp_path / "venv"
        monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))
        monkeypatch.setattr(sys, "prefix", str(venv))
        monkeypatch.setattr(sys, "base_prefix", "/usr/local")
        cmd = hooks_mod._heal_command()
        assert "exit 1" in cmd
        # Must warn and exit — NOT auto-run pipx or pip (those are in the
        # echoed message text, not as executed auto-heal commands).
        assert "pipx reinstall" not in cmd
        assert not cmd.startswith("python3 -m pip install")

    def test_pipx_detection_requires_path_segment(self, monkeypatch):
        # Only the `pipx/venvs/` PATH SEGMENT triggers pipx mode. A random
        # path that contains 'pipx' elsewhere must not false-positive.
        from techne.platforms.claude_code import hooks as hooks_mod
        monkeypatch.setattr(sys, "executable", "/opt/pipx-tools/bin/python")
        monkeypatch.setattr(sys, "prefix", "/opt/pipx-tools")
        monkeypatch.setattr(sys, "base_prefix", "/opt/pipx-tools")
        cmd = hooks_mod._heal_command()
        # system path without pipx/venvs/ → pip path, not pipx reinstall
        assert cmd != "pipx reinstall techne"
        assert "pip install" in cmd


class TestRegisterMCP:
    def test_mcp_command_uses_sys_executable(self, fake_home, cfg):
        # Regression: previously "python", same problem as hooks.
        claude_code.register_mcp(cfg)
        config = json.loads(claude_code.MCP_CONFIG_PATH.read_text())
        techne_server = config["mcpServers"]["techne"]
        assert techne_server["command"] == sys.executable
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
