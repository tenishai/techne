"""Contradiction detection → supersession candidates (techne#213).

D1 of the coherence layer: detects when a knowledge entry contradicts an existing
one by comparing entries that share entities/tags/subject AND the same `type`.

Why NOT FTS/embedding similarity: cosine distance cannot distinguish a contradiction
from a duplicate (MemStrata AUROC 0.59, near chance). Selection is structural
(shared tags → same domain) + LLM judgment for the semantic question.

Emit/ingest pattern (no-API-key, no sampling — the resident model judges):
  conflict_check_emit  → returns prompts for the agent to answer
  conflict_check_ingest → stages supersession/update candidates for review

Memory sovereignty invariant: nothing is auto-applied. Candidates land in
knowledge_candidates (action='supersede' or 'add') for operator review.

Two-call-site design: run_conflict_check + select_conflict_candidates are pure
functions callable at the federation read-merge call site as well as the local
consolidate emit path. Only the emit/ingest wrappers touch the DB.
"""

from __future__ import annotations

import logging
from pathlib import Path

from techne.config import Config
from techne.core._resident import extract_json_array
from techne.core.database import connect
from techne.core.redaction import redact_credentials
from techne.core.sleepdream import SYSTEM_SLEEP_SESSION, _cluster_hash, _ensure_system_session

log = logging.getLogger(__name__)

# Verdicts the LLM can return per comparison entry
VALID_VERDICTS = ("SUPERSEDE", "UPDATE", "NOOP", "DISTINCT")

MAX_ENTRY_CHARS = 400

CONFLICT_PROMPT = """\
You are checking whether a knowledge entry contradicts, updates, or duplicates \
existing knowledge of the same type.

# Focus entry (the entry being checked for conflicts)
[#{focus_id}] type={focus_type}  event_at={focus_event_at}
{focus_content}

# Existing entries to compare against
{comparison_entries}

# Task
For EACH existing entry give ONE verdict:

- SUPERSEDE  Both entries address the same fact but they *contradict* each other.
             The entry with the LATER event_at is the authoritative current fact;
             the EARLIER one should be retired. Provide the surviving content in
             "content" (the text of the newer/current entry).
- UPDATE     The focus entry *augments or refines* the existing entry without
             contradiction — they are compatible but the existing can be improved.
             Provide a merged/updated version in "content".
- NOOP       These two entries state the same thing — already known, no action.
- DISTINCT   Genuinely different topics — no conflict, no overlap worth acting on.

Return ONLY a JSON array — one object per existing entry:
[
  {{
    "id": <existing_entry_id>,
    "verdict": "SUPERSEDE"|"UPDATE"|"NOOP"|"DISTINCT",
    "content": "<surviving or merged content — REQUIRED for SUPERSEDE and UPDATE>",
    "reason": "<one-sentence explanation>"
  }},
  ...
]

Be conservative: prefer DISTINCT when uncertain.
Output the JSON array and nothing else.
"""


# ---------------------------------------------------------------------------
# Candidate selection — structural (NOT FTS/similarity)
# ---------------------------------------------------------------------------


def _parse_tags(tags: str | None) -> frozenset[str]:
    """Split a comma-separated tag string into a frozenset of stripped tokens."""
    if not tags:
        return frozenset()
    return frozenset(t.strip().lower() for t in tags.split(",") if t.strip())


_STOP_WORDS = frozenset(
    "a an and are as at be by do for from has have he in is it its of on or that"
    " the this to was were will with".split()
)


def _content_keywords(content: str) -> frozenset[str]:
    """Extract non-trivial words from content for fallback entity matching.

    Strips punctuation, lowercases, removes stop words and single-char tokens.
    Returns an empty set for empty/whitespace-only content.
    """
    import re

    tokens = re.findall(r"[a-z0-9_]+", content.lower())
    return frozenset(t for t in tokens if len(t) > 2 and t not in _STOP_WORDS)


