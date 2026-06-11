"""Session state management — starting, ending, and querying sessions."""

import re
import uuid
from pathlib import Path

from techne.core.database import connect


# Heuristic-summary detectors.
#
# The auto-generator emits two recognizable shapes:
#   - multi-thread: lines like "**Thread 1: label** — content"
#                   (see threads.py:format_combined_summary)
#   - single-thread fallback: "N observations; tools: ..."
#                   (see heuristic_threads.py:fallback_summary)
# A "## Resume Points" header is a third tell — only the heuristic emits it.
_HEURISTIC_PATTERNS = (
    re.compile(r"\*\*Thread\s+\d+:"),
    re.compile(r"^\s*\d+\s+observations;\s+tools:", re.MULTILINE),
    re.compile(r"^##\s+Resume\s+Points\s*$", re.MULTILINE),
)


def looks_heuristic(text: str) -> bool:
    """True if `text` matches the structural fingerprint of the auto-generator.

    Used by the one-shot legacy-summary cleanup to decide whether a row's
    pre-typed-columns `summary` content is actually heuristic-shaped and
    should be moved to summary_heuristic. False negatives are fine (we just
    leave the row alone); false positives would silently reclassify a manual
    summary, so the patterns are kept narrow.
    """
    if not text:
        return False
    return any(p.search(text) for p in _HEURISTIC_PATTERNS)


def migrate_legacy_summaries(db_path: Path, *, dry_run: bool = False) -> dict:
    """Move heuristic-shaped content from sessions.summary → summary_heuristic.

    Pre-dual-column rows packed all sources into `summary`. Going forward each
    source writes to its own column, but legacy rows still pin the "manual"
    slot and beat newer LLM/heuristic signals at read time. This cleanup
    reclassifies them so the priority rule (manual > LLM > heuristic) gives
    the intended result.

    For each row where `summary` is heuristic-shaped:
      - if summary_heuristic is empty → move legacy content into it
      - if summary_heuristic already has content (a fresher auto-summary) →
        just clear `summary`; the legacy is older and redundant
    Either way, after migration `summary` no longer holds heuristic content,
    so it stops winning the manual > LLM > heuristic read priority. Returns
    {scanned, moved, cleared, skipped}.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, summary, summary_heuristic FROM sessions
               WHERE summary IS NOT NULL AND summary != ''"""
        ).fetchall()
        moved = 0
        cleared = 0
        skipped = 0
        for r in rows:
            if not looks_heuristic(r["summary"]):
                skipped += 1
                continue
            already_has_heuristic = bool((r["summary_heuristic"] or "").strip())
            if not dry_run:
                if already_has_heuristic:
                    conn.execute(
                        "UPDATE sessions SET summary = NULL WHERE id = ?", (r["id"],)
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET summary_heuristic = summary, summary = NULL WHERE id = ?",
                        (r["id"],),
                    )
            if already_has_heuristic:
                cleared += 1
            else:
                moved += 1
        if not dry_run:
            conn.commit()
        return {
            "scanned": len(rows),
            "moved": moved,
            "cleared": cleared,
            "skipped": skipped,
        }
    finally:
        conn.close()


