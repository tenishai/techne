"""End-of-session reflection.

For each freshly detected thread, ask an LLM whether anything in it is worth
keeping across sessions (durable facts, decisions, patterns about the project;
durable identity claims about the operator). Proposals are written to
`knowledge_candidates` for human review; while config.reflect.write_to is
'candidates' the live knowledge / identity tables are never touched.

Designed as a 2-week opt-in experiment — see config.ReflectConfig. The hook
contract still applies: any failure here exits 0 with stderr noise; thread
detection and session teardown must complete regardless.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from techne.config import Config
from techne.core.database import connect
from techne.core.knowledge import VALID_TYPES as KNOWLEDGE_TYPES
from techne.core.redaction import redact_credentials
from techne.core.threads import Thread, _make_generate_fn

MAX_OBS_FOR_PROMPT = 40
MAX_OBS_CONTENT_CHARS = 200

PROMPT_TEMPLATE = """You are reviewing a thread of work an AI agent just finished.
Decide what (if anything) should be saved for the agent's future sessions.

# Thread label
{label}

# Thread summary
{summary}

# Observations (truncated; ordered by time)
{observations}

# What the agent already knows (do NOT restate any of these)
{existing}

# Your task

Return ONLY a JSON array. Each item:

  {{
    "scope":      "knowledge" | "identity",
    "type":       <see types below>,
    "content":   "<one sentence, durable, evidence-anchored>",
    "tags":      "<comma-separated short tags>",
    "confidence": "low" | "medium" | "high"
  }}

For scope=knowledge, type must be one of: {knowledge_types}.
For scope=identity, type is a dotted key path (e.g. "user.prefers.terse_responses").

Be strict. Return [] when nothing in this thread is worth keeping. Skip:
  - Anything implementation-ephemeral (a one-off bug fix, a now-deleted file).
  - Anything already in "What the agent already knows".
  - Anything that would be obvious from re-reading the code.
Prefer fewer, sharper items over many vague ones. Hard cap: {max_items} items.

Output the JSON array and nothing else.
"""


@dataclass
class Candidate:
    scope: str  # 'knowledge' | 'identity'
    type: str
    content: str
    tags: str
    confidence: str


def _render_observations(observations: list[dict]) -> str:
    """Render a thread's observations into a compact, prompt-safe block."""
    if not observations:
        return "(no observations)"
    chunk = observations[:MAX_OBS_FOR_PROMPT]
    lines = []
    for o in chunk:
        tool = o.get("tool_name") or "?"
        content = (o.get("content") or "").replace("\n", " ")
        if len(content) > MAX_OBS_CONTENT_CHARS:
            content = content[:MAX_OBS_CONTENT_CHARS] + "…"
        lines.append(f"- [{tool}] {content}")
    if len(observations) > MAX_OBS_FOR_PROMPT:
        lines.append(f"… ({len(observations) - MAX_OBS_FOR_PROMPT} more elided)")
    return "\n".join(lines)


def _existing_knowledge_block(db_path: Path, query: str, k: int) -> str:
    """Top-K existing knowledge entries relevant to the thread, for dedup."""
    if k <= 0:
        return "(dedup disabled)"
    try:
        from techne.core.search import search_fts

        hits = search_fts(db_path, query, table="knowledge", limit=k)
    except Exception:
        return "(no existing knowledge available)"
    if not hits:
        return "(none yet)"
    lines = []
    for h in hits:
        content = (h.get("content") or "")[:200]
        lines.append(f"- {content}")
    return "\n".join(lines)


