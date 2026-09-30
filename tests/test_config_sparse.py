"""Sparse config.yaml and the legacy full-config migration (techne#1070, #1074)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from techne import config_migration as cm
from techne.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "config_legacy"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TECHNE_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path))
    return h


def _legacy(home: Path, tag: str) -> Path:
    """Place the real `techne init` output of an old tag as home/config.yaml."""
    text = (FIXTURES / f"{tag}.yaml").read_text().replace("__TECHNE_HOME__", str(home))
    path = home / "config.yaml"
    path.write_text(text)
    return path


def _file(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text())


def _backups(home: Path) -> list[Path]:
    return sorted(home.glob("config.yaml.bak-*"))


# --- sparse save -------------------------------------------------------------


def test_a_default_config_saves_only_the_version_key(home):
    Config().save()
    assert _file(home) == {cm.VERSION_KEY: cm.CONFIG_VERSION}


def test_save_writes_a_value_that_differs_from_the_default(home):
    cfg = Config()
    cfg.reflect.emit_max_chars = 30_000
    cfg.hooks.capture_read = True
    cfg.save()
    assert _file(home) == {
        cm.VERSION_KEY: cm.CONFIG_VERSION,
        "hooks": {"capture_read": True},
        "reflect": {"emit_max_chars": 30_000},
    }


def test_a_key_the_file_set_stays_pinned_even_at_todays_default(home):
    """The case "differs from default" alone would lose: a deliberate pin equal to
    today's default. It must survive a load->save round-trip so a later default
    change does not move it."""
    (home / "config.yaml").write_text(
        yaml.safe_dump({cm.VERSION_KEY: 2, "hooks": {"capture_read": False}, "tag_threshold": 0.42})
    )
    cfg = Config.load()
    assert sorted(cfg.pinned_keys()) == ["hooks.capture_read", "tag_threshold"]
    cfg.save()
    assert _file(home) == {
        cm.VERSION_KEY: 2,
        "hooks": {"capture_read": False},
        "tag_threshold": 0.42,
    }


def test_an_unpinned_key_follows_a_changed_default(home, monkeypatch):
    """The point of #1070: a key the file does not set takes the running default,
    also when its section is present for another key. `_from_dict` used to fall
    back to literal copies of the defaults there, which a release could forget."""
    from dataclasses import dataclass

    from techne import config as config_mod

    cfg = Config()
    cfg.reflect.enabled = True
    cfg.save()
    assert _file(home)["reflect"] == {"enabled": True}

    @dataclass
    class NewRelease(config_mod.ReflectConfig):
        emit_max_chars: int = 12_345

    monkeypatch.setattr(config_mod, "ReflectConfig", NewRelease)
    loaded = Config.load()
    assert loaded.reflect.enabled is True
    assert loaded.reflect.emit_max_chars == 12_345


def test_save_keeps_keys_it_does_not_recognise(home):
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                cm.VERSION_KEY: 2,
                "from_a_newer_techne": {"x": 1},
                "hooks": {"not_a_hook_field": "keep me"},
            }
        )
    )
    cfg = Config.load()
    cfg.reflect.enabled = True
    cfg.save()
    assert _file(home) == {
        cm.VERSION_KEY: 2,
        "reflect": {"enabled": True},
        "hooks": {"not_a_hook_field": "keep me"},
        "from_a_newer_techne": {"x": 1},
    }


# --- #1074: retired knobs ----------------------------------------------------


def test_expansion_ratio_is_gone_and_a_file_carrying_it_still_loads(home):
    (home / "config.yaml").write_text(
        yaml.safe_dump({cm.VERSION_KEY: 2, "search": {"expansion_ratio": 7, "bm25_weight": 0.9}})
    )
    cfg = Config.load()
    assert not hasattr(cfg.search, "expansion_ratio")
    assert cfg.search.bm25_weight == 0.9
    cfg.save()
    assert _file(home)["search"] == {"bm25_weight": 0.9}


def test_a_retired_top_level_key_is_not_written_back(home, monkeypatch):
    from techne import config as config_mod

    monkeypatch.setattr(config_mod, "RETIRED_KEYS", frozenset({"old_knob"}))
    (home / "config.yaml").write_text(yaml.safe_dump({cm.VERSION_KEY: 2, "old_knob": 1}))
    Config.load().save()
    assert _file(home) == {cm.VERSION_KEY: 2}


def test_the_dead_sync_fields_are_gone():
    from techne.config import SyncConfig

    for name in ("auth", "audience", "extra"):
        assert not hasattr(SyncConfig(), name)


# --- migration ---------------------------------------------------------------


def _legacy_raw(**overrides) -> dict:
    """A minimal dict with the full-save signature, plus overrides by section."""
    raw = {
        "platform": "claude-code",
        "model": Config().model,
        "sync": {"adapter": "noop"},
        "threads": {
            "min_observations": 15,
            "link_threshold": 0.05,
            "detector": "heuristic",
            "summarizer": "heuristic",
        },
        "hooks": {"post_tool_use": True, "capture_bash": False, "capture_read": False},
    }
    for section, values in overrides.items():
        if isinstance(values, dict):
            raw.setdefault(section, {}).update(values)
        else:
            raw[section] = values
    return raw


def _migrate(raw, home):
    return cm.migrate_raw(raw, Config().to_yaml_dict(), home)


def test_migration_unpins_past_defaults_and_keeps_deliberate_values(tmp_path):
    raw = _legacy_raw(
        reflect={"emit_max_chars": 100_000, "min_confidence": "high"},
        hooks={"per_turn_recall": False, "per_turn_recall_chars": 450, "per_turn_recall_limit": 7},
        threads={"link_threshold": 0.85},
    )
    out, report = _migrate(raw, tmp_path)
    assert out == {
        "reflect": {"min_confidence": "high"},
        "hooks": {"per_turn_recall_limit": 7},
    }
    assert "reflect.emit_max_chars" in report.unpinned
    assert ("reflect.emit_max_chars", 100_000, 45_000) in report.changed
    assert ("hooks.per_turn_recall", False, True) in report.changed
    assert ("threads.link_threshold", 0.85, 0.05) in report.changed
    # at today's default already: un-pinned, but not a change of value
    assert "platform" in report.unpinned
    assert all(k != "platform" for k, _, _ in report.changed)


def test_migration_keeps_a_value_no_release_shipped(tmp_path):
    out, _ = _migrate(_legacy_raw(reflect={"emit_max_chars": 60_000}), tmp_path)
    assert out["reflect"] == {"emit_max_chars": 60_000}


def test_migration_is_strict_about_yaml_booleans(tmp_path):
    """`capture_bash: 0` is not the `false` any release wrote, so it is kept."""
    out, _ = _migrate(_legacy_raw(hooks={"capture_bash": 0}), tmp_path)
    assert out["hooks"] == {"capture_bash": 0}


def test_migration_keeps_unknown_keys_and_drops_retired_ones(tmp_path):
    raw = _legacy_raw(
        search={"expansion_ratio": 2, "mmr_lambda": 0.3, "mystery": 1},
        hooks={"future_hook": True},
        sync={"auth": "tok", "tenant": "t-1"},
        not_a_techne_key="x",
    )
    out, report = _migrate(raw, tmp_path)
    assert out["search"] == {"mystery": 1}
    assert out["hooks"] == {"future_hook": True}
    assert out["sync"] == {"auth": "tok", "tenant": "t-1"}
    assert out["not_a_techne_key"] == "x"
    assert report.retired == ["search.expansion_ratio", "search.mmr_lambda"]


def test_migration_is_idempotent(tmp_path):
    out, report = _migrate(_legacy_raw(reflect={"emit_max_chars": 100_000}), tmp_path)
    assert report is not None
    again, second = _migrate(out, tmp_path)
    assert second is None and again == out


def test_a_versioned_or_hand_written_file_is_never_migrated(tmp_path):
    versioned = {**_legacy_raw(), cm.VERSION_KEY: 2}
    assert _migrate(versioned, tmp_path) == (versioned, None)
    partial = {"reflect": {"emit_max_chars": 100_000}}  # someone typed this
    assert _migrate(partial, tmp_path) == (partial, None)


def test_emit_max_chars_100000_on_an_old_file_becomes_the_45000_default(home):
    """The live example in #1070: #993's fix never reached an old install."""
    _legacy(home, "v0.16.1")
    assert _file(home)["reflect"]["emit_max_chars"] == 100_000
    assert Config.load().reflect.emit_max_chars == 45_000


