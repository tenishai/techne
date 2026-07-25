"""Rubric scoring for the recall-effectiveness eval (#68). Deterministic, no LLM.

Per-task score in [0,1]:
    0.7 * evidence_recall@K  +  0.3 * phrase_coverage
zeroed if a `must_exclude_phrases` entry appears anywhere in the results
(anti-relevance, whole-list since techne#118 — a stale leak at any rank fails).

Honest because gold ids/phrases are authored against the committed synthetic
corpus, where "correct" is knowable.
"""


def _text_of(r: dict) -> str:
    return r.get("content") or r.get("summary") or r.get("label") or ""


def score_task(
    task: dict, retrieved: list[dict], gold_resolver: dict[str, str]
) -> tuple[float, dict]:
    """Score one task's retrieved results. Returns (score, detail)."""
    gold_ids = {gold_resolver[k] for k in task.get("gold_keys", [])}
    got_ids = {f"{r['type']}:{r['id']}" for r in retrieved}
    hits = gold_ids & got_ids
    recall = len(hits) / len(gold_ids) if gold_ids else 1.0

    blob = " ".join(_text_of(r) for r in retrieved).lower()
    musts = task.get("must_include_phrases", []) or []
    coverage = (sum(1 for p in musts if p.lower() in blob) / len(musts)) if musts else 1.0

    # Anti-relevance, checked over ALL retrieved results, not just the top one
    # (techne#118): a stale revision leaking in at rank 2 must fail a tracking task
    # too. No effect on existing tasks — they carry no must_exclude_phrases.
    forb = task.get("must_exclude_phrases", []) or []
    killed = any(f.lower() in blob for f in forb)

    # Cost/precision axis (techne#118) — observability only, NOT part of the score.
    # precision = gold hits per result returned (curated recall is precise + cheap;
    # a raw-source-dump arm recalls everything but precision craters); token_cost is
    # a char-count proxy for what the arm would cost to inject.
    n_results = len(retrieved)
    precision = (len(hits) / n_results) if n_results else 0.0
    token_cost = sum(len(_text_of(r)) for r in retrieved)

    detail = {
        "recall": round(recall, 3),
        "coverage": round(coverage, 3),
        "precision": round(precision, 3),
        "token_cost": token_cost,
        "hits": sorted(hits),
        "misses": sorted(gold_ids - got_ids),
        "killed_by_exclude": killed,
        "n_results": n_results,
    }
    if killed:
        return 0.0, detail
    return round(0.7 * recall + 0.3 * coverage, 3), detail


# ---------------------------------------------------------------------------
# Stage-2 helpers (LLM-judged negative-polarity tasks, techne#118)
# ---------------------------------------------------------------------------

_NONANSWER_SUBSTRINGS = (
    "i don't know",
    "i do not know",
    "no information",
    "i cannot",
    "not available",
    "i have no",
    "no memory",
    "no relevant",
)


def is_nonanswer(text: str) -> bool:
    """Return True for empty or refusal-style responses.

    Checks a fixed set of normalised substrings against the lowercased text.
    Pure function — no side effects.

    Note: for Stage-2 (negative-polarity) tasks, a non-answer is often the
    CORRECT response.  The judge owns that decision; this function only flags
    the trait so the report can surface it.
    """
    if not text or not text.strip():
        return True
    normalized = text.lower()
    return any(s in normalized for s in _NONANSWER_SUBSTRINGS)


def score_answer(
    _task: dict,  # reserved for a future positive-task guard; unused today
    answer: str,
    verdict: dict,
) -> tuple[float, dict]:
    """Score a Stage-2 (LLM-judged) answer.

    Returns (score, detail) where score is 1.0 iff verdict["verdict"] == "CORRECT",
    else 0.0.  detail carries: verdict (str), reason (str), nonanswer (bool).

    Trivial-pass note: for positive-polarity tasks a non-answer must NOT score
    1.0; but all Stage-2 tasks here are negative-polarity, where declining IS
    often the correct behaviour — the judge owns that decision.  score_answer
    records ``nonanswer`` for report purposes only and does NOT hard-zero it.
    """
    is_correct = verdict.get("verdict") == "CORRECT"
    detail: dict = {
        "verdict": verdict.get("verdict", "INCORRECT"),
        "reason": verdict.get("reason", ""),
        "nonanswer": is_nonanswer(answer),
    }
    return (1.0 if is_correct else 0.0), detail


