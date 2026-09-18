"""Import Claude Code JSONL transcripts into the techne database.

Backfills historical sessions that predate the live PostToolUse hook, so the
memory store contains the same shape of observations regardless of capture
mechanism.
"""

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from techne.core.database import connect, normalize_timestamp
from techne.core.redaction import redact_credentials
from techne.hooks.post_tool_use import summarize
from techne.platforms.claude_code.paths import claude_projects_dir, project_slug
from techne.sync.jsonl_records import (
    content_blocks,
    extract_text,
    iter_jsonl_records,
    should_import,
    tool_result_text,
)

if TYPE_CHECKING:
    from techne.config import Config

log = logging.getLogger(__name__)

#: Skip reason for a transcript with no user/assistant records (techne#941). The CLI
#: matches on it to treat the skip as benign, so change both together.
NO_EVENTS_REASON = "no events (metadata-only transcript)"

# Import-time snapshot; honors CLAUDE_CONFIG_DIR set before launch (techne#113).
PROJECTS_DIR = claude_projects_dir()


def project_slug_for_cwd(cwd: Path | None = None) -> str:
    """Encode a cwd into Claude Code's project-dir slug.

    Thin wrapper over the single authoritative ``paths.project_slug`` (techne#167);
    kept for its callers and for the None→cwd default. The earlier local copy
    diverged from the reconciliation path (replaced only ``/`` and skipped realpath),
    so the two could disagree on the same cwd — now they cannot.
    """
    return project_slug(cwd or Path.cwd())


def default_transcripts_dir(cwd: Path | None = None) -> Path:
    """Return ``~/.claude/projects/<slug>/`` for the given (or current) cwd."""
    return PROJECTS_DIR / project_slug_for_cwd(cwd)


# Claude Code writes ``<session_id>.jsonl``; an archived one keeps that name and gains
# ``.gz`` (#621). Both spellings live here rather than in ``sync.jsonl_records`` because
# the ``<session_id>.<ext>`` naming is a Claude Code on-disk convention — that module
# owns record parsing, not path layout.
TRANSCRIPT_SUFFIXES = (".jsonl", ".jsonl.gz")


def session_id_for(path: Path) -> str:
    """Session id from a transcript filename, for either spelling.

    ``Path("abc.jsonl.gz").stem`` is ``"abc.jsonl"``, so plain ``.stem`` silently
    yields a WRONG id for an archived transcript — and that id reaches ``sessions.id``,
    every ``observations.session_id``, and the Librarian's ``source_url``, so the same
    session lands twice under two identities.

    Raises ValueError on a name that is not a transcript. Falling back to ``.stem``
    would reintroduce exactly the bug above, quietly, on the one input that reaches it.
    """
    name = path.name
    for suffix in TRANSCRIPT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    raise ValueError(f"not a transcript filename: {path.name!r}")


