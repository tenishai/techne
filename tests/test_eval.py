"""Tests for the memory/skill eval engine (techne#118).

The engine takes the answerer, judge, and recall as injected callables, so
every path is exercised deterministically with stubs — no live model.
"""

from __future__ import annotations

from techne.core.eval import (
    Category,
    EvalTask,
    Variant,
    build_context,
    detect_trivial_pass,
    is_abstention,
    judge,
    run_task,
    score,
)


def _task(**kw) -> EvalTask:
    base = dict(
        id="t1",
        category=Category.EXACT_RECALL,
        sources=("The capital of Ruritania is Strelsau.",),
        question="What is the capital of Ruritania?",
        expected="Strelsau",
    )
    base.update(kw)
    return EvalTask(**base)


# --- build_context ----------------------------------------------------------


def test_context_no_memory_is_empty():
    assert build_context(_task(), Variant.NO_MEMORY, lambda q: ["X"]) == ""


def test_context_curated_uses_recall():
    ctx = build_context(_task(), Variant.CURATED_RECALL, lambda q: ["fact A", "fact B"])
    assert "fact A" in ctx and "fact B" in ctx and "memory" in ctx.lower()


def test_context_curated_empty_recall_is_empty():
    assert build_context(_task(), Variant.CURATED_RECALL, lambda q: []) == ""


def test_context_raw_dump_concatenates_sources():
    t = _task(sources=("doc one", "doc two"))
    ctx = build_context(t, Variant.RAW_SOURCE_DUMP, lambda q: [])
    assert "doc one" in ctx and "doc two" in ctx


# --- defensive judge --------------------------------------------------------


def test_judge_empty_answer_autofails_without_calling_judge():
    called = []
    passed, reason = judge("q", "e", "   ", lambda p: called.append(p) or "PASS")
    assert passed is False
    assert not called, "judge must not be called on an empty answer"
    assert "empty answer" in reason


def test_judge_parses_leading_verdict_token():
    passed, reason = judge("q", "e", "some answer", lambda p: "PASS\nmatches expected")
    assert passed is True
    assert "matches expected" in reason


def test_judge_salvage_parses_verdict_anywhere():
    """A judge that buries the token still yields a verdict (truncation-robust)."""
    passed, _ = judge("q", "e", "some answer", lambda p: "After review the verdict is FAIL.")
    assert passed is False


def test_judge_no_verdict_token_autofails_with_debug():
    passed, reason = judge("q", "e", "some answer", lambda p: "I am not sure how to grade this")
    assert passed is False
    assert "no PASS/FAIL token" in reason


def test_judge_call_error_autofails():
    def boom(p):
        raise RuntimeError("model down")

    passed, reason = judge("q", "e", "some answer", boom)
    assert passed is False and "judge call failed" in reason


# --- trivial-pass detection -------------------------------------------------


def test_is_abstention_bare():
    assert is_abstention("I don't know")
    assert is_abstention("No information available.")
    assert is_abstention("")


def test_is_abstention_substantive_answer_is_not():
    assert not is_abstention("The capital is Strelsau.")
    # abstention phrase present but riding a substantive answer → not bare
    assert not is_abstention(
        "The population is 5 million and the leader is Rudolf; the rest is unknown to me here."
    )


def test_trivial_pass_only_when_gameable_and_bare_and_passed():
    absence = _task(category=Category.ABSENCE, abstention_correct=True)
    # bare abstention that passed on a gameable task → trivial
    assert detect_trivial_pass(absence, "I don't know", True) is True
    # substantive pass on the same task → not trivial
    assert detect_trivial_pass(absence, "It was deleted on Tuesday", True) is False
    # a fail is never trivial
    assert detect_trivial_pass(absence, "I don't know", False) is False
    # not gameable (abstention_correct=False) → never trivial
    assert detect_trivial_pass(_task(), "I don't know", True) is False


# --- runner + scoring -------------------------------------------------------


def test_run_task_runs_all_variants_matched():
    t = _task()
    results = run_task(
        t,
        [Variant.NO_MEMORY, Variant.CURATED_RECALL, Variant.RAW_SOURCE_DUMP],
        answerer=lambda p: "Strelsau",
        judge_fn=lambda p: "PASS ok",
        recall=lambda q: ["The capital of Ruritania is Strelsau."],
    )
    assert [r.variant for r in results] == [
        Variant.NO_MEMORY, Variant.CURATED_RECALL, Variant.RAW_SOURCE_DUMP,
    ]
    assert all(r.passed for r in results)
    assert all(r.answer_tokens > 0 for r in results)


def test_run_task_answerer_error_becomes_autofail_result():
    def boom(p):
        raise RuntimeError("answerer down")

    results = run_task(
        _task(), [Variant.NO_MEMORY],
        answerer=boom, judge_fn=lambda p: "PASS", recall=lambda q: [],
    )
    assert len(results) == 1
    assert results[0].passed is False and "answerer call failed" in results[0].judge_reason


def test_score_excludes_trivial_passes_from_credit():
    absence = _task(category=Category.ABSENCE, abstention_correct=True)
    # curated: a real substantive pass; no_memory: a trivial abstention pass
    results = run_task(
        absence, [Variant.CURATED_RECALL],
        answerer=lambda p: "It was deleted on Tuesday",
        judge_fn=lambda p: "PASS", recall=lambda q: ["deleted Tuesday"],
    ) + run_task(
        absence, [Variant.NO_MEMORY],
        answerer=lambda p: "I don't know",
        judge_fn=lambda p: "PASS", recall=lambda q: [],
    )
    s = score(results)
    assert s[Variant.CURATED_RECALL].credited == 1  # substantive
    assert s[Variant.NO_MEMORY].passed == 1 and s[Variant.NO_MEMORY].trivial == 1
    assert s[Variant.NO_MEMORY].credited == 0  # abstention-gamed → no credit
    assert s[Variant.CURATED_RECALL].pass_rate == 1.0
    assert s[Variant.NO_MEMORY].pass_rate == 0.0


# --- seed corpus (techne#118) ----------------------------------------------


def test_seed_corpus_is_well_formed():
    from techne.core.eval_tasks import SEED_TASKS

    assert len(SEED_TASKS) >= 5
    ids = [t.id for t in SEED_TASKS]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    cats = {t.category for t in SEED_TASKS}
    # the two thesis-critical categories are present
    assert Category.CASCADE in cats and Category.ABSENCE in cats
    # every gameable task is marked so trivial-pass detection can fire
    for t in SEED_TASKS:
        if t.category in (Category.ABSENCE, Category.DELETION):
            assert t.abstention_correct, f"{t.id} should mark abstention_correct"
        assert t.sources and t.question and t.expected


def test_engine_runs_over_the_whole_corpus():
    """Plumbing: every corpus task runs across all three variants and yields a
    per-variant scorecard — exercised with stubs (no live model)."""
    from techne.core.eval import Variant, run_task, score
    from techne.core.eval_tasks import SEED_TASKS

    variants = [Variant.NO_MEMORY, Variant.CURATED_RECALL, Variant.RAW_SOURCE_DUMP]
    all_results = []
    for t in SEED_TASKS:
        all_results += run_task(
            t, variants,
            answerer=lambda p: "some answer",
            judge_fn=lambda p: "FAIL not graded",
            recall=lambda q: ["a recalled line"],
        )
    assert len(all_results) == len(SEED_TASKS) * len(variants)
    s = score(all_results)
    assert set(s) == set(variants)
    assert all(sc.n == len(SEED_TASKS) for sc in s.values())
