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
