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

import json
import logging
from pathlib import Path

from techne.config import Config
from techne.core._resident import extract_json_array, extract_json_array_or_none
from techne.core.database import connect
from techne.core.knowledge import LESSON_TYPES_SQL
from techne.core.redaction import redact_credentials
from techne.core.sleepdream import (
    SYSTEM_SLEEP_SESSION,
    _cluster_hash,
    _ensure_system_session,
    invalid_target_ids,
)

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


#: Producer label for a candidate written by a RESIDENT agent. Imported from sleepdream
#: so ONE definition governs every rail — a second copy of this literal drifts the moment
#: either side moves (the copy-not-mechanism shape techne#518 found five times).
from techne.core.sleepdream import RESIDENT_PRODUCER  # noqa: E402


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


def _overlap_coefficient(a: frozenset[str], b: frozenset[str]) -> float:
    """Shared tokens over the SMALLER set (techne#933).

    NOT Jaccard. The union denominator is what makes Jaccard useless on short
    keyword sets: ECC recorded the measurement in its own clustering code --
    trigger keyword sets average ~7 words, so even clearly-related pairs top out
    near 0.33 and nothing ever groups.

    USED ONLY AS A TIEBREAK, NEVER AS THE PRIMARY KEY, AND A PRE-PASS IS WHY.
    On its own it scores a one-token set 1.0 against anything, so sorting by it
    ranks SHORT rows above STRONG ones -- the opposite of the intent. Measured
    against a focus of {canary, deploy, five, minutes, window}:

        "the deploy canary window is thirty minutes"   4 shared, overlap 0.800
        "canary"                                       1 shared, overlap 1.000

    The one-token row won. `_rank_key` therefore sorts on the SHARED COUNT first
    and uses this only to separate rows that share the same number of tokens.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _rank_key(row_tokens: frozenset[str], focus_tokens: frozenset[str], row_id: int) -> tuple:
    """Sort key for a candidate: most shared tokens first, then tightest overlap.

    ABSOLUTE OVERLAP IS THE PRIMARY SIGNAL because it is the one that answers
    "how much of this focus does the row actually account for". The coefficient
    normalises that away -- which is what made a one-token row outrank a
    four-token match -- so it is demoted to a tiebreak between rows that share
    equally many tokens. `row_id` last keeps equal scores DETERMINISTIC, which
    matters because the SQL carries no ORDER BY.
    """
    shared = row_tokens & focus_tokens
    return (-len(shared), -_overlap_coefficient(row_tokens, focus_tokens), row_id)


def select_conflict_candidates(
    db_path: Path,
    candidate_row: dict,
    *,
    max_candidates: int = 20,
    min_shared_keywords: int = 1,
) -> list[dict]:
    """Select live knowledge rows that overlap with *candidate_row* by shared
    entities/tags and the same knowledge ``type``.

    Selection strategy (deliberate non-similarity):
    1. Same ``type`` — contradictions only exist within the same epistemic category.
    2. Shared tags (at least one common comma-separated token).
    3. Fallback when *candidate_row* has no tags: significant-word overlap in content,
       requiring at least *min_shared_keywords* shared tokens (default 1).

    Excludes superseded rows, soft-archived rows (`status = 'archived'`, techne#216 —
    techne#1033 review round 2: an archived row is out of the default working set and
    must not be treated as a live peer to flag a conflict against), and the candidate
    itself. Capped at *max_candidates*, STRONGEST FIRST — see the ordering note below.

    TWO DEFECTS THIS GUARDS, both on the path that builds the conflict-check prompt
    (techne#933). They compounded: weak matches did not merely add noise, they took
    slots from the contradictions the check exists to find.

    NO FLOOR ON THE MATCH. Both paths were bare set intersections, so ONE shared
    token qualified a row -- one incidental word like "session" or "config" pulled in
    something with no topical relation. ``min_shared_keywords`` is the knob for it,
    on the CONTENT path only (tags are curated; one shared tag is evidence in a way
    one shared word is not, and the same floor there would drop a row whose single
    tag is the exact subject).

    IT DEFAULTS TO 1 -- TODAY'S BEHAVIOUR -- AND techne#933 ASKED FOR 2. Raising it
    is a measured recall regression, not a free win:
    ``test_select_candidates_keyword_fallback_no_tags`` pairs "we use postgres for
    storage" with "switched from postgres to sqlite", a REAL contradiction sharing
    exactly one token, and a floor of 2 drops it. What separates that from an
    incidental match is the token's SPECIFICITY, not the count; a count is a crude
    proxy, and an IDF-weighted guard is the honest version of the idea. The harm the
    issue documents -- weak matches TAKING SLOTS -- is closed by the ordering below
    instead, which needs no such trade. NOTE the ordering had to be CORRECTED to
    deliver that: ranking on the coefficient alone put SHORT rows above strong
    ones, so the primary key is the shared COUNT and the coefficient is a
    tiebreak. See `_rank_key`.

    NO ORDERING BEFORE TRUNCATION. The SQL has no ORDER BY and nothing sorted the
    matches, so the rows that survived ``max_candidates`` were whichever SQLite
    returned first -- not the most related. Same class as techne#861
    (``import_resolve`` resolving by row order). Sorting is by descending SHARED-TOKEN
    COUNT, then by descending overlap coefficient, then by id -- see `_rank_key`
    for why the count is primary. The id tiebreak is not cosmetic: the SQL carries no
    ORDER BY, so two rows with identical scores would otherwise be ordered by
    whatever SQLite returned first. (An earlier draft cited techne#873's
    PYTHONHASHSEED hazard here. That analogy is wrong -- `all_rows` is a list
    from a cursor and no set is iterated in ranking, only intersected for its
    size. The real hazard is the missing ORDER BY, which is a different thing.)

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
                 AND status != 'archived'
                 AND type = ?
                 AND id != ?""",
            (candidate_type, candidate_id if candidate_id is not None else -1),
        ).fetchall()
        all_rows = [dict(r) for r in rows]
    finally:
        conn.close()

    scored: list[tuple[tuple, dict]] = []
    if candidate_tags:
        # Primary path: at least one shared tag token. Tags are curated, so no floor.
        for r in all_rows:
            row_tags = _parse_tags(r.get("tags"))
            if row_tags & candidate_tags:
                scored.append((_rank_key(row_tags, candidate_tags, int(r.get("id") or 0)), r))
    else:
        # Fallback: significant-word overlap in content (no tags on candidate).
        candidate_words = _content_keywords(candidate_row.get("content", ""))
        if candidate_words:
            for r in all_rows:
                row_words = _content_keywords(r.get("content", ""))
                shared = row_words & candidate_words
                if len(shared) >= min_shared_keywords:
                    scored.append((_rank_key(row_words, candidate_words, int(r.get("id") or 0)), r))

    # _rank_key is already ordered "best first"; the id inside it keeps ties stable.
    scored.sort(key=lambda t: t[0])
    matched = [r for _key, r in scored]

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


def _already_proposed(conn, check_hash: str, origin: str) -> bool:
    """True if this conflict group already has a record keyed by (check_hash, origin).

    Guards against re-proposing the same pair regardless of prior verdict status
    (pending / promoted / discarded).  The ``origin`` parameter isolates the local
    conflict path (origin='conflict') from the federation cross-store path
    (origin='federation-conflict') so the two anti-re-proposal guards never suppress
    each other.

    For origin='conflict': check_hash is _cluster_hash(sorted local ids), so the guard
    keys on group MEMBERSHIP only.  An in-place content EDIT that keeps the same ids does
    NOT reopen a discarded group; re-check it explicitly if the wording change matters.

    For origin='federation-conflict': check_hash is
    _cluster_hash_cross(content_fingerprint(remote), sorted local ids), so a changed
    remote fact reopens the check even when the local candidate set is unchanged.
    """
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
                WHERE prompt_hash = ? AND origin = ?
                  AND status IN ('pending', 'promoted', 'discarded') LIMIT 1""",
            (check_hash, origin),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Temporal tiebreak
# ---------------------------------------------------------------------------


def _effective_event_at(row: dict) -> str:
    """event_at if set, else created_at. Comparable as ISO strings (YYYY-MM-DD …)."""
    return row.get("event_at") or row.get("created_at") or ""


def _is_newer(a: dict, b: dict) -> bool:
    """Return True if *a* has a later effective event_at than *b*."""
    return _effective_event_at(a) >= _effective_event_at(b)


# ---------------------------------------------------------------------------
# Candidate insertion helpers
# ---------------------------------------------------------------------------


def _insert_candidate(
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
    # REQUIRED, NO DEFAULT, and that is the mechanism rather than the discipline
    # (techne#555). A defaulted producer label is a silent lie: the row claims a
    # resident agent judged it. Mutation found FOUR call sites that could drop the
    # forward with the whole suite green, so the guard cannot be "remember to pass it"
    # — omitting it must be a TypeError at the call, which is what no default buys.
    model_used: str,
    origin: str,
) -> None:
    """Insert one ingest result into knowledge_candidates for operator review.

    Shared by the local conflict path (origin='conflict') and the federation cross-store
    path (origin='federation-conflict').  The ``origin`` parameter isolates the two
    anti-re-proposal guards so they never suppress each other.

    target_ids is stored in the ORDER GIVEN — not sorted.  For a 'retire' candidate the
    order is load-bearing: [old_id, survivor_id] (old retired into survivor).  The
    anti-re-proposal dedup key (check_hash) is computed separately from sorted ids, so
    preserving order here does not affect dedup.

    Both ``content`` and ``decision_reason`` are run through redact_credentials: the LLM
    and federation paths can interpolate foreign or model-generated text into these fields,
    so credentials must be scrubbed at this one chokepoint regardless of which branch
    produced them (#244).
    """
    conn.execute(
        """INSERT INTO knowledge_candidates
             (scope, type, content, tags, confidence, source_session_id,
              source_thread_id, model_used, prompt_hash, raw_response,
              action, target_ids, origin, status, decided_by, decision_reason)
           VALUES ('knowledge', ?, ?, '', ?, ?, NULL, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?)""",
        (
            ktype,
            redact_credentials(content),
            confidence,
            SYSTEM_SLEEP_SESSION,
            model_used,
            check_hash,
            redact_credentials(raw) if raw else None,
            action,
            json.dumps(list(target_ids)),
            origin,
            status,
            decided_by,
            redact_credentials(decision_reason) if decision_reason else decision_reason,
        ),
    )


# ---------------------------------------------------------------------------
# Emit / ingest
# ---------------------------------------------------------------------------


def conflict_check_emit(config: Config, *, max_items: int | None = None) -> list[dict]:
    """Find live knowledge pairs that may conflict and build prompts for the agent.

    Selection: pairs of live knowledge rows that share at least one tag token AND
    have the same ``type`` — no FTS/embedding similarity (AUROC ~0.59 for contradictions).

    PREREQUISITE — CONSISTENT TAGGING. Detection is structural: two entries are only ever
    compared if they share a tag token (or, when an entry is untagged, a significant
    content word). This is a hard recall limit — contradictory ANTONYMS in different
    vocabulary with no shared tag are NEVER compared. "use postgres" tagged `datastore`
    and "we switched to sqlite" tagged `datastore` WILL be compared; the same two facts
    with no shared tag and no shared content word will NOT. Consistent tagging (a shared
    subject/entity tag on related facts) is therefore a prerequisite for contradiction
    recall — untagged antonyms slip through.

    Returns a list of emit items:
      {
        "focus_id":       int,          # the 'new' entry being checked
        "comparison_ids": list[int],    # the 'existing' entries to compare against
        "check_hash":     str,          # stable identifier for anti-re-proposal
        "prompt":         str,          # prompt for the agent to answer
      }

    Each prompt should be answered with a JSON array verdict (see CONFLICT_PROMPT)
    and fed back via conflict_check_ingest.

    "Live" excludes soft-archived rows (`status = 'archived'`, techne#1033 review
    round 2) for the same reason `select_conflict_candidates` does: an archived row
    is out of the default working set and must not be proposed as a live focus or
    comparison peer.
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
                   FROM knowledge WHERE superseded_by IS NULL AND status != 'archived'
                   AND type NOT IN """
                + LESSON_TYPES_SQL
            )
        ]

        batch: list[dict] = []
        seen_pairs: set[frozenset] = set()

        # TODO(perf): this calls select_conflict_candidates per focus, each opening a
        # fresh connection and re-scanning the same-type set (1+N connections, O(N²) over
        # the live table). Fine while knowledge is small (see sleepdream's note). A future
        # optimisation could group `live` by tag once in-memory here — but must NOT change
        # select_conflict_candidates' pure (db_path, dict) signature, which the federation
        # read-merge call site depends on. Left as a TODO to protect that decoupling.
        for focus in live:
            if len(batch) >= max_items:
                break

            comparisons = select_conflict_candidates(
                config.db_path,
                focus,
                max_candidates=max_candidates,
                min_shared_keywords=config.conflict.min_shared_keywords,
            )
            if not comparisons:
                continue

            # Only include pairs not yet emitted this run (symmetric dedup)
            fresh = [c for c in comparisons if frozenset([focus["id"], c["id"]]) not in seen_pairs]
            if not fresh:
                continue

            # Stable identifier for this group
            all_ids = sorted([focus["id"]] + [c["id"] for c in fresh])
            check_hash = _cluster_hash(all_ids)

            if _already_proposed(conn, check_hash, "conflict"):
                log.debug("conflict: skipping already-proposed group hash=%s", check_hash)
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
    *,
    model_used: str = RESIDENT_PRODUCER,
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
        "unparsed": 0,
        "invalid": 0,
    }

    raw_for_store = response if _log_raw(config) else None
    decisions = parse_conflict_response(response)

    _ensure_system_session(config.db_path)

    conn = connect(config.db_path)
    try:
        # The focus and every comparison must be live rows an emit could have
        # offered (techne#1257); otherwise nothing is written, not even a marker.
        # Checked before the hash, which sorts the ids.
        if (why := invalid_target_ids(conn, [focus_id, *comparison_ids])) is not None:
            log.warning("conflict_ingest: refused ids %s: %s", [focus_id, *comparison_ids], why)
            counts["invalid"] += 1
            counts["reason"] = why
            return counts
        all_ids = sorted([focus_id] + list(comparison_ids))
        ch = _cluster_hash(all_ids)  # recompute from ids; caller-supplied hash is advisory
        if _already_proposed(conn, ch, "conflict"):
            counts["skipped"] += 1
            return counts

        # Load focus row and comparison rows for tiebreak logic
        focus_row = _fetch_row(conn, focus_id)
        comp_by_id = {cid: _fetch_row(conn, cid) for cid in comparison_ids}

        if not decisions and extract_json_array_or_none(response or "") is None:
            # NO ARRAY AT ALL IS NOT A JUDGEMENT (techne#1246). A refusal or a
            # truncated reply used to fall into the marker below and hide the group
            # forever. Write nothing, so the next emit offers it again.
            log.debug("conflict_ingest: unparseable answer for %s, not recorded", ch)
            counts["unparsed"] += 1
            return counts

        if not decisions:
            # A parsed array with no usable verdicts — record a discarded marker so
            # this group isn't re-proposed on every emit run.
            _insert_candidate(
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
                origin="conflict",
                model_used=model_used,
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
                    model_used=model_used,
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
                    model_used=model_used,
                )
                counts["update"] += 1

            elif verdict == "NOOP":
                _insert_candidate(
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
                    model_used=model_used,
                    origin="conflict",
                )
                counts["noop"] += 1

            elif verdict == "DISTINCT":
                _insert_candidate(
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
                    model_used=model_used,
                    origin="conflict",
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
    model_used: str,
) -> None:
    """Stage a RETIRE-LINK candidate: the older entry is retired into the newer one.

    Both entries are already live knowledge rows, so we do NOT mint a new revision —
    that would duplicate the survivor's fact into a second live row with a divergent
    decay clock (the integrity bug). Instead we stage an action='retire' candidate whose
    applier points older.superseded_by at the EXISTING survivor id (_apply_retire →
    _retire_in_conn), keeping the survivor's row/id/clock intact.

    Temporal tiebreak (event_at, then created_at fallback): the entry with the later
    event_at is the survivor (current fact); the earlier one is retired. This is decided
    in code, not by the LLM — the model only says "these contradict".

    target_ids = [older_id, survivor_id] (ORDER-SIGNIFICANT). The candidate's `content`
    carries the survivor's (optionally LLM-refined) text for operator display only — the
    applier links, it does not write content.
    """
    if focus_row is None:
        log.debug("conflict: SUPERSEDE skipped — focus row not found")
        return

    focus_newer = _is_newer(focus_row, comp_row)
    if focus_newer:
        # Focus is the current fact (survivor); comp is retired.
        older_id = comp_row["id"]
        survivor_id = focus_row["id"]
        survivor_row = focus_row
    else:
        # Comparison is the current fact (survivor); focus is retired.
        older_id = focus_row["id"]
        survivor_id = comp_row["id"]
        survivor_row = comp_row

    surviving_content = (suggested_content or "").strip() or (survivor_row.get("content") or "")
    surviving_type = survivor_row.get("type", "fact")
    surviving_confidence = survivor_row.get("confidence", "medium")

    log.debug(
        "conflict: staging RETIRE candidate (contradiction) — retire id=%s into survivor id=%s"
        " surviving='%.60s…'",
        older_id,
        survivor_id,
        surviving_content,
    )

    _insert_candidate(
        conn,
        action="retire",
        content=surviving_content,
        ktype=surviving_type,
        confidence=surviving_confidence,
        target_ids=[older_id, survivor_id],
        check_hash=check_hash,
        status="pending",
        decided_by=None,
        decision_reason=reason,
        raw=raw,
        origin="conflict",
        model_used=model_used,
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
    model_used: str,
) -> None:
    """Stage a RETIRE-LINK candidate for an UPDATE verdict: the focus (the augmenting
    NEW entry) is retired into the comparison (the canonical EXISTING entry it refines).

    UPDATE means the new entry augments the existing WITHOUT contradiction. Like
    SUPERSEDE, both rows are already live, so we link rather than mint — retiring the
    focus into comp collapses the redundant pair to the single canonical row.

    Direction is deliberately NOT event_at-ordered (unlike SUPERSEDE): a refinement folds
    the new entry into the established canonical one regardless of which is newer, so the
    canonical entry keeps its id and history. The LLM's suggested merged wording is kept
    in the candidate's `content` + `decision_reason` so the operator can optionally apply
    it as an edit on review — the applier itself only links (no mint, no content write).

    target_ids = [focus_id, comp_id] (ORDER-SIGNIFICANT: focus retired into comp).
    """
    if focus_row is None:
        log.debug("conflict: UPDATE skipped — focus row not found")
        return

    update_content = (suggested_content or "").strip() or (comp_row.get("content") or "")

    log.debug(
        "conflict: staging RETIRE candidate (update/refinement) — retire focus id=%s into"
        " canonical id=%s content='%.60s…'",
        focus_row["id"],
        comp_row["id"],
        update_content,
    )

    _insert_candidate(
        conn,
        action="retire",
        content=update_content,
        ktype=comp_row.get("type", "fact"),
        confidence=comp_row.get("confidence", "medium"),
        target_ids=[focus_row["id"], comp_row["id"]],
        check_hash=check_hash,
        status="pending",
        decided_by=None,
        decision_reason=f"update (refinement — apply merged wording as an edit if desired): "
        f"{reason or ''}",
        raw=raw,
        origin="conflict",
        model_used=model_used,
    )


def _log_raw(config: Config) -> bool:
    """Whether to store the raw LLM response (mirrors reflect.log_raw_response)."""
    cfg_conflict = getattr(config, "conflict", None)
    if cfg_conflict is not None and hasattr(cfg_conflict, "log_raw_response"):
        return cfg_conflict.log_raw_response
    return getattr(config.reflect, "log_raw_response", True)
