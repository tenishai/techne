"""Tests for Config platform field type enforcement."""

from pathlib import Path
from typing import get_args

import pytest
import yaml

from techne.config import PLATFORMS, Config, Platform, PostureConfig, techne_home


def test_techne_home_unset_keeps_default(monkeypatch):
    """Unset TECHNE_HOME → the historical ~/.techne root (backward compatible)."""
    monkeypatch.delenv("TECHNE_HOME", raising=False)
    assert techne_home() == Path.home() / ".techne"


def test_techne_home_env_relocates_root(monkeypatch, tmp_path):
    """TECHNE_HOME relocates db_path/data_dir when no config.yaml exists, so each
    agent on one OS user can own a separate store (techne#112)."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    cfg = Config.load()
    assert cfg.db_path == tmp_path / "techne.db"
    assert cfg.data_dir == tmp_path


def test_techne_home_explicit_db_path_still_wins(monkeypatch, tmp_path):
    """An explicit db_path in config.yaml overrides the TECHNE_HOME default;
    an absent data_dir falls back to the TECHNE_HOME root."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    explicit = tmp_path / "custom" / "mem.db"
    (tmp_path / "config.yaml").write_text(yaml.dump({"db_path": str(explicit)}))
    cfg = Config.load()
    assert cfg.db_path == explicit
    assert cfg.data_dir == tmp_path


def test_config_load_rejects_invalid_platform(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"platform": "bogus_value"}))
    with pytest.raises(ValueError, match="Invalid platform 'bogus_value'"):
        Config.load(config_file)


def test_config_load_accepts_every_declared_platform(tmp_path):
    # Every platform in the Literal must validate — covers the whole enum, not
    # a hand-maintained subset, so a newly-added client can't be rejected.
    for platform in PLATFORMS:
        config_file = tmp_path / f"config_{platform.replace('-', '_')}.yaml"
        config_file.write_text(yaml.dump({"platform": platform}))
        cfg = Config.load(config_file)
        assert cfg.platform == platform


def test_platforms_is_single_source_of_truth():
    # PLATFORMS must equal the Literal's args, and the CLI `--platform` choices
    # must derive from it — so the three previously-duplicated lists can't drift
    # (the CLI list was missing 'cabal' before A.2).
    assert PLATFORMS == get_args(Platform)
    assert "opencode" in PLATFORMS  # the client validated in #57

    from techne.cli import init

    choice = next(p for p in init.params if p.name == "platform").type
    assert tuple(choice.choices) == PLATFORMS


def test_threads_detector_defaults_to_heuristic():
    cfg = Config()
    assert cfg.threads.detector == "heuristic"
    assert cfg.threads.summarizer == "heuristic"


def test_threads_empty_block_inherits_dataclass_defaults(tmp_path):
    # #82: a `threads:` block that omits a field must fall back to the dataclass
    # default, not a drifted literal. The old loader hardcoded link_threshold's
    # fallback at the unreachable 0.85 while the default was 0.05 — so a config
    # with `threads: {}` silently disabled all cross-thread linking.
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"threads": {}}))
    loaded = Config.load(f)
    defaults = Config().threads
    assert loaded.threads.link_threshold == defaults.link_threshold == 0.05
    assert loaded.threads.min_observations == defaults.min_observations
    assert loaded.threads.stale_after_days == defaults.stale_after_days
    assert loaded.threads.complete_after_hours == defaults.complete_after_hours


def test_threads_detector_loaded_from_yaml(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"threads": {"detector": "llm", "summarizer": "llm"}}))
    cfg = Config.load(f)
    assert cfg.threads.detector == "llm"
    assert cfg.threads.summarizer == "llm"


def test_threads_rejects_invalid_detector(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"threads": {"detector": "magic"}}))
    with pytest.raises(ValueError, match="Invalid threads.detector"):
        Config.load(f)


def test_threads_rejects_invalid_summarizer(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"threads": {"summarizer": "magic"}}))
    with pytest.raises(ValueError, match="Invalid threads.summarizer"):
        Config.load(f)


def test_threads_tool_families_loaded(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(
        yaml.dump(
            {
                "threads": {
                    "tool_families": {
                        "mcp__claude_ai_Datadog__search": "mcp:datadog",
                        "mcp__slack__post": "mcp:slack",
                    }
                }
            }
        )
    )
    cfg = Config.load(f)
    assert cfg.threads.tool_families["mcp__claude_ai_Datadog__search"] == "mcp:datadog"
    assert cfg.threads.tool_families["mcp__slack__post"] == "mcp:slack"


def test_threads_entity_patterns_loaded(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(
        yaml.dump({"threads": {"entity_patterns": [r"service:([a-z0-9-]+)", r"C[A-Z0-9]{8,12}"]}})
    )
    cfg = Config.load(f)
    assert len(cfg.threads.entity_patterns) == 2


def test_threads_round_trip_with_extensions(tmp_path):
    cfg = Config()
    cfg.threads.tool_families = {"X": "y"}
    cfg.threads.entity_patterns = [r"foo"]
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.threads.tool_families == {"X": "y"}
    assert reloaded.threads.entity_patterns == ["foo"]


def test_platform_accepts_cabal(tmp_path):
    """`cabal` is a valid platform value (added in v0.6.0 for Cabal runtime)."""
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"platform": "cabal"}))
    cfg = Config.load(f)
    assert cfg.platform == "cabal"


