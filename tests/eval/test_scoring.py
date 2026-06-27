"""Unit tests for the eval rubric (techne#118). NOT eval-marked — these run in the
default suite to lock the score_task contract (the formula + the detail-field schema
that PR2's cost/precision thesis consumes programmatically) and the Stage-2 helpers
(is_nonanswer, score_answer) added in PR3."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scoring import is_nonanswer, score_answer, score_task  # noqa: E402

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


# ---------------------------------------------------------------------------
# Stage-2 helpers — is_nonanswer (techne#118 PR3)
# ---------------------------------------------------------------------------


def test_is_nonanswer_empty():
    assert is_nonanswer("") is True
    assert is_nonanswer("   ") is True


def test_is_nonanswer_positive_cases():
    assert is_nonanswer("I don't know.") is True
    assert is_nonanswer("I do not know.") is True
    assert is_nonanswer("No information available.") is True
    assert is_nonanswer("I cannot answer that.") is True
    assert is_nonanswer("That is not available.") is True
    assert is_nonanswer("I have no memory of this.") is True
    assert is_nonanswer("No memory found.") is True
    assert is_nonanswer("No relevant entries in memory.") is True


def test_is_nonanswer_negative_cases():
    assert is_nonanswer("The deploy target is dev03.") is False
    assert is_nonanswer("According to memory, the path is /opt/eval/v2.") is False
    assert is_nonanswer("Techne does not support auto-bisect.") is False


def test_is_nonanswer_case_insensitive():
    assert is_nonanswer("I DON'T KNOW") is True
    assert is_nonanswer("NO INFORMATION") is True


# ---------------------------------------------------------------------------
# Stage-2 helpers — score_answer (techne#118 PR3)
# ---------------------------------------------------------------------------

_STAGE2_TASK = {"id": "absent-feature", "category": "absence"}


def test_score_answer_correct_verdict():
    verdict = {"verdict": "CORRECT", "reason": "model declined correctly"}
    score, detail = score_answer(_STAGE2_TASK, "I don't know.", verdict)
    assert score == 1.0
    assert detail["verdict"] == "CORRECT"
    assert detail["nonanswer"] is True


def test_score_answer_incorrect_verdict():
    verdict = {"verdict": "INCORRECT", "reason": "model asserted a stale fact"}
    score, detail = score_answer(_STAGE2_TASK, "The feature exists.", verdict)
    assert score == 0.0
    assert detail["verdict"] == "INCORRECT"
    assert detail["nonanswer"] is False


def test_score_answer_nonanswer_recorded_not_zeroed():
    """A non-answer on a negative-polarity task can score 1.0 (judge decides)."""
    verdict = {"verdict": "CORRECT", "reason": "correct decline"}
    score, detail = score_answer(_STAGE2_TASK, "I don't know.", verdict)
    assert score == 1.0
    assert detail["nonanswer"] is True  # flag is present but did NOT zero the score


def test_score_answer_detail_shape():
    verdict = {"verdict": "INCORRECT", "reason": "bad answer"}
    _, detail = score_answer(_STAGE2_TASK, "some answer", verdict)
    assert set(detail.keys()) == {"verdict", "reason", "nonanswer"}