def select_conflict_candidates(
    db_path: Path,
    candidate_row: dict,
    *,
    max_candidates: int = 20,
) -> list[dict]:
    """Select live knowledge rows that overlap with *candidate_row* by shared
    entities/tags and the same knowledge ``type``.

    Selection strategy (deliberate non-similarity):
    1. Same ``type`` — contradictions only exist within the same epistemic category.
    2. Shared tags (at least one common comma-separated token).
    3. Fallback when *candidate_row* has no tags: significant-word overlap in content.

    Excludes superseded rows and the candidate itself. Capped at *max_candidates*.

    This function is callable at the federation read-merge call site as well as
    the local consolidate emit path — it takes a plain dict and a db_path, with
    no reference to Config or the emit batch.
    """
    candidate_id = candidate_row.get("id")
    candidate_type = candidate_row.get("type", "fact")
    candidate_tags = _parse_tags(candidate_row.get("tags"))

    log.debug(
        "conflict: selecting candidates for id=%s type=%s tags=%r",
        candidate_id,
        candidate_type,
        candidate_row.get("tags"),
    )

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, type, content, tags, confidence, event_at, created_at
               FROM knowledge
               WHERE superseded_by IS NULL
                 AND type = ?
                 AND id != ?""",
            (candidate_type, candidate_id if candidate_id is not None else -1),
        ).fetchall()
        all_rows = [dict(r) for r in rows]
    finally:
        conn.close()

    if candidate_tags:
        # Primary path: at least one shared tag token
        matched = [r for r in all_rows if _parse_tags(r.get("tags")) & candidate_tags]
    else:
        # Fallback: significant-word overlap in content (no tags on candidate)
        candidate_words = _content_keywords(candidate_row.get("content", ""))
        if candidate_words:
            matched = [
                r
                for r in all_rows
                if _content_keywords(r.get("content", "")) & candidate_words
            ]
        else:
            matched = []

    log.debug("conflict: found %d candidate(s) for id=%s", len(matched), candidate_id)
    return matched[:max_candidates]


# ---------------------------------------------------------------------------
# Prompt construction (pure — no DB, no LLM)
# ---------------------------------------------------------------------------


def _render_entry(row: dict) -> str:
    content = (row.get("content") or "").replace("\n", " ")
    if len(content) > MAX_ENTRY_CHARS:
        content = content[:MAX_ENTRY_CHARS] + "…"
    event_at = row.get("event_at") or row.get("created_at") or "unknown"
    return f"  [#{row['id']}] event_at={event_at}\n  {content}"


def run_conflict_check(
    focus_item: dict,
    comparison_set: list[dict],
) -> str:
    """Build the conflict-detection prompt for the resident model to judge.

    Pure function — no DB access, no LLM call. The caller (conflict_check_emit)
    returns this prompt in the emit batch; the agent answers it and passes the
    response to conflict_check_ingest.

    Also callable at the federation read-merge call site: pass a foreign entry as
    *focus_item* and local matches as *comparison_set*, then feed the response to
    parse_conflict_response to get verdicts.
    """
    if not comparison_set:
        raise ValueError("run_conflict_check: comparison_set must not be empty")

    event_at = focus_item.get("event_at") or focus_item.get("created_at") or "unknown"
    focus_content = (focus_item.get("content") or "").replace("\n", " ")
    if len(focus_content) > MAX_ENTRY_CHARS:
        focus_content = focus_content[:MAX_ENTRY_CHARS] + "…"

    comparisons_text = "\n\n".join(_render_entry(r) for r in comparison_set)

    return CONFLICT_PROMPT.format(
        focus_id=focus_item.get("id", "?"),
        focus_type=focus_item.get("type", "?"),
        focus_event_at=event_at,
        focus_content=focus_content,
        comparison_entries=comparisons_text,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_conflict_response(raw: str) -> list[dict]:
    """Parse the agent's JSON conflict verdict array.

    Returns a list of normalized decision dicts:
      {"id": <int>, "verdict": <str>, "content": <str|None>, "reason": <str|None>}

    Silently drops malformed items. Returns [] on total parse failure.
    """
    items = extract_json_array(raw or "")
    decisions: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            entry_id = int(item.get("id", -1))
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("verdict", "")).upper()
        if verdict not in VALID_VERDICTS:
            log.debug("conflict: ignoring unknown verdict %r for id=%s", verdict, entry_id)
            continue
        content = item.get("content")
        if content is not None and not isinstance(content, str):
            content = None
        reason = item.get("reason")
        if reason is not None:
            reason = str(reason)[:500]
        decisions.append(
            {
                "id": entry_id,
                "verdict": verdict,
                "content": (content or "").strip() or None,
                "reason": reason,
            }
        )
    log.debug("conflict: parsed %d decision(s) from response", len(decisions))
    return decisions


# ---------------------------------------------------------------------------
# Anti-re-proposal guard
# ---------------------------------------------------------------------------


def _already_proposed_conflict(conn, check_hash: str) -> bool:
    """This exact conflict pair/group already has a record — pending, promoted, OR
    discarded. Discarded counts: the pair was already judged (NOOP/DISTINCT/etc.) and
    should not be re-nagged until the underlying entries change."""
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
                WHERE prompt_hash = ? AND origin = 'conflict'
                  AND status IN ('pending', 'promoted', 'discarded') LIMIT 1""",
            (check_hash,),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Temporal tiebreak