def test_llm_api_base_and_key_round_trip(tmp_path):
    """When set, llm_api_base + llm_api_key load and save losslessly. When
    unset (empty string), they don't appear in the saved YAML."""
    f = tmp_path / "in.yaml"
    f.write_text(
        yaml.dump(
            {
                "llm_api_base": "http://litellm:4000",
                "llm_api_key": "sk-cabal-rook-internal-key",
            }
        )
    )
    cfg = Config.load(f)
    assert cfg.llm_api_base == "http://litellm:4000"
    assert cfg.llm_api_key == "sk-cabal-rook-internal-key"

    out = tmp_path / "out.yaml"
    cfg.save(out)
    text = out.read_text()
    assert "llm_api_base: http://litellm:4000" in text
    assert "llm_api_key: sk-cabal-rook-internal-key" in text

    reloaded = Config.load(out)
    assert reloaded.llm_api_base == cfg.llm_api_base
    assert reloaded.llm_api_key == cfg.llm_api_key


def test_llm_fields_omitted_when_empty(tmp_path):
    """Default Config (no LiteLLM proxy) shouldn't write llm_api_base / key
    to YAML — keeps the saved config clean for the common case where techne
    uses LiteLLM's standard provider routing via env vars."""
    out = tmp_path / "default.yaml"
    Config().save(out)
    text = out.read_text()
    assert "llm_api_base" not in text
    assert "llm_api_key" not in text


def test_make_generate_fn_passes_proxy_fields(tmp_path, monkeypatch):
    """_make_generate_fn forwards api_base + api_key to litellm.completion()
    when configured. Without those, it omits them (relying on litellm's
    env-var routing)."""
    pytest.importorskip("litellm")
    from techne.config import Config
    from techne.core.threads import _make_generate_fn

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    import litellm

    monkeypatch.setattr(litellm, "completion", fake_completion)

    # With proxy fields set: both pass through.
    cfg = Config()
    cfg.llm_api_base = "http://litellm:4000"
    cfg.llm_api_key = "sk-test"
    _make_generate_fn(cfg)("hi")
    assert captured["api_base"] == "http://litellm:4000"
    assert captured["api_key"] == "sk-test"

    # Without proxy fields: neither key appears (lets litellm's defaults win).
    captured.clear()
    _make_generate_fn(Config())("hi")
    assert "api_base" not in captured
    assert "api_key" not in captured


def test_heuristic_config_defaults():
    """HeuristicConfig fields default to the module-constant values."""
    from techne.config import HeuristicConfig

    cfg = HeuristicConfig()
    assert cfg.boundary_threshold == 0.7
    assert cfg.user_prompt_score == 0.3


