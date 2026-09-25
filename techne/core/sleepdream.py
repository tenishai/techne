"""Sleep/dream consolidation over already-promoted knowledge (techne#108).

reflect is add-only (observations → candidates → knowledge); over time near-duplicate
facts pile up. The **consolidate (sleep)** pass finds duplicate/overlapping live
knowledge, and the agent — using its own model (the #19 resident-model emit/ingest
pattern, no API key, no sampling) — proposes collapsing each cluster into one row.
Proposals land in `knowledge_candidates` (action='merge', origin='consolidate') for
review; promoting one runs `merge_into` (#108 PR1 dispatch). Nothing auto-mutates live
knowledge — reflect's safety contract.

The **dream (recombine)** pass (techne#108 PR B) surfaces NON-OBVIOUS connections
across already-promoted knowledge: given a group of related-but-distinct facts (shared
tag), the agent proposes a NEW derived insight (action='add', origin='dream',
confidence='low') if one genuinely emerges. Abstention-first: most groups yield nothing.
Proposals land in `knowledge_candidates` for human review — never auto-applied.

Selection is layered + degrade-safe:
  Tier 0 (always): exact `content_fingerprint(content)` dup buckets — copy-paste dups, free.
  Tier 1 (no fastembed needed): FTS self-similarity — each row's top neighbor.
  (Tier 2, vec dup-band over stored knowledge_vec embeddings, and an updated_at
   watermark to bound the scan on a large table, are scaling follow-ups — the table is
   small today and the prompt_hash anti-re-proposal guard below already prevents churn.)

The load-bearing safety against an anti-bloat feature bloating its OWN review queue:
every cluster has a stable `cluster_hash` (hash of its sorted target ids) stored in
`prompt_hash`. A cluster already proposed — pending, promoted, OR discarded — is skipped
(`_already_proposed`). A 'keep' verdict writes a *discarded* marker, so a cluster the
agent judged distinct is remembered and never re-judged. (Tier-1 pairs without a BM25
floor, so some weak pairs reach the agent once; the keep-memory bounds that to a single
judgement per pair — a BM25 floor to cut it at the source is a follow-up.)
"""

from __future__ import annotations

import hashlib
import json
import logging

from techne.config import Config

log = logging.getLogger(__name__)
from techne.core._resident import extract_json_object
from techne.core.database import connect
from techne.core.knowledge import VALID_CONFIDENCE, VALID_TYPES, content_fingerprint
from techne.core.redaction import redact_credentials
from techne.core.search import search_fts

# Sentinel session the session-less passes FK their candidates to (knowledge_candidates
# .source_session_id is NOT NULL). Additive: INSERT OR IGNORE, no migration.
SYSTEM_SLEEP_SESSION = "system:sleep"

MAX_ENTRY_CHARS = 300


#: Default producer label. A candidate written by a RESIDENT agent answering an emitted
#: prompt carries this; one written by a model-bound headless run carries that model's id.
#: THE COLUMN IS THE ONLY RECORD OF WHICH PRODUCER JUDGED THE ROW (techne#555). When the
#: automatic reflect rail failed for two months, `model_used` was the one field that could
#: have distinguished it from the resident route — hardcoding this literal for a second
#: producer would destroy that signal for every rail the custodian now drives.
RESIDENT_PRODUCER = "agent-assisted"


def _ensure_system_session(db_path) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, project_path, platform) VALUES (?, '', 'system')",
            (SYSTEM_SLEEP_SESSION,),
        )
        conn.commit()
    finally:
        conn.close()


