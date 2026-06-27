"""Stage-2 LLM-judged recall eval runner — techne#118.

Run explicitly (excluded from the default unit suite and CI):
    pytest -m eval_llm -s

Tests the NEGATIVE-POLARITY tasks (absence / deletion / cascade) that Stage-1
cannot cover: their gold_keys=[] makes the deterministic retrieval rubric
trivially score 1.0 because the real failure is "an agent ASSERTS a fact that
is absent/deleted/should-be-uncertain."  The LLM answerer + judge detect that.

THE HARD CONTRACT: no required API key, no required network — SKIP if no model
is reachable.  Resolver order in answerer.resolve_eval_generate_fn():
  local Ollama → operator-configured model (env-var key already set) → skip.

Single-arm (techne) for now.  The 3-variant comparison (bare / techne / raw)
is a deferred follow-up (#118); a comment marks the gap below.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from techne.core.knowledge import annotate_uncertain
from techne.core.search import search

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from answerer import answer, resolve_eval_generate_fn  # noqa: E402
from judge import judge  # noqa: E402
from scoring import score_answer  # noqa: E402
from seed_eval_db import seed  # noqa: E402

pytestmark = pytest.mark.eval_llm


def _load_stage2_tasks() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "recall_baseline.yaml").read_text())
    tasks = [t for t in doc["tasks"] if t.get("stage", 1) == 2]
    return tasks, doc.get("config", {})


def test_stage2_recall_eval_llm(tmp_path, capsys):
    # --- resolver: skip if no model is reachable (CI-safe) ---
    gen = resolve_eval_generate_fn()
    if gen is None:
        pytest.skip(
            "no eval LLM reachable (no Ollama, no API key) — Stage 2 is opt-in"
        )

    db_path = tmp_path / "eval_llm.db"
    seed(db_path)
    tasks, cfg = _load_stage2_tasks()
    top_k = cfg.get("top_k", 10)
    gates = cfg.get("gates", {})

    cat_score: dict[str, float] = defaultdict(float)
    cat_weight: dict[str, float] = defaultdict(float)
    report_rows: list[dict] = []

    for task in tasks:
        task_id = task["id"]
        category = task.get("category", "unknown")
        w = task.get("weight", 1.0)
        question = task["query"]

        # Retrieve from techne (single arm; bare/raw comparison deferred #118).
        # use_vectors=False → deterministic FTS path (matches Stage-1 runner).
        retrieved = search(
            db_path,
            question,
            table=task.get("table", "all"),
            limit=top_k,
            use_vectors=False,
        )
        # Annotate uncertain rows (#117) so the answerer can flag them.
        retrieved = annotate_uncertain(db_path, retrieved)

        answer_text = answer(question, retrieved, gen)
        verdict = judge(question, answer_text, task, gen)
        score, detail = score_answer(task, answer_text, verdict)

        cat_score[category] += score * w
        cat_weight[category] += w
        report_rows.append({
            "task_id": task_id,
            "category": category,
            "score": score,
            "n_results": len(retrieved),
            **detail,
        })

    def cat_agg(category: str) -> float:
        cw = cat_weight[category]
        return round(cat_score[category] / cw, 3) if cw else 0.0

    # --- print per-task report (mirrors Stage-1 style) ---
    with capsys.disabled():
        print("\n=== Stage-2 LLM eval (#118) ===")
        print(
            f"{'task':<22} {'cat':<12} {'score':>6} "
            f"{'verdict':<12} {'nonanswer':>10} {'n':>4}"
        )
        for r in report_rows:
            print(
                f"{r['task_id']:<22} {r['category']:<12} {r['score']:>6.1f} "
                f"{r['verdict']:<12} {str(r['nonanswer']):>10} {r['n_results']:>4}"
            )
        print("-" * 70)
        print("per-category aggregate (techne arm):")
        for cat in sorted(cat_weight):
            print(f"  [{cat}]  techne={cat_agg(cat)}")
        print()
        for r in report_rows:
            print(f"  [{r['task_id']}] reason: {r['reason']}")

    # --- per-category kill-gates ---
    for category in sorted(cat_weight):
        g = gates.get(category)
        assert g is not None, f"no gate configured for category '{category}'"
        techne_score = cat_agg(category)
        assert techne_score >= g["floor"], (
            f"KILL-GATE [{category}]: techne {techne_score} < floor {g['floor']}"
        )
