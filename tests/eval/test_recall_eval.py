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

ARMS = ("bare", "techne")


def _load_baseline() -> tuple[list[dict], dict]:
    doc = yaml.safe_load((_EVAL_DIR / "recall_baseline.yaml").read_text())
    return doc["tasks"], doc.get("config", {})


def _retrieve(arm: str, db_path: Path, task: dict, top_k: int) -> list[dict]:
    if arm == "bare":
        return []  # control: no memory
    # use_vectors=False → deterministic FTS path (v1 measures the FTS retriever;
    # vector fusion is a later eval dimension and needs fastembed).
    return search(db_path, task["query"], table=task.get("table", "all"),
                  limit=top_k, use_vectors=False)


def test_recall_eval(tmp_path, capsys):
    db_path = tmp_path / "eval.db"
    gold = seed(db_path)
    tasks, cfg = _load_baseline()
    top_k = cfg.get("top_k", 10)
    gate = cfg.get("gate")
    margin = cfg.get("margin")

    rows: list[dict] = []
    agg = {arm: 0.0 for arm in ARMS}
    total_weight = sum(t.get("weight", 1.0) for t in tasks)

    for task in tasks:
        w = task.get("weight", 1.0)
        for arm in ARMS:
            retrieved = _retrieve(arm, db_path, task, top_k)
            score, detail = score_task(task, retrieved, gold)
            agg[arm] += score * w
            rows.append({
                "task_id": task["id"], "arm": arm, "score": score,
                "weight": w, **detail,
            })

    for arm in ARMS:
        agg[arm] = round(agg[arm] / total_weight, 3) if total_weight else 0.0

    # --- write JSONL results ---
    results_dir = _EVAL_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with (results_dir / "last_run.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps({"run_at": stamp, **r}) + "\n")

    # --- print the table ---
    with capsys.disabled():
        print(f"\n=== recall eval (#68) — {stamp} ===")
        print(f"{'task':<20} {'arm':<8} {'score':>6} {'recall':>7} {'cover':>6} {'n':>3}  misses")
        for r in rows:
            print(f"{r['task_id']:<20} {r['arm']:<8} {r['score']:>6} "
                  f"{r['recall']:>7} {r['coverage']:>6} {r['n_results']:>3}  {r['misses']}")
        print("-" * 60)
        for arm in ARMS:
            print(f"aggregate[{arm}] = {agg[arm]}")
        observed_margin = round(agg["techne"] - agg["bare"], 3)
        print(f"techne - bare margin = {observed_margin}")
        print(f"kill-gate: techne >= {gate}  AND  margin >= {margin}  (see EVAL.md)")

    # Sanity: the tasks must genuinely require memory (bare can't satisfy them).
    assert agg["bare"] == 0.0, "a task is answerable without memory — tighten it"

    # Kill-gate (#68 M2): a change that drops recall below the floor, or fails to
    # beat the no-memory control by the margin, fails here and does not merge.
    assert agg["techne"] >= gate, (
        f"KILL-GATE: techne aggregate {agg['techne']} < gate {gate} on the frozen baseline"
    )
    assert (agg["techne"] - agg["bare"]) >= margin, (
        f"KILL-GATE: techne beats bare by {observed_margin} < required margin {margin}"
    )
