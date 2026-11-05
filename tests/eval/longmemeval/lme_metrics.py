"""LongMemEval retrieval metrics, under upstream's names.

Upstream (xiaowu0162/LongMemEval, src/retrieval/eval_utils.py) defines
recall_all@k as 1.0 only when every gold id is in the top k, recall_any@k as 1.0
when at least one is, and ndcg_any@k as binary-relevance NDCG with an
undiscounted first rank and log2(rank) after it. Its printer
(src/evaluation/print_retrieval_metrics.py) reports recall_all and ndcg_any at
session k=5,10 and turn k=5,10,50, and drops `_abs` questions.

One difference, stated in the results: techne ranks turns (observation rows), so
the session ranking here is those turns collapsed to their sessions in first-hit
order, not a retriever that ranks whole sessions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

SESSION_KS = (5, 10)
TURN_KS = (5, 10, 50)


def first_hit_order(ids: Iterable[str]) -> list[str]:
    """`ids` with repeats dropped, keeping each one's first position."""
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def recall_all(ranked: Sequence[str], gold: set[str], k: int) -> float:
    return float(bool(gold) and gold <= set(ranked[:k]))


def recall_any(ranked: Sequence[str], gold: set[str], k: int) -> float:
    return float(bool(gold & set(ranked[:k])))


def _dcg(rels: Sequence[int]) -> float:
    return sum(r if i == 0 else r / math.log2(i + 1) for i, r in enumerate(rels))


def ndcg_any(ranked: Sequence[str], gold: set[str], k: int) -> float:
    ideal = _dcg([1] * min(len(gold), k))
    if ideal == 0:
        return 0.0
    return _dcg([1 if r in gold else 0 for r in ranked[:k]]) / ideal


def _at(ranked: Sequence[str], gold: set[str], ks: Sequence[int]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in ks:
        out[f"recall_all@{k}"] = recall_all(ranked, gold, k)
        out[f"recall_any@{k}"] = recall_any(ranked, gold, k)
        out[f"ndcg_any@{k}"] = ndcg_any(ranked, gold, k)
    return out


def score_question(
    ranked_turns: Sequence[tuple[str, str]],
    gold_sessions: set[str],
    gold_turns: set[str],
) -> dict[str, dict[str, float]]:
    """Session and turn metrics for one question.

    `ranked_turns` is the search result in rank order, as (session_id, turn_id).
    """
    sessions = first_hit_order(s for s, _ in ranked_turns)
    turns = [t for _, t in ranked_turns]
    return {
        "session": _at(sessions, gold_sessions, SESSION_KS),
        "turn": _at(turns, gold_turns, TURN_KS),
    }
