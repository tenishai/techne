"""Sleep/dream consolidation over already-promoted knowledge (techne#108).

reflect is add-only (observations → candidates → knowledge); over time near-duplicate
facts pile up. The **consolidate (sleep)** pass finds duplicate/overlapping live
knowledge, and the agent — using its own model (the #19 resident-model emit/ingest
pattern, no API key, no sampling) — proposes collapsing each cluster into one row.
Proposals land in `knowledge_candidates` (action='merge', origin='consolidate') for
review; promoting one runs `merge_into` (#108 PR1 dispatch). Nothing auto-mutates live
knowledge — reflect's safety contract.

This module is the consolidate pass. dream/recombine is the deferred PR3.

Selection is layered + degrade-safe:
  Tier 0 (always): exact `content_hash(content)` dup buckets — copy-paste dups, free.
  Tier 1 (no fastembed needed): FTS self-similarity — each row's top neighbor.
  (Tier 2, vec dup-band over stored knowledge_vec embeddings, and an updated_at
   watermark to bound the scan on a large table, are scaling follow-ups — the table is
   small today and the prompt_hash anti-re-proposal guard below already prevents churn.)

The load-bearing safety against an anti-bloat feature bloating its OWN review queue:
every cluster has a stable `cluster_hash` (hash of its sorted target ids) stored in
`prompt_hash`; a cluster that already has a pending/promoted consolidate candidate is
skipped (`_already_proposed`).
"""

from __future__ import annotations

import hashlib
import json

from techne.config import Config
from techne.core.database import connect
from techne.core.knowledge import VALID_CONFIDENCE, VALID_TYPES, content_hash
from techne.core.redaction import redact_credentials
from techne.core.search import search_fts

# Sentinel session the session-less passes FK their candidates to (knowledge_candidates
# .source_session_id is NOT NULL). Additive: INSERT OR IGNORE, no migration.
SYSTEM_SLEEP_SESSION = "system:sleep"

MAX_ENTRY_CHARS = 300


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
    """
    conn = connect(config.db_path)
    try:
        live = [
            dict(r)
            for r in conn.execute(
                "SELECT id, type, content, tags, confidence FROM knowledge "
                "WHERE superseded_by IS NULL"
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

    # Tier 0 — exact content_hash dup buckets (copy-paste dups; free, deterministic).
    buckets: dict[str, list[int]] = {}
    for r in live:
        buckets.setdefault(content_hash(r["content"]), []).append(r["id"])
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
            h["id"] for h in hits
            if h.get("id") != r["id"] and h.get("id") in by_id and h["id"] not in clustered_ids
        ]
        if neighbors:
            pair = [r["id"], neighbors[0]]
            _add(pair, "fts")
            clustered_ids.update(pair)

    return clusters[:max_clusters]


def _already_proposed(conn, cluster_hash: str) -> bool:
    """A consolidate proposal for this exact cluster is already pending/promoted."""
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
                WHERE prompt_hash = ? AND origin = 'consolidate'
                  AND status IN ('pending', 'promoted') LIMIT 1""",
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
            prompt = CONSOLIDATE_PROMPT.format(
                entries=_render_cluster(c["rows"]), types=", ".join(VALID_TYPES)
            )
            batch.append({"cluster_hash": ch, "target_ids": c["target_ids"], "prompt": prompt})
    finally:
        conn.close()
    return batch


def parse_consolidation_response(raw: str) -> dict | None:
    """Parse the agent's JSON verdict object. Returns a normalized dict for a valid
    'merge', or None for 'keep'/unparseable/invalid (caller counts it as kept)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(ln for ln in lines[1:] if not ln.startswith("```")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("verdict") != "merge":
        return None
    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    ktype = obj.get("type") if obj.get("type") in VALID_TYPES else "fact"
    conf = obj.get("confidence") if obj.get("confidence") in VALID_CONFIDENCE else "medium"
    return {"verdict": "merge", "content": content.strip(), "type": ktype, "confidence": conf}


def consolidate_ingest(
    config: Config, target_ids: list[int], cluster_hash: str, response: str
) -> dict:
    """Process one cluster's agent verdict into a candidate. A 'merge' verdict writes an
    action='merge' candidate (origin='consolidate', target_ids set) for review; a 'keep'
    (or unparseable) verdict writes nothing. Returns counts."""
    counts = {"proposed": 0, "written": 0, "kept": 0, "errors": 0}
    verdict = parse_consolidation_response(response)
    if verdict is None:
        counts["kept"] += 1
        return counts
    _ensure_system_session(config.db_path)
    raw_for_store = response if config.reflect.log_raw_response else None
    conn = connect(config.db_path)
    try:
        conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, tags, confidence, source_session_id,
                  source_thread_id, model_used, prompt_hash, raw_response,
                  action, target_ids, origin)
               VALUES ('knowledge', ?, ?, '', ?, ?, NULL, 'agent-assisted', ?, ?,
                       'merge', ?, 'consolidate')""",
            (
                verdict["type"],
                redact_credentials(verdict["content"]),
                verdict["confidence"],
                SYSTEM_SLEEP_SESSION,
                cluster_hash,
                redact_credentials(raw_for_store) if raw_for_store else None,
                json.dumps(sorted(target_ids)),
            ),
        )
        conn.commit()
        counts["proposed"] += 1
        counts["written"] += 1
    except Exception:
        counts["errors"] += 1
    finally:
        conn.close()
    return counts
