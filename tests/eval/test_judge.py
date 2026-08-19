"""Unit tests for the Stage-2 LLM judge helpers (techne#118 PR3).

NOT eval_llm-marked — these run in the default suite with no model present.
Fake generate_fn lambdas supply canned strings so no real LLM call is made.
Tests cover:
  - parse_verdict (CORRECT / INCORRECT / truncated / garbage → fail-closed)
  - answer() prompt construction (no model; just verify the prompt shape)
  - judge() prompt routing (fake generate_fn; verify verdict extraction)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from answerer import answer  # noqa: E402
from judge import judge, parse_verdict  # noqa: E402

# ---------------------------------------------------------------------------
# parse_verdict — pure function, deterministic
# ---------------------------------------------------------------------------


def test_parse_verdict_correct():
    raw = "VERDICT: CORRECT\nREASON: The model declined correctly."
    result = parse_verdict(raw)
    assert result["verdict"] == "CORRECT"
    assert "correctly" in result["reason"]


def test_parse_verdict_incorrect():
    raw = "VERDICT: INCORRECT\nREASON: The model asserted a stale fact."
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"
    assert "stale" in result["reason"]


def test_parse_verdict_case_insensitive_header():
    raw = "verdict: correct\nreason: fine"
    result = parse_verdict(raw)
    assert result["verdict"] == "CORRECT"


def test_parse_verdict_fallback_to_text_scan_incorrect():
    """No VERDICT: header but INCORRECT appears in the body."""
    raw = "I think the answer is INCORRECT because the model asserted a fact."
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"


def test_parse_verdict_bare_correct_without_header_fails_closed():
    """A bare 'CORRECT' with NO VERDICT: line is malformed → INCORRECT (fail-closed).
    Only a proper VERDICT: line can yield CORRECT (techne#118 review)."""
    raw = "The assessment is CORRECT — the model declined."
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"


def test_parse_verdict_negation_does_not_false_pass():
    """The exact false-CORRECT hole the review flagged: 'not correct' contains CORRECT
    but not INCORRECT, with no VERDICT: line → must NOT pass."""
    raw = "This answer is not correct; the model asserted a deleted fact."
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"


def test_parse_verdict_no_header_defaults_incorrect():
    """No VERDICT: line (even mentioning INCORRECT in prose) → INCORRECT."""
    raw = "INCORRECT — the model was wrong."
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"


def test_parse_verdict_truncated_no_reason():
    """Truncated output with a verdict token but no REASON: line."""
    raw = "VERDICT: CORRECT"
    result = parse_verdict(raw)
    assert result["verdict"] == "CORRECT"
    assert result["reason"]  # some fallback text is always present


def test_parse_verdict_garbage_defaults_incorrect():
    """Garbage output (no VERDICT line) → fail-closed → INCORRECT."""
    raw = "blah blah blah"
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"
    assert "VERDICT" in result["reason"]  # explains it lacked the required line


def test_parse_verdict_empty_string_defaults_incorrect():
    result = parse_verdict("")
    assert result["verdict"] == "INCORRECT"
    assert "unparseable" in result["reason"]


def test_parse_verdict_whitespace_only_defaults_incorrect():
    result = parse_verdict("   \n\t  ")
    assert result["verdict"] == "INCORRECT"
    assert "unparseable" in result["reason"]


def test_parse_verdict_markdown_wrapped():
    """Judge may return markdown; verdict token still parseable from body."""
    raw = "```\nVERDICT: INCORRECT\nREASON: asserted a deleted fact\n```"
    result = parse_verdict(raw)
    assert result["verdict"] == "INCORRECT"


# ---------------------------------------------------------------------------
# answer() — prompt construction (fake generate_fn, no real model)
# ---------------------------------------------------------------------------

_CAPTURED: list[str] = []


def _fake_gen(prompt: str, max_tokens: int = 512) -> str:
    _CAPTURED.append(prompt)
    return "I don't know."


def test_answer_no_context_contains_no_memory_message():
    _CAPTURED.clear()
    answer("Does techne auto-bisect?", [], _fake_gen)
    assert _CAPTURED, "generate_fn must be called"
    assert "no memory" in _CAPTURED[-1].lower() or "no mem" in _CAPTURED[-1].lower()


def test_answer_with_context_rows_included():
    _CAPTURED.clear()
    rows = [{"source_table": "knowledge", "id": 1, "content": "techne uses sqlite-vec"}]
    answer("What vector store does techne use?", rows, _fake_gen)
    assert "sqlite-vec" in _CAPTURED[-1]


def test_answer_uncertain_row_flagged_in_prompt():
    _CAPTURED.clear()
    rows = [
        {
            "source_table": "knowledge",
            "id": 2,
            "content": "the eval deploy path is /opt/eval/v1/logs",
            "uncertainty": {
                "state": "uncertain",
                "tag": "[uncertain — basis changed]",
                "note": "dependency was superseded",
            },
        }
    ]
    answer("What is the eval deploy log path?", rows, _fake_gen)
    prompt = _CAPTURED[-1]
    assert "UNCERTAIN" in prompt.upper()


def test_answer_current_row_not_flagged():
    """A current (non-uncertain) row must not receive the [UNCERTAIN] marker."""
    _CAPTURED.clear()
    rows = [{"source_table": "knowledge", "id": 3, "content": "canonical remote is github"}]
    answer("What is the canonical remote?", rows, _fake_gen)
    prompt = _CAPTURED[-1]
    # The instructions mention "UNCERTAIN" in general; check only the memory
    # entries block, which follows "Memory entries:\n".
    entries_block = prompt.split("Memory entries:\n", 1)[-1]
    assert "[UNCERTAIN" not in entries_block


# ---------------------------------------------------------------------------
# judge() — prompt routing with fake generate_fn
# ---------------------------------------------------------------------------


def _fake_judge_gen(prompt: str, max_tokens: int = 512) -> str:
    """Returns a fixed INCORRECT verdict regardless of prompt content."""
    return "VERDICT: INCORRECT\nREASON: asserted an absent feature"


def test_judge_routes_through_parse_verdict():
    task = {"id": "absent-feature", "category": "absence"}
    result = judge("Does techne auto-bisect?", "Yes it does.", task, _fake_judge_gen)
    assert result["verdict"] == "INCORRECT"
    assert result["reason"]


def _fake_correct_gen(prompt: str, max_tokens: int = 512) -> str:
    return "VERDICT: CORRECT\nREASON: model correctly declined"


def test_judge_correct_verdict_returned():
    task = {"id": "deleted-fact", "category": "deletion"}
    result = judge(
        "Tell me about the deleted scratch note.",
        "I don't know.",
        task,
        _fake_correct_gen,
    )
    assert result["verdict"] == "CORRECT"


def test_judge_unknown_category_uses_default_criteria():
    """An unknown category should not raise — falls back to default criteria."""
    task = {"id": "unknown-task", "category": "mystery"}
    result = judge("question", "answer", task, _fake_correct_gen)
    assert result["verdict"] in ("CORRECT", "INCORRECT")
