"""Exit-code contract for scripts/compare_retrieval_metrics.py — techne#832.

Runs the comparer as a subprocess, the same shape as test_arch_cards_gate.py, so
the contract is pinned independently of how CI happens to invoke it. Stdlib-only.

THE DEFECT THIS GUARDS. The eval-vec kill-gates are absolute floors, and
retrieval_gold.yaml sizes the paraphrase recall@10 floor (0.8) so that "a single
gold slipping rank 10->11 (r@10 1.0->0.833) still passes" — a tolerance for
cross-machine BLAS drift. A draft of #827's fix spent it deterministically and
the gate went green; PR #829's own body records the measurement, and its review
rounds caught and ablated the draft before merge, so it never reached `main`.
`test_catches_the_dropped_gold_shape` below is that exact shape.
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


def test_catches_the_dropped_gold_shape(tmp_path):
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


# ---------------------------------------------------------------------------
# techne#857 — a leg that measured fewer cases is a broken run, not a clean diff.
#
# WHY THESE EXIST. #857 adds `--continue-on-collection-errors` to eval-vec's BASE
# leg, because that leg runs MAIN's library against HEAD's tests and any PR that
# lands a module plus its test module makes that file unimportable — killing the
# whole leg at collection over tests `-m eval_vec` would have deselected anyway.
# The cost of tolerating the error is that a SHORT base run now reaches the
# comparer. Diffing it against a full head run produces a table that looks clean
# and compares different case sets, which is techne#832's defect wearing a new
# hat. These pin the refusal.
# ---------------------------------------------------------------------------


def test_a_short_base_leg_is_refused_not_diffed(tmp_path):
    """The #857 acceptance criterion: unequal `n` must not produce a verdict."""
    t, base_c = _clean()
    base_c["paraphrase"]["n"] = 4          # base collected fewer cases
    _, head_c = _clean()                   # head has all 6
    r = _run(_dump(tmp_path / "b.json", t, base_c), _dump(tmp_path / "h.json", t, head_c))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "CANNOT DIFF" in r.stdout
    assert "base n=4, head n=6" in r.stdout


def test_the_refusal_prints_no_comparison_table(tmp_path):
    """Refuse BEFORE reporting, not after.

    A printed `base -> head` row invites exactly the comparison the refusal
    exists to prevent, and a reader who sees numbers will use them.
    """
    t, base_c = _clean()
    base_c["paraphrase"]["n"] = 4
    _, head_c = _clean()
    r = _run(_dump(tmp_path / "b.json", t, base_c), _dump(tmp_path / "h.json", t, head_c))
    assert "per-class means" not in r.stdout, r.stdout
    assert "recall_at_10=" not in r.stdout, r.stdout


def test_a_class_absent_from_one_leg_is_refused(tmp_path):
    """A whole class vanishing is the same failure at larger amplitude."""
    t, c = _clean()
    two = {**c, "exact": {"recall_at_5": 1.0, "recall_at_10": 1.0, "mrr": 1.0,
                          "fpr": 0.0, "n": 6}}
    r = _run(_dump(tmp_path / "b.json", t, c), _dump(tmp_path / "h.json", t, two))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "MISSING from base" in r.stdout


def test_equal_case_counts_still_reach_the_regression_check(tmp_path):
    """The guard must not shadow the check it precedes.

    Same `n` on both legs and a real regression: #832's verdict must still be
    the one reported, not #857's.
    """
    bt, bc = _clean(r10=1.0)
    ht, hc = _clean(r10=0.833)
    r = _run(_dump(tmp_path / "b.json", bt, bc), _dump(tmp_path / "h.json", ht, hc))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "CANNOT DIFF" not in r.stdout
    assert "REGRESSION vs base" in r.stdout
