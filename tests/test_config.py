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


def test_sync_round_trip_preserves_all_fields(tmp_path):
    """Config.save() previously wrote only sync.adapter (plus url/auth/path if
    truthy), silently dropping sync.audience and sync.extra. A round-trip on a
    fully-populated sync block should be lossless."""
    f = tmp_path / "in.yaml"
    f.write_text(
        yaml.dump(
            {
                "sync": {
                    "adapter": "team-archivist",
                    "url": "https://archivist.example.com",
                    "auth": "token-xyz",
                    "audience": "techne.archivist",
                    "path": "/var/data/sync",
                    "tenant_id": "t-42",  # adapter-specific extra
                    "region": "us-east",  # adapter-specific extra
                }
            }
        )
    )
    cfg = Config.load(f)
    assert cfg.sync.audience == "techne.archivist"
    assert cfg.sync.extra == {"tenant_id": "t-42", "region": "us-east"}

    out = tmp_path / "out.yaml"
    cfg.save(out)
    reloaded = Config.load(out)
    assert reloaded.sync.adapter == "team-archivist"
    assert reloaded.sync.url == "https://archivist.example.com"
    assert reloaded.sync.auth == "token-xyz"
    assert reloaded.sync.audience == "techne.archivist"
    assert reloaded.sync.path == "/var/data/sync"
    assert reloaded.sync.extra == {"tenant_id": "t-42", "region": "us-east"}


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


def test_posture_invalid_mode_falls_back_to_observe(tmp_path):
    """An unrecognised posture.mode in YAML falls back to 'observe' instead of raising."""
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"posture": {"mode": "banana", "cc_memory_detected": True}}))
    cfg = Config.load(f)
    assert cfg.posture.mode == "observe"
    assert cfg.posture.cc_memory_detected is True


def test_posture_absent_block_gives_defaults(tmp_path):
    """A config.yaml that predates posture must load cleanly with default values."""
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({"platform": "claude-code"}))
    cfg = Config.load(f)
    assert cfg.posture.mode == "observe"
    assert cfg.posture.cc_memory_detected is False
