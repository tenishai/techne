"""Import Claude Code JSONL transcripts into the techne database.

Backfills historical sessions that predate the live PostToolUse hook, so the
memory store contains the same shape of observations regardless of capture
mechanism.
"""

from pathlib import Path, PurePosixPath

from techne.core.database import connect
from techne.core.redaction import redact_credentials
from techne.hooks.post_tool_use import summarize
from techne.platforms.claude_code.paths import claude_projects_dir
from techne.sync.jsonl_records import (
    content_blocks,
    extract_text,
    iter_jsonl_records,
    should_import,
    tool_result_text,
)

# Import-time snapshot; honors CLAUDE_CONFIG_DIR set before launch (techne#113).
PROJECTS_DIR = claude_projects_dir()


def project_slug_for_cwd(cwd: Path | None = None) -> str:
    """Encode a POSIX cwd into Claude Code's project-dir slug.

    Claude Code stores transcripts under ``~/.claude/projects/<slug>/`` where the
    slug is the absolute path with ``/`` → ``-`` and a leading ``-``. Works the
    same on macOS and Linux. (Claude Code on Windows uses a different convention
    and is out of scope.)
    """
    # Do NOT call .resolve() — it dereferences symlinks/firmlinks (macOS APFS
    # turns /home into /System/Volumes/Data/home), producing a slug that does
    # not match the cwd Claude Code recorded. Path.cwd() is already absolute.
    cwd = (cwd or Path.cwd()).absolute()
    posix = PurePosixPath(cwd.as_posix())
    return "-" + str(posix).lstrip("/").replace("/", "-")


def default_transcripts_dir(cwd: Path | None = None) -> Path:
    """Return ``~/.claude/projects/<slug>/`` for the given (or current) cwd."""
    return PROJECTS_DIR / project_slug_for_cwd(cwd)


