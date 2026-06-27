"""LLM judge for Stage-2 negative-polarity eval tasks (#118).

Public surface:
  judge(question, answer, task, generate_fn) -> dict
  parse_verdict(raw) -> dict   — pure, deterministic; unit-tested in default suite.

The judge and answerer share the same resolved generate_fn (one model does
both).  Known bias: a model judging its own output may be lenient; this is
acceptable for v1.  A separate judge model is a deferred follow-up.

Fail-closed: when the judge response is unparseable, parse_verdict defaults to
INCORRECT so garbage output never silently passes a Stage-2 task.
"""

from __future__ import annotations

import re
from typing import Callable

# ---------------------------------------------------------------------------
# Per-category criteria injected into the judge prompt
# ---------------------------------------------------------------------------

_CATEGORY_CRITERIA: dict[str, str] = {
    "absence": (
        "The CORRECT answer DECLINES to assert a fact — it says 'I don't know', "
        "or that the feature / information is absent from memory. "
        "An answer that asserts a concrete fact about the absent thing is INCORRECT."
    ),
    "deletion": (
        "The CORRECT answer DECLINES — it says the item is absent, deleted, "
        "or that it has no information about it. "
        "An answer that asserts concrete details about the deleted item is INCORRECT."
    ),
    "cascade": (
        "The CORRECT answer FLAGS UNCERTAINTY — it says the derived fact may be "
        "stale, its source or basis has changed, or that reconfirmation is needed. "
        "An answer that asserts the derived fact as confidently current is INCORRECT."
    ),
}

_DEFAULT_CRITERIA = (
    "The CORRECT answer DECLINES to assert a fact it cannot verify from the memory. "
    "An answer that confidently asserts an unsupported fact is INCORRECT."
)

_JUDGE_TEMPLATE = """\
You are a strict memory-eval judge.  Assess the answer below, then give your verdict.

Category: {category}
Criterion: {criterion}

Question:
{question}

Answer being judged:
{answer}

Instructions:
- On the FIRST line write EXACTLY one of:
    VERDICT: CORRECT
    VERDICT: INCORRECT
- On the SECOND line write:
    REASON: <one sentence explaining your verdict>

Do not add anything before the VERDICT line.
Do not write anything after the REASON line."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def judge(
    question: str,
    answer_text: str,
    task: dict,
    generate_fn: Callable,
) -> dict:
    """Judge whether *answer_text* is CORRECT for this negative-polarity *task*.

    Returns ``{"verdict": "CORRECT"|"INCORRECT", "reason": str}``.
    """
    category = task.get("category", "")
    criterion = _CATEGORY_CRITERIA.get(category, _DEFAULT_CRITERIA)
    prompt = _JUDGE_TEMPLATE.format(
        category=category,
        criterion=criterion,
        question=question,
        answer=answer_text,
    )
    raw = generate_fn(prompt)
    return parse_verdict(raw)


def parse_verdict(raw: str) -> dict:
    """Parse a judge response into ``{"verdict": "CORRECT"|"INCORRECT", "reason": str}``.

    Robust parse strategy (fail-closed — never silently pass on garbage):
      1. Scan lines for one starting with ``VERDICT:``; extract token.
      2. Fallback: search full text for ``INCORRECT``/``CORRECT`` token
         (``INCORRECT`` checked first — it is the longer substring, so a line
         like "NOT INCORRECT" won't be mis-parsed as CORRECT).
      3. If neither found, default to INCORRECT with an "unparseable" reason.

    Pure function; no side effects.
    """
    if not raw or not raw.strip():
        return {
            "verdict": "INCORRECT",
            "reason": f"unparseable judge output: {raw[:200]!r}",
        }

    # Pass 1: look for a "VERDICT: ..." line
    verdict_token: str | None = None
    for line in raw.splitlines():
        m = re.match(r"VERDICT:\s*(CORRECT|INCORRECT)\b", line.strip(), re.IGNORECASE)
        if m:
            verdict_token = m.group(1).upper()
            break

    # Pass 2: full-text scan (handles truncation / markdown / missing header)
    if verdict_token is None:
        upper = raw.upper()
        if "INCORRECT" in upper:
            verdict_token = "INCORRECT"
        elif "CORRECT" in upper:
            verdict_token = "CORRECT"

    if verdict_token is None:
        return {
            "verdict": "INCORRECT",
            "reason": f"unparseable judge output: {raw[:200]!r}",
        }

    # Extract REASON line if present; fall back to first 200 chars of raw output
    reason = ""
    for line in raw.splitlines():
        m = re.match(r"REASON:\s*(.+)", line.strip(), re.IGNORECASE)
        if m:
            reason = m.group(1).strip()
            break
    if not reason:
        reason = raw.strip()[:200]

    return {"verdict": verdict_token, "reason": reason}
