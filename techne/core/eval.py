"""Memory/skill eval harness — engine (techne#118, Phase 1).

An INTERNAL regression benchmark, not a public leaderboard: does curated recall
actually beat no-memory and raw-source-dump? We measure it across a task
taxonomy with a fixed answerer + fixed LLM judge (matched across variants) and
validity guards, rather than asserting it.

This module is the pure engine: the answerer, the judge, and memory recall are
all INJECTED callables, so the whole thing is deterministically unit-testable
without a live model. The CLI (Phase 2) wires the real operator-model path
(`_make_generate_fn`) and real techne recall into these seams.

Design lifted from cultivar (three-variant control, defensive judge) and
omni_agent_memory (MeME task taxonomy, trivial-pass detection).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

# prompt -> completion text. Matches thread_orchestrator._make_generate_fn's
# `generate(prompt) -> str`, so the CLI can pass that straight in.
GenerateFn = Callable[[str], str]
# question -> the curated knowledge lines techne recall surfaces for it.
RecallFn = Callable[[str], list[str]]


class Category(str, Enum):
    """The MeME task taxonomy (omni_agent_memory). `cascade` and `absence`
    specifically exercise techne#117 (cascade->uncertain) and supersession."""

    EXACT_RECALL = "exact_recall"
    AGGREGATION = "aggregation"
    TRACKING = "tracking"
    DELETION = "deletion"
    CASCADE = "cascade"
    ABSENCE = "absence"


class Variant(str, Enum):
    """The three-variant control (cultivar). Two deltas fall out: does memory
    help at all (curated/raw vs none), and does CURATED recall beat dumping the
    raw material into context (curated vs raw)? The second tests techne's thesis."""

    NO_MEMORY = "no_memory"            # answerer gets the question alone
    CURATED_RECALL = "curated_recall"  # answerer gets techne's recalled knowledge
    RAW_SOURCE_DUMP = "raw_source_dump"  # answerer gets the raw sources, uncurated


@dataclass(frozen=True)
class EvalTask:
    """One benchmark task. `sources` is the raw material — dumped verbatim in the
    raw-source variant, and (by the CLI) curated into techne for the curated
    variant. `expected` is the graded-correct answer.

    `abstention_correct` marks deletion/absence tasks where "I don't
    know / that was removed" IS the right answer — so a blanket abstention that
    happens to score correct can be flagged as a trivial (gamed) pass.
    """

    id: str
    category: Category
    sources: tuple[str, ...]
    question: str
    expected: str
    abstention_correct: bool = False


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    category: Category
    variant: Variant
    answer: str
    passed: bool
    trivial_pass: bool
    judge_reason: str
    answer_tokens: int  # rough economy signal — see _rough_tokens


# --- context building (per variant) ----------------------------------------

_NO_MEMORY_PREAMBLE = (
    "Answer the question from your own knowledge only; no notes are provided."
)


def build_context(task: EvalTask, variant: Variant, recall: RecallFn) -> str:
    """Assemble the answerer's context for one (task, variant).

    NO_MEMORY yields no material; CURATED_RECALL calls the injected recall for
    the question; RAW_SOURCE_DUMP concatenates every source verbatim.
    """
    if variant is Variant.NO_MEMORY:
        return ""
    if variant is Variant.CURATED_RECALL:
        lines = recall(task.question)
        if not lines:
            return ""
        return "Relevant notes from memory:\n" + "\n".join(f"- {ln}" for ln in lines)
    if variant is Variant.RAW_SOURCE_DUMP:
        return "Source material:\n" + "\n\n".join(task.sources)
    raise ValueError(f"unknown variant {variant!r}")  # pragma: no cover


def _answer_prompt(task: EvalTask, context: str) -> str:
    head = _NO_MEMORY_PREAMBLE if not context else context
    return f"{head}\n\nQuestion: {task.question}\n\nAnswer concisely."


# --- defensive judge (cultivar) --------------------------------------------

# The judge is told to emit the verdict token FIRST so a truncated response is
# still salvageable; we accept PASS/FAIL anywhere but prefer the leading token.
_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)


def _judge_prompt(question: str, expected: str, answer: str) -> str:
    return (
        "You are a strict grader. Decide whether the ANSWER is correct for the "
        "QUESTION given the EXPECTED answer. Reply with the single token PASS or "
        "FAIL on the FIRST line, then a one-line reason.\n\n"
        f"QUESTION: {question}\n"
        f"EXPECTED: {expected}\n"
        f"ANSWER: {answer}\n"
    )


