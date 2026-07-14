"""Utilization audit over the observation ledger + injection log (techne#329).

Two read-only panels, no writes:
  - ``injection_utilization`` — per-source aggregate over ``context_injections``
    (the memory read-path that #331 made visible).
  - ``read_write_ledger`` — read-vs-write counts per memory domain over
    ``observations`` (refreshes #329's original findings table on live data).

Deliberately DESCRIPTIVE only. Attribution — "was the injected item actually
used this turn" — is the harder counterfactual and lives in techne#105; this
module does not attempt it.
"""

from __future__ import annotations

from pathlib import Path

from techne.core.stores import plain_connect

# Tool-name groupings — domain semantics, not user config. Full MCP tool names
# as stored in observations.tool_name. Centralised here so the audit's notion of
# "a memory read" has one home as the tool surface grows.
_MEMORY_WRITE = ("mcp__techne__techne_store",)
_MEMORY_READ = ("mcp__techne__techne_recall", "mcp__techne__techne_search")
_CODEX_READ = ("mcp__techne__codex_search", "mcp__techne__codex_blast_radius")
_ARCHITECT_READ = ("mcp__techne__pattern_search", "mcp__techne__pattern_related")
_LIBRARIAN_READ_PREFIX = "mcp__techne__librarian_"  # the librarian_* family
# Code-write baseline for the codex "reads per edit" ratio. Edit only, to stay
# directly comparable to #329's original finding (Edit dominates by ~100x).
_CODE_EDIT = ("Edit",)


def _window(since_days: int | None) -> tuple[str, list]:
    """Optional recency filter on a ``timestamp`` column. Returns (where, params).

    ``timestamp`` is stored UTC ('YYYY-MM-DD HH:MM:SS'), so a lexical compare
    against ``datetime('now', '-N days')`` (also UTC) windows correctly.
    """
    if since_days is None:
        return "", []
    return "WHERE timestamp >= datetime('now', ?)", [f"-{int(since_days)} days"]


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def injection_utilization(db_path: Path, *, since_days: int | None = None) -> list[dict]:
    """Per-source aggregate over ``context_injections``.

    One row per ``source`` (session_start / per_turn_recall / codex_edit_rail /
    …): event count, total+avg hits, total+avg approx_tokens, distinct sessions,
    empty-fire count (``hit_count=0`` — fired but selected nothing, itself a
    signal), and the first/last timestamp. Returns [] if nothing has been logged
    (the table is created lazily by the injection writer, so it may not exist).
    """
    where, params = _window(since_days)
    conn = plain_connect(db_path)
    try:
        if not _table_exists(conn, "context_injections"):
            return []
        rows = conn.execute(
            f"""SELECT source,
                       COUNT(*)                                    AS events,
                       COALESCE(SUM(hit_count), 0)                 AS total_hits,
                       COALESCE(SUM(approx_tokens), 0)             AS total_tokens,
                       COUNT(DISTINCT session_id)                  AS sessions,
                       SUM(CASE WHEN hit_count = 0 THEN 1 ELSE 0 END) AS empty_events,
                       MIN(timestamp)                             AS first_at,
                       MAX(timestamp)                             AS last_at
                FROM context_injections
                {where}
                GROUP BY source
                ORDER BY events DESC""",
            params,
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        events = r["events"]
        out.append(
            {
                "source": r["source"],
                "events": events,
                "total_hits": r["total_hits"],
                "avg_hits": round(r["total_hits"] / events, 1) if events else 0.0,
                "total_tokens": r["total_tokens"],
                "avg_tokens": round(r["total_tokens"] / events) if events else 0,
                "sessions": r["sessions"],
                "empty_events": r["empty_events"],
                "first_at": r["first_at"],
                "last_at": r["last_at"],
            }
        )
    return out


def read_write_ledger(db_path: Path, *, since_days: int | None = None) -> dict:
    """Read-vs-write counts per memory domain over ``observations``.

    Refreshes #329's findings table: memory store-vs-recall/search, codex
    reads-vs-edits, and the two write-only ghosts (architect patterns, librarian
    — 0 reads at the time of the audit). Ratios are ``None`` when the denominator
    is zero (never read) rather than raising.
    """
    where, params = _window(since_days)
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT tool_name, COUNT(*) AS n FROM observations {where} GROUP BY tool_name",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM observations {where}", params
        ).fetchone()[0]
    finally:
        conn.close()

    counts = {r["tool_name"]: r["n"] for r in rows}

    def sum_tools(names: tuple[str, ...]) -> int:
        return sum(counts.get(n, 0) for n in names)

    def sum_prefix(prefix: str) -> int:
        return sum(n for t, n in counts.items() if t.startswith(prefix))

    mem_write = sum_tools(_MEMORY_WRITE)
    mem_read = sum_tools(_MEMORY_READ)
    codex_read = sum_tools(_CODEX_READ)
    code_edit = sum_tools(_CODE_EDIT)
    architect_read = sum_tools(_ARCHITECT_READ)
    librarian_read = sum_prefix(_LIBRARIAN_READ_PREFIX)

    return {
        "total_observations": total,
        "memory": {
            "write": mem_write,
            "read": mem_read,
            "writes_per_read": round(mem_write / mem_read, 2) if mem_read else None,
        },
        "codex": {
            "read": codex_read,
            "edit": code_edit,
            "edits_per_read": round(code_edit / codex_read, 1) if codex_read else None,
        },
        "architect": {"read": architect_read},
        "librarian": {"read": librarian_read},
    }