def start_session(
    db_path: Path,
    project_path: str = "",
    agent_identity: str = "",
    platform: str = "claude-code",
    *,
    session_id: str | None = None,
) -> str:
    """Create a new session. Returns the session id.

    If `session_id` is provided, use it verbatim; otherwise generate a UUID.
    The explicit-id path is for runtimes that have their own conversation /
    task / dispatch identifiers (e.g., A2A Task IDs) and want a 1:1 mapping
    onto techne sessions. Caller is responsible for namespacing if collisions
    are possible; the INSERT fails with a PRIMARY KEY violation on duplicate
    ids — use `lifecycle.init_session_for_external_id` for idempotent setup.
    """
    if session_id is None:
        session_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions (id, project_path, agent_identity, platform)
               VALUES (?, ?, ?, ?)""",
            (session_id, project_path, agent_identity, platform),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


_SUMMARY_COLUMN_FOR_SOURCE = {
    "manual": "summary",
    "llm": "summary_llm",
    "heuristic": "summary_heuristic",
}


def _resolve_summary_column(source: str) -> str:
    """Validate `source` against the provenance whitelist. Raises ValueError
    on an unknown value rather than silently routing to a default — a typo
    or a new enum case should fail loudly, not corrupt the manual slot."""
    try:
        return _SUMMARY_COLUMN_FOR_SOURCE[source]
    except KeyError:
        raise ValueError(
            f"unknown summary source {source!r}; "
            f"expected one of {sorted(_SUMMARY_COLUMN_FOR_SOURCE)}"
        ) from None


def end_session(
    db_path: Path,
    session_id: str,
    summary: str = "",
    *,
    source: str = "manual",
):
    """Mark a session ended and store its summary.

    `source` routes the summary to the right typed column:
    - 'manual'    → sessions.summary (highest priority at read time)
    - 'llm'       → sessions.summary_llm
    - 'heuristic' → sessions.summary_heuristic
    See effective_summary() for the read rule.
    """
    column = _resolve_summary_column(source)
    # Defense in depth at the SQL construction site: `column` is f-string
    # interpolated below; ensure it's still one of the whitelisted column
    # names even if _resolve_summary_column is changed in the future.
    if column not in _SUMMARY_COLUMN_FOR_SOURCE.values():
        raise ValueError(f"refusing to interpolate unknown column {column!r} into SQL")
    conn = connect(db_path)
    try:
        conn.execute(
            f"""UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, {column} = ?
                WHERE id = ?""",
            (summary or None, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_session_summary(
    db_path: Path,
    session_id: str,
    summary: str,
    *,
    source: str = "manual",
) -> None:
    """Write a summary to the typed column for `source` without touching
    ended_at. Used by `redetect_threads` and any other path that updates a
    session summary in place rather than at end-of-session."""
    column = _resolve_summary_column(source)
    conn = connect(db_path)
    try:
        conn.execute(
            f"UPDATE sessions SET {column} = ? WHERE id = ?",
            (summary or None, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def effective_summary(row) -> str:
    """Pick the best session summary from a sqlite Row or dict.

    Priority: manual > LLM > heuristic. Empty strings are treated as missing,
    so a session with `summary=""` and `summary_llm="..."` returns the LLM one.
    """
    def get(key):
        try:
            v = row[key]
        except (KeyError, IndexError):
            return ""
        return (v or "").strip() if isinstance(v, str) else ""

    return get("summary") or get("summary_llm") or get("summary_heuristic") or ""


def get_session(db_path: Path, session_id: str) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_session(db_path: Path) -> dict | None:
    """Get the most recent session that hasn't ended."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_sessions(db_path: Path, limit: int = 20) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_observation(
    db_path: Path,
    session_id: str,
    tool_name: str,
    content: str,
    tier: str = "hot",
):
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO observations (session_id, tool_name, content, tier)
               VALUES (?, ?, ?, ?)""",
            (session_id, tool_name, content, tier),
        )
        conn.commit()
    finally:
        conn.close()


def get_observations(
    db_path: Path,
    session_id: str,
    limit: int | None = None,
) -> list[dict]:
    conn = connect(db_path)
    try:
        query = """SELECT id, session_id, thread_id, timestamp, tool_name, content, tier
                   FROM observations WHERE session_id = ? ORDER BY timestamp"""
        params: list = [session_id]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def observation_count(db_path: Path, session_id: str) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def save_checkpoint(
    db_path: Path,
    session_id: str,
    trigger: str,
    data: dict,
):
    """Save a pre-compaction checkpoint."""
    import json

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO checkpoints (session_id, trigger, data_json) VALUES (?, ?, ?)",
            (session_id, trigger, json.dumps(data)),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_checkpoint(db_path: Path, session_id: str) -> dict | None:
    """Get the most recent checkpoint for a session."""
    import json

    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT data_json, trigger, created_at FROM checkpoints
               WHERE session_id = ? ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row:
            return {
                "data": json.loads(row["data_json"]),
                "trigger": row["trigger"],
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def get_resume_context(db_path: Path, project_path: str = "", limit: int = 5) -> dict:
    """Build resume context for session start injection.

    Returns active threads, recent knowledge, and identity context relevant
    to the given project.
    """
    conn = connect(db_path)
    try:
        # Active threads with resume points
        if project_path:
            threads = conn.execute(
                """SELECT t.id, t.label, t.summary, t.resume_points, t.updated_at
                   FROM threads t
                   JOIN sessions s ON t.session_id = s.id
                   WHERE t.status = 'active'
                     AND t.resume_points IS NOT NULL
                     AND t.resume_points != ''
                     AND s.project_path = ?
                   ORDER BY t.updated_at DESC LIMIT ?""",
                (project_path, limit),
            ).fetchall()
        else:
            threads = conn.execute(
                """SELECT id, label, summary, resume_points, updated_at
                   FROM threads
                   WHERE status = 'active'
                     AND resume_points IS NOT NULL
                     AND resume_points != ''
                   ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        # Recent threads (last 48h, whether or not they have resume points)
        recent = conn.execute(
            """SELECT id, label, summary, resume_points, status, updated_at
               FROM threads
               WHERE updated_at > datetime('now', '-48 hours')
               ORDER BY updated_at DESC LIMIT ?""",
            (limit * 2,),
        ).fetchall()

        # High-confidence knowledge
        knowledge = conn.execute(
            """SELECT content, type, tags FROM knowledge
               WHERE superseded_by IS NULL AND confidence = 'high'
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        return {
            "active_threads": [dict(t) for t in threads],
            "recent_threads": [dict(r) for r in recent],
            "knowledge": [dict(k) for k in knowledge],
        }
    finally:
        conn.close()


def db_stats(db_path: Path) -> dict:
    """Database statistics for status display."""
    conn = connect(db_path)
    try:
        stats = {}
        stats["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        stats["threads"] = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        stats["observations"] = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        stats["knowledge"] = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        stats["identity"] = conn.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        stats["outbox"] = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

        stats["active_threads"] = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE status = 'active'"
        ).fetchone()[0]

        # Pre-v2 schema used `synced_at IS NULL` to mean "pending"; v4 schema has
        # a real status column with values 'pending' | 'synced' | 'dead'. Querying
        # synced_at IS NULL incorrectly counts dead rows as pending.
        stats["pending_outbox"] = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
        ).fetchone()[0]

        row = conn.execute(
            "SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        stats["last_session"] = row["started_at"] if row else None

        return stats
    finally:
        conn.close()