# ---------------------------------------------------------------------------


def _effective_event_at(row: dict) -> str:
    """event_at if set, else created_at. Comparable as ISO strings (YYYY-MM-DD …)."""
    return (row.get("event_at") or row.get("created_at") or "")


def _is_newer(a: dict, b: dict) -> bool:
    """Return True if *a* has a later effective event_at than *b*."""
    return _effective_event_at(a) >= _effective_event_at(b)


# ---------------------------------------------------------------------------
# Candidate insertion helpers
# ---------------------------------------------------------------------------


import json as _json


def _insert_conflict_candidate(
    conn,
    *,
    action: str,
    content: str,
    ktype: str,
    confidence: str,
    target_ids: list[int],
    check_hash: str,
    status: str,
    decided_by: str | None,
    decision_reason: str | None,
    raw: str | None,
) -> None:
    """Insert one conflict-check result into knowledge_candidates."""
    conn.execute(
        """INSERT INTO knowledge_candidates
             (scope, type, content, tags, confidence, source_session_id,
              source_thread_id, model_used, prompt_hash, raw_response,
              action, target_ids, origin, status, decided_by, decision_reason)
           VALUES ('knowledge', ?, ?, '', ?, ?, NULL, 'agent-assisted', ?, ?,
                   ?, ?, 'conflict', ?, ?, ?)""",
        (
            ktype,
            redact_credentials(content),
            confidence,
            SYSTEM_SLEEP_SESSION,
            check_hash,
            redact_credentials(raw) if raw else None,
            action,
            _json.dumps(sorted(target_ids)),
            status,
            decided_by,
            decision_reason,
        ),
    )


# ---------------------------------------------------------------------------
# Emit / ingest
# ---------------------------------------------------------------------------


def conflict_check_emit(config: Config, *, max_items: int | None = None) -> list[dict]:
    """Find live knowledge pairs that may conflict and build prompts for the agent.

    Selection: pairs of live knowledge rows that share at least one tag token AND
    have the same ``type`` — no FTS/embedding similarity (AUROC ~0.59 for contradictions).

    Returns a list of emit items:
      {
        "focus_id":       int,          # the 'new' entry being checked
        "comparison_ids": list[int],    # the 'existing' entries to compare against
        "check_hash":     str,          # stable identifier for anti-re-proposal
        "prompt":         str,          # prompt for the agent to answer
      }

    Each prompt should be answered with a JSON array verdict (see CONFLICT_PROMPT)
    and fed back via conflict_check_ingest.
    """
    if max_items is None:
        max_items = getattr(config, "conflict", None)
        max_items = max_items.max_items_per_emit if max_items else 20

    _ensure_system_session(config.db_path)

    max_candidates = 20
    cfg_conflict = getattr(config, "conflict", None)
    if cfg_conflict is not None:
        max_candidates = cfg_conflict.max_comparison_candidates

    conn = connect(config.db_path)
    try:
        live = [
            dict(r)
            for r in conn.execute(
                """SELECT id, type, content, tags, confidence, event_at, created_at
                   FROM knowledge WHERE superseded_by IS NULL"""
            )
        ]

        batch: list[dict] = []
        seen_pairs: set[frozenset] = set()

        for focus in live:
            if len(batch) >= max_items:
                break

            comparisons = select_conflict_candidates(
                config.db_path, focus, max_candidates=max_candidates
            )
            if not comparisons:
                continue

            # Only include pairs not yet emitted this run (symmetric dedup)
            fresh = [
                c
                for c in comparisons
                if frozenset([focus["id"], c["id"]]) not in seen_pairs
            ]
            if not fresh:
                continue

            # Stable identifier for this group
            all_ids = sorted([focus["id"]] + [c["id"] for c in fresh])
            check_hash = _cluster_hash(all_ids)

            if _already_proposed_conflict(conn, check_hash):
                log.debug(
                    "conflict: skipping already-proposed group hash=%s", check_hash
                )
                # Mark pairs seen anyway so we don't re-check the same pair below
                for c in fresh:
                    seen_pairs.add(frozenset([focus["id"], c["id"]]))
                continue

            # Mark pairs seen
            for c in fresh:
                seen_pairs.add(frozenset([focus["id"], c["id"]]))

            prompt = run_conflict_check(focus, fresh)
            batch.append(
                {
                    "focus_id": focus["id"],
                    "comparison_ids": [c["id"] for c in fresh],
                    "check_hash": check_hash,
                    "prompt": prompt,
                }
            )
            log.debug(
                "conflict: emit item focus_id=%s comparisons=%s hash=%s",
                focus["id"],
                [c["id"] for c in fresh],
                check_hash,
            )
    finally:
        conn.close()

    return batch


