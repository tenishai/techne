"""Unit tests for the eval rubric (techne#118). NOT eval-marked — these run in the
default suite to lock the score_task contract (the formula + the detail-field schema
that PR2's cost/precision thesis consumes programmatically) and the Stage-2 helpers
(is_nonanswer, score_answer) added in PR3."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scoring import (  # noqa: E402
    aggregate_gold,
    false_positive_rate,
    is_nonanswer,
    mrr,
    recall_at_k,
    score_answer,
    score_task,
)

_RESOLVER = {"g": "knowledge:1"}


def _row(type_: str, id_: int, content: str) -> dict:
    # `source_table` since techne#579 — scoring keys gold ids on the TABLE
    # ("knowledge:1"), never on the row's own kind.
    return {"source_table": type_, "id": id_, "content": content}


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
    _, detail = score_task(task, [], _RESOLVER)
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
    assert is_nonanswer("The deploy target is host-b.") is False
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


# ---------------------------------------------------------------------------
# Rank-aware retrieval metrics (techne#385) — recall_at_k, mrr,
# false_positive_rate, aggregate_gold
# ---------------------------------------------------------------------------


def test_recall_at_k_full_hit_within_k():
    assert recall_at_k({"knowledge:1"}, ["knowledge:1", "knowledge:2"], 5) == 1.0


def test_recall_at_k_hit_outside_k_truncated():
    """A hit at rank 6 does not count toward recall@5."""
    ranked = [f"knowledge:{i}" for i in range(1, 7)]  # gold is knowledge:6, rank 6
    assert recall_at_k({"knowledge:6"}, ranked, 5) == 0.0
    assert recall_at_k({"knowledge:6"}, ranked, 10) == 1.0


def test_recall_at_k_partial_hit_multiple_expected():
    ranked = ["knowledge:1", "knowledge:2", "knowledge:3"]
    assert recall_at_k({"knowledge:1", "knowledge:9"}, ranked, 5) == 0.5


def test_recall_at_k_empty_expected_is_vacuous_pass():
    assert recall_at_k(set(), [], 5) == 1.0
    assert recall_at_k(set(), ["knowledge:1"], 5) == 1.0


def test_recall_at_k_empty_ranked_no_expected_hit():
    assert recall_at_k({"knowledge:1"}, [], 5) == 0.0


def test_mrr_first_rank_hit():
    assert mrr({"knowledge:1"}, ["knowledge:1", "knowledge:2"]) == 1.0


def test_mrr_third_rank_hit():
    ranked = ["knowledge:9", "knowledge:8", "knowledge:1"]
    assert mrr({"knowledge:1"}, ranked) == 1 / 3


def test_mrr_no_hit_is_zero():
    assert mrr({"knowledge:1"}, ["knowledge:9", "knowledge:8"]) == 0.0


def test_mrr_empty_ranked_no_hit():
    assert mrr({"knowledge:1"}, []) == 0.0


def test_mrr_empty_expected_is_vacuous_pass():
    assert mrr(set(), ["knowledge:1"]) == 1.0


def test_mrr_uses_first_relevant_not_best():
    """Multiple expected ids present — mrr uses whichever ranks FIRST, not the
    highest-ranked among all expected (there's only one 'first')."""
    ranked = ["knowledge:9", "knowledge:1", "knowledge:2"]
    assert mrr({"knowledge:1", "knowledge:2"}, ranked) == 0.5  # rank 2


def test_fpr_leak_at_top_scores_one():
    assert false_positive_rate({"knowledge:1"}, ["knowledge:1", "knowledge:2"], 5) == 1.0


def test_fpr_leak_outside_k_scores_zero():
    ranked = [f"knowledge:{i}" for i in range(1, 7)]  # forbidden at rank 6
    assert false_positive_rate({"knowledge:6"}, ranked, 5) == 0.0
    assert false_positive_rate({"knowledge:6"}, ranked, 10) == 1.0


def test_fpr_no_leak_scores_zero():
    assert false_positive_rate({"knowledge:99"}, ["knowledge:1", "knowledge:2"], 5) == 0.0


def test_fpr_empty_forbidden_scores_zero():
    assert false_positive_rate(set(), ["knowledge:1"], 5) == 0.0


def test_fpr_is_binary_not_fractional():
    """Two forbidden ids both leaking still scores 1.0, not 2/2 fractional —
    a single leak already fails the task (techne#118 must_exclude spirit)."""
    ranked = ["knowledge:1", "knowledge:2"]
    assert false_positive_rate({"knowledge:1", "knowledge:2"}, ranked, 5) == 1.0


def test_aggregate_gold_means_per_class():
    """Per-class means, NOT one blended positive-class mean (techne#385): a
    regression in one servable class must not hide behind another's headroom."""
    rows = [
        {"class": "exact", "recall_at_5": 1.0, "recall_at_10": 1.0, "mrr": 1.0, "fpr": 0.0},
        {"class": "exact", "recall_at_5": 0.0, "recall_at_10": 0.5, "mrr": 0.5, "fpr": 0.0},
        {"class": "paraphrase", "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0, "fpr": 0.0},
        {"class": "filter", "recall_at_5": 1.0, "recall_at_10": 1.0, "mrr": 1.0, "fpr": 0.0},
        {"class": "negative", "recall_at_5": 1.0, "recall_at_10": 1.0, "mrr": 1.0, "fpr": 1.0},
    ]
    agg = aggregate_gold(rows)
    assert set(agg) == {"exact", "paraphrase", "filter", "negative"}
    assert agg["exact"] == {
        "recall_at_5": 0.5, "recall_at_10": 0.75, "mrr": 0.75, "fpr": 0.0, "n": 2,
    }
    assert agg["filter"]["recall_at_5"] == 1.0 and agg["filter"]["n"] == 1
    assert agg["paraphrase"]["mrr"] == 0.0
    assert agg["negative"]["fpr"] == 1.0 and agg["negative"]["n"] == 1


def test_aggregate_gold_empty_input_is_empty_dict():
    """No classes present → empty dict, never a ZeroDivisionError."""
    assert aggregate_gold([]) == {}


def test_aggregate_gold_only_keys_present_classes():
    rows = [{"class": "exact", "recall_at_5": 1.0, "recall_at_10": 1.0, "mrr": 1.0, "fpr": 0.0}]
    agg = aggregate_gold(rows)
    assert set(agg) == {"exact"}
