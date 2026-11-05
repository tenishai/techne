"""Unit tests for the LongMemEval metrics. Not eval-marked: they run in the default
suite, so the numbers the benchmark reports rest on checked arithmetic."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from lme_metrics import (  # noqa: E402
    first_hit_order,
    ndcg_any,
    recall_all,
    recall_any,
    score_question,
)


def test_first_hit_order_keeps_first_positions() -> None:
    assert first_hit_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_recall_all_needs_every_gold_id_and_any_needs_one() -> None:
    ranked = ["x", "g1", "y", "g2"]
    assert recall_all(ranked, {"g1", "g2"}, 4) == 1.0
    assert recall_all(ranked, {"g1", "g2"}, 3) == 0.0
    assert recall_any(ranked, {"g1", "g2"}, 3) == 1.0
    assert recall_any(ranked, {"g1", "g2"}, 1) == 0.0
    # No gold is never a hit, so an abstention question that slipped through scores 0.
    assert recall_all(ranked, set(), 4) == 0.0


def test_ndcg_matches_upstream_discount() -> None:
    # Upstream: rel[0] + sum(rel[i] / log2(i + 1)) for i >= 1, ideal over the gold count.
    assert ndcg_any(["g", "x"], {"g"}, 5) == 1.0
    assert ndcg_any(["x", "g"], {"g"}, 5) == pytest.approx(1 / math.log2(2))
    assert ndcg_any(["x", "x2", "g"], {"g"}, 5) == pytest.approx(1 / math.log2(3))
    # Two gold, found at ranks 1 and 3: (1 + 1/log2 3) / (1 + 1/log2 2).
    assert ndcg_any(["g1", "x", "g2"], {"g1", "g2"}, 5) == pytest.approx((1 + 1 / math.log2(3)) / 2)
    assert ndcg_any(["x"], set(), 5) == 0.0


def test_two_question_fixture() -> None:
    # Question A: one evidence session s2, its evidence turn s2_1 ranked second.
    a = score_question(
        [("s1", "s1_0"), ("s2", "s2_1"), ("s1", "s1_3"), ("s3", "s3_0")],
        gold_sessions={"s2"},
        gold_turns={"s2_1"},
    )
    assert a["session"]["recall_all@5"] == 1.0
    assert a["session"]["ndcg_any@5"] == pytest.approx(1.0)  # 1/log2(2)
    assert a["turn"]["recall_all@5"] == 1.0

    # Question B: two evidence sessions; s9 only appears after six other sessions.
    ranked = [(f"o{i}", f"o{i}_0") for i in range(6)]
    b = score_question(
        [("s8", "s8_2"), *ranked, ("s9", "s9_4")],
        gold_sessions={"s8", "s9"},
        gold_turns={"s8_2", "s9_4"},
    )
    assert b["session"]["recall_any@5"] == 1.0
    assert b["session"]["recall_all@5"] == 0.0  # s9 is the 8th session
    assert b["session"]["recall_all@10"] == 1.0
    assert b["turn"]["recall_all@5"] == 0.0
    assert b["turn"]["recall_all@10"] == 1.0
