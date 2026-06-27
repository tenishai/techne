"""Tests for the Claude Code platform adapter (hook + MCP registration)."""

import json
import sys

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
                    assert " -m techne.hooks." in h["command"]

    def test_matcher_is_empty_string(self, fake_home, cfg):
        # techne wants every fire of every event, so matcher is "".
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings["hooks"].items():
            for group in groups:
                assert group["matcher"] == ""

    def test_hook_commands_use_sys_executable(self, fake_home, cfg):
        # Regression: previously hardcoded "python -m ...", which fails on
        # systems where only `python3` exists, or where `python` resolves to
        # a system interpreter without techne installed.
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        for event, groups in settings["hooks"].items():
            for group in groups:
                for h in group["hooks"]:
                    cmd = h["command"]
                    assert cmd.startswith(sys.executable + " ") or cmd.startswith(
                        f'"{sys.executable}" '
                    ), f"{event} hook missing sys.executable: {cmd!r}"

    def test_hook_commands_quote_spaces(self, fake_home, cfg, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/Users/some user/python")
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert cmd.startswith("'/Users/some user/python' ") or cmd.startswith(
            '"/Users/some user/python" '
        )

    def test_post_tool_use_respects_config(self, fake_home, cfg):
        cfg.hooks.post_tool_use = False
        claude_code.register_hooks(cfg)
        settings = json.loads(claude_code.SETTINGS_PATH.read_text())
        assert "PostToolUse" not in settings["hooks"]

    def test_idempotent_no_duplicate_groups(self, fake_home, cfg):
        # Two inits in a row should leave exactly one techne group per event,
        # not two.
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

        # Legacy techne flat entries gone, new wrapped entries present
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
        tmp = path.with_name(path.name + ".tmp")
        assert not tmp.exists(), "atomic-write temp file leaked"
        json.loads(path.read_text())  # parses cleanly

    def test_save_json_roundtrip(self, tmp_path):
        from techne.platforms.claude_code import hooks as hooks_mod

        target = tmp_path / "sub" / "x.json"
        hooks_mod._save_json(target, {"a": 1, "b": [2, 3]})
        assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}
        # _load_json returns the same dict; a missing file returns None.
        assert hooks_mod._load_json(target) == {"a": 1, "b": [2, 3]}
        assert hooks_mod._load_json(tmp_path / "nope.json") is None