def test_save_backs_up_a_legacy_file_once(home):
    original = _legacy(home, "v0.16.1").read_bytes()
    cfg = Config.load()
    cfg.save()
    backups = _backups(home)
    assert len(backups) == 1 and cfg.config_backup == backups[0]
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
    # the rewritten file is sparse and versioned; saving again makes no second copy
    assert _file(home)[cm.VERSION_KEY] == cm.CONFIG_VERSION
    Config.load().save()
    assert len(_backups(home)) == 1


@pytest.mark.parametrize("tag", ["v0.16.1", "v0.17.0"])
def test_a_real_old_config_loads_with_current_defaults(home, tag):
    """Fixtures are real `techne init --platform standalone` output from each tag."""
    _legacy(home, tag)
    cfg = Config.load()
    assert cfg.platform == "standalone"  # the one deliberate value
    assert cfg.hooks.per_turn_recall is True
    assert cfg.hooks.capture_bash is True
    assert cfg.hooks.codex_grep_assist is True
    assert cfg.hooks.codex_reindex_on_edit is True
    assert cfg.hooks.codex_edit_rail is True
    assert cfg.hooks.per_turn_recall_chars == 1000
    assert cfg.reflect.emit_max_chars == 45_000
    assert cfg.db_path == home / "techne.db"
    assert cfg.pinned_keys() == ["platform"]
    assert "search.expansion_ratio" in cfg.config_migration.retired


