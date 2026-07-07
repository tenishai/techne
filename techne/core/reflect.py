"""End-of-session reflection.

For each freshly detected thread, ask an LLM whether anything in it is worth
keeping across sessions (durable facts, decisions, procedures about the project;
durable identity claims about the operator). Proposals are written to
`knowledge_candidates` for human review; while config.reflect.write_to is
'candidates' the live knowledge / identity tables are never touched.

Opt-in (off by default; enable via reflect.enabled) — see config.ReflectConfig.
The hook contract still applies: any failure here exits 0 with stderr noise;
thread detection and session teardown must complete regardless.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from techne.config import Config

log = logging.getLogger(__name__)
from techne.core._resident import extract_json_array
from techne.core._resident import prompt_hash as _prompt_hash
from techne.core.database import connect
from techne.core.knowledge import VALID_TYPES as KNOWLEDGE_TYPES
from techne.core.redaction import redact_credentials
from techne.core.threads import Thread, _make_generate_fn

MAX_OBS_FOR_PROMPT = 40
MAX_OBS_CONTENT_CHARS = 200

# Actions that require a faithfulness check before promotion (techne#252, 2026-07-06).
# Must stay in sync with faithfulness_emit's SQL filter (action IN ('merge','supersede')).
_FAITHFULNESS_GATED_ACTIONS = frozenset({"merge", "supersede"})


class FaithfulnessGateError(ValueError):
    """Raised by promote_candidate when an explicitly-unfaithful merge or supersede
    candidate is promoted without ``force=True``.

    Subclasses ValueError so existing ``except ValueError`` callers (e.g. the CLI)
    continue to work without change. The message cites the stored
    ``faithfulness_warning`` so the operator knows exactly what the concern was.
    """

PROMPT_TEMPLATE = """You are reviewing a thread of work an AI agent just finished.
Decide what (if anything) should be saved for the agent's future sessions.

# Thread label
{label}

# Thread summary
{summary}

# Observations (truncated; ordered by time)
# Each line is `- [tool|kind] text`; `kind` is an inferred shape. Weight
# `decision-shaped` and `fact-shaped` observations most — they most often carry
# the durable decisions/facts worth keeping.
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
        # Part A's inferred kind (techne#33) is the second field — it tells the model
        # which observations are decision-/fact-shaped and worth extracting (techne#19).
        kind = o.get("kind") or ""
        label = f"{tool}|{kind}" if kind else tool
        content = (o.get("content") or "").replace("\n", " ")
        if len(content) > MAX_OBS_CONTENT_CHARS:
            content = content[:MAX_OBS_CONTENT_CHARS] + "…"
        lines.append(f"- [{label}] {content}")
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
    items = extract_json_array(raw)

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
        if (
            thread_id is not None
            and not conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
        ):
            thread_id = None
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, tags, confidence,
                  source_session_id, source_thread_id,
                  model_used, prompt_hash, raw_response, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reflect')""",
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


def pending_reflection(config: Config, *, limit: int = 5) -> list[dict]:
    """Recent sessions that look reflectable but haven't been emitted yet (techne#19).

    A cheap SessionStart heuristic for the auto-trigger: a session with at least
    ``reflect.min_observations_per_thread`` observations and no
    ``reflect:emitted:<id>`` marker. It deliberately COUNTS observations rather than
    running full thread detection (too costly for the bounded hook) — the emit
    round-trip self-corrects: ``techne_reflect_emit`` sets the marker even when it
    yields an empty batch, so a non-reflectable session is nudged at most once.

    Returns the unemitted sessions among the ``limit`` most-recent (newest first),
    each ``{"session_id", "observations"}`` — the marker filter runs *after* the
    LIMIT, so a genuinely-old unemitted session past that window is intentionally not
    surfaced (newest-unreflected is what the nudge cares about; explicit emit always
    works). Empty when nothing is pending.
    """
    from techne.core.extension_state import get_state

    min_obs = config.reflect.min_observations_per_thread
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            """SELECT s.id AS session_id, COUNT(o.id) AS n
                 FROM sessions s
                 JOIN observations o ON o.session_id = s.id
             GROUP BY s.id
               HAVING n >= ?
             ORDER BY s.started_at DESC
                LIMIT ?""",
            (min_obs, limit),
        ).fetchall()
        return [
            {"session_id": r["session_id"], "observations": r["n"]}
            for r in rows
            if get_state(conn, f"reflect:emitted:{r['session_id']}") is None
        ]
    finally:
        conn.close()


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
        prompt_hash = _prompt_hash(prompt)
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