def import_transcript(
    db_path: Path,
    jsonl_path: Path,
    *,
    include_user_prompts: bool = True,
    force: bool = False,
) -> dict:
    """Import a single JSONL transcript. Idempotent — if the session already
    has observations, returns {"skipped": True, ...} unless `force=True`.

    The gate ("session has any observations" → skip) intentionally conflates
    live-captured and previously-imported observations. A session with a
    handful of PostToolUse-captured observations will block its own full
    historical import; pass `force=True` to override and insert the full
    transcript on top. Observations are append-only here, so re-running with
    `force` produces duplicates rather than updates — only use when you know
    the live capture was partial.

    Returns: {session_id, project_path, tool_obs, prompt_obs, summary_set, skipped}.
    """
    records = list(iter_jsonl_records(jsonl_path))
    if not records:
        return {"session_id": None, "skipped": True, "reason": "empty"}

    session_id = None
    project_path = ""
    first_ts = None
    last_ts = None
    ai_title = ""
    for rec in records:
        sid = rec.get("sessionId")
        if sid and not session_id:
            session_id = sid
        if not project_path:
            cwd = rec.get("cwd")
            if cwd:
                project_path = cwd
        ts = rec.get("timestamp")
        if ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        if rec.get("type") == "ai-title":
            title = (rec.get("aiTitle") or "").strip()
            if title:
                ai_title = title  # last one wins (titles within a session are identical anyway)

    if not session_id:
        session_id = jsonl_path.stem

    conn = connect(db_path)
    try:
        # UPSERT session metadata FIRST — runs on every import, including re-runs
        # that will skip observation insertion. This lets us backfill new
        # session-level fields (ai-title summary, etc.) onto sessions that were
        # imported before those fields were captured.
        existing_session = conn.execute(
            "SELECT id, summary_llm FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        summary_set = False
        if not existing_session:
            conn.execute(
                """INSERT INTO sessions
                   (id, started_at, ended_at, project_path, platform, summary_llm)
                   VALUES (?, ?, ?, ?, 'claude-code', ?)""",
                (session_id, first_ts, last_ts, project_path, ai_title or None),
            )
            summary_set = bool(ai_title)
        else:
            # ai-title is stable within a session (we saw it re-emitted with the
            # same value 89 times). Always overwrite summary_llm — if the model
            # ever did change the title, the latest one is the right one.
            conn.execute(
                """UPDATE sessions
                   SET project_path = COALESCE(NULLIF(project_path, ''), ?),
                       started_at = COALESCE(started_at, ?),
                       ended_at = COALESCE(ended_at, ?),
                       summary_llm = COALESCE(?, summary_llm)
                   WHERE id = ?""",
                (project_path, first_ts, last_ts, ai_title or None, session_id),
            )
            prior_llm = (existing_session["summary_llm"] or "").strip()
            summary_set = bool(ai_title) and ai_title != prior_llm

        existing_obs = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if existing_obs > 0 and not force:
            conn.commit()
            return {
                "session_id": session_id,
                "project_path": project_path,
                "tool_obs": 0,
                "prompt_obs": 0,
                "summary_set": summary_set,
                "skipped": True,
                "reason": f"already has {existing_obs} observations",
            }

        pending: dict[str, dict] = {}
        tool_count = 0
        prompt_count = 0
        assistant_text_count = 0

        for rec in records:
            rtype = rec.get("type")
            ts = rec.get("timestamp")
            blocks = content_blocks(rec)

            if rtype == "assistant":
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        pending[b.get("id", "")] = {
                            "name": b.get("name", ""),
                            "input": b.get("input") or {},
                            "ts": ts,
                        }
                text = extract_text(blocks)
                if text:
                    conn.execute(
                        """INSERT INTO observations
                           (session_id, timestamp, tool_name, content, tier, event_at)
                           VALUES (?, ?, 'AssistantText', ?, 'hot', ?)""",
                        (session_id, ts, redact_credentials(text[:4000]), ts),
                    )
                    assistant_text_count += 1

            elif rtype == "user":
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        tu = pending.pop(b.get("tool_use_id", ""), None)
                        if not tu:
                            continue
                        tool_name = tu["name"]
                        if not should_import(tool_name):
                            continue
                        output = tool_result_text(b.get("content"))
                        content = summarize(tool_name, tu["input"], output)
                        event_ts = tu["ts"] or ts
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at)
                               VALUES (?, ?, ?, ?, 'hot', ?)""",
                            (
                                session_id,
                                event_ts,
                                tool_name,
                                redact_credentials(content),
                                event_ts,
                            ),
                        )
                        tool_count += 1

                # Capture human text on the same record as a tool_result too —
                # Claude Code emits both blocks together when a tool is
                # cancelled and the user follows up. Suppressing the prompt
                # whenever `paired_any` is True silently drops that text.
                if include_user_prompts:
                    text = extract_text(blocks)
                    if text and not text.startswith("<"):
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at)
                               VALUES (?, ?, 'UserPrompt', ?, 'hot', ?)""",
                            (session_id, ts, redact_credentials(text[:4000]), ts),
                        )
                        prompt_count += 1

        conn.commit()
        return {
            "session_id": session_id,
            "project_path": project_path,
            "tool_obs": tool_count,
            "prompt_obs": prompt_count,
            "assistant_text_obs": assistant_text_count,
            "summary_set": summary_set,
            "skipped": False,
        }
    finally:
        conn.close()


def import_directory(
    db_path: Path,
    directory: Path,
    *,
    include_user_prompts: bool = True,
    force: bool = False,
) -> list[dict]:
    """Import every *.jsonl file in `directory`. Returns one result per file."""
    results = []
    for jsonl_path in sorted(directory.glob("*.jsonl")):
        try:
            results.append(
                import_transcript(
                    db_path,
                    jsonl_path,
                    include_user_prompts=include_user_prompts,
                    force=force,
                )
            )
        except Exception as e:
            results.append(
                {
                    "session_id": jsonl_path.stem,
                    "skipped": True,
                    "reason": f"error: {e}",
                }
            )
    return results
