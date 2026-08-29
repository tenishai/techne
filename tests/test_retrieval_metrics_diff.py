"""Exit-code contract for scripts/compare_retrieval_metrics.py — techne#832.

Runs the comparer as a subprocess, the same shape as test_arch_cards_gate.py, so
the contract is pinned independently of how CI happens to invoke it. Stdlib-only.

THE DEFECT THIS GUARDS. The eval-vec kill-gates are absolute floors, and
retrieval_gold.yaml sizes the paraphrase recall@10 floor (0.8) so that "a single
gold slipping rank 10->11 (r@10 1.0->0.833) still passes" — a tolerance for
cross-machine BLAS drift. Commit 275035a spent it deterministically and the gate
went green. `test_catches_the_275035a_shape` below is that exact shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compare_retrieval_metrics.py"


def _dump(path: Path, per_task: dict, per_class: dict) -> Path:
    path.write_text(
        json.dumps({"schema": 1, "gate": "retrieval_vec",
                    "per_task": per_task, "per_class": per_class})
    )
    return path


def _run(base: Path, head: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(base), str(head), *args],
        capture_output=True, text=True,
    )


def _clean(r10: float = 1.0, mrr: float = 0.546, fpr: float = 0.0) -> tuple[dict, dict]:
    task = {"paraphrase-dbpool": {"class": "paraphrase", "recall_at_5": 0.0,
                                  "recall_at_10": r10, "mrr": mrr, "fpr": fpr}}
    cls = {"paraphrase": {"recall_at_5": 0.667, "recall_at_10": r10, "mrr": mrr,
                          "fpr": fpr, "n": 6}}
    return task, cls


def test_identical_dumps_pass(tmp_path):
    t, c = _clean()
    base, head = _dump(tmp_path / "b.json", t, c), _dump(tmp_path / "h.json", t, c)
    r = _run(base, head)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "No regression" in r.stdout


def test_catches_the_275035a_shape(tmp_path):
    """THE ACCEPTANCE CRITERION FOR techne#832.

    One gold collapses 1.0 -> 0.0 while the class mean only sags to 0.833, which
    is still above the 0.8 kill-gate — so the absolute gate passes and this must
    not. Verified against the real leg on 2026-08-29: reinstating `_fts_floor`'s
    pre-#829 `limit // 3` sizing gives `pytest -m eval_vec` = 2 passed and this
    comparer exit 1.
    """
    bt, bc = _clean()
    ht = {"paraphrase-dbpool": {"class": "paraphrase", "recall_at_5": 0.0,
                                "recall_at_10": 0.0, "mrr": 0.0, "fpr": 0.0}}
    hc = {"paraphrase": {"recall_at_5": 0.667, "recall_at_10": 0.833, "mrr": 0.528,
                         "fpr": 0.0, "n": 6}}
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", ht, hc))
    assert r.returncode == 1, r.stdout + r.stderr
    # Per-task at FULL amplitude is the half the class mean hides.
    assert "paraphrase-dbpool" in r.stdout
    assert "1.0 -> 0.0" in r.stdout


def test_class_mean_alone_would_have_missed_it(tmp_path):
    """The per-task rows are load-bearing, not decoration.

    Same regression with the per_task scope emptied: 0.167 of class-mean movement
    is all that is left, and it is still caught — but this pins that per-task is
    what reports the collapse at amplitude, so deleting it would silently narrow
    the gate to the thing that already failed once.
    """
    _, bc = _clean()
    hc = {"paraphrase": {"recall_at_5": 0.667, "recall_at_10": 0.833, "mrr": 0.528,
                         "fpr": 0.0, "n": 6}}
    r = _run(_dump(tmp_path / "b.json", {}, bc), _dump(tmp_path / "h.json", {}, hc))
    assert r.returncode == 1
    assert "paraphrase-dbpool" not in r.stdout  # nothing per-task to report
    assert "per_class" in r.stdout


def test_improvement_is_not_a_regression(tmp_path):
    bt, bc = _clean(r10=0.833, mrr=0.528)
    ht, hc = _clean(r10=1.0, mrr=0.546)
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", ht, hc))
    assert r.returncode == 0, r.stdout + r.stderr


def test_fpr_is_scored_the_other_way(tmp_path):
    """A leak rate going UP is a regression — the one metric where more is worse."""
    bt, bc = _clean(fpr=0.0)
    ht, hc = _clean(fpr=0.2)
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", ht, hc))
    assert r.returncode == 1
    assert "fpr" in r.stdout


def test_a_disappearing_task_is_reported_not_ignored(tmp_path):
    """Dropping a gold out of the set must not read as 'no regression'."""
    bt, bc = _clean()
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", {}, bc))
    assert r.returncode == 1
    assert "MISSING" in r.stdout


def test_epsilon_zero_is_the_default(tmp_path):
    """Zero tolerance is the point: the leg is byte-deterministic on one runner,
    so the smallest real movement is the code. A drop under any epsilon a drift
    argument would motivate must still fail by default."""
    bt, bc = _clean(mrr=0.546)
    ht, hc = _clean(mrr=0.545)
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", ht, hc))
    assert r.returncode == 1
    # ...and --epsilon is the documented escape hatch for the cross-machine case.
    r2 = _run(_dump(tmp_path / "b2.json", bt, bc), _dump(tmp_path / "h2.json", ht, hc),
              "--epsilon", "0.01")
    assert r2.returncode == 0, r2.stdout + r2.stderr


def test_unknown_schema_is_refused_rather_than_guessed(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"schema": 99, "per_task": {}, "per_class": {}}))
    t, c = _clean()
    r = _run(p, _dump(tmp_path / "h.json", t, c))
    assert r.returncode != 0
    assert "schema" in (r.stdout + r.stderr)