def test_heuristic_config_loaded_from_yaml(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(
        yaml.dump(
            {
                "heuristic": {
                    "boundary_threshold": 0.5,
                    "user_prompt_score": 0.1,
                }
            }
        )
    )
    cfg = Config.load(f)
    assert cfg.heuristic.boundary_threshold == 0.5
    assert cfg.heuristic.user_prompt_score == 0.1


def test_heuristic_config_round_trip(tmp_path):
    cfg = Config()
    cfg.heuristic.boundary_threshold = 0.6
    cfg.heuristic.user_prompt_score = 0.2
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.heuristic.boundary_threshold == 0.6
    assert reloaded.heuristic.user_prompt_score == 0.2


def test_reflect_emit_bounds_round_trip(tmp_path):
    """The reflect emit-bounding knobs load and save losslessly (techne#268)."""
    cfg = Config()
    cfg.reflect.emit_max_threads = 7
    cfg.reflect.emit_max_chars = 12345
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.reflect.emit_max_threads == 7
    assert reloaded.reflect.emit_max_chars == 12345


def test_a_retired_sources_block_loads_and_survives_a_save(tmp_path):
    """Issue 1262 deleted `sources.claude_code_transcripts` and the tailer it
    configured, which nothing ever constructed. A config.yaml that still sets it
    must load without error and keep the block on save, like any key this version
    does not recognise: removing a dead key is no reason to rewrite an operator's
    file."""
    f = tmp_path / "config.yaml"
    sources = {"claude_code_transcripts": {"paths": ["/tmp/projects"], "debounce_ms": 500}}
    f.write_text(yaml.dump({"sources": sources}))
    cfg = Config.load(f)
    assert not hasattr(cfg, "sources")

    out = tmp_path / "out.yaml"
    cfg.save(out)
    assert yaml.safe_load(out.read_text())["sources"] == sources


def test_sync_round_trip_preserves_all_fields(tmp_path):
    """Config.save() once wrote only sync.adapter (plus url/auth/path if truthy),
    silently dropping sync.audience and adapter-specific keys. techne#1074 removed
    the `auth`/`audience`/`extra` fields because nothing read them, but the FILE
    must still round-trip losslessly: save writes unrecognised keys back as-is."""
    f = tmp_path / "in.yaml"
    sync = {
        "adapter": "team-archivist",
        "url": "https://archivist.example.com",
        "auth": "token-xyz",
        "audience": "techne.archivist",
        "path": "/var/data/sync",
        "tenant_id": "t-42",  # adapter-specific key
        "region": "us-east",  # adapter-specific key
    }
    f.write_text(yaml.dump({"sync": sync}))
    cfg = Config.load(f)
    assert not hasattr(cfg.sync, "auth")
    assert not hasattr(cfg.sync, "audience")
    assert not hasattr(cfg.sync, "extra")

    out = tmp_path / "out.yaml"
    cfg.save(out)
    assert yaml.safe_load(out.read_text())["sync"] == sync
    reloaded = Config.load(out)
    assert reloaded.sync.adapter == "team-archivist"
    assert reloaded.sync.url == "https://archivist.example.com"
    assert reloaded.sync.path == "/var/data/sync"


def test_posture_config_defaults():
    """PostureConfig defaults are observe/False."""
    p = PostureConfig()
    assert p.mode == "observe"
    assert p.cc_memory_detected is False

    cfg = Config()
    assert cfg.posture.mode == "observe"
    assert cfg.posture.cc_memory_detected is False


def test_posture_config_round_trip(tmp_path):
    """posture block survives save() → load() without loss (techne#107 PR4)."""
    cfg = Config()
    cfg.posture.mode = "spool-primary"
    cfg.posture.cc_memory_detected = True
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.posture.mode == "spool-primary"
    assert reloaded.posture.cc_memory_detected is True


def test_posture_invalid_mode_raises(tmp_path):
    """techne#167: an unrecognised posture.mode RAISES, like every other enum
    validator — silently clamping to 'observe' would mask a typo that left the
    unattended writer un-gated."""
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"posture": {"mode": "banana", "cc_memory_detected": True}}))
    with pytest.raises(ValueError, match="Invalid posture.mode"):
        Config.load(f)


def test_injection_and_staleness_round_trip(tmp_path):
    """techne#167: save() previously dropped injection + staleness_hours (parsed by
    _from_dict but never serialized), so re-running `techne init` silently reset any
    customization. They must now survive save() → load()."""
    cfg = Config()
    cfg.injection.enabled = False
    cfg.injection.char_budget = 4096
    cfg.injection.recent_reserve_frac = 0.25
    cfg.staleness_hours = 36
    cfg.tag_threshold = 0.5  # techne#352: config-driven, must survive round-trip
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.injection.enabled is False
    assert reloaded.injection.char_budget == 4096
    assert reloaded.injection.recent_reserve_frac == 0.25
    assert reloaded.staleness_hours == 36
    assert reloaded.tag_threshold == 0.5


def test_save_is_owner_only(tmp_path):
    """techne#167 SECURITY: the config can hold secrets (llm_api_key, sync.auth);
    save() must create the file owner-only (0600) from the start — never a
    world-readable window. Verified even when no key is set (sync.auth, future
    secrets)."""
    import stat

    out = tmp_path / "config.yaml"
    cfg = Config()
    cfg.llm_api_key = "sk-secret-value"
    cfg.save(out)
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # No leftover temp from the atomic write.
    assert not list(out.parent.glob(out.name + ".tmp*"))
    # And a key-less config is still 0600.
    out2 = tmp_path / "nokey.yaml"
    Config().save(out2)
    assert stat.S_IMODE(out2.stat().st_mode) == 0o600


def test_posture_absent_block_gives_defaults(tmp_path):
    """A config.yaml that predates posture must load cleanly with default values."""
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"platform": "claude-code"}))
    cfg = Config.load(f)
    assert cfg.posture.mode == "observe"
    assert cfg.posture.cc_memory_detected is False


def test_correction_reserve_frac_round_trip(tmp_path):
    """correction_reserve_frac must survive save() → load() (mirrors the
    recent_reserve_frac check in test_injection_and_staleness_round_trip)."""
    cfg = Config()
    cfg.injection.correction_reserve_frac = 0.15
    out = tmp_path / "config.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.injection.correction_reserve_frac == 0.15