def test_init_on_a_real_old_config_rewrites_it_and_says_what_moved(home, monkeypatch):
    from techne.cli import main

    original = _legacy(home, "v0.17.0").read_bytes()
    result = CliRunner().invoke(
        main, ["init", "--platform", "standalone", "--no-vectors", "--posture", "observe"]
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if "migrated to sparse form" in ln]
    assert len(lines) == 1
    assert "reflect.emit_max_chars 100000->45000" in lines[0]
    assert "hooks.per_turn_recall false->true" in lines[0]
    assert _file(home) == {cm.VERSION_KEY: cm.CONFIG_VERSION, "platform": "standalone"}
    (backup,) = _backups(home)
    assert backup.read_bytes() == original
    assert str(backup) in lines[0]
    # A second init has nothing to migrate.
    again = CliRunner().invoke(
        main, ["init", "--platform", "standalone", "--no-vectors", "--posture", "observe"]
    )
    assert again.exit_code == 0, again.output
    assert "migrated to sparse form" not in again.output
    assert len(_backups(home)) == 1


# --- doctor visibility -------------------------------------------------------


def test_doctor_names_pinned_keys_and_a_pending_migration(home, capsys):
    from techne.cli import _render_config_pins

    _legacy(home, "v0.16.1")
    _render_config_pins(Config.load())
    out = capsys.readouterr().out
    assert "pins 1 key(s): platform." in out
    assert "migrated to sparse form" in out and "`techne init` rewrites" in out

    Config.load().save()
    _render_config_pins(Config.load())
    out = capsys.readouterr().out
    assert "pins 1 key(s): platform." in out
    assert "migrated" not in out


