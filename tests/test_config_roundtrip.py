"""Every config key `_from_dict` READS must survive a load->save cycle (techne#620).

`conflict` was read and written by nothing, so a hand-written block was silently
dropped by any load->save — and `techne init` IS such a cycle. No error, no diff
to notice, the operator's setting simply gone.

The instance is one line. The CLASS is what this file guards: these tests derive
the read-key set from `_from_dict`'s own source, so a NEW read-only block fails
here the day it is added rather than being found by accident years later, which
is how #620 surfaced (as a side effect of testing something else for #586).
"""

from __future__ import annotations

import inspect
import re

from techne.config import Config

#: Keys `to_yaml_dict` emits ONLY when populated, deliberately — "avoid polluting
#: a minimal config with empty stubs". They are exercised by the populated
#: round-trip below rather than the default-Config one.
#: `db_path` and `data_dir` are omitted when they equal the TECHNE_HOME default
#: (techne#947): persisting the resolved default pinned a copied home to the
#: original store.
CONDITIONAL_KEYS = frozenset(
    {
        "identity_dir",
        "llm_api_base",
        "llm_api_key",
        "sources",
        "stores",
        "connections",
        "db_path",
        "data_dir",
    }
)


def _keys_read_by_from_dict() -> set[str]:
    """The top-level YAML keys `Config._from_dict` looks for, from its own source.

    Deliberately derived rather than hand-listed: a hand-listed copy is one more
    thing to forget to update, and forgetting is the entire defect here.
    """
    return set(re.findall(r'"(\w+)" in raw', inspect.getsource(Config._from_dict)))


def test_every_unconditional_read_key_is_also_written():
    """The general guard. `conflict` is the instance; this catches the class."""
    read = _keys_read_by_from_dict()
    emitted = set(Config().to_yaml_dict())
    dropped = sorted(k for k in read if k not in emitted and k not in CONDITIONAL_KEYS)
    assert not dropped, (
        f"config keys read by _from_dict but never written by to_yaml_dict: {dropped}. "
        "A hand-written block for these is silently lost on any load->save cycle, "
        "and `techne init` is one (techne#620)."
    )


def test_a_hand_written_conflict_block_survives_a_round_trip():
    """The reported defect, driven end to end through the real surfaces."""
    raw = {
        "conflict": {
            "model": "claude-haiku-4-5-20251001",
            "max_comparison_candidates": 17,
            "max_items_per_emit": 3,
            "log_raw_response": True,
        }
    }

    cfg = Config._from_dict(raw)
    assert cfg.conflict.max_items_per_emit == 3, "precondition: the block was read"

    round_tripped = Config._from_dict(cfg.to_yaml_dict())

    assert round_tripped.conflict.model == "claude-haiku-4-5-20251001"
    assert round_tripped.conflict.max_comparison_candidates == 17
    assert round_tripped.conflict.max_items_per_emit == 3
    assert round_tripped.conflict.log_raw_response is True


def test_a_populated_config_loses_no_top_level_key():
    """The conditional keys too, so CONDITIONAL_KEYS cannot quietly become an
    excuse list. Each is populated, then required to survive."""
    from pathlib import Path

    raw = {
        "db_path": "/srv/techne/mem.db",
        "data_dir": "/srv/techne",
        "identity_dir": "/tmp/identity",
        "llm_api_base": "http://localhost:11434",
        "llm_api_key": "sk-test",
        "sources": {
            "claude_code_transcripts": {
                "paths": ["/tmp/projects"],
                "include_globs": ["*.jsonl"],
                "exclude_globs": [],
                "debounce_ms": 500,
                "adapter_name": "claude-code",
            }
        },
        "conflict": {"max_items_per_emit": 9},
    }

    cfg = Config._from_dict(raw)
    out = cfg.to_yaml_dict()

    for key in raw:
        assert key in out, f"{key!r} was read but not written back (techne#620)"

    assert out["identity_dir"] == str(Path("/tmp/identity"))
    assert out["llm_api_base"] == "http://localhost:11434"
    assert out["sources"]["claude_code_transcripts"]["paths"] == ["/tmp/projects"]
    assert out["conflict"]["max_items_per_emit"] == 9
