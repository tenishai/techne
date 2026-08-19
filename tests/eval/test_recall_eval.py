"""Recall-effectiveness eval runner — #68 M1.

Run explicitly (excluded from the default unit suite):
    pytest -m eval -s

Two arms against a frozen synthetic corpus:
    bare    — no techne retrieval (control; proves the task needs memory)
    techne  — full search() recall

Scored by deterministic rubric (see scoring.py). Prints a per-(task, arm)
table + per-arm aggregate and writes JSONL to results/. No frozen kill-gate
yet (M2); M1 only soft-asserts that techne does not underperform bare.
"""

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from techne.core.search import search

_EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(_EVAL_DIR))
from scoring import score_task  # noqa: E402
from seed_eval_db import seed  # noqa: E402

pytestmark = pytest.mark.eval

# The three control variants (techne#118). RECALL_ARMS feed the per-category
# kill-gate; raw-source-dump is judged on the COST axis only (it recalls
# everything, so folding it into the recall gate would mask regressions).
ARMS = ("bare", "techne", "raw-source-dump")
RECALL_ARMS = ("bare", "techne")


def _load_baseline() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "recall_baseline.yaml").read_text())
    return doc["tasks"], doc.get("config", {})


def _all_rows(db_path: Path, cap: int = 500) -> list[dict]:
    """raw-source-dump arm: every stored row, no query, no curation — INCLUDING
    superseded knowledge. A raw dump has no freshness filter, which is exactly why
    it leaks stale revisions on a tracking task (and loses on the cost axis)."""
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        rows: list[dict] = []
        for r in conn.execute("SELECT id, content FROM knowledge"):
            rows.append({"source_table": "knowledge", "id": r["id"], "content": r["content"]})
        for r in conn.execute("SELECT id, content FROM observations"):
            rows.append({"source_table": "observation", "id": r["id"], "content": r["content"]})
        for r in conn.execute("SELECT id, label, summary FROM threads"):
            rows.append({"source_table": "thread", "id": r["id"], "label": r["label"],
                         "summary": r["summary"]})
        # No silent cap: if the corpus ever outgrows `cap`, raw-dump would drop rows
        # (maybe gold), silently shifting the precision ratio + token_cost. Fail loud.
        assert len(rows) <= cap, (
            f"raw-source-dump corpus {len(rows)} exceeds cap {cap}; raise the cap so "
            "the dump stays complete (techne#118)"
        )
        return rows
    finally:
        conn.close()


def _retrieve(arm: str, db_path: Path, task: dict, top_k: int) -> list[dict]:
    if arm == "bare":
        return []  # no-memory control
    if arm == "raw-source-dump":
        return _all_rows(db_path)  # stuff the whole corpus in, uncurated
    # use_vectors=False → deterministic FTS path (v1 measures the FTS retriever;
    # vector fusion is a later eval dimension and needs fastembed).
    return search(db_path, task["query"], table=task.get("table", "all"),
                  limit=top_k, use_vectors=False)


