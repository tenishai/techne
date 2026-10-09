"""scripts/ci_queue_wait.py -- CI queue-wait measurement (issue 897). No network."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_queue_wait.py"
_spec = importlib.util.spec_from_file_location("ci_queue_wait", _PATH)
assert _spec and _spec.loader
cq = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cq
_spec.loader.exec_module(cq)


def _t(sec: int) -> str:
    return f"2026-10-08T10:{sec // 60:02d}:{sec % 60:02d}Z"


def _job(name, created, started, done, conclusion="success"):
    return {
        "name": name, "runner_name": "r1", "conclusion": conclusion,
        "created_at": _t(created) if created is not None else None,
        "started_at": _t(started) if started is not None else None,
        "completed_at": _t(done) if done is not None else None,
    }


def _run(rid, conclusion="success", event="pull_request"):
    return {"id": rid, "conclusion": conclusion, "event": event, "run_started_at": _t(0)}


# exec 148,141,175,26,8,6 from the spec
SIX = [
    _job("test (3.12)", 0, 0, 148), _job("test (3.13)", 0, 10, 151),
    _job("eval-vec", 0, 20, 195), _job("crash-recovery", 0, 150, 176),
    _job("acceptance", 0, 160, 168), _job("arch-cards", 0, 170, 176),
]


def test_percentile_nearest_rank():
    v = [15, 20, 35, 40, 50]
    assert cq.pct(v, 0.5) == 35
    assert cq.pct(v, 0.9) == 50
    assert cq.pct(v, 0.2) == 15
    assert cq.pct([7], 0.5) == 7
    with pytest.raises(ValueError):
        cq.pct([], 0.5)


def test_skipped_and_unstarted_jobs_are_excluded():
    jobs = [_job("a", 0, 5, 15), _job("b", 0, None, None, "cancelled"),
            _job("c", 0, 5, 9, "skipped")]
    rows = cq.job_rows(_run(1), jobs)
    assert [r["job"] for r in rows] == ["a"]
    assert rows[0]["queue_wait_s"] == 5 and rows[0]["exec_s"] == 10


def test_cancelled_runs_are_dropped(monkeypatch):
    runs = [_run(1), _run(2, "cancelled"), _run(3, "failure")]
    monkeypatch.setattr(cq, "gh", lambda path: {"workflow_runs": runs if "&page=1" in path else []})
    assert [r["id"] for r in cq.fetch_runs("o/r", "w.yml", 10)] == [1, 3]
    assert [r["id"] for r in cq.fetch_runs("o/r", "w.yml", 1)] == [1]


def test_fetch_runs_paginates_until_n(monkeypatch):
    pages = {1: [_run(1), _run(2, "cancelled")], 2: [_run(3), _run(4)], 3: [_run(5)]}
    monkeypatch.setattr(
        cq, "gh", lambda path: {"workflow_runs": pages.get(int(path.rsplit("page=", 1)[1]), [])}
    )
    assert [r["id"] for r in cq.fetch_runs("o/r", "w.yml", 3)] == [1, 3, 4]


def test_collect_uses_gh_helper_only(monkeypatch):
    def fake(path):
        if "/jobs" in path:
            return {"jobs": SIX}
        return {"workflow_runs": [_run(9)] if "&page=1" in path else []}
    monkeypatch.setattr(cq, "gh", fake)
    rows = cq.collect("o/r", "w.yml", 5)
    assert len(rows) == 6 and {r["run_id"] for r in rows} == {9}


def test_run_level_numbers_and_ideal():
    rows = cq.job_rows(_run(1), SIX)
    (run,) = cq.run_summaries(rows)
    assert run["first_wait"] == 0
    assert run["wall"] == 195
    assert run["critical"] == 175
    assert run["total_exec"] == 504
    assert cq.ideal(run, 2) == 252
    assert cq.ideal(run, 3) == 175


def test_model_predicts_improvement():
    rows = cq.job_rows(_run(1), SIX)
    m = cq.model(cq.run_summaries(rows), 2)
    assert m["ideal_now"] == 252 and m["ideal_next"] == 175
    assert m["observed_wall_p50"] == 195
    assert m["predicted_improvement"] == pytest.approx((195 - 175) / 195)


def test_verdict_boundaries():
    assert cq.verdict(0.25, 60) == "JUSTIFIED"
    assert cq.verdict(0.2499, 60) == "INCONCLUSIVE"
    assert cq.verdict(0.9, 59) == "INCONCLUSIVE"
    assert cq.verdict(0.9, 30) == "INCONCLUSIVE"
    assert cq.verdict(0.9, 29.9).startswith("NOT JUSTIFIED")
    assert cq.verdict(0.0, 0).startswith("NOT JUSTIFIED")


def test_cross_run_share_counts_only_other_runs():
    a = cq.job_rows(_run(1), [_job("x", 0, 0, 100)])
    b = cq.job_rows(_run(2), [_job("y", 10, 60, 70)])
    total, cross = cq.cross_run_share(a + b)
    assert (total, cross) == (50, 50)
    own = cq.job_rows(_run(3), [_job("x", 0, 0, 100), _job("y", 10, 60, 70)])
    assert cq.cross_run_share(own) == (50, 0)


def test_gh_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(
        cq.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 4, stdout="", stderr="gh: Bad credentials"),
    )
    with pytest.raises(RuntimeError, match="Bad credentials"):
        cq.gh("repos/o/r/actions/runs")


def test_main_reports_and_writes_json(monkeypatch, tmp_path, capsys):
    def fake(path):
        if "/jobs" in path:
            return {"jobs": SIX}
        return {"workflow_runs": [_run(9)] if "&page=1" in path else []}
    monkeypatch.setattr(cq, "gh", fake)
    out = tmp_path / "rows.json"
    assert cq.main(["--runs", "1", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    for name in ("acceptance", "arch-cards", "crash-recovery", "eval-vec", "test (3.12)", "test (3.13)"):
        assert name in text
    assert "model:" in text and "THIRD RUNNER:" in text
    assert len(json.loads(out.read_text())) == 6


def test_main_fails_loudly_when_gh_fails(monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("gh api x exited 1: nope")
    monkeypatch.setattr(cq, "gh", boom)
    assert cq.main(["--runs", "1"]) == 1
    assert "nope" in capsys.readouterr().err
