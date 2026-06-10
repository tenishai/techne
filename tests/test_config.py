"""Tests for Config platform field type enforcement."""

import pytest
import yaml

from techne.config import Config


def test_config_load_rejects_invalid_platform(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"platform": "bogus_value"}))
    with pytest.raises(ValueError, match="Invalid platform 'bogus_value'"):
        Config.load(config_file)


def test_config_load_accepts_valid_platforms(tmp_path):
    for platform in ("claude-code", "codex", "standalone"):
        config_file = tmp_path / f"config_{platform.replace('-', '_')}.yaml"
        config_file.write_text(yaml.dump({"platform": platform}))
        cfg = Config.load(config_file)
        assert cfg.platform == platform


def test_threads_detector_defaults_to_heuristic():
    cfg = Config()
    assert cfg.threads.detector == "heuristic"
    assert cfg.threads.summarizer == "heuristic"


def test_threads_detector_loaded_from_yaml(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({
        "threads": {"detector": "llm", "summarizer": "llm"}
    }))
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
    f.write_text(yaml.dump({
        "threads": {
            "tool_families": {
                "mcp__claude_ai_Datadog__search": "mcp:datadog",
                "mcp__slack__post": "mcp:slack",
            }
        }
    }))
    cfg = Config.load(f)
    assert cfg.threads.tool_families["mcp__claude_ai_Datadog__search"] == "mcp:datadog"
    assert cfg.threads.tool_families["mcp__slack__post"] == "mcp:slack"


def test_threads_entity_patterns_loaded(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.dump({
        "threads": {"entity_patterns": [r"service:([a-z0-9-]+)", r"C[A-Z0-9]{8,12}"]}
    }))
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