def conflict_check_ingest(
    config: Config,
    focus_id: int,
    comparison_ids: list[int],
    check_hash: str,
    response: str,
) -> dict:
    """Process the agent's conflict verdict and stage candidates.

    Verdict routing:
    - SUPERSEDE → pending 'supersede' candidate targeting the OLDER entry
                  (determined by event_at; newer content survives).
    - UPDATE     → pending 'supersede' candidate with merged/updated content
                  targeting the existing entry.
    - NOOP       → discarded marker (this pair was already known — don't re-nag).
    - DISTINCT   → discarded marker (genuinely different topics — don't re-nag).

    The check_hash is RECOMPUTED from focus_id + comparison_ids — never trusted from
    the caller — so stored prompt_hash always matches real ids and can't silently
    defeat _already_proposed_conflict.

    Returns counts dict.
    """
    counts = {
        "supersede": 0,
        "update": 0,
        "noop": 0,
        "distinct": 0,
        "skipped": 0,
        "errors": 0,
    }

    all_ids = sorted([focus_id] + list(comparison_ids))
    ch = _cluster_hash(all_ids)  # recompute from ids; caller-supplied hash is advisory

    raw_for_store = response if _log_raw(config) else None
    decisions = parse_conflict_response(response)

    _ensure_system_session(config.db_path)

    conn = connect(config.db_path)
    try:
        if _already_proposed_conflict(conn, ch):
            counts["skipped"] += 1
            return counts

        # Load focus row and comparison rows for tiebreak logic
        focus_row = _fetch_row(conn, focus_id)
        comp_by_id = {cid: _fetch_row(conn, cid) for cid in comparison_ids}

        if not decisions:
            # No parseable verdicts — record a discarded marker so this group isn't
            # re-proposed on every emit run.
            _insert_conflict_candidate(
                conn,
                action="add",
                content="(no conflict — agent found no actionable verdict)",
                ktype="fact",
                confidence="low",
                target_ids=all_ids,
                check_hash=ch,
                status="discarded",
                decided_by="agent-triage",
                decision_reason="no-verdicts",
                raw=raw_for_store,
            )
            conn.commit()
            return counts

        for decision in decisions:
            comp_id = decision["id"]
            verdict = decision["verdict"]
            suggested_content = decision.get("content")
            reason = decision.get("reason")

            comp_row = comp_by_id.get(comp_id)
            if comp_row is None:
                log.debug("conflict: comparison id=%s not found (may be superseded)", comp_id)
                continue

            log.debug(
                "conflict: verdict=%s focus=%s vs comp=%s reason=%r",
                verdict,
                focus_id,
                comp_id,
                reason,
            )

            if verdict == "SUPERSEDE":
                _handle_supersede(
                    conn,
                    focus_row=focus_row,
                    comp_row=comp_row,
                    suggested_content=suggested_content,
                    check_hash=ch,
                    reason=reason,
                    raw=raw_for_store,
                )
                counts["supersede"] += 1

            elif verdict == "UPDATE":
                _handle_update(
                    conn,
                    focus_row=focus_row,
                    comp_row=comp_row,
                    suggested_content=suggested_content,
                    check_hash=ch,
                    reason=reason,
                    raw=raw_for_store,
                )
                counts["update"] += 1

            elif verdict == "NOOP":
                _insert_conflict_candidate(
                    conn,
                    action="add",
                    content=f"(noop — focus #{focus_id} already known in #{comp_id})",
                    ktype=comp_row.get("type", "fact"),
                    confidence=comp_row.get("confidence", "medium"),
                    target_ids=[focus_id, comp_id],
                    check_hash=ch,
                    status="discarded",
                    decided_by="agent-triage",
                    decision_reason=f"noop: {reason or ''}",
                    raw=raw_for_store,
                )
                counts["noop"] += 1

            elif verdict == "DISTINCT":
                _insert_conflict_candidate(
                    conn,
                    action="add",
                    content=f"(distinct — #{focus_id} and #{comp_id} are different topics)",
                    ktype=comp_row.get("type", "fact"),
                    confidence="low",
                    target_ids=[focus_id, comp_id],
                    check_hash=ch,
                    status="discarded",
                    decided_by="agent-triage",
                    decision_reason=f"distinct: {reason or ''}",
                    raw=raw_for_store,
                )
                counts["distinct"] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        counts["errors"] += 1
        log.exception("conflict_check_ingest: unexpected error")
    finally:
        conn.close()

    return counts