def judge(
    question: str, expected: str, answer: str, judge_fn: GenerateFn
) -> tuple[bool, str]:
    """Grade `answer`, defensively.

    - An empty/blank answer AUTOFAILS without calling the judge — don't let the
      judge hallucinate a verdict from an empty trace (cultivar's empty-trace
      guard).
    - The verdict is parsed as the first PASS/FAIL token; on a judge that
      truncates, we salvage-parse anywhere in the text. If NO verdict token is
      present at all, that's an autofail-with-debug, not a coin-flip.
    """
    if not answer.strip():
        return False, "autofail: empty answer (no agent-turn signal to grade)"
    try:
        raw = judge_fn(_judge_prompt(question, expected, answer))
    except Exception as exc:  # noqa: BLE001 — a judge error must not crash the run
        return False, f"autofail: judge call failed: {exc}"
    m = _VERDICT_RE.search(raw or "")
    if m is None:
        return False, f"autofail: no PASS/FAIL token in judge output: {raw[:120]!r}"
    passed = m.group(1).upper() == "PASS"
    reason = (raw or "").strip().splitlines()
    detail = reason[1] if len(reason) > 1 else reason[0] if reason else ""
    return passed, detail[:200]


# --- trivial-pass detection (omni) -----------------------------------------

_ABSTENTION_RE = re.compile(
    r"\b(i\s+don'?t\s+know|no\s+(information|record|data|memory)|"
    r"not\s+(sure|available|found)|unknown|no\s+such|cannot\s+find|n/?a)\b",
    re.IGNORECASE,
)


def is_abstention(answer: str) -> bool:
    """Heuristic: is this answer a bare 'I don't know'-style abstention?

    Bare = short AND dominated by an abstention phrase (no substantive content
    riding alongside). A long answer that merely contains 'unknown' isn't bare.
    """
    a = answer.strip()
    if not a:
        return True
    if not _ABSTENTION_RE.search(a):
        return False
    # "Bare" ~ under a dozen words: a substantive answer that happens to include
    # an abstention phrase (e.g. "The value is 5, the rest is unknown") is not.
    return len(a.split()) <= 12


def detect_trivial_pass(task: EvalTask, answer: str, passed: bool) -> bool:
    """A PASS is TRIVIAL when the task is gameable by blanket abstention
    (deletion/absence with abstention_correct) and the answer is a bare
    abstention. Such passes shouldn't count as evidence the memory system works
    — a system that answers "I don't know" to everything would ace them."""
    if not passed:
        return False
    if not task.abstention_correct:
        return False
    return is_abstention(answer)


# --- runner + scoring -------------------------------------------------------


def _rough_tokens(text: str) -> int:
    """Cheap, model-agnostic token proxy (~4 chars/token). The harness scores
    token ECONOMY, not just correctness — recall that costs 10x isn't free."""
    return max(1, len(text) // 4)


def run_task(
    task: EvalTask,
    variants: list[Variant],
    *,
    answerer: GenerateFn,
    judge_fn: GenerateFn,
    recall: RecallFn,
) -> list[TaskResult]:
    """Run one task across `variants` with a fixed answerer + judge (matched
    across variants — the same callables for every variant, so any delta is the
    variant's, not the model's)."""
    results: list[TaskResult] = []
    for variant in variants:
        context = build_context(task, variant, recall)
        try:
            answer = answerer(_answer_prompt(task, context))
        except Exception as exc:  # noqa: BLE001 — one variant erroring must not sink the rest
            results.append(
                TaskResult(
                    task_id=task.id, category=task.category, variant=variant,
                    answer="", passed=False, trivial_pass=False,
                    judge_reason=f"autofail: answerer call failed: {exc}",
                    answer_tokens=0,
                )
            )
            continue
        passed, reason = judge(task.question, task.expected, answer, judge_fn)
        results.append(
            TaskResult(
                task_id=task.id, category=task.category, variant=variant,
                answer=answer, passed=passed,
                trivial_pass=detect_trivial_pass(task, answer, passed),
                judge_reason=reason, answer_tokens=_rough_tokens(answer),
            )
        )
    return results


@dataclass
class VariantScore:
    variant: Variant
    n: int = 0
    passed: int = 0
    trivial: int = 0
    tokens: int = 0

    @property
    def credited(self) -> int:
        """Passes that AREN'T trivial (abstention-gamed) — the honest score."""
        return self.passed - self.trivial

    @property
    def pass_rate(self) -> float:
        return self.credited / self.n if self.n else 0.0


def score(results: list[TaskResult]) -> dict[Variant, VariantScore]:
    """Aggregate per-variant: credited pass-rate (trivial passes excluded) and
    total token cost. The two deltas the operator reads off this:
      curated vs no_memory  — does memory help?
      curated vs raw_dump   — does CURATION beat dumping raw material?
    """
    scores: dict[Variant, VariantScore] = {}
    for r in results:
        s = scores.setdefault(r.variant, VariantScore(variant=r.variant))
        s.n += 1
        s.passed += int(r.passed)
        s.trivial += int(r.trivial_pass)
        s.tokens += r.answer_tokens
    return scores