def test_correction_reserve_frac_default():
    """Default correction_reserve_frac is 0.3 (matches select_tier0_injection default)."""
    cfg = Config()
    assert cfg.injection.correction_reserve_frac == 0.3


def test_a_config_carrying_the_removed_mmr_lambda_still_loads(tmp_path, monkeypatch):
    """Backward compat for #420: both live configs on this machine carry a
    serialized `search.mmr_lambda: 0.3`, so removing the field must not make
    them fail to load.

    A FORWARD GUARD, not an active proof: `_from_dict` reads every key through
    an explicit `s.get(...)` and structurally ignores unknown ones, and there is
    no strict validator, so the load half cannot fail today — only the
    `not hasattr` line has teeth against this removal. It fires the day someone
    adds strict schema parsing. (Prudence, PR #513.)"""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"search": {"mmr_lambda": 0.3, "bm25_weight": 0.9}})
    )

    cfg = Config.load()

    assert cfg.search.bm25_weight == 0.9
    assert not hasattr(cfg.search, "mmr_lambda")


def test_save_does_not_reintroduce_the_removed_knob(tmp_path, monkeypatch):
    """A load->save round-trip must not write it back, or `techne init` would
    keep reseeding a knob that does nothing."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.dump({"search": {"mmr_lambda": 0.3}}))

    Config.load().save()

    assert "mmr_lambda" not in (tmp_path / "config.yaml").read_text()


# ---- loaded_from: did these values come from a file? (techne#480) ----------


def test_load_records_the_file_it_read(tmp_path, monkeypatch):
    """`loaded_from` is the contract callers use to tell a CHOICE from a DEFAULT."""
    from techne.config import Config

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("platform: claude-code\n")

    cfg = Config.load()

    assert cfg.loaded_from == config_file


def test_load_leaves_loaded_from_none_when_there_is_no_config_file(tmp_path, monkeypatch):
    """The never-initialised machine, which is the whole point of the field.

    Every value on this Config is a built-in default. Anything that reads an
    attribute here and treats it as an operator's answer is wrong.
    """
    from techne.config import Config

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    assert not (tmp_path / "config.yaml").exists()

    cfg = Config.load()

    assert cfg.loaded_from is None
    assert cfg.posture.mode == "observe"


def test_loaded_from_is_never_written_into_config_yaml(tmp_path, monkeypatch):
    """It describes a resolution, not a setting — same rule as egress_trust.

    Serializing it would put a path into the agent-writable file that a later
    load could read back as evidence about its own provenance.
    """
    import yaml

    from techne.config import Config

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    out = tmp_path / "config.yaml"

    cfg = Config()
    cfg.loaded_from = tmp_path / "somewhere-else.yaml"
    cfg.save(out)

    assert "loaded_from" not in yaml.safe_load(out.read_text())


def test_pattern_targets_accepts_a_scalar_and_normalises_case():
    """A bad roster fails SILENTLY — every token drops — so parsing must be forgiving.

    Found reviewing #1014: `pattern_targets: cabal` iterated the STRING, giving
    {'c','a','b','l'}, and `[Cabal]` never matched because _apply_targets
    lowercases what it parses. Both left every new pattern untagged with no error.
    """
    from techne.config import _pattern_targets

    assert _pattern_targets("cabal") == ["cabal"]
    assert _pattern_targets(["Cabal", "TECHNE"]) == ["cabal", "techne"]
    assert _pattern_targets([" liminal "]) == ["liminal"]
    for empty in (None, "", [], 123, {}):
        assert _pattern_targets(empty) == ["techne"], empty


def test_pattern_targets_round_trip_through_config(tmp_path, monkeypatch):
    from techne.config import Config

    home = tmp_path / "h"
    home.mkdir()
    (home / "config.yaml").write_text("architect:\n  pattern_targets: Cabal\n")
    monkeypatch.setenv("TECHNE_HOME", str(home))

    cfg = Config.load()
    assert cfg.architect.pattern_targets == ["cabal"]
    assert cfg.to_yaml_dict()["architect"]["pattern_targets"] == ["cabal"]


def test_a_partial_hooks_section_inherits_every_default(tmp_path):
    """#1028 r1: Config.load's per-field fallbacks were a hand-written second copy of
    the HookConfig defaults, and reverting all six new defaults there left the suite
    green. The fallbacks now come from HookConfig itself; this pins that an upgraded
    config with a `hooks:` section gets exactly the dataclass defaults."""
    from dataclasses import asdict

    from techne.config import HookConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("hooks:\n  post_tool_use: true\n")
    assert asdict(Config.load(cfg_file).hooks) == asdict(HookConfig())
