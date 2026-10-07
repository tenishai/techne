"""Session state management — starting, ending, and querying sessions."""

import hashlib
import re
import uuid
from pathlib import Path

from techne.config import CLAUDE_CODE
from techne.core.database import connect, normalize_timestamp
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
                        "UPDATE sessions SET summary_heuristic = summary,"
                        " summary = NULL WHERE id = ?",
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
    platform: str = CLAUDE_CODE,
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
    summary: str | None = "",
    *,
    source: str = "manual",
    summary_llm_source: str | None = None,
    derive_if_idle_hours: int | None = None,
    mark_ended: bool = True,
):
    """Mark a session ended and store its summary.

    `mark_ended=False` stores the summary and leaves `ended_at` alone. The Stop hook
    passes it: Stop fires at the end of every TURN, so stamping there marked live
    sessions as ended (techne#1187). Only the final pass (SessionEnd, the CLI, a
    sweep of a quiet session) records the end. `summary=None` records the end and
    leaves every summary column as it is.

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
        # "Now" and "clean" are right for the SessionEnd hook, which fires as the
        # session ends (Stop passes mark_ended=False, above). They are wrong for a
        # replay of a session that went quiet long ago (issue #986): `techne session-end` on a session dead for 30 days recorded a
        # clean exit 30 days late. A caller that can be replaying passes
        # `derive_if_idle_hours`; past that silence the end is DERIVED from last
        # activity, the same value and stamp `close_abandoned_sessions` writes.
        ended_sql = "ended_at = CURRENT_TIMESTAMP, ended_at_source = 'clean'"
        ended_args: tuple = ()
        if not mark_ended:
            ended_sql = "ended_at = ended_at"
        elif derive_if_idle_hours is not None:
            row = conn.execute(
                f"""SELECT {_LAST_SEEN_SQL} AS last_seen,
                           replace(substr(s.ended_at, 1, 19), 'T', ' ') AS ended,
                           datetime('now', ?) AS cutoff
                      FROM sessions s
                      LEFT JOIN observations o
                             ON o.session_id = s.id AND {_OBS_TS_IS_DATE}
                     WHERE s.id = ?
                     GROUP BY s.id""",
                (f"-{int(derive_if_idle_hours)} hours", session_id),
            ).fetchone()
            if row is not None and row["last_seen"] < row["cutoff"]:
                if row["ended"] is not None and row["ended"] >= row["last_seen"]:
                    # Already carries an end at or after its last activity (a real
                    # Stop, or an earlier derive). Moving it would replace a true
                    # value with a lower bound, or push it forward to "now".
                    ended_sql, ended_args = "ended_at = ended_at", ()
                else:
                    ended_sql = "ended_at = ?, ended_at_source = 'derived'"
                    ended_args = (row["last_seen"],)
        if summary is None:
            conn.execute(
                f"UPDATE sessions SET {ended_sql} WHERE id = ?", (*ended_args, session_id)
            )
        else:
            conn.execute(
                f"""UPDATE sessions SET {ended_sql}, {column} = ?,
                        summary_llm_source = COALESCE(?, summary_llm_source)
                    WHERE id = ?""",
                (
                    *ended_args,
                    redact_credentials(summary) if summary else None,
                    llm_src,
                    session_id,
                ),
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
            (redact_credentials(summary) if summary else None, llm_src, session_id),
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


# How long a session must be silent before the sweep is willing to call it over.
# Generous on purpose: the cost of waiting is a NULL that resolves an hour later,
# and the cost of being wrong is stamping a live session as ended.
ABANDONED_SESSION_IDLE_HOURS = 6

#: A session's last known activity: its newest date-shaped observation, else its
#: start. Shared by the sweep and `end_session`'s replay path so the two cannot
#: derive different ends for the same row. Needs `sessions s` LEFT JOIN
#: `observations o ON o.session_id = s.id AND {_OBS_TS_IS_DATE}`, grouped by s.id.
_LAST_SEEN_SQL = (
    "COALESCE(MAX(replace(substr(o.timestamp, 1, 19), 'T', ' ')), "
    "replace(substr(s.started_at, 1, 19), 'T', ' '))"
)
_OBS_TS_IS_DATE = "o.timestamp LIKE '____-__-__%'"


def close_abandoned_sessions(
    db_path: Path, idle_hours: int = ABANDONED_SESSION_IDLE_HOURS, now: str | None = None
) -> int:
    """Stamp `ended_at` for sessions that died without running session_end (#516).

    Returns the number of rows updated.

    WHY THIS IS NEEDED AT ALL: `end_session` is the only writer that records a
    CLEAN end (transcript_import also writes `ended_at`, from a transcript's last
    message, and stamps it 'derived'),
    and it runs from `run_session_end`. A session killed, crashed, or dropped by
    the Remote Control timeout (techne#490) never reaches it. The miss rate is
    not static — 52% of June sessions have an `ended_at`, 19% of July, 7% of
    August — so unclean exit is now the common case, and the column was decaying
    toward useless.

    THE DERIVED VALUE IS LAST KNOWN ACTIVITY, not a guess at when the process
    died. That is the honest reading of the evidence we have, and it is the value
    #516 actually wants: the replay index asks "which session was live at T",
    which last-activity answers correctly for every T inside the session.

    SELF-CORRECTING RATHER THAN ONE-SHOT. A row is updated when `ended_at` is
    NULL *or* is EARLIER than known activity, so a session that was swept, then
    resumed, then abandoned again converges instead of keeping a stale stamp. It
    only ever moves the value forward, so re-running is safe and idempotent once
    the session is truly quiet.

    THE IDLE WINDOW IS THE WHOLE SAFETY. Without it this would close the session
    that is currently running, and concurrent peer sessions on the same store
    (which is the normal case here) would close each other. Keying on silence
    rather than on "older than the newest session" is what makes it safe with
    several live sessions.

    Timestamps are normalised in the comparison rather than trusted: `#515`
    established that the ledger carried three formats, and this must be correct
    whether or not that backfill has run.
    """
    conn = connect(db_path)
    try:
        n = close_abandoned_sessions_in_conn(conn, idle_hours=idle_hours, now=now)
        conn.commit()
        return n
    finally:
        conn.close()


def close_abandoned_sessions_in_conn(
    conn, *, idle_hours: int = ABANDONED_SESSION_IDLE_HOURS, now: str | None = None
) -> int:
    """The body of `close_abandoned_sessions`, against a caller's connection.

    Exists so migration step 24 and the live sweep run the SAME statement rather
    than two copies that can drift — the migration cannot open its own connection
    (it is mid-transaction on the caller's) and a second hand-written copy of this
    SQL is exactly the shape that goes stale silently.

    Does NOT commit; the caller owns the transaction.
    """
    cutoff = (
        now
        if now is not None
        else conn.execute("SELECT datetime('now', ?)", (f"-{int(idle_hours)} hours",)).fetchone()[0]
    )
    # A MIGRATION STEP CANNOT ASSUME ITS TABLES EXIST YET. This runs from step 24,
    # and `_migrate` executes BEFORE the reconciler creates tables from SCHEMA_SQL
    # — so on a fixture migrating up from an old version `observations` is
    # genuinely absent and the query raises "no such table". Second instance of
    # this trap in one change (the #515 backfill hit it with an additive COLUMN),
    # which is why it is checked rather than assumed.
    #
    # Returning 0 is correct here, not a silent swallow: no observations table
    # means no activity to derive an end from, and the live sweep at
    # session_start will do the work once the schema is complete.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"sessions", "observations"} <= tables:
        return 0

    # SELECTED FIRST, THEN UPDATED BY ID, rather than one `WITH ... UPDATE`.
    # SQLite does not report a usable rowcount for that form — it comes back -1 —
    # so the count this function returns would have been a driver artefact rather
    # than a fact about the data. Callers (and the tests that assert idempotence)
    # need a real number, and selecting first also makes the target set
    # inspectable when something looks wrong.
    targets = conn.execute(
        f"""
        WITH activity AS (
            SELECT s.id AS sid,
                   {_LAST_SEEN_SQL} AS last_seen
              FROM sessions s
              LEFT JOIN observations o
                     ON o.session_id = s.id
                    -- IGNORE A MALFORMED TIMESTAMP RATHER THAN LET IT WIN THE MAX.
                    -- MAX() takes the lexically greatest value, so one garbage row
                    -- ('not-a-timestamp') beats every real one and then never
                    -- compares < cutoff — dropping the whole session from the
                    -- target set silently, invisible even in the returned count,
                    -- with no future sweep able to recover it. This fails SAFE
                    -- (a NULL, the status quo) but it fails INVISIBLY, so the
                    -- date-shaped rows are the only ones consulted and a session
                    -- with nothing usable falls back to started_at via COALESCE.
                   AND {_OBS_TS_IS_DATE}
             GROUP BY s.id
        )
        SELECT s.id, a.last_seen
          FROM sessions s
          JOIN activity a ON a.sid = s.id
         WHERE a.last_seen < ?
           AND (
                s.ended_at IS NULL
             OR replace(substr(s.ended_at, 1, 19), 'T', ' ') < a.last_seen
           )
        """,
        (cutoff,),
    ).fetchall()

    # Defensive, for the same reason the migration step adds the column: a caller
    # on a partially-reconciled store must degrade, not raise. The stamp is the
    # refinement; the ended_at repair is the point.
    has_source = any(r[1] == "ended_at_source" for r in conn.execute("PRAGMA table_info(sessions)"))
    stamp = ", ended_at_source = 'derived'" if has_source else ""

    for sid, last_seen in targets:
        # STAMP THE PROVENANCE. Deriving ended_at changes what the column MEANS —
        # for a cleanly-ended session it is the true end, for this one it is a
        # LOWER BOUND (last activity), understating duration by however long the
        # process lived after its final observation. Without a discriminator a
        # row cannot say which it holds, and check_outbox_targets would read a
        # crashed session as evidence its producer is dead.
        conn.execute(
            f"UPDATE sessions SET ended_at = ?{stamp} WHERE id = ?",
            (last_seen, sid),
        )
    return len(targets)


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
    platform: str = CLAUDE_CODE,
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

    # Hash the RAW payload BEFORE redaction, but only store it when redaction
    # actually changed the content (techne#270). When nothing was redacted,
    # `content` already IS the raw text, so a hash adds nothing — the stored
    # `content` is already verifiable directly. Storing a hash only when
    # redaction fired gives crisp audit semantics: content_hash IS NOT NULL iff
    # something was stripped, and the hash lets a holder of the original prove
    # the exact pre-redaction payload without re-exposing it.
    content_hash = hashlib.sha256(content.encode()).hexdigest() if redacted != content else None

    result = infer(redacted, config)
    tags_str = " ".join(result.tags) if result.tags else None

    # NORMALISE AT THE PUBLIC BOUNDARY (#515). event_at is caller-supplied, and
    # both surfaces ASK for the shape migration 023 exists to eliminate — the MCP
    # tool schema (mcp_server.py) and the CLI flag both say "ISO-8601 event time".
    # The four transcript-importer sites normalised; this one did not, so a
    # documented API path re-introduced the ISO shape into a column this migration
    # repairs, and _migrate can never re-run to clean it up. Invisible in a census
    # of existing rows because event_at is usually NULL on live capture.
    event_at = normalize_timestamp(event_at)

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO observations
                   (session_id, tool_name, content, tier, tags, kind, event_at, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, tool_name, redacted, tier, tags_str, result.kind, event_at, content_hash),
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
        parts = [
            "SELECT o.id, o.session_id, o.timestamp, o.tool_name, o.content",
            "FROM observations o",
        ]
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

    # Redact every free-text value in the checkpoint blob before it is
    # serialized into data_json. The pre_compact hook packs several raw
    # transcript-derived fields here — recent_commands (raw bash argv) AND
    # last_task (the last substantive user message, verbatim). Users paste
    # secrets into chat as readily as into argv, so we redact at the
    # serialization boundary rather than field-by-field: any string value
    # (or string item in a list) is filtered, so a future field can't reopen
    # the hole. Non-string / structured values (files_modified paths,
    # tools_used counts, git_branch) are left as-is — they don't match the
    # anchored patterns anyway. A shallow copy avoids mutating the caller's dict.
    def _redact_value(v):
        if isinstance(v, str):
            return redact_credentials(v)
        if isinstance(v, list):
            return [redact_credentials(x) if isinstance(x, str) else x for x in v]
        return v

    data = {k: _redact_value(v) for k, v in data.items()}

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


def platform_breakdown(db_path: Path) -> dict[str, dict]:
    """Platform breakdown for status display.

    Returns ``{platform: {"sessions": int, "observations": int}}``.
    Sessions and observations grouped by ``sessions.platform``; NULL/''
    coalesced to 'unknown'. Sorted by session count descending.
    """
    conn = connect(db_path)
    try:
        sess_rows = conn.execute(
            """SELECT COALESCE(NULLIF(platform, ''), 'unknown') AS plat, COUNT(*) AS cnt
               FROM sessions
               GROUP BY plat
               ORDER BY cnt DESC"""
        ).fetchall()
        obs_rows = conn.execute(
            """SELECT COALESCE(NULLIF(s.platform, ''), 'unknown') AS plat, COUNT(*) AS cnt
               FROM observations o
               JOIN sessions s ON o.session_id = s.id
               GROUP BY plat
               ORDER BY cnt DESC"""
        ).fetchall()
        obs_map = {r["plat"]: r["cnt"] for r in obs_rows}
        result: dict[str, dict] = {}
        for r in sess_rows:
            plat = r["plat"]
            result[plat] = {"sessions": r["cnt"], "observations": obs_map.get(plat, 0)}
        return result
    finally:
        conn.close()


def get_resume_context(db_path: Path, project_path: str = "", limit: int = 5) -> dict:
    """Build resume context for session start injection.

    Returns active threads, recent knowledge, and identity context relevant
    to the given project.
    """
    conn = connect(db_path)
    try:
        # Active threads with resume points.
        #
        # Cross-platform by deliberate design (techne#81): projects span agents/clients;
        # shared memory is the point. Provenance (platform + agent_identity) is selected
        # and surfaced so a future per-platform scoping decision is data-supported without
        # a migration. We deliberately do NOT filter by platform.
        if project_path:
            threads = conn.execute(
                """SELECT t.id, t.label, t.summary, t.resume_points, t.updated_at,
                          s.platform AS platform, s.agent_identity AS agent_identity
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
                """SELECT t.id, t.label, t.summary, t.resume_points, t.updated_at,
                          s.platform AS platform, s.agent_identity AS agent_identity
                   FROM threads t
                   JOIN sessions s ON t.session_id = s.id
                   WHERE t.status = 'active'
                     AND t.resume_points IS NOT NULL
                     AND t.resume_points != ''
                   ORDER BY t.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        # Recent threads (last 48h, whether or not they have resume points).
        # Joined to sessions so platform + agent_identity provenance is available.
        recent = conn.execute(
            """SELECT t.id, t.label, t.summary, t.resume_points, t.status, t.updated_at,
                      s.platform AS platform, s.agent_identity AS agent_identity
               FROM threads t
               JOIN sessions s ON t.session_id = s.id
               WHERE t.updated_at > datetime('now', '-48 hours')
               ORDER BY t.updated_at DESC LIMIT ?""",
            (limit * 2,),
        ).fetchall()

        # High-confidence knowledge. `status != 'archived'` (techne#1033 review
        # round 2): a soft-archived row (techne#216) is out of the default working
        # set by design — without this it renders under "Durable facts:" in
        # format_resume_context(), so a fact `delete()` retired (predecessor of a
        # deleted chain head, archived rather than resurrected) came back as
        # established the moment this ran.
        knowledge = conn.execute(
            """SELECT content, type, tags FROM knowledge
               WHERE superseded_by IS NULL AND confidence = 'high' AND status != 'archived'
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


def provenance_tag(row: dict) -> str:
    """Terse provenance tag for a thread/session row (techne#81).

    ``[platform]`` (e.g. ``[opencode]``), or ``[platform:agent_identity]`` when an
    agent identity is present. Empty/None platform coalesces to ``unknown``. This is
    surfaced at injection so cross-platform threads are visibly attributed — it never
    filters which threads are shown (projects span agents by design).
    """
    plat = (row.get("platform") or "").strip() or "unknown"
    agent = (row.get("agent_identity") or "").strip()
    return f"[{plat}:{agent}]" if agent else f"[{plat}]"


def format_resume_context(ctx: dict, project_path: str = "", max_chars: int = 1800) -> str:
    """Render get_resume_context() output as a compact memory block for prompt
    injection — the read-side analogue of the SessionStart banner.

    Used by `techne context` (and thence a client plugin, e.g. opencode's
    experimental.chat.system.transform) to prepend "here's what you were working
    on + durable facts" to the model's system prompt. Returns "" when there is
    nothing worth injecting, so callers can skip cleanly. Capped at *max_chars*.

    Trust boundary (techne#83-8): stored content (labels, summaries, knowledge)
    is operator-trusted but concatenated verbatim into the model context. A
    literal ``</techne-memory>`` in that content would prematurely close the
    wrapper tag, so it is neutralised before wrapping (zero-width space inserted
    after ``<``). The tag boundary is structural, not a security boundary, but
    keeping the block well-formed avoids confusing the model.
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
            tag = " " + provenance_tag(t)
            body.append((f"- {label}: {detail[:200]}".rstrip(": ").rstrip()) + tag)

    if knowledge:
        body.append("\nDurable facts:")
        for k in knowledge:
            content = (k.get("content") or "").strip().replace("\n", " ")
            if content:
                body.append(f"- {content[:200]}")

    inner = "\n".join(body)
    if len(inner) > max_chars:
        inner = inner[:max_chars].rstrip() + " …"
    # Neutralise any literal closing tag so injected content cannot prematurely
    # end the <techne-memory> block (zero-width space after "<").
    inner = inner.replace("</techne-memory>", "<​/techne-memory>")
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
        # Per-category knowledge counts (techne#334) — lets the banner surface
        # fill-state by type so the agent can self-regulate what it writes,
        # instead of an opaque store-level total.
        stats["knowledge_by_type"] = {
            row[0]: row[1]
            for row in conn.execute("SELECT type, COUNT(*) FROM knowledge GROUP BY type").fetchall()
        }
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