def iter_transcript_files(directory: Path) -> list[Path]:
    """Every transcript in `directory`, one per session, ordered by session id.

    Prefers the uncompressed file when a session has both — an archive run that has
    written ``x.jsonl.gz`` but not yet unlinked ``x.jsonl`` must not yield that session
    twice (it would double-import observations, which are append-only).
    """
    by_session: dict[str, Path] = {}
    for suffix in reversed(TRANSCRIPT_SUFFIXES):  # .gz first, so .jsonl overwrites it
        for path in directory.glob(f"*{suffix}"):
            by_session[session_id_for(path)] = path
    return [by_session[sid] for sid in sorted(by_session)]


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
        ts = normalize_timestamp(rec.get("timestamp"))
        if ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        if rec.get("type") == "ai-title":
            title = (rec.get("aiTitle") or "").strip()
            if title:
                ai_title = title  # last one wins (titles within a session are identical anyway)

    if not session_id:
        # NOT .stem — this function's input domain now includes ".jsonl.gz", where
        # .stem leaves the ".jsonl" on and the same session lands twice in
        # `sessions.id` and every `observations.session_id`.
        session_id = session_id_for(jsonl_path)

    # A TRANSCRIPT WITH NO CONVERSATION IS NOT A SESSION TO IMPORT (techne#941).
    # Claude Code writes a metadata-only file when a session is titled and never
    # takes a turn: measured, one line, `{"type":"custom-title",...}`, no timestamp
    # anywhere. `first_ts` stays None, the INSERT below passes an explicit NULL,
    # which skips `sessions.started_at`'s DEFAULT, and the import failed with
    # `NOT NULL constraint failed`. Defaulting the timestamp would record a session
    # that never happened at a time nobody observed; there is nothing to keep, so
    # skip it and say why. The CLI treats this reason as benign.
    if not any(rec.get("type") in ("user", "assistant") for rec in records):
        log.debug("transcript_import: %s has no user/assistant records, skipping", jsonl_path)
        return {"session_id": session_id, "skipped": True, "reason": NO_EVENTS_REASON}

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
                # ended_at_source='derived' (#516): last_ts is the transcript's
                # LAST MESSAGE timestamp — last-activity, the same lower-bound
                # semantics the sweep records as 'derived'. Leaving it NULL would
                # read as 'clean' and make an imported session count as evidence
                # that a producer is dead, which is the false-STALE #516 fixes.
                """INSERT INTO sessions
                   (id, started_at, ended_at, ended_at_source, project_path,
                    platform, summary_llm)
                   VALUES (?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE 'derived' END,
                           ?, 'claude-code', ?)""",
                (session_id, first_ts, last_ts, last_ts, project_path, ai_title or None),
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
                       -- Stamp the source ONLY when this statement is the one
                       -- supplying ended_at. Evaluated against the pre-update
                       -- row, so a session that already ended cleanly keeps its
                       -- 'clean'.
                       ended_at_source = CASE
                           WHEN ended_at IS NULL AND ? IS NOT NULL THEN 'derived'
                           ELSE ended_at_source END,
                       summary_llm = COALESCE(?, summary_llm)
                   WHERE id = ?""",
                (project_path, first_ts, last_ts, last_ts, ai_title or None, session_id),
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

        replayed_skipped = 0

        for rec in records:
            rtype = rec.get("type")
            ts = normalize_timestamp(rec.get("timestamp"))
            blocks = content_blocks(rec)

            # A RESUMED SESSION REPLAYS ITS PARENT'S TURNS (techne#962). The new
            # transcript carries the parent's messages under a NEW sessionId while
            # the records keep their ORIGINAL uuid, and import idempotency is PER
            # SESSION ("already has N observations"), so those turns imported a
            # second time under a second id. Measured here before the fix: 52
            # transcript pairs share message uuids, and of the 2 pairs with both
            # halves imported, 44 observation rows were duplicated.
            #
            # UUID, NOT A CONTENT HASH, AND THAT CHOICE IS LOAD-BEARING. Grip's
            # corpus is six concurrent sessions ("facets") sharing one store, one
            # of which is an aggregator whose job is to read across its siblings.
            # Two facets can legitimately describe the same event from different
            # vantage points, in different transcripts, with DIFFERENT message
            # uuids -- so a content-keyed dedup would collapse a real second
            # observation, while the uuid key cannot: it is exact, it is an
            # identity the transcript already carries, and it is only equal when
            # the record literally IS the same record.
            src_uuid = rec.get("uuid")
            if src_uuid and not force:
                already = conn.execute(
                    "SELECT 1 FROM observations WHERE source_uuid = ? LIMIT 1",
                    (src_uuid,),
                ).fetchone()
                if already:
                    replayed_skipped += 1
                    continue

            if rtype == "assistant":
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tu_id = b.get("id")
                        if not tu_id:
                            # An id-less tool_use can't be matched to its tool_result.
                            # The old `b.get("id", "")` fallback keyed every such block
                            # on "" so they clobbered each other (and a stray id-less
                            # tool_result would pop the wrong one). Skip it. (techne#170)
                            log.debug("transcript_import: skipping id-less tool_use block")
                            continue
                        pending[tu_id] = {
                            "name": b.get("name", ""),
                            "input": b.get("input") or {},
                            "ts": ts,
                        }
                text = extract_text(blocks)
                if text:
                    # Compute hash BEFORE redaction so the audit invariant holds:
                    # content_hash IS NOT NULL iff redaction fired. (techne#270)
                    raw_text = text[:4000]
                    redacted_text = redact_credentials(raw_text)
                    text_hash = (
                        hashlib.sha256(raw_text.encode()).hexdigest()
                        if redacted_text != raw_text
                        else None
                    )
                    conn.execute(
                        """INSERT INTO observations
                           (session_id, timestamp, tool_name, content, tier, event_at,
                            content_hash, source_uuid)
                           VALUES (?, ?, 'AssistantText', ?, 'hot', ?, ?, ?)""",
                        (session_id, ts, redacted_text, ts, text_hash, src_uuid),
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
                        if not should_import(tool_name, tu["input"]):
                            continue
                        output = tool_result_text(b.get("content"))
                        content = summarize(tool_name, tu["input"], output)
                        event_ts = tu["ts"] or ts
                        # Compute hash BEFORE redaction (techne#270).
                        redacted_content = redact_credentials(content)
                        content_hash = (
                            hashlib.sha256(content.encode()).hexdigest()
                            if redacted_content != content
                            else None
                        )
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at,
                                content_hash, source_uuid)
                               VALUES (?, ?, ?, ?, 'hot', ?, ?, ?)""",
                            (
                                session_id,
                                event_ts,
                                tool_name,
                                redacted_content,
                                event_ts,
                                content_hash,
                                src_uuid,
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
                        # Compute hash BEFORE redaction (techne#270).
                        raw_prompt = text[:4000]
                        redacted_prompt = redact_credentials(raw_prompt)
                        prompt_hash = (
                            hashlib.sha256(raw_prompt.encode()).hexdigest()
                            if redacted_prompt != raw_prompt
                            else None
                        )
                        conn.execute(
                            """INSERT INTO observations
                               (session_id, timestamp, tool_name, content, tier, event_at,
                                content_hash, source_uuid)
                               VALUES (?, ?, 'UserPrompt', ?, 'hot', ?, ?, ?)""",
                            (session_id, ts, redacted_prompt, ts, prompt_hash, src_uuid),
                        )
                        prompt_count += 1

        conn.commit()
        return {
            # Surfaced, not silent: a caller seeing 0 new observations needs to know
            # whether the transcript was empty or whether every turn was a replay.
            "replayed_skipped": replayed_skipped,
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
    """Import every transcript in `directory` (.jsonl or archived .jsonl.gz, one per
    session). Returns one result per file."""
    results = []
    for jsonl_path in iter_transcript_files(directory):
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
                    "session_id": session_id_for(jsonl_path),
                    "skipped": True,
                    "reason": f"error: {e}",
                }
            )
    return results