# --- the hook fast path sees a changed default ---------------------------------


def test_fast_cache_goes_stale_when_the_defaults_module_changes(home, tmp_path, monkeypatch):
    """A sparse file does not change when a default does, so the cache stamp must
    cover the defaults module too, or pre_tool_use keeps the old value."""
    from techne import fastconfig

    fake_defaults = tmp_path / "config.py"
    fake_defaults.write_text("a = 1\n")
    monkeypatch.setattr(fastconfig, "_DEFAULTS_SOURCE", fake_defaults)
    Config().save()
    Config.load()  # writes the cache
    assert fastconfig.load_fast() is not None
    fake_defaults.write_text("a = 22\n")  # an upgrade rewrote config.py
    assert fastconfig.load_fast() is None


# --- hand edits made before `techne init` (Prudence, PR #1118 r1) ---------------

_OPT_OUTS = {
    ("hooks", "per_turn_recall"): False,
    ("hooks", "capture_bash"): False,
    ("reflect", "emit_max_chars"): 100_000,
}


def _hand_edit(home: Path) -> None:
    data = _file(home)
    for (section, key), value in _OPT_OUTS.items():
        data[section][key] = value
    (home / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def _pending_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if "not in effect until it is rewritten" in ln]


def test_opt_outs_typed_into_a_legacy_file_are_named_until_init(home, capsys):
    """Upgrade, then set the three keys by hand, no init: the loader un-pins them
    again, so the file says false while true is in effect. doctor must say so."""
    from techne.cli import _render_config_pins

    _legacy(home, "v0.17.0")
    _hand_edit(home)
    cfg = Config.load()
    assert cfg.hooks.per_turn_recall is True  # the defect being reported, not fixed
    _render_config_pins(cfg)
    (line,) = _pending_lines(capsys.readouterr().out)
    assert "hooks.per_turn_recall (file: false, in effect: true)" in line
    assert "hooks.capture_bash (file: false, in effect: true)" in line
    assert "reflect.emit_max_chars (file: 100000, in effect: 45000)" in line
    assert "Run `techne init` to rewrite config.yaml, then re-set any key" in line


def test_opt_outs_set_after_init_stay_pinned(home, capsys):
    from techne.cli import _render_config_pins, main

    _legacy(home, "v0.17.0")
    result = CliRunner().invoke(
        main, ["init", "--platform", "standalone", "--no-vectors", "--posture", "observe"]
    )
    assert result.exit_code == 0, result.output
    data = _file(home)
    for (section, key), value in _OPT_OUTS.items():
        data.setdefault(section, {})[key] = value
    (home / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False))

    cfg = Config.load()
    assert cfg.config_migration is None
    assert cfg.hooks.per_turn_recall is False
    assert cfg.hooks.capture_bash is False
    assert cfg.reflect.emit_max_chars == 100_000
    _render_config_pins(cfg)
    assert _pending_lines(capsys.readouterr().out) == []
    # and a later init keeps them
    CliRunner().invoke(
        main, ["init", "--platform", "standalone", "--no-vectors", "--posture", "observe"]
    )
    assert Config.load().hooks.per_turn_recall is False


def test_the_session_start_banner_names_the_same_keys(home):
    """Operator surface: systemMessage, where hook drift already goes."""
    import json
    import subprocess
    import sys

    _legacy(home, "v0.17.0")
    _hand_edit(home)
    env = {
        **os.environ,
        "HOME": str(home.parent),
        "TECHNE_HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home.parent / ".claude"),
    }
    r = subprocess.run(
        [sys.executable, "-m", "techne.hooks.session_start"],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    banner = json.loads(r.stdout).get("systemMessage", "")
    (line,) = _pending_lines(banner)
    assert "hooks.per_turn_recall (file: false, in effect: true)" in line
    assert "reflect.emit_max_chars (file: 100000, in effect: 45000)" in line
