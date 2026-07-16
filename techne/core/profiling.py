"""Hot-path profiling (techne#359).

A repeatable, read-only profiling pass over the costs that the test suite can
never see — import overhead, thread-detection time at real corpus size, and
query plans that silently degrade to table scans as the corpus grows. This is
the forcing function the review found missing: correctness has tests, latency
had nothing, so #350/#351 went unnoticed until a deliberate profiling pass.

Never mutates the DB. Intended for `techne profile` and for CI threshold gates.
"""

from __future__ import annotations

import cProfile
import pstats
import subprocess
import sys
import time
from pathlib import Path

# Representative hot queries. Kept as an explicit, confidently-valid set (mirrors
# the real WHERE/ORDER BY shapes in state.py) so EXPLAIN QUERY PLAN can flag a
# SCAN before it becomes a latency-at-scale problem. Params are placeholders —
# EXPLAIN plans the statement without executing it.
_HOT_QUERIES: list[tuple[str, str, tuple]] = [
    (
        "observations_by_session",
        "SELECT id, content FROM observations WHERE session_id = ? ORDER BY timestamp",
        ("x",),
    ),
    (
        "observations_recent_by_time",
        "SELECT id FROM observations ORDER BY timestamp DESC LIMIT 2000",
        (),
    ),
    (
        "resume_active_threads",
        "SELECT id, label, summary, resume_points, updated_at FROM threads "
        "WHERE status = 'active' AND resume_points IS NOT NULL AND resume_points != '' "
        "ORDER BY updated_at DESC LIMIT 5",
        (),
    ),
]


def profile_import_cost() -> dict:
    """Cold import cost of ``techne.config`` — the price every hook subprocess
    pays before doing any work (see #351). Measured in a fresh interpreter via
    ``-X importtime`` (in-process timing is meaningless once imported)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "importtime", "-c", "import techne.config"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        return {"cumulative_us": None, "cumulative_ms": None, "error": str(e)}

    cumulative_us: int | None = None
    for line in proc.stderr.splitlines():
        # "import time: self | cumulative | imported package"
        if line.rstrip().endswith("techne.config") and "|" in line:
            parts = line.split("|")
            try:
                cumulative_us = int(parts[1].strip())
            except (ValueError, IndexError):
                pass
    return {
        "cumulative_us": cumulative_us,
        "cumulative_ms": round(cumulative_us / 1000, 1) if cumulative_us else None,
    }


def _largest_session(db_path: Path) -> str | None:
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT session_id FROM observations "
            "GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def profile_thread_detection(db_path: Path, session_id: str | None = None) -> dict:
    """Time heuristic thread detection over a real session (the #350 hot path),
    with the top functions by cumulative time. Defaults to the largest session —
    the one most likely to be near the hook budget."""
    from techne.core import heuristic_threads as ht
    from techne.core.state import get_observations

    if session_id is None:
        session_id = _largest_session(db_path)
    if session_id is None:
        return {"session_id": None, "obs_count": 0, "elapsed_s": None, "top_functions": []}

    observations = get_observations(db_path, session_id)
    profiler = cProfile.Profile()
    start = time.monotonic()
    profiler.enable()
    ht.detect_threads(observations)
    profiler.disable()
    elapsed = time.monotonic() - start

    stats = pstats.Stats(profiler)
    ranked = sorted(stats.stats.items(), key=lambda kv: kv[1][3], reverse=True)
    top = [
        {
            "func": f"{Path(fn[0]).name}:{fn[1]}({fn[2]})",
            "ncalls": data[0],
            "cumtime_s": round(data[3], 4),
        }
        for fn, data in ranked[:8]
    ]
    return {
        "session_id": session_id,
        "obs_count": len(observations),
        "elapsed_s": round(elapsed, 4),
        "top_functions": top,
    }


def profile_query_plans(db_path: Path) -> list[dict]:
    """Run EXPLAIN QUERY PLAN on the hot queries and flag full table SCANs — the
    kind that are cheap now and quadratic once the corpus grows."""
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        results: list[dict] = []
        for name, sql, params in _HOT_QUERIES:
            try:
                rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                detail = " ; ".join(str(r[3]) for r in rows)
                has_scan = any("SCAN" in str(r[3]) for r in rows)
                results.append({"name": name, "plan": detail, "has_scan": has_scan})
            except Exception as e:  # noqa: BLE001 — a bad query self-reports, never crashes
                results.append({"name": name, "plan": f"error: {e}", "has_scan": None})
        return results
    finally:
        conn.close()


def run_profile(db_path: Path, session_id: str | None = None) -> dict:
    """Full read-only profiling report."""
    return {
        "import_cost": profile_import_cost(),
        "thread_detection": profile_thread_detection(db_path, session_id),
        "query_plans": profile_query_plans(db_path),
    }


def format_report(report: dict) -> str:
    """Render the profiling report as terse operator-facing text."""
    lines: list[str] = ["techne hot-path profile"]

    imp = report["import_cost"]
    ms = imp.get("cumulative_ms")
    lines.append(
        f"\nimport techne.config: {ms}ms"
        if ms is not None
        else "\nimport techne.config: (unavailable)"
    )

    det = report["thread_detection"]
    if det["session_id"] is None:
        lines.append("\nthread detection: (no sessions to profile)")
    else:
        lines.append(
            f"\nthread detection: {det['elapsed_s']}s over {det['obs_count']} obs "
            f"(session {det['session_id']})"
        )
        for f in det["top_functions"]:
            lines.append(f"  {f['cumtime_s']}s  {f['ncalls']:>7}x  {f['func']}")

    lines.append("\nquery plans:")
    for q in report["query_plans"]:
        mark = "⚠️ SCAN" if q["has_scan"] else ("ok" if q["has_scan"] is False else "?")
        lines.append(f"  [{mark}] {q['name']}: {q['plan']}")

    return "\n".join(lines)