def _cluster_hash(target_ids: list[int]) -> str:
    """Stable id for a cluster — hash of its SORTED member ids. Same members → same
    hash regardless of discovery order, so re-runs dedup against prior proposals."""
    joined = ",".join(str(i) for i in sorted(target_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def consolidate_clusters(
    config: Config, *, max_clusters: int = 20, fts_neighbors: int = 3
) -> list[dict]:
    """Find candidate duplicate/overlapping clusters of LIVE knowledge.

    Returns ``[{"target_ids": [...], "rows": [row dicts], "via": "hash"|"fts"}]`` —
    each a set of ≥2 live rows the agent should judge for consolidation. Bounded by
    *max_clusters*; deterministic for Tier 0, FTS-order-dependent for Tier 1.

    Excludes `status = 'archived'` (techne#1033 review round 2): a soft-archived row
    is out of the default working set, and consolidating one into a merged `current`
    row (merge_into) would resurrect it under a new id.
    """
    conn = connect(config.db_path)
    try:
        live = [
            dict(r)
            for r in conn.execute(
                "SELECT id, type, content, tags, confidence FROM knowledge "
                "WHERE superseded_by IS NULL AND status != 'archived'"
            )
        ]
    finally:
        conn.close()
    by_id = {r["id"]: r for r in live}

    clusters: list[dict] = []
    seen_keys: set[str] = set()

    def _add(ids: list[int], via: str) -> None:
        ids = sorted(set(ids))
        if len(ids) < 2:
            return
        key = _cluster_hash(ids)
        if key in seen_keys:
            return
        seen_keys.add(key)
        clusters.append({"target_ids": ids, "rows": [by_id[i] for i in ids], "via": via})

    # Tier 0 — exact content_fingerprint dup buckets (copy-paste dups; free, deterministic).
    buckets: dict[str, list[int]] = {}
    for r in live:
        buckets.setdefault(content_fingerprint(r["content"]), []).append(r["id"])
    for ids in buckets.values():
        if len(ids) >= 2:
            _add(ids, "hash")

    # Tier 1 — FTS self-similarity: pair each row with its strongest non-self neighbor.
    # The agent's keep/merge verdict is the precision backstop, so over-proposing a weak
    # pair is cheap; we just cap the count. Rows already in a Tier-0 cluster are skipped.
    clustered_ids = {i for c in clusters for i in c["target_ids"]}
    for r in live:
        if len(clusters) >= max_clusters:
            break
        if r["id"] in clustered_ids:
            continue
        hits = search_fts(config.db_path, r["content"], table="knowledge", limit=fts_neighbors + 1)
        neighbors = [
            h["id"]
            for h in hits
            if h.get("id") != r["id"] and h.get("id") in by_id and h["id"] not in clustered_ids
        ]
        if neighbors:
            pair = [r["id"], neighbors[0]]
            _add(pair, "fts")
            clustered_ids.update(pair)

    return clusters[:max_clusters]


def _already_proposed(conn, cluster_hash: str) -> bool:
    """This exact cluster already has a consolidate proposal on record — pending,
    promoted, OR discarded. Discarded counts: a human (or the agent via a 'keep'
    verdict) already judged this cluster, so re-proposing it would re-nag a decision
    that was already made. This is the load-bearing guard against the anti-bloat pass
    bloating its own review queue (techne#108)."""
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
                WHERE prompt_hash = ? AND origin = 'consolidate'
                  AND status IN ('pending', 'promoted', 'discarded') LIMIT 1""",
            (cluster_hash,),
        ).fetchone()
        is not None
    )


CONSOLIDATE_PROMPT = """These knowledge entries may be duplicates or overlapping.
Decide whether to collapse them into a single entry.

# Entries
{entries}

# Your task
Return ONLY a JSON object:
  {{
    "verdict": "merge" | "keep",
    "content": "<the single merged fact, one sentence>",   // required for merge
    "type": "{types}",                                      // required for merge
    "confidence": "low" | "medium" | "high",               // required for merge
    "reason": "<one line>"
  }}
- "merge": the entries say the same thing — collapse them into ONE. Write the clearest,
  most complete single statement as `content`; pick the strongest `confidence`.
- "keep": they are genuinely distinct — do nothing.
Be conservative: when unsure, "keep". Output the JSON object and nothing else.
"""


def _render_cluster(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        content = (r["content"] or "").replace("\n", " ")
        if len(content) > MAX_ENTRY_CHARS:
            content = content[:MAX_ENTRY_CHARS] + "…"
        lines.append(f"- (#{r['id']}, {r['type']}, {r['confidence']}) {content}")
    return "\n".join(lines)


def consolidate_emit(config: Config, *, max_clusters: int = 20) -> list[dict]:
    """Render one consolidation prompt per fresh cluster for the agent to answer with
    its own model. Skips clusters already proposed (anti-re-proposal). Returns
    ``[{"cluster_hash", "target_ids", "prompt"}]``; the agent answers each `prompt` and
    feeds it back via consolidate_ingest with the same cluster_hash + target_ids."""
    _ensure_system_session(config.db_path)
    clusters = consolidate_clusters(config, max_clusters=max_clusters)
    conn = connect(config.db_path)
    try:
        batch = []
        for c in clusters:
            ch = _cluster_hash(c["target_ids"])
            if _already_proposed(conn, ch):
                continue
            from techne.core.prompts import render_prompt

            prompt = render_prompt(
                config, "consolidate", CONSOLIDATE_PROMPT,
                entries=_render_cluster(c["rows"]), types=", ".join(VALID_TYPES),
            )
            batch.append({"cluster_hash": ch, "target_ids": c["target_ids"], "prompt": prompt})
    finally:
        conn.close()
    return batch


def parse_consolidation_response(raw: str) -> dict | None:
    """Parse the agent's JSON verdict object. Returns a normalized dict for a valid
    'merge', or None for 'keep'/unparseable/invalid (caller counts it as kept)."""
    obj = extract_json_object(raw or "")
    if not isinstance(obj, dict) or obj.get("verdict") != "merge":
        return None
    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    ktype = obj.get("type") if obj.get("type") in VALID_TYPES else "fact"
    conf = obj.get("confidence") if obj.get("confidence") in VALID_CONFIDENCE else "medium"
    return {"verdict": "merge", "content": content.strip(), "type": ktype, "confidence": conf}


def _insert_consolidation_candidate(
    conn,
    *,
    target_ids: list[int],
    cluster_hash: str,
    content: str,
    ktype: str,
    confidence: str,
    status: str,
    decided_by: str | None,
    decision_reason: str | None,
    raw: str | None,
    # REQUIRED, NO DEFAULT — see the note on conflict._insert_candidate. A defaulted
    # producer label lets a headless row claim a resident agent judged it, silently.
    model_used: str,
) -> None:
    conn.execute(
        """INSERT INTO knowledge_candidates
             (scope, type, content, tags, confidence, source_session_id,
              source_thread_id, model_used, prompt_hash, raw_response,
              action, target_ids, origin, status, decided_by, decision_reason)
           VALUES ('knowledge', ?, ?, '', ?, ?, NULL, ?, ?, ?,
                   'merge', ?, 'consolidate', ?, ?, ?)""",
        (
            ktype,
            redact_credentials(content),
            confidence,
            SYSTEM_SLEEP_SESSION,
            model_used,
            cluster_hash,
            redact_credentials(raw) if raw else None,
            json.dumps(sorted(target_ids)),
            status,
            decided_by,
            decision_reason,
        ),
    )


def consolidate_ingest(
    config: Config,
    target_ids: list[int],
    cluster_hash: str,
    response: str,
    *,
    model_used: str = RESIDENT_PRODUCER,
) -> dict:
    """Process one cluster's agent verdict.

    A 'merge' verdict writes a pending action='merge' candidate (origin='consolidate',
    target_ids set) for review. A 'keep' (or unparseable) verdict writes a *discarded*
    marker recording that the agent judged the cluster distinct — so the anti-re-proposal
    guard won't re-judge the same cluster on every future sleep run (techne#108 review).

    The cluster_hash is RECOMPUTED from target_ids — never trusted from the caller — so a
    stored prompt_hash always corresponds to its real target ids and can't silently defeat
    `_already_proposed`. Returns counts.
    """
    counts = {"proposed": 0, "written": 0, "kept": 0, "errors": 0, "skipped": 0}
    ch = _cluster_hash(target_ids)  # recompute; the passed cluster_hash is advisory only
    verdict = parse_consolidation_response(response)
    _ensure_system_session(config.db_path)
    raw_for_store = response if config.reflect.log_raw_response else None
    conn = connect(config.db_path)
    try:
        # Enforce the anti-re-proposal guard at INGEST too (techne#170). consolidate_emit
        # skips already-proposed clusters, but a direct ingest — or a duplicate ingest of
        # the same cluster — bypasses that and would re-add a candidate the agent/human
        # already judged. Re-check on the recomputed hash; no-op if already on record.
        if _already_proposed(conn, ch):
            counts["skipped"] += 1
            return counts
        if verdict is None:
            _insert_consolidation_candidate(
                conn,
                target_ids=target_ids,
                cluster_hash=ch,
                content="(kept — agent judged these entries distinct)",
                ktype="fact",
                confidence="low",
                status="discarded",
                decided_by="agent-triage",
                decision_reason="keep",
                raw=raw_for_store,
                model_used=model_used,
            )
            counts["kept"] += 1
        else:
            _insert_consolidation_candidate(
                conn,
                target_ids=target_ids,
                cluster_hash=ch,
                content=verdict["content"],
                ktype=verdict["type"],
                confidence=verdict["confidence"],
                status="pending",
                decided_by=None,
                decision_reason=None,
                raw=raw_for_store,
                model_used=model_used,
            )
            counts["proposed"] += 1
            counts["written"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception(
            "consolidate_ingest: unexpected error (cluster_hash=%s target_ids=%s)",
            cluster_hash,
            target_ids,
        )
        counts["errors"] += 1
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------------------
# Dream / recombine pass (techne#108 PR B)
# ---------------------------------------------------------------------------
# Surfaces NON-OBVIOUS connections across already-promoted knowledge.
# Selector: shared tag (deterministic, no fastembed). Abstention-first: the
# DREAM_PROMPT makes 'none' the easy default; noise control is paramount.
#
# Noise controls:
#   - Only high-confidence live rows (confidence='high', superseded_by IS NULL).
#   - Skip meta/identity rows (memory-type:user/feedback/project tags).
#   - Skip generic/giant tags (> min(60% of eligible rows, 3*group_size) rows).
#   - group_size cap (default 4) and max_groups cap (default 10).
#   - Anti-churn: group_hash (same _cluster_hash mechanic) stored under
#     origin='dream'; scoped separately from consolidate so the two passes
#     never cross-block each other.
#   - Forced LOW confidence on all dream candidates; never auto-promote.
# ---------------------------------------------------------------------------

_DREAM_SKIP_TAGS = frozenset({"memory-type:user", "memory-type:feedback", "memory-type:project"})


def _tag_has_any(tags: str | None, skip: frozenset) -> bool:
    """Return True if any comma-separated token in *tags* is in *skip*."""
    if not tags:
        return False
    return any(t.strip() in skip for t in tags.split(","))


def _already_proposed_dream(conn, group_hash: str) -> bool:
    """Dream group already has a proposal — pending, promoted, OR discarded.

    Sibling of _already_proposed scoped to origin='dream' so dream and consolidate
    proposals never cross-block each other (a group proposed by dream is NOT skipped
    by consolidate's check, and vice-versa — origin isolates the two passes).
    """
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
                WHERE prompt_hash = ? AND origin = 'dream'
                  AND status IN ('pending', 'promoted', 'discarded') LIMIT 1""",
            (group_hash,),
        ).fetchone()
        is not None
    )


DREAM_PROMPT = """Here are stored knowledge entries that share a common topic.
ONLY propose a new insight if together they clearly imply something GENUINELY NEW,
NON-OBVIOUS, and USEFUL — something that is NOT already stated by any single entry.

MOST groups will NOT yield a real connection. When in doubt, abstain.

# Entries
{entries}

# Your task
Respond ONLY with a JSON object:
  {{
    "verdict": "connect" | "none",
    "content": "<the new combined insight, one sentence>",  // required for connect
    "type": "{types}",                                       // required for connect
    "basis": "<one line: which facts combine and why>"      // required for connect
  }}
- "connect": only when the entries together — not any single one alone — imply a
  genuinely new insight worth storing. Write the clearest statement as `content`.
- "none": the entries are independently useful but do not imply anything new together.
Use "none" (and nothing else) unless you are confident the connection is real and
non-trivial. Output the JSON object and nothing else.
"""


def recombine_groups(config: Config, *, max_groups: int = 10, group_size: int = 4) -> list[dict]:
    """Select groups of related-but-distinct live knowledge for recombination.

    Selector: shared tag (deterministic, no fastembed). Returns
    ``[{"group_ids": sorted([...]), "rows": [row dicts], "via": "tag:<tag>"}]``.

    Noise controls applied here:
    - Only high-confidence live rows (superseded_by IS NULL, confidence='high',
      status != 'archived' — techne#1033 review round 2: a soft-archived row is out
      of the default working set, and recombining it feeds a retired fact back into
      a dream-derived candidate as though it were still current).
    - Skip meta/identity rows: tags containing memory-type:user, memory-type:feedback,
      or memory-type:project are excluded — recombining identity/feedback/project-state
      with factual knowledge produces noise, not insight.
    - Giant-tag guard: any tag shared by too many rows is too generic to signal a
      meaningful relationship. Threshold = max(group_size, min(60% of eligible rows,
      3*group_size)). The OR of the two conditions fires at the lower (stricter)
      threshold, but a floor of group_size prevents false positives on tiny corpora
      where the 60% rule would otherwise filter every shared tag.
    - group_size cap (up to 4 rows per group, most-recent first by updated_at DESC).
    - max_groups cap (default 10).
    - Dedup by sorted id-set; skip groups whose ids are a subset of a group already
      added (a strict-subset group adds no new cross-fact signal).
    """
    conn = connect(config.db_path)
    try:
        raw = conn.execute(
            "SELECT id, type, content, tags, confidence, updated_at FROM knowledge "
            "WHERE superseded_by IS NULL AND confidence='high' AND status != 'archived' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        rows_all = [dict(r) for r in raw]
    finally:
        conn.close()

    # Meta-tag exclusion.
    eligible = [r for r in rows_all if not _tag_has_any(r.get("tags"), _DREAM_SKIP_TAGS)]

    if not eligible:
        return []

    n_eligible = len(eligible)
    # Giant-tag guard: OR of two conditions (fire at the lower/stricter threshold),
    # but floored at group_size so tiny corpora (where 60% is < group_size) don't
    # have EVERY shared tag mislabeled as generic. Formula:
    #   max(group_size, min(0.6 * n_eligible, 3 * group_size))
    # On large corpora the 3*group_size absolute cap dominates; on medium corpora
    # the 60% fractional cap dominates; the floor prevents false positives when
    # n_eligible is very small.
    giant_threshold = max(group_size, min(0.6 * n_eligible, 3 * group_size))

    # Build per-tag row-id lists (rows already ordered most-recent first above).
    by_tag: dict[str, list[int]] = {}
    by_id: dict[int, dict] = {r["id"]: r for r in eligible}
    for r in eligible:
        for raw_tag in (r.get("tags") or "").split(","):
            tag = raw_tag.strip()
            if not tag:
                continue
            by_tag.setdefault(tag, []).append(r["id"])

    groups: list[dict] = []
    added_sets: list[frozenset] = []
    seen_hashes: set[str] = set()

    for tag, row_ids in by_tag.items():
        if len(groups) >= max_groups:
            break
        # Dedup ids preserving the most-recent-first order (a row may carry a
        # duplicated tag token, e.g. tags="foo,foo", which would otherwise inflate
        # the counts and yield a degenerate group with the same row twice).
        seen_ids: set[int] = set()
        uniq_ids = [i for i in row_ids if not (i in seen_ids or seen_ids.add(i))]
        if len(uniq_ids) < 2:
            continue
        # Giant-tag guard (on distinct rows).
        if len(uniq_ids) > giant_threshold:
            continue
        ids = sorted(uniq_ids[:group_size])
        id_set = frozenset(ids)
        key = _cluster_hash(ids)
        if key in seen_hashes:
            continue
        # Subset dedup: no new cross-fact signal if ids are a subset of something added.
        if any(id_set.issubset(s) for s in added_sets):
            continue
        seen_hashes.add(key)
        added_sets.append(id_set)
        groups.append(
            {
                "group_ids": ids,
                "rows": [by_id[i] for i in ids],
                "via": f"tag:{tag}",
            }
        )

    return groups


def dream_emit(config: Config, *, max_groups: int = 10) -> list[dict]:
    """Render one recombination prompt per fresh group for the agent to answer with its
    own model. Skips groups already proposed (anti-re-proposal, origin='dream'). Returns
    ``[{"group_hash", "group_ids", "prompt"}]``; the agent answers each `prompt` and
    feeds it back via dream_ingest with the same group_hash + group_ids."""
    _ensure_system_session(config.db_path)
    groups = recombine_groups(config, max_groups=max_groups)
    conn = connect(config.db_path)
    try:
        batch = []
        for g in groups:
            gh = _cluster_hash(g["group_ids"])
            if _already_proposed_dream(conn, gh):
                continue
            from techne.core.prompts import render_prompt

            prompt = render_prompt(
                config, "dream", DREAM_PROMPT,
                entries=_render_cluster(g["rows"]), types=", ".join(VALID_TYPES),
            )
            batch.append({"group_hash": gh, "group_ids": g["group_ids"], "prompt": prompt})
    finally:
        conn.close()
    return batch


def parse_dream_response(raw: str) -> dict | None:
    """Parse the agent's JSON dream verdict. Returns a normalized dict for a valid
    'connect' with non-empty content and type in VALID_TYPES, or None for
    'none'/unparseable/invalid (caller treats as abstain)."""
    obj = extract_json_object(raw or "")
    if not isinstance(obj, dict) or obj.get("verdict") != "connect":
        return None
    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    ktype = obj.get("type") if obj.get("type") in VALID_TYPES else "fact"
    return {"verdict": "connect", "content": content.strip(), "type": ktype}


def _insert_dream_candidate(
    conn,
    *,
    group_ids: list[int],
    group_hash: str,
    content: str,
    ktype: str,
    confidence: str,
    status: str,
    decided_by: str | None,
    decision_reason: str | None,
    raw: str | None,
    # REQUIRED, NO DEFAULT — see the note on conflict._insert_candidate. A defaulted
    # producer label lets a headless row claim a resident agent judged it, silently.
    model_used: str,
) -> None:
    """Insert a dream candidate row.

    Sibling of _insert_consolidation_candidate with origin='dream' and action='add'
    (a new derived fact — NOT a merge; the basis facts stay live). Written as a
    separate function rather than generalizing _insert_consolidation_candidate to avoid
    any risk of altering consolidate's hardcoded origin/action values.
    """
    conn.execute(
        """INSERT INTO knowledge_candidates
             (scope, type, content, tags, confidence, source_session_id,
              source_thread_id, model_used, prompt_hash, raw_response,
              action, target_ids, origin, status, decided_by, decision_reason)
           VALUES ('knowledge', ?, ?, '', ?, ?, NULL, ?, ?, ?,
                   'add', ?, 'dream', ?, ?, ?)""",
        (
            ktype,
            redact_credentials(content),
            confidence,
            SYSTEM_SLEEP_SESSION,
            model_used,
            group_hash,
            redact_credentials(raw) if raw else None,
            json.dumps(sorted(group_ids)),
            status,
            decided_by,
            decision_reason,
        ),
    )


def dream_ingest(
    config: Config,
    group_ids: list[int],
    group_hash: str,
    response: str,
    *,
    model_used: str = RESIDENT_PRODUCER,
) -> dict:
    """Process one group's agent dream verdict.

    A 'connect' verdict writes a PENDING action='add' candidate (origin='dream',
    confidence='low' forced — speculative by construction, never trust the model's
    confidence, target_ids=group_ids for provenance/audit) for human review.

    A 'none' (or unparseable) verdict writes a *discarded* keep-memory marker
    (origin='dream') so the group is never re-judged on future dream runs — same
    pattern as consolidate's 'keep' marker.

    The group_hash is RECOMPUTED from group_ids — never trusted from the caller — so a
    stored prompt_hash always corresponds to its real group ids and can't silently defeat
    _already_proposed_dream. Returns counts.
    """
    counts = {"proposed": 0, "written": 0, "kept": 0, "errors": 0, "skipped": 0}
    gh = _cluster_hash(group_ids)  # recompute; caller-supplied hash is advisory only
    verdict = parse_dream_response(response)
    _ensure_system_session(config.db_path)
    raw_for_store = response if config.reflect.log_raw_response else None
    conn = connect(config.db_path)
    try:
        # Anti-churn: enforce at ingest too (mirrors consolidate_ingest techne#170 fix).
        if _already_proposed_dream(conn, gh):
            counts["skipped"] += 1
            return counts
        if verdict is None:
            _insert_dream_candidate(
                conn,
                group_ids=group_ids,
                group_hash=gh,
                content="(no connection — agent found no non-obvious link)",
                ktype="fact",
                confidence="low",
                status="discarded",
                decided_by="agent-triage",
                decision_reason="none",
                raw=raw_for_store,
                model_used=model_used,
            )
            counts["kept"] += 1
        else:
            # Confidence FORCED to 'low' — dream candidates are speculative by
            # construction; the model's stated confidence is ignored.
            _insert_dream_candidate(
                conn,
                group_ids=group_ids,
                group_hash=gh,
                content=verdict["content"],
                ktype=verdict["type"],
                confidence="low",
                status="pending",
                decided_by=None,
                decision_reason=None,
                raw=raw_for_store,
                model_used=model_used,
            )
            counts["proposed"] += 1
            counts["written"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception(
            "dream_ingest: unexpected error (group_hash=%s group_ids=%s)",
            group_hash,
            group_ids,
        )
        counts["errors"] += 1
    finally:
        conn.close()
    return counts