def test_recall_eval(tmp_path, capsys):
    db_path = tmp_path / "eval.db"
    gold = seed(db_path)
    all_tasks, cfg = _load_baseline()
    top_k = cfg.get("top_k", 10)
    gates = cfg.get("gates", {})
    cost_ratio = cfg.get("cost_precision_ratio", 3.0)

    # Stage 1 only — absence/deletion/cascade (stage 2) need the LLM answerer
    # (techne#118 PR3+); they're authored in the YAML for taxonomy completeness.
    tasks = [t for t in all_tasks if t.get("stage", 1) == 1]
    deferred = [t for t in all_tasks if t.get("stage", 1) != 1]

    rows: list[dict] = []
    cat_score: dict[tuple[str, str], float] = defaultdict(float)
    cat_weight: dict[str, float] = defaultdict(float)
    # Cost-axis accumulators (techne#118 thesis): precision + token_cost per arm.
    cost_precision: dict[str, float] = defaultdict(float)
    cost_tokens: dict[str, int] = defaultdict(int)

    for task in tasks:
        w = task.get("weight", 1.0)
        category = task.get("category", "exact-recall")
        cat_weight[category] += w
        for arm in ARMS:
            t0 = time.perf_counter()
            retrieved = _retrieve(arm, db_path, task, top_k)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            score, detail = score_task(task, retrieved, gold)
            cat_score[(category, arm)] += score * w
            cost_precision[arm] += detail["precision"]
            cost_tokens[arm] += detail["token_cost"]
            rows.append({
                # `variant` is a TASK-CORPUS dimension (curated vs raw-dump vs the
                # reserved cold/warm), not an arm property — so it reads the same on
                # the bare control row. The arm is the separate `arm` field.
                "task_id": task["id"], "category": category,
                "variant": task.get("variant", "curated"), "arm": arm,
                "score": score, "weight": w, "latency_ms": latency_ms, **detail,
            })

    n_tasks = len(tasks) or 1
    mean_prec = {a: cost_precision[a] / n_tasks for a in ARMS}
    mean_tok = {a: cost_tokens[a] / n_tasks for a in ARMS}

    def cat_agg(category: str, arm: str) -> float:
        cw = cat_weight[category]
        return round(cat_score[(category, arm)] / cw, 3) if cw else 0.0

    # --- write JSONL results ---
    results_dir = _EVAL_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with (results_dir / "last_run.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps({"run_at": stamp, **r}) + "\n")

    # --- print the report ---
    with capsys.disabled():
        print(f"\n=== memory eval (#118) — {stamp} ===")
        print(f"{'task':<22} {'arm':<16} {'score':>6} {'recall':>7} {'prec':>6} {'n':>4}")
        for r in rows:
            print(f"{r['task_id']:<22} {r['arm']:<16} {r['score']:>6} "
                  f"{r['recall']:>7} {r['precision']:>6} {r['n_results']:>4}")
        print("-" * 64)
        print("per-category recall (kill-gated on bare/techne):")
        for category in sorted(cat_weight):
            per = "  ".join(f"{arm}={cat_agg(category, arm)}" for arm in ARMS)
            print(f"  [{category}]  {per}")
        print("cost axis (the curated-beats-raw thesis):")
        for arm in ("techne", "raw-source-dump"):
            print(f"  {arm:<16} mean_precision={round(mean_prec[arm], 3)}  "
                  f"mean_token_cost={round(mean_tok[arm])}")
        if deferred:
            print(f"deferred to Stage 2 (LLM judge): {[t['id'] for t in deferred]}")

    # --- per-category kill-gates (techne#118) — positive categories only ---
    for category in sorted(cat_weight):
        g = gates.get(category)
        assert g is not None, f"no gate configured for category '{category}'"
        bare, techne = cat_agg(category, "bare"), cat_agg(category, "techne")
        assert bare == 0.0, f"[{category}] answerable without memory (bare={bare}) — tighten it"
        assert techne >= g["floor"], (
            f"KILL-GATE [{category}]: techne {techne} < floor {g['floor']}"
        )
        assert (techne - bare) >= g["margin"], (
            f"KILL-GATE [{category}]: margin {round(techne - bare, 3)} < {g['margin']}"
        )

    # --- cost-axis thesis: curated recall is far more precise + cheaper than a raw
    #     dump. Gated here (raw-dump is kept OUT of the recall gate by design). ---
    assert mean_prec["techne"] >= cost_ratio * mean_prec["raw-source-dump"], (
        f"THESIS: curated precision {round(mean_prec['techne'], 3)} not >= {cost_ratio}× "
        f"raw-dump {round(mean_prec['raw-source-dump'], 3)}"
    )
    assert mean_tok["techne"] < mean_tok["raw-source-dump"], (
        f"THESIS: curated token_cost {round(mean_tok['techne'])} not < raw-dump "
        f"{round(mean_tok['raw-source-dump'])}"
    )
