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
    task: dict,  # noqa: ARG001 — reserved for future positive-task guard
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
