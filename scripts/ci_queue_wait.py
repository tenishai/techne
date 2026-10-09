#!/usr/bin/env python3
"""Measure per-job CI queue-wait for the self-hosted runners -- issue 897.

On demand, read-only, stdlib plus the `gh` CLI (must be authenticated).

    python3 scripts/ci_queue_wait.py                  # last 100 runs of test.yml
    python3 scripts/ci_queue_wait.py --runs 30 --json rows.json
    python3 scripts/ci_queue_wait.py --runners 2      # the model's current runner count

Queue-wait of a job is job.started_at - job.created_at. Cancelled runs and
skipped jobs are dropped (they never started). The output has a per-job table,
the same table split by event and by hour of day (UTC), a run-level line, a
packing model, the share of queued seconds that overlapped another run's jobs,
and a verdict on whether one more runner is justified:

    JUSTIFIED      predicted p50 wall improvement >= 25% AND all-jobs queue-wait p50 >= 60s
    NOT JUSTIFIED  all-jobs queue-wait p50 < 30s
    INCONCLUSIVE   anything else

The model is ideal(R) = max(longest job, total exec / R) per run, with the
median taken across runs. It assumes jobs pack perfectly, so it is optimistic.
Cost: one API call per page of runs plus one per run (about 1 + N).
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import math
import subprocess
import sys
from datetime import datetime, timezone

log = logging.getLogger("ci_queue_wait")

IMPROVEMENT_MIN = 0.25
WAIT_JUSTIFIED_S = 60
WAIT_NOT_JUSTIFIED_S = 30
MIN_HOUR_SAMPLES = 5
OVERLAP_MIN_WAIT_S = 5
KEPT_CONCLUSIONS = ("success", "failure")


def gh(path: str) -> dict:
    """GET `path` through `gh api`; raise with gh's stderr on failure."""
    log.debug("gh api %s", path)
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {path} exited {proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile, q in (0, 1]."""
    if not values:
        raise ValueError("percentile of an empty list")
    v = sorted(values)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def fetch_runs(repo: str, workflow: str, n: int) -> list[dict]:
    kept: list[dict] = []
    seen: set[int] = set()
    page = 1
    while len(kept) < n:
        data = gh(
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            f"?per_page=100&status=completed&page={page}"
        )
        batch = data.get("workflow_runs", [])
        if not batch:
            break
        for r in batch:
            if r.get("conclusion") in KEPT_CONCLUSIONS and r["id"] not in seen:
                seen.add(r["id"])
                kept.append(r)
        page += 1
    return kept[:n]


def job_rows(run: dict, jobs: list[dict]) -> list[dict]:
    rows = []
    for j in jobs:
        if not j.get("started_at") or not j.get("completed_at") or j.get("conclusion") == "skipped":
            continue
        rows.append({
            "run_id": run["id"],
            "event": run.get("event"),
            "job": j["name"],
            "runner_name": j.get("runner_name"),
            "run_started_at": run["run_started_at"],
            "created_at": j["created_at"],
            "started_at": j["started_at"],
            "completed_at": j["completed_at"],
            "queue_wait_s": ts(j["started_at"]) - ts(j["created_at"]),
            "exec_s": ts(j["completed_at"]) - ts(j["started_at"]),
        })
    return rows


def collect(repo: str, workflow: str, n: int) -> list[dict]:
    rows: list[dict] = []
    for run in fetch_runs(repo, workflow, n):
        jobs = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest")["jobs"]
        rows += job_rows(run, jobs)
    return rows


def run_summaries(rows: list[dict]) -> list[dict]:
    by_run: dict[int, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)
    out = []
    for run_id, js in by_run.items():
        t0 = ts(js[0]["run_started_at"])
        out.append({
            "run_id": run_id,
            "event": js[0]["event"],
            "first_wait": min(ts(j["started_at"]) for j in js) - t0,
            "wall": max(ts(j["completed_at"]) for j in js) - t0,
            "critical": max(j["exec_s"] for j in js),
            "total_exec": sum(j["exec_s"] for j in js),
        })
    return out


def ideal(run: dict, runners: int) -> float:
    return max(run["critical"], run["total_exec"] / runners)


def model(runs: list[dict], runners: int) -> dict:
    wall = pct([r["wall"] for r in runs], 0.5)
    ideal_now = pct([ideal(r, runners) for r in runs], 0.5)
    ideal_next = pct([ideal(r, runners + 1) for r in runs], 0.5)
    return {
        "runners": runners,
        "observed_wall_p50": wall,
        "ideal_now": ideal_now,
        "ideal_next": ideal_next,
        "predicted_improvement": (wall - ideal_next) / wall if wall else 0.0,
    }


def verdict(predicted_improvement: float, wait_p50: float) -> str:
    if wait_p50 < WAIT_NOT_JUSTIFIED_S:
        return "NOT JUSTIFIED (queue-wait is not the gap)"
    if predicted_improvement >= IMPROVEMENT_MIN and wait_p50 >= WAIT_JUSTIFIED_S:
        return "JUSTIFIED"
    return "INCONCLUSIVE"


def cross_run_share(rows: list[dict]) -> tuple[float, float]:
    """(queued seconds, of which overlapped another run's executing jobs)."""
    iv = [(r["run_id"], ts(r["started_at"]), ts(r["completed_at"]), ts(r["created_at"])) for r in rows]
    total = cross = 0.0
    for run_id, s, _e, c in iv:
        wait = s - c
        if wait <= OVERLAP_MIN_WAIT_S:
            continue
        spans = sorted(
            (max(os_, c), min(oe, s)) for orun, os_, oe, _ in iv
            if orun != run_id and os_ < s and oe > c
        )
        covered, cur = 0.0, None
        for a, b in spans:
            if cur is None or a > cur[1]:
                covered += cur[1] - cur[0] if cur else 0.0
                cur = [a, b]
            else:
                cur[1] = max(cur[1], b)
        covered += cur[1] - cur[0] if cur else 0.0
        total += wait
        cross += min(covered, wait)
    return total, cross


def wait_line(label: str, vals: list[float], execs: list[float]) -> str:
    return (
        f"{label:<22} n={len(vals):<4} wait p50={pct(vals, .5):>5.0f} p90={pct(vals, .9):>5.0f}"
        f" max={max(vals):>5.0f} | exec p50={pct(execs, .5):>5.0f}"
    )


def report(rows: list[dict], runners: int) -> str:
    runs = run_summaries(rows)
    waits = [r["queue_wait_s"] for r in rows]
    execs = [r["exec_s"] for r in rows]
    out = [f"runs={len(runs)} jobs={len(rows)} (seconds throughout)", "", "per job:"]
    by_job: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_job[r["job"]].append(r)
    for name in sorted(by_job):
        js = by_job[name]
        out.append("  " + wait_line(name, [j["queue_wait_s"] for j in js], [j["exec_s"] for j in js]))
    out.append("  " + wait_line("ALL JOBS", waits, execs))

    out += ["", "by event:"]
    by_event: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_event[str(r["event"])].append(r)
    for ev in sorted(by_event):
        js = by_event[ev]
        out.append("  " + wait_line(ev, [j["queue_wait_s"] for j in js], [j["exec_s"] for j in js]))

    out += ["", f"by hour of day UTC (job created; buckets with >= {MIN_HOUR_SAMPLES} jobs):"]
    by_hour: dict[int, list[float]] = collections.defaultdict(list)
    for r in rows:
        by_hour[datetime.fromtimestamp(ts(r["created_at"]), timezone.utc).hour].append(r["queue_wait_s"])
    shown = [h for h in sorted(by_hour) if len(by_hour[h]) >= MIN_HOUR_SAMPLES]
    out += [f"  {h:02d}h n={len(by_hour[h]):<4} wait p50={pct(by_hour[h], .5):>5.0f}" for h in shown]
    if not shown:
        out.append("  (no bucket has enough samples)")

    out += ["", "run level: first-job wait p50=%.0f p90=%.0f | wall p50=%.0f p90=%.0f | critical p50=%.0f | total exec p50=%.0f" % (
        pct([r["first_wait"] for r in runs], .5), pct([r["first_wait"] for r in runs], .9),
        pct([r["wall"] for r in runs], .5), pct([r["wall"] for r in runs], .9),
        pct([r["critical"] for r in runs], .5), pct([r["total_exec"] for r in runs], .5))]

    m = model(runs, runners)
    out.append(
        "model: observed_wall_p50=%.0f ideal(%d)=%.0f ideal(%d)=%.0f predicted_improvement=%.1f%%" % (
            m["observed_wall_p50"], runners, m["ideal_now"], runners + 1, m["ideal_next"],
            100 * m["predicted_improvement"]))
    total, cross = cross_run_share(rows)
    share = cross / total if total else 0.0
    out.append(
        f"cross-run share (UPPER BOUND): {100 * share:.0f}% of {total:.0f}s queued overlapped "
        "another run's executing jobs"
    )
    wait_p50 = pct(waits, .5)
    out += ["", f"all-jobs queue-wait p50={wait_p50:.0f}s",
            f"THIRD RUNNER: {verdict(m['predicted_improvement'], wait_p50)}"
            if runners == 2 else
            f"RUNNER {runners + 1}: {verdict(m['predicted_improvement'], wait_p50)}",
            "",
            "caveats: job.created_at as 'queued' is GitHub's field, not documented as such; "
            "percentiles on few runs are noisy; the model assumes perfect packing (optimistic); "
            "runners shared with other workflows or repos cannot be told apart."]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="tenishai/techne")
    ap.add_argument("--workflow", default="test.yml")
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--json", metavar="PATH", help="write the raw job rows here")
    ap.add_argument("--runners", type=int, default=2, help="current runner count, for the model")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if args.runs < 1 or args.runners < 1:
        ap.error("--runs and --runners must be >= 1")
    try:
        rows = collect(args.repo, args.workflow, args.runs)
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("error: no completed success/failure runs with started jobs found", file=sys.stderr)
        return 1
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)
    print(report(rows, args.runners))
    return 0


if __name__ == "__main__":
    sys.exit(main())