def parse_response(raw: str, max_items: int) -> list[Candidate]:
    """Parse the LLM's JSON array into validated Candidate objects.

    Tolerant of common LLM noise: leading/trailing prose, fenced code blocks.
    Returns [] on any parse failure (caller decides whether to surface that).
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # drop the first fence line and any trailing fence
        text = "\n".join(line for line in lines[1:] if not line.startswith("```")).strip()
    # Find the first '[' and matching ']' — handles trailing prose
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    out: list[Candidate] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        if scope not in ("knowledge", "identity"):
            continue
        type_ = item.get("type")
        content = item.get("content")
        if not isinstance(type_, str) or not type_:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if scope == "knowledge" and type_ not in KNOWLEDGE_TYPES:
            continue
        confidence = item.get("confidence", "medium")
        if confidence not in ("low", "medium", "high"):
            confidence = "medium"
        tags = item.get("tags") or ""
        if not isinstance(tags, str):
            tags = ""
        out.append(
            Candidate(
                scope=scope,
                type=type_,
                content=content.strip(),
                tags=tags.strip(),
                confidence=confidence,
            )
        )
    return out


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _meets_min_confidence(candidate: Candidate, min_confidence: str) -> bool:
    return _CONFIDENCE_RANK[candidate.confidence] >= _CONFIDENCE_RANK[min_confidence]


def _build_prompt(
    thread: Thread,
    observations: list[dict],
    db_path: Path,
    config: Config,
) -> str:
    existing_block = "(dedup disabled)"
    if config.reflect.include_existing_knowledge:
        existing_block = _existing_knowledge_block(
            db_path,
            f"{thread.label} {thread.summary[:200]}",
            config.reflect.existing_knowledge_k,
        )
    return PROMPT_TEMPLATE.format(
        label=thread.label,
        summary=thread.summary[:1000],
        observations=_render_observations(observations),
        existing=existing_block,
        knowledge_types=", ".join(KNOWLEDGE_TYPES),
        max_items=config.reflect.max_candidates_per_thread,
    )


def _insert_candidate(
    db_path: Path,
    candidate: Candidate,
    *,
    session_id: str,
    thread_id: str,
    model_used: str,
    prompt_hash: str,
    raw_response: str | None,
) -> int:
    conn = connect(db_path)
    try:
        # FK safety net (techne#106): never let a stale/missing thread id drop a
        # candidate — fall back to a NULL FK (loses provenance) rather than raising.
        if thread_id is not None and not conn.execute(
            "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
        ).fetchone():
            thread_id = None
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, tags, confidence,
                  source_session_id, source_thread_id,
                  model_used, prompt_hash, raw_response)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.scope,
                candidate.type,
                redact_credentials(candidate.content),
                candidate.tags,
                candidate.confidence,
                session_id,
                thread_id,
                model_used,
                prompt_hash,
                redact_credentials(raw_response) if raw_response else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_live(db_path: Path, candidate: Candidate, source: str) -> int | None:
    """Write straight to the live knowledge / identity tables. Used only when
    config.reflect.write_to='live' — intended for after the test concludes."""
    if candidate.scope == "knowledge":
        from techne.core.knowledge import store

        return store(
            db_path,
            candidate.content,
            type=candidate.type,
            tags=candidate.tags,
            confidence=candidate.confidence,
            source=source,
        )
    if candidate.scope == "identity":
        from techne.core.identity import set_value

        set_value(db_path, candidate.type, candidate.content, source=source)
        return None
    return None


def _bucket_observations(threads: list[Thread], observations: list[dict]) -> dict[int, list[dict]]:
    """Map each thread_num to its observation dicts (resolved from indices)."""
    out: dict[int, list[dict]] = {}
    for t in threads:
        out[t.thread_num] = [
            observations[i] for i in t.observation_indices if 0 <= i < len(observations)
        ]
    return out


def load_session_for_reflection(config: Config, session_id: str) -> tuple[list[Thread], list[dict]]:
    """Reconstruct (threads, observations) for a session the same way
    run_session_end does — heuristic detection, no LLM. Lets the agent-assisted
    emit/ingest path operate on any past session by id.
    """
    from techne.core.state import get_observations
    from techne.core.thread_orchestrator import detect_summarize_with_metadata

    observations = get_observations(config.db_path, session_id)
    threads, _scores = detect_summarize_with_metadata(observations, config)
    return threads, observations


def emit_reflection_batch(
    config: Config,
    session_id: str,
    threads: list[Thread],
    observations: list[dict],
) -> list[dict]:
    """Build the per-thread reflection prompts WITHOUT calling an LLM.

    The agent-assisted (no-API-key) half of the reflect pass: techne renders the
    prompts here, an in-session agent (running on the operator's OAuth model)
    produces the JSON candidate arrays, and ingest_reflection_response feeds them
    back. Thread selection mirrors reflect_on_session exactly (same
    min_observations gate), so emit→agent→ingest is equivalent to the automated
    pass with the LLM step relocated into the agent loop.

    Returns [{thread_id, session_id, thread_num, prompt_hash, prompt}].
    """
    from techne.core.thread_orchestrator import persisted_thread_id

    obs_by_thread = _bucket_observations(threads, observations)
    batch: list[dict] = []
    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue
        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        batch.append(
            {
                # The real persisted FK id — resolved against the threads table, not
                # recomputed (techne#106: a re-detected thread hashes differently than
                # the saved row on legacy sessions). None → candidate gets a NULL FK.
                "thread_id": persisted_thread_id(config.db_path, session_id, t),
                "session_id": session_id,
                "thread_num": t.thread_num,
                "prompt_hash": prompt_hash,
                "prompt": prompt,
            }
        )
    return batch


def ingest_reflection_response(
    config: Config,
    session_id: str,
    thread_id: str,
    prompt_hash: str,
    raw_response: str,
    *,
    model_used: str = "agent-assisted",
) -> dict:
    """Process one agent-produced raw response (the JSON array parse_response
    expects) into knowledge_candidates — and live knowledge when
    config.reflect.write_to == 'live'. The single insertion path shared with
    reflect_on_session. `thread_id` is the real threads.id (from thread_row_id /
    the emit batch), so the candidates FK resolves. Returns per-thread counts.
    """
    counts = {
        "candidates_proposed": 0,
        "candidates_written": 0,
        "candidates_promoted": 0,
        "errors": 0,
    }
    candidates = parse_response(raw_response, config.reflect.max_candidates_per_thread)
    kept = [c for c in candidates if _meets_min_confidence(c, config.reflect.min_confidence)]
    counts["candidates_proposed"] += len(kept)

    raw_for_store = raw_response if config.reflect.log_raw_response else None
    for c in kept:
        try:
            cand_id = _insert_candidate(
                config.db_path,
                c,
                session_id=session_id,
                thread_id=thread_id,
                model_used=model_used,
                prompt_hash=prompt_hash,
                raw_response=raw_for_store,
            )
            counts["candidates_written"] += 1
            if config.reflect.write_to == "live":
                promoted_id = _write_live(config.db_path, c, source=f"reflect:{thread_id}")
                _mark_promoted(config.db_path, cand_id, promoted_id)
                counts["candidates_promoted"] += 1
        except Exception:
            counts["errors"] += 1
    return counts


def reflect_on_session(
    config: Config,
    session_id: str,
    threads: list[Thread],
    observations: list[dict],
    *,
    generate_fn=None,
) -> dict:
    """Run reflection on every qualifying thread in the just-ended session.

    Returns {threads_considered, threads_reflected, candidates_proposed,
             candidates_written, candidates_promoted, errors}.
    Never raises — failures are counted in `errors`. The caller (run_session_end)
    must still treat session teardown as successful.

    `generate_fn` is an injectable LLM callable for tests; in prod it's built
    from config.reflect.model (falling back to config.model).
    """
    result = {
        "threads_considered": len(threads),
        "threads_reflected": 0,
        "candidates_proposed": 0,
        "candidates_written": 0,
        "candidates_promoted": 0,
        "errors": 0,
    }
    if not config.reflect.enabled:
        return result

    # One LLM client for the whole pass, with optional override of the
    # session-wide model (so reflection can run on a cheaper model than
    # whatever the thread-summarizer uses).
    reflect_config = config
    if config.reflect.model:
        # Shallow override of config.model for the generate fn only
        import copy

        reflect_config = copy.copy(config)
        reflect_config.model = config.reflect.model
    if generate_fn is None:
        try:
            generate_fn = _make_generate_fn(reflect_config)
        except Exception:
            result["errors"] += 1
            return result

    from techne.core.thread_orchestrator import thread_row_id

    obs_by_thread = _bucket_observations(threads, observations)

    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue

        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

        try:
            raw = generate_fn(prompt)
        except Exception:
            result["errors"] += 1
            continue
        result["threads_reflected"] += 1

        # Same insertion path as the agent-assisted ingest — one code path. Use
        # the real threads.id (thread_row_id), or the candidates FK fails for
        # heuristic-detected threads (the silent-error bug behind starved #92).
        thread_id = thread_row_id(session_id, t)
        counts = ingest_reflection_response(
            config, session_id, thread_id, prompt_hash, raw, model_used=reflect_config.model
        )
        for key, val in counts.items():
            result[key] += val

    return result


def _mark_promoted(db_path: Path, candidate_id: int, promoted_to_id: int | None) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted',
                      promoted_to_id = ?,
                      reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (promoted_to_id, candidate_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Read-side helpers used by the CLI -----------------------------------


def list_candidates(
    db_path: Path,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    if status not in ("pending", "promoted", "discarded", "all"):
        raise ValueError(f"Invalid status '{status}'")
    conn = connect(db_path)
    try:
        if status == "all":
            rows = conn.execute(
                """SELECT * FROM knowledge_candidates
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM knowledge_candidates
                    WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def promote_candidate(db_path: Path, candidate_id: int) -> int | None:
    """Promote a candidate to live knowledge / identity. Returns the new
    knowledge.id (or None for identity entries). Raises if already reviewed.

    Atomic: a single connection performs the status guard (UPDATE WHERE
    status='pending'), the live-table INSERT, and the promoted_to_id update
    inside one transaction.  Concurrent callers that both see 'pending' will
    race on the UPDATE; the loser gets rowcount==0 and raises immediately
    without writing a duplicate knowledge row.
    """
    from techne.core.knowledge import CONFIDENCE_NUMERIC, VALID_CONFIDENCE, VALID_TYPES

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Candidate {candidate_id} not found")

        # Atomic guard: only one concurrent caller can flip status from pending.
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted', reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'""",
            (candidate_id,),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Candidate {candidate_id} already {row['status']} — cannot re-promote"
            )

        # Write to the live table using the same connection so a failure
        # here rolls back the status update too.
        scope = row["scope"]
        source = f"reflect-promote:{row['source_session_id']}:{candidate_id}"
        promoted_to_id: int | None = None

        if scope == "knowledge":
            content = redact_credentials(row["content"])
            conf = row["confidence"] if row["confidence"] in VALID_CONFIDENCE else "medium"
            numeric = CONFIDENCE_NUMERIC[conf]
            ktype = row["type"] if row["type"] in VALID_TYPES else "fact"
            tags = row["tags"] or ""
            cur2 = conn.execute(
                """INSERT INTO knowledge
                     (type, content, source, tags, confidence, confidence_numeric)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ktype, content, source, tags, conf, numeric),
            )
            promoted_to_id = cur2.lastrowid
        elif scope == "identity":
            from techne.core.identity import set_value

            # Use the SAME connection — a second connection would deadlock against
            # this one's open write (the candidate UPDATE above). Commit happens
            # below, so an exception still rolls back the status update.
            set_value(db_path, row["type"], row["content"], source=source, conn=conn)
            promoted_to_id = None

        # Update promoted_to_id now that we know the new knowledge row id.
        conn.execute(
            "UPDATE knowledge_candidates SET promoted_to_id = ? WHERE id = ?",
            (promoted_to_id, candidate_id),
        )
        conn.commit()
        return promoted_to_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def discard_candidate(db_path: Path, candidate_id: int) -> None:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'discarded', reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'""",
            (candidate_id,),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Candidate {candidate_id} not found or already reviewed")
        conn.commit()
    finally:
        conn.close()


def candidate_stats(db_path: Path) -> dict:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS n
                 FROM knowledge_candidates GROUP BY status"""
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        scope_rows = conn.execute(
            """SELECT scope, status, COUNT(*) AS n
                 FROM knowledge_candidates GROUP BY scope, status"""
        ).fetchall()
        by_scope = {(r["scope"], r["status"]): r["n"] for r in scope_rows}
        return {
            "pending": counts.get("pending", 0),
            "promoted": counts.get("promoted", 0),
            "discarded": counts.get("discarded", 0),
            "by_scope": {f"{s[0]}.{s[1]}": n for s, n in by_scope.items()},
        }
    finally:
        conn.close()