# ---------------------------------------------------------------------------
# Internal helpers for verdict routing
# ---------------------------------------------------------------------------


def _fetch_row(conn, entry_id: int) -> dict | None:
    """Fetch a knowledge row by id; returns None if not found."""
    row = conn.execute(
        "SELECT id, type, content, tags, confidence, event_at, created_at "
        "FROM knowledge WHERE id = ?",
        (entry_id,),
    ).fetchone()
    return dict(row) if row else None


def _handle_supersede(
    conn,
    *,
    focus_row: dict | None,
    comp_row: dict,
    suggested_content: str | None,
    check_hash: str,
    reason: str | None,
    raw: str | None,
) -> None:
    """Stage a supersession candidate, retiring the older entry.

    Temporal tiebreak: the entry with the later event_at is the current fact;
    the earlier one is retired (superseded). Do NOT use created_at alone — the
    spec mandates event_at as the authoritative real-world time.

    target_ids=[older_id] → _apply_supersede in promote_candidate will call
    _supersede_in_conn(older_id, surviving_content), creating a new row and
    marking older_id.superseded_by = new_id.
    """
    if focus_row is None:
        log.debug("conflict: SUPERSEDE skipped — focus row not found")
        return

    focus_newer = _is_newer(focus_row, comp_row)
    if focus_newer:
        # Focus is the current fact; comp should be retired.
        older_id = comp_row["id"]
        surviving_content = suggested_content or focus_row.get("content", "")
        surviving_type = focus_row.get("type", "fact")
        surviving_confidence = focus_row.get("confidence", "medium")
    else:
        # Comparison is the current fact; focus should be retired.
        older_id = focus_row["id"]
        surviving_content = suggested_content or comp_row.get("content", "")
        surviving_type = comp_row.get("type", "fact")
        surviving_confidence = comp_row.get("confidence", "medium")

    surviving_content = surviving_content.strip() or (
        focus_row.get("content") if focus_newer else comp_row.get("content")
    ) or ""

    log.debug(
        "conflict: staging SUPERSEDE candidate — retire id=%s surviving='%.60s…'",
        older_id,
        surviving_content,
    )

    _insert_conflict_candidate(
        conn,
        action="supersede",
        content=surviving_content,
        ktype=surviving_type,
        confidence=surviving_confidence,
        target_ids=[older_id],
        check_hash=check_hash,
        status="pending",
        decided_by=None,
        decision_reason=reason,
        raw=raw,
    )


def _handle_update(
    conn,
    *,
    focus_row: dict | None,
    comp_row: dict,
    suggested_content: str | None,
    check_hash: str,
    reason: str | None,
    raw: str | None,
) -> None:
    """Stage an update (supersede-with-improved-content) candidate.

    UPDATE means the new entry augments the existing without contradiction. We
    stage a supersede candidate so the operator can review the merged wording
    before applying. If the LLM provided no merged content, we fall back to the
    focus entry's content.
    """
    if focus_row is None:
        log.debug("conflict: UPDATE skipped — focus row not found")
        return

    update_content = (suggested_content or "").strip() or focus_row.get("content", "")

    log.debug(
        "conflict: staging UPDATE candidate — target id=%s content='%.60s…'",
        comp_row["id"],
        update_content,
    )

    _insert_conflict_candidate(
        conn,
        action="supersede",
        content=update_content,
        ktype=comp_row.get("type", "fact"),
        confidence=comp_row.get("confidence", "medium"),
        target_ids=[comp_row["id"]],
        check_hash=check_hash,
        status="pending",
        decided_by=None,
        decision_reason=f"update: {reason or ''}",
        raw=raw,
    )


def _log_raw(config: Config) -> bool:
    """Whether to store the raw LLM response (mirrors reflect.log_raw_response)."""
    cfg_conflict = getattr(config, "conflict", None)
    if cfg_conflict is not None and hasattr(cfg_conflict, "log_raw_response"):
        return cfg_conflict.log_raw_response
    return getattr(config.reflect, "log_raw_response", True)