def _session_jsonl(session_id: str) -> Path | None:
    """Locate a session's transcript across all Claude Code project dirs.

    Claude Code names each transcript ``<session_id>.jsonl`` and files it under
    the slug for the session's cwd, so search every slug rather than assume the
    current working directory. An archived ``.jsonl.gz`` resolves too, and the
    uncompressed file wins when both exist (#621).
    """
    for suffix in TRANSCRIPT_SUFFIXES:
        hits = sorted(PROJECTS_DIR.glob(f"*/{session_id}{suffix}"))
        if hits:
            return hits[0]
    return None


def backfill_uncaptured(
    config: "Config", session_id: str, *, jsonl_path: Path | None = None
) -> dict:
    """Backfill the tools the LIVE PostToolUse hook skipped — flag-gated Bash/Read —
    from a session's JSONL into the observation ledger.

    Complementary to live capture (techne#734 topology): the hook records every
    tool EXCEPT Bash (when ``capture_bash`` is off) and Read (when ``capture_read``
    is off). This fills exactly that gap so reflect/recall/threads see shell work,
    WITHOUT the duplication a full ``import_transcript(force=True)`` would cause —
    that would re-insert every already-captured Edit/Write row. Read-only Bash
    navigation noise (``ls``/``grep``/``cd``…) is dropped via ``should_import``'s
    denylist; only gap tools are considered, so nothing collides with live rows.

    Idempotent: a row already present for this session — matched on
    ``(tool_name, stored content, event_at)`` — is skipped, so a second session_end
    (e.g. after a compact) never double-inserts. ``event_at`` comes from the
    transcript, so the key is stable across re-runs yet still admits genuine repeats
    of an identical command at different times.

    Dedup parity note: live-captured rows store ``event_at = NULL`` while backfill
    stores the transcript timestamp, so their keys can never match. That asymmetry is
    only safe because gap tools are NEVER live-captured (we backfill exactly the tools
    the live hook skips). If ``capture_bash`` were toggled ON→OFF mid-session, the
    live Bash rows (``event_at=NULL``) would not dedup against backfill's ts-keyed
    rows and could double — an accepted edge, not a supported flow.

    Best-effort and side-effect-only on the ledger; returns a summary dict:
    ``{session_id, backfilled, skipped_existing, gap_tools, jsonl?, reason?}``.
    """
    # Only tools the live hook skipped are safe to backfill (else we duplicate).
    gap_tools = set()
    if not config.hooks.capture_bash:
        gap_tools.add("Bash")
    if not config.hooks.capture_read:
        gap_tools.add("Read")
    base = {
        "session_id": session_id,
        "backfilled": 0,
        "skipped_existing": 0,
        "gap_tools": sorted(gap_tools),
    }
    if not gap_tools:
        return {**base, "reason": "all gap tools captured live"}

    path = jsonl_path or _session_jsonl(session_id)
    if not path or not path.exists():
        return {**base, "reason": "transcript not found"}

    conn = connect(config.db_path)
    try:
        # Ensure the parent session row exists (observations FK to sessions). It
        # normally does — live capture created it — but a pure-shell session may
        # have none, so make backfill self-sufficient rather than FK-failing.
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, platform) VALUES (?, 'claude-code')",
            (session_id,),
        )
        # Dedup key already present for this session (idempotency across re-runs).
        existing = {
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT tool_name, content, event_at FROM observations WHERE session_id = ?",
                (session_id,),
            )
        }
        pending: dict[str, dict] = {}
        backfilled = 0
        skipped_existing = 0
        for rec in iter_jsonl_records(path):
            rtype = rec.get("type")
            ts = normalize_timestamp(rec.get("timestamp"))
            blocks = content_blocks(rec)

            if rtype == "assistant":
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        pending[b["id"]] = {
                            "name": b.get("name", ""),
                            "input": b.get("input") or {},
                            "ts": ts,
                        }
            elif rtype == "user":
                for b in blocks:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    tu = pending.pop(b.get("tool_use_id", ""), None)
                    if not tu:
                        continue
                    tool_name = tu["name"]
                    if tool_name not in gap_tools:
                        continue
                    if not should_import(tool_name, tu["input"]):
                        continue
                    output = tool_result_text(b.get("content"))
                    content = summarize(tool_name, tu["input"], output)
                    event_ts = tu["ts"] or ts
                    # Compute hash BEFORE redaction (techne#270 invariant).
                    redacted_content = redact_credentials(content)
                    key = (tool_name, redacted_content, event_ts)
                    if key in existing:
                        skipped_existing += 1
                        continue
                    content_hash = (
                        hashlib.sha256(content.encode()).hexdigest()
                        if redacted_content != content
                        else None
                    )
                    conn.execute(
                        """INSERT INTO observations
                           (session_id, timestamp, tool_name, content, tier, event_at, content_hash)
                           VALUES (?, ?, ?, ?, 'hot', ?, ?)""",
                        (session_id, event_ts, tool_name, redacted_content, event_ts, content_hash),
                    )
                    existing.add(key)  # also dedup within this same run
                    backfilled += 1
        conn.commit()
        return {
            **base,
            "backfilled": backfilled,
            "skipped_existing": skipped_existing,
            "jsonl": str(path),
        }
    finally:
        conn.close()