# ---------------------------------------------------------------------------
# Rank-aware retrieval metrics (techne#385) — the recall-effectiveness eval's
# score_task() above is rank-blind (a hit anywhere in the retrieved list counts
# equally). These are pure, order-sensitive metrics for the retrieval-gate:
# recall@k truncates to the top k before scoring, and mrr rewards hits that
# rank higher. All three take `ranked: list[str]` — result ids in the exact
# order search() returned them, built as f"{r['type']}:{r['id']}" (same
# pattern as score_task's got_ids) — never raw dict rows, so these stay pure
# and don't re-derive the id-building convention.
# ---------------------------------------------------------------------------


def recall_at_k(expected: set[str], ranked: list[str], k: int) -> float:
    """Fraction of `expected` ids present in the top-k of `ranked`.

    1.0 when `expected` is empty (vacuously satisfied — mirrors score_task's
    `gold_ids` empty-set convention). Never raises on k <= 0 or empty ranked.
    """
    if not expected:
        return 1.0
    top_k = set(ranked[:k])
    hits = expected & top_k
    return len(hits) / len(expected)


def mrr(expected: set[str], ranked: list[str]) -> float:
    """1 / (rank of the first relevant hit), 0.0 if none of `expected` appear.

    Rank is 1-based. 1.0 when `expected` is empty (nothing to find, no miss
    possible) — mirrors recall_at_k's vacuous-pass convention.
    """
    if not expected:
        return 1.0
    for i, item in enumerate(ranked, start=1):
        if item in expected:
            return 1.0 / i
    return 0.0


def false_positive_rate(forbidden: set[str], ranked: list[str], k: int) -> float:
    """1.0 if ANY `forbidden` id appears in the top-k of `ranked`, else 0.0.

    Binary per-task leak indicator (not a fraction of `forbidden` matched) —
    a single stale/deleted row surfacing is a full failure for that task,
    same "whole-list, any-leak-fails" spirit as score_task's must_exclude_phrases
    (techne#118). 0.0 when `forbidden` is empty (nothing to leak).
    """
    if not forbidden:
        return 0.0
    top_k = set(ranked[:k])
    return 1.0 if (forbidden & top_k) else 0.0


def aggregate_gold(per_task_results: list[dict]) -> dict:
    """Aggregate per-task retrieval-gate results into PER-CLASS mean metrics.

    Each item in `per_task_results` is expected to carry:
      `class`      — "exact" | "paraphrase" | "filter" | "negative"
      `recall_at_5`, `recall_at_10`, `mrr` — from recall_at_k/mrr, computed
                     for every task (including negative, for the report).
      `fpr`        — from false_positive_rate.

    Returns ``{class_name: {recall_at_5, recall_at_10, mrr, fpr, n}, ...}`` —
    one entry per class actually present in the input. Per-class rather than a
    single blended aggregate ON PURPOSE (techne#385): a blended positive-class
    mean lets a real regression in one class (e.g. exact recall 1.0→0.5) hide
    behind another class's headroom (or behind paraphrase, which is pinned near
    0 on the FTS-only path). The gate asserts each servable class's floor
    SEPARATELY against these per-class means. Any mean over an empty group
    never arises (only present classes are keyed); the group is never empty.
    """
    by_class: dict[str, list[dict]] = {}
    for r in per_task_results:
        by_class.setdefault(r.get("class", "unknown"), []).append(r)

    def _mean(rows: list[dict], key: str) -> float:
        return round(sum(r[key] for r in rows) / len(rows), 3) if rows else 0.0

    return {
        cls: {
            "recall_at_5": _mean(rows, "recall_at_5"),
            "recall_at_10": _mean(rows, "recall_at_10"),
            "mrr": _mean(rows, "mrr"),
            "fpr": _mean(rows, "fpr"),
            "n": len(rows),
        }
        for cls, rows in by_class.items()
    }