def effective_write_to(config: Config) -> str:
    """Return the effective write_to mode honoring the coexistence posture.

    When posture.mode == 'spool-primary', reflect is ADVISORY: proposals land in
    candidates, never auto-promoted into live knowledge that could drift from CC's
    curated memory. techne#107 PR4.

    Under posture 'observe' (default), returns config.reflect.write_to as-is so
    existing behaviour is preserved exactly.
    """
    posture = getattr(config, "posture", None)
    if posture is not None and posture.mode == "spool-primary":
        return "candidates"
    return config.reflect.write_to


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
            if effective_write_to(config) == "live":
                promoted_id = _write_live(
                    config.db_path, c, source=f"reflect:{thread_id or 'unknown'}"
                )
                _mark_promoted(config.db_path, cand_id, promoted_id)
                counts["candidates_promoted"] += 1
        except Exception:
            log.exception(
                "ingest_reflection_response: failed to insert/promote candidate"
                " (session=%s thread=%s)",
                session_id,
                thread_id,
            )
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
            log.exception(
                "reflect_on_session: could not build generate_fn (session=%s)", session_id
            )
            result["errors"] += 1
            return result

    from techne.core.thread_orchestrator import persisted_thread_id

    obs_by_thread = _bucket_observations(threads, observations)

    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue

        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = _prompt_hash(prompt)

        try:
            raw = generate_fn(prompt)
        except Exception:
            log.exception(
                "reflect_on_session: LLM call failed (session=%s thread_num=%s)",
                session_id,
                t.thread_num,
            )
            result["errors"] += 1
            continue
        result["threads_reflected"] += 1

        # Same insertion path as the agent-assisted ingest — one code path. Resolve
        # the ACTUAL persisted threads.id (the techne#106 drift-resistant lookup that
        # emit_reflection_batch already uses), not a recomputed thread_row_id which
        # drifts for re-detected heuristic threads → candidate FK silently NULLed
        # (techne#165). None → ingest stores a NULL FK rather than losing the candidate.
        thread_id = persisted_thread_id(config.db_path, session_id, t)
        counts = ingest_reflection_response(
            config, session_id, thread_id, prompt_hash, raw, model_used=reflect_config.model
        )
        for key, val in counts.items():
            result[key] += val

    return result


def _mark_promoted(db_path: Path, candidate_id: int, promoted_to_id: int | None) -> None:
    """Mark a candidate promoted by the write_to='live' auto-path. Stamps
    decided_by='reflect-auto' for the audit trail (techne#108) — no human reviewed it.
    A separate UPDATE from promote_candidate's dispatch (this path never applies an
    action); decision_reason is left NULL — there's no human reason on the auto-path."""
    conn = connect(db_path)
    try:
        conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted',
                      promoted_to_id = ?,
                      reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = 'reflect-auto'
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


