"""Tests for content-addressed spawn contracts — drift detection for agent handoffs."""

from __future__ import annotations

from techne.core.spawn_contract import freeze, record_spawn, spawn_history


def test_freeze_is_deterministic_and_content_addressed():
    a = freeze("tmpl", "the prompt", {"x": 1, "y": 2})
    b = freeze("tmpl", "the prompt", {"y": 2, "x": 1})  # key order must not matter
    assert a.digest == b.digest
    assert a.prompt_hash and a.inputs_hash and a.digest

    # A different prompt changes prompt_hash + digest, not inputs_hash.
    c = freeze("tmpl", "a DIFFERENT prompt", {"x": 1, "y": 2})
    assert c.prompt_hash != a.prompt_hash
    assert c.inputs_hash == a.inputs_hash
    assert c.digest != a.digest

    # Different inputs change inputs_hash + digest, not prompt_hash.
    d = freeze("tmpl", "the prompt", {"x": 1, "y": 3})
    assert d.inputs_hash != a.inputs_hash
    assert d.prompt_hash == a.prompt_hash
    assert d.digest != a.digest

    # template_id is part of the identity.
    assert freeze("other", "the prompt", {"x": 1, "y": 2}).digest != a.digest


def test_freeze_handles_missing_inputs():
    assert freeze("t", "p").digest == freeze("t", "p", {}).digest  # None == empty dict


def test_record_spawn_first_seen_then_drift(tmp_path):
    ledger = tmp_path / "spawn.jsonl"

    r1 = record_spawn(ledger, freeze("investigate", "PROMPT v1", {"url": "x"}))
    assert r1["drift"] == {
        "first_seen": True,
        "prompt_changed": False,
        "inputs_changed": False,
        "changed": False,
    }
    assert r1["prior_digest"] is None

    # Same template, identical contract → no drift.
    r2 = record_spawn(ledger, freeze("investigate", "PROMPT v1", {"url": "x"}))
    assert r2["drift"]["changed"] is False
    assert r2["prior_digest"] == r1["digest"]

    # Prompt template silently edited → prompt drift flagged.
    r3 = record_spawn(ledger, freeze("investigate", "PROMPT v2 (edited)", {"url": "x"}))
    assert r3["drift"]["prompt_changed"] is True
    assert r3["drift"]["inputs_changed"] is False
    assert r3["drift"]["changed"] is True

    # Input shape changed → inputs drift flagged.
    r4 = record_spawn(ledger, freeze("investigate", "PROMPT v2 (edited)", {"repo": "y"}))
    assert r4["drift"]["inputs_changed"] is True
    assert r4["drift"]["prompt_changed"] is False


def test_record_spawn_isolates_by_template(tmp_path):
    ledger = tmp_path / "spawn.jsonl"
    record_spawn(ledger, freeze("A", "pa", {"i": 1}))
    # A different template's first record is first_seen even though the ledger is non-empty.
    assert record_spawn(ledger, freeze("B", "pb", {"i": 1}))["drift"]["first_seen"] is True


def test_spawn_history(tmp_path):
    ledger = tmp_path / "spawn.jsonl"
    record_spawn(ledger, freeze("A", "p1"), recorded_at="2026-07-12T00:00:00Z")
    record_spawn(ledger, freeze("A", "p2"))
    record_spawn(ledger, freeze("B", "p3"))

    assert [r["template_id"] for r in spawn_history(ledger)] == ["A", "A", "B"]
    assert [r["prompt_hash"] for r in spawn_history(ledger, "A")] == [
        freeze("A", "p1").prompt_hash,
        freeze("A", "p2").prompt_hash,
    ]
    assert spawn_history(ledger, "nonexistent") == []
    assert spawn_history(ledger, "A")[0]["recorded_at"] == "2026-07-12T00:00:00Z"  # metadata kept


def test_history_empty_ledger(tmp_path):
    assert spawn_history(tmp_path / "nope.jsonl") == []


def test_ledger_tolerates_torn_line(tmp_path):
    ledger = tmp_path / "spawn.jsonl"
    record_spawn(ledger, freeze("A", "p1"))
    with open(ledger, "a", encoding="utf-8") as f:
        f.write('{"template_id": "A", "prompt_hash": "incomplete\n')  # a torn/garbage line
    record_spawn(ledger, freeze("A", "p2"))
    # The reader skips the torn line; history returns only the two valid rows.
    assert [r["prompt_hash"] for r in spawn_history(ledger, "A")] == [
        freeze("A", "p1").prompt_hash,
        freeze("A", "p2").prompt_hash,
    ]
