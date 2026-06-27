"""Session state management — starting, ending, and querying sessions."""

import re
import uuid
from pathlib import Path

from techne.core.database import connect
from techne.core.redaction import redact_credentials

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
                    conn.execute("UPDATE sessions SET summary = NULL WHERE id = ?", (r["id"],))
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
    summary_llm_source: str | None = None,
):
    """Mark a session ended and store its summary.

    `source` routes the summary to the right typed column:
    - 'manual'    → sessions.summary (highest priority at read time)
    - 'llm'       → sessions.summary_llm
    - 'heuristic' → sessions.summary_heuristic
    See effective_summary() for the read rule.

    `summary_llm_source` records the provenance of an LLM-derived summary
    (e.g. 'ai-title', 'parent-model', 'compaction'). Stored only when
    source='llm'; silently ignored otherwise.
    """
    column = _resolve_summary_column(source)
    # Defense in depth at the SQL construction site: `column` is f-string
    # interpolated below; ensure it's still one of the whitelisted column
    # names even if _resolve_summary_column is changed in the future.
    if column not in _SUMMARY_COLUMN_FOR_SOURCE.values():
        raise ValueError(f"refusing to interpolate unknown column {column!r} into SQL")
    conn = connect(db_path)
    try:
        llm_src = summary_llm_source if source == "llm" else None
        conn.execute(
            f"""UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, {column} = ?,
                    summary_llm_source = COALESCE(?, summary_llm_source)
                WHERE id = ?""",
            (summary or None, llm_src, session_id),
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
    summary_llm_source: str | None = None,
) -> None:
    """Write a summary to the typed column for `source` without touching
    ended_at. Used by `redetect_threads` and any other path that updates a
    session summary in place rather than at end-of-session.

    `summary_llm_source` records provenance ('ai-title', 'parent-model',
    'compaction', …) when source='llm'; silently ignored otherwise.
    """
    column = _resolve_summary_column(source)
    conn = connect(db_path)
    try:
        llm_src = summary_llm_source if source == "llm" else None
        conn.execute(
            f"""UPDATE sessions SET {column} = ?,
                    summary_llm_source = COALESCE(?, summary_llm_source)
                WHERE id = ?""",
            (summary or None, llm_src, session_id),
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


def ensure_session(
    db_path: Path,
    session_id: str,
    *,
    project_path: str = "",
    agent_identity: str = "",
    platform: str = "claude-code",
) -> None:
    """Idempotently ensure a session row exists, stamped with its platform.

    Unlike `start_session` (which INSERTs and raises on a duplicate id), this is
    a no-op when the session already exists. Used by capture paths that receive
    a client-supplied session id and must not assume `run_session_start` ran
    first — notably the `techne_observe` MCP tool, where a non-Claude-Code
    client (opencode, standalone) records observations against its own session
    id. The first observe for a session creates the row and stamps the client's
    `platform`; later observes no-op. (#57: this is where `sessions.platform`
    gains meaning for a second client.)
    """
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO sessions (id, project_path, agent_identity, platform)
               VALUES (?, ?, ?, ?)""",
            (session_id, project_path, agent_identity, platform),
        )
        conn.commit()
    finally:
        conn.close()


def record_observation(
    config,
    session_id: str,
    content: str,
    *,
    tool_name: str = "Observation",
    platform: str | None = None,
    project_path: str = "",
    agent_identity: str = "",
    tier: str = "hot",
    event_at: str | None = None,
) -> dict:
    """Capture one observation: ensure the session, then write it.

    The single platform-agnostic capture path (#57), shared by the
    ``techne_observe`` MCP tool (model-driven capture) and the ``techne observe``
    CLI command (plugin/hook-driven capture, e.g. an opencode TS plugin shelling
    out). Ensures the session row exists — stamped with the client's ``platform``
    — then writes through ``add_observation`` (redaction + tag/kind inference).

    ``platform`` defaults to ``config.platform``. Returns
    ``{"recorded": True, "session_id": ...}``. Raises ValueError on a missing
    session_id or empty content.
    """
    if not session_id:
        raise ValueError("record_observation requires a session_id")
    if not content:
        raise ValueError("record_observation requires content (non-empty)")

    ensure_session(
        config.db_path,
        session_id,
        project_path=project_path,
        agent_identity=agent_identity,
        platform=platform or config.platform,
    )
    add_observation(
        config.db_path,
        session_id=session_id,
        tool_name=tool_name,
        content=content,
        tier=tier,
        config=config,
        event_at=event_at,
    )
    return {"recorded": True, "session_id": session_id}


def add_observation(
    db_path: Path,
    session_id: str,
    tool_name: str,
    content: str,
    tier: str = "hot",
    config=None,
    event_at: str | None = None,
):
    """Record a tool-use observation.

    `event_at` is an optional ISO-8601 timestamp for the real-world event time.
    Leave None for live captures (tool-use → DB write is sub-second, so
    `timestamp` is already a faithful event time). Supply it explicitly for
    ingest paths where the record's subject-event time differs from the write
    time — e.g. importing a transcript dated last year: `timestamp` = now,
    `event_at` = original session date.

    Query layer uses COALESCE(event_at, timestamp) for event-time ordering.
    """
    from techne.core.inference import infer

    redacted = redact_credentials(content)
    result = infer(redacted, config)
    tags_str = " ".join(result.tags) if result.tags else None

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO observations
                   (session_id, tool_name, content, tier, tags, kind, event_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, tool_name, redacted, tier, tags_str, result.kind, event_at),
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
        query = """SELECT id, session_id, thread_id, timestamp, tool_name, content, tier, tags, kind
                   FROM observations WHERE session_id = ? ORDER BY timestamp"""
        params: list = [session_id]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def deed_log(
    db_path: Path,
    *,
    project_path: str | None = None,
    since_days: int | None = None,
    limit: int = 2000,
) -> list[dict]:
    """The raw deed-log: observations ACROSS sessions (techne#107 reconciliation).

    Unlike ``get_observations`` (single session), this windows the whole ledger —
    optionally scoped to a project via ``sessions.project_path`` and to the last
    ``since_days`` — so reconciliation can diff curated memory against everything that
    actually happened. Newest first; capped at ``limit`` (raise it if a window
    legitimately exceeds the cap). Returns observation dicts.
    """
    conn = connect(db_path)
    try:
        parts = ["SELECT o.id, o.session_id, o.timestamp, o.tool_name, o.content",
                 "FROM observations o"]
        wheres: list[str] = []
        params: list = []
        if project_path is not None:
            parts.append("JOIN sessions s ON s.id = o.session_id")
            wheres.append("s.project_path = ?")
            params.append(project_path)
        if since_days is not None:
            # Lexical TEXT compare against datetime('now',…); both UTC for live captures.
            # Imported deeds store ISO ('…T…Z'), so windowing is day-accurate, not
            # hour-accurate, for those — fine for a coarse reconciliation window.
            wheres.append("o.timestamp >= datetime('now', ?)")
            params.append(f"-{int(since_days)} days")
        if wheres:
            parts.append("WHERE " + " AND ".join(wheres))
        parts.append("ORDER BY o.timestamp DESC LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(parts), params).fetchall()
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
    """Save a pre-compaction checkpoint.

    Auto-creates a session row for `session_id` if one doesn't exist yet.
    This handles the case where the pre_compact hook fires with Claude Code's
    own session UUID before session_start has had a chance to register it
    (or when session_start is not configured).
    """
    import json

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id) VALUES (?)",
            (session_id,),
        )
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
               WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
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


def format_resume_context(ctx: dict, project_path: str = "", max_chars: int = 1800) -> str:
    """Render get_resume_context() output as a compact memory block for prompt
    injection — the read-side analogue of the SessionStart banner.

    Used by `techne context` (and thence a client plugin, e.g. opencode's
    experimental.chat.system.transform) to prepend "here's what you were working
    on + durable facts" to the model's system prompt. Returns "" when there is
    nothing worth injecting, so callers can skip cleanly. Capped at *max_chars*.
    """
    active = ctx.get("active_threads") or []
    knowledge = ctx.get("knowledge") or []
    if not active and not knowledge:
        return ""

    scope = f" for {project_path}" if project_path else ""
    body = [f"Relevant memory{scope} from your techne store — prior context for this work:"]

    if active:
        body.append("\nActive threads (what you were working on):")
        for t in active:
            label = (t.get("label") or "thread").strip()
            detail = (t.get("resume_points") or t.get("summary") or "").strip().replace("\n", " ")
            body.append(f"- {label}: {detail[:200]}".rstrip(": ").rstrip())

    if knowledge:
        body.append("\nDurable facts:")
        for k in knowledge:
            content = (k.get("content") or "").strip().replace("\n", " ")
            if content:
                body.append(f"- {content[:200]}")

    inner = "\n".join(body)
    if len(inner) > max_chars:
        inner = inner[:max_chars].rstrip() + " …"
    return f"<techne-memory>\n{inner}\n</techne-memory>"


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