def _apply_add(conn, row, db_path: Path) -> int | None:
    """Apply an additive candidate — the original reflect promotion path, verbatim.

    Runs inside promote_candidate's open transaction (techne#108 dispatch); a
    failure here rolls back the status flip too. Returns the new knowledge.id, or
    None for an identity entry.

    Dependency edges (techne#215): a dream-origin candidate is a new insight
    explicitly synthesized from its source entries (recorded in `target_ids`). The
    derivation relationship is genuinely known at promotion time — NOT inferred from
    similarity. Writing dep edges here, inside the same transaction as the knowledge
    insert, makes the relationship atomic and ensures the cascade can fire the moment
    any source is superseded. No edges are written for reflect/consolidate add
    candidates: those have no structural source relationship.
    """
    from techne.core.knowledge import (
        CONFIDENCE_NUMERIC,
        VALID_CONFIDENCE,
        VALID_TYPES,
        _add_dep_in_conn,
    )

    scope = row["scope"]
    source = f"reflect-promote:{row['source_session_id']}:{row['id']}"
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
        new_id = cur2.lastrowid

        # Populate dependency edges for dream candidates (techne#215).
        # Only dream candidates carry a meaningful derived-from relationship: the
        # new insight was synthesized from the explicit source entries in target_ids.
        # Superseding any source later will cascade the insight to `uncertain` via
        # the existing _cascade_uncertain machinery — no new cascade logic needed.
        if row["origin"] == "dream":
            source_ids = json.loads(row["target_ids"] or "[]")
            for src_id in source_ids:
                _add_dep_in_conn(conn, new_id, src_id)

        return new_id
    if scope == "identity":
        from techne.core.identity import set_value

        # Use the SAME connection — a second connection would deadlock against
        # this one's open write (the candidate status UPDATE).
        set_value(db_path, row["type"], row["content"], source=source, conn=conn)
        return None
    return None


def _apply_supersede(conn, row, db_path: Path) -> int | None:
    """Apply a 'supersede' proposal (techne#108 consolidate): replace ONE existing
    row (target_ids[0]) with the proposal's corrected/clarified content, inheriting
    the old row's type/tags/anchor (supersede semantics). Returns the new knowledge.id."""
    from techne.core.knowledge import _supersede_in_conn

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) != 1:
        raise ValueError(
            f"supersede candidate {row['id']} needs exactly one target_id, got {targets}"
        )
    source = f"consolidate-supersede:{row['id']}"
    return _supersede_in_conn(conn, targets[0], redact_credentials(row["content"]), source)


def _apply_retire(conn, row, db_path: Path) -> int | None:
    """Apply a 'retire' proposal (techne#213 contradiction detection): link an EXISTING
    live row to an EXISTING live survivor via `superseded_by` — no new row minted.

    target_ids = [old_id, survivor_id] (ORDER-SIGNIFICANT — old is retired into survivor).
    Both are already live knowledge rows (the two entries the conflict check compared),
    so retirement points old_id.superseded_by at survivor_id rather than minting a
    duplicate of the survivor's fact. The candidate's `content` holds the survivor's
    (optionally LLM-refined) text for operator DISPLAY only; the applier links, it does
    not write content — apply a wording edit separately if desired. Returns survivor_id."""
    from techne.core.knowledge import _retire_in_conn

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) != 2:
        raise ValueError(
            f"retire candidate {row['id']} needs exactly two target_ids "
            f"[old, survivor], got {targets}"
        )
    old_id, survivor_id = targets[0], targets[1]
    return _retire_in_conn(conn, old_id, survivor_id)


def _apply_merge(conn, row, db_path: Path) -> int | None:
    """Apply a 'merge' proposal (techne#108 consolidate): collapse N existing rows
    (target_ids) into one merged row carrying the proposal's explicitly chosen
    type/confidence/tags. Returns the new knowledge.id."""
    from techne.core.knowledge import (
        CONFIDENCE_NUMERIC,
        VALID_CONFIDENCE,
        VALID_TYPES,
        _merge_in_conn,
    )

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) < 2:
        raise ValueError(
            f"merge candidate {row['id']} needs at least two target_ids, got {targets}"
        )
    conf = row["confidence"] if row["confidence"] in VALID_CONFIDENCE else "medium"
    ktype = row["type"] if row["type"] in VALID_TYPES else "fact"
    return _merge_in_conn(
        conn,
        targets,
        redact_credentials(row["content"]),
        ktype=ktype,
        confidence=conf,
        numeric=CONFIDENCE_NUMERIC[conf],
        tags=row["tags"] or "",
        source=f"consolidate-merge:{row['id']}",
    )


