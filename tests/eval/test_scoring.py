"""Unit tests for the eval rubric (techne#118). NOT eval-marked — these run in the
default suite to lock the score_task contract (the formula + the detail-field schema
that PR2's cost/precision thesis consumes programmatically)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scoring import score_task  # noqa: E402

_RESOLVER = {"g": "knowledge:1"}


def _row(type_: str, id_: int, content: str) -> dict:
    return {"type": type_, "id": id_, "content": content}


def test_score_formula_unchanged():
    """0.7*recall + 0.3*coverage; a full hit with the must-phrase scores 1.0."""
    task = {"gold_keys": ["g"], "must_include_phrases": ["alpha"]}
    retrieved = [_row("knowledge", 1, "alpha beta")]
    score, _ = score_task(task, retrieved, _RESOLVER)
    assert score == 1.0


def test_killed_by_exclude_zeroes_score():
    task = {"gold_keys": ["g"], "must_exclude_phrases": ["stale"]}
    retrieved = [_row("knowledge", 1, "a stale value")]  # gold hit, but excluded phrase in top
    score, detail = score_task(task, retrieved, _RESOLVER)
    assert score == 0.0
    assert detail["killed_by_exclude"] is True


def test_detail_carries_cost_axis_fields():
    """techne#118 PR1: precision + token_cost are always present (PR2 reads them)."""
    task = {"gold_keys": ["g"]}
    retrieved = [_row("knowledge", 1, "gold"), _row("knowledge", 9, "distractor")]
    _, detail = score_task(task, retrieved, _RESOLVER)
    assert "precision" in detail and "token_cost" in detail
    assert detail["precision"] == 0.5  # 1 gold of 2 results
    assert detail["token_cost"] == len("gold") + len("distractor")


def test_precision_zero_guarded_on_empty_retrieval():
    """The bare/null arm returns nothing — precision must be 0.0, never a ZeroDivision."""
    task = {"gold_keys": ["g"]}
    score, detail = score_task(task, [], _RESOLVER)
    assert detail["precision"] == 0.0
    assert detail["token_cost"] == 0
    assert detail["n_results"] == 0