# Per-action appliers (techne#108). promote_candidate owns the transaction + status
# guard; each applier takes the open conn + the candidate row and returns the new
# knowledge.id (or None). 'add' is reflect's original path; 'supersede'/'merge' are
# the consolidate pass; 'retire' is the contradiction-detection pass (techne#213 —
# links two EXISTING live rows without minting). ('connect' / dream is the deferred PR3.)
_APPLIERS = {
    "add": _apply_add,
    "supersede": _apply_supersede,
    "merge": _apply_merge,
    "retire": _apply_retire,
}


def promote_candidate(
    db_path: Path,
    candidate_id: int,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
    force: bool = False,
) -> int | None:
    """Promote a candidate to live knowledge / identity. Returns the new
    knowledge.id (or None for identity entries). Raises if already reviewed.

    Atomic: a single connection performs the status guard (UPDATE WHERE
    status='pending'), the action-specific apply, and the promoted_to_id update
    inside one transaction.  Concurrent callers that both see 'pending' will
    race on the UPDATE; the loser gets rowcount==0 and raises immediately
    without writing a duplicate knowledge row.

    The `action` column (techne#108) selects how the proposal is applied; a NULL
    action (legacy rows) routes to 'add'. `decided_by`/`decision_reason` record the
    review provenance.

    Faithfulness soft gate (techne#252): for merge/supersede candidates that have
    been explicitly flagged unfaithful (faithfulness_ok == 0), promotion is refused
    and a FaithfulnessGateError (subclass of ValueError) is raised — the candidate
    status is NOT changed.  Pass ``force=True`` to override after reviewing the
    warning.  Unchecked (faithfulness_ok IS NULL) and faithful (== 1) candidates
    always promote normally; the gate only blocks explicitly-flagged-unfaithful
    merge/supersede candidates.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Candidate {candidate_id} not found")

        # action is NOT NULL DEFAULT 'add' (reconciler backfills pre-#108 rows), so
        # this `or` never actually fires — defensive only.
        action = row["action"] or "add"
        applier = _APPLIERS.get(action)
        if applier is None:
            raise ValueError(f"Candidate {candidate_id} has unknown action {action!r}")

        # Faithfulness soft gate (techne#252): refuse explicitly-unfaithful
        # merge/supersede candidates before touching status. The check fires only when
        # faithfulness_ok == 0 (checked and flagged); NULL (unchecked) and 1 (faithful)
        # pass through. force=True bypasses the gate after operator review.
        if (
            not force
            and action in _FAITHFULNESS_GATED_ACTIONS
            and row["faithfulness_ok"] == 0
        ):
            warning = row["faithfulness_warning"] or "candidate was flagged unfaithful"
            raise FaithfulnessGateError(
                f"Candidate {candidate_id} ({action}) was flagged unfaithful: "
                f"{warning}. "
                f"Promote with force=True (CLI: --force) to override after reviewing."
            )

        # Atomic guard: only one concurrent caller can flip status from pending.
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted', reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = ?, decision_reason = ?
                WHERE id = ? AND status = 'pending'""",
            (decided_by, decision_reason, candidate_id),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Candidate {candidate_id} already {row['status']} — cannot re-promote"
            )

        # Apply on the SAME connection so a failure here rolls back the status flip.
        promoted_to_id = applier(conn, row, db_path)

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


def discard_candidate(
    db_path: Path,
    candidate_id: int,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
) -> None:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'discarded', reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = ?, decision_reason = ?
                WHERE id = ? AND status = 'pending'""",
            (decided_by, decision_reason, candidate_id),
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
