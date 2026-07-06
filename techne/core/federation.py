"""Federation read-merge — fan-out recall over local + team/static stores (techne#222).

Design: docs/proposals/federation-read-merge.md

Pure functions (dicts + Path, no Config coupling in core logic — same discipline as
conflict.py). Five steps per recall:

  A — Fan out: local via search(); each remote via _search_remote_knowledge() (knowledge
      layer ONLY — raw/threads/observations are private and never federate).
  B — Content-hash dedup: drop remote rows that are exact-copy of local rows.
  C — Cross-store contradiction detection: for each remaining remote knowledge row,
      find entity-overlapping LOCAL rows via select_conflict_candidates (id=None proxy),
      build conflict prompts via run_conflict_check. Anti-re-proposal guard against local db.
  D — Merge + rank: pool + _rerank (per-type BM25 normalisation handles cross-store, techne#56).
  E — The ingest tool writes LOCAL db only (never foreign); all candidates pending.

Sovereignty invariants (§4 in the proposal):
  1. Remotes opened ?mode=ro — writes physically impossible.
  2. recall is read-only — this module has no write path; federation_conflict_ingest is
     the ONLY writer and it writes the local db only.
  3. No auto-apply — all federation-conflict candidates are status='pending'.
  4. Raw never federates — remote reads use table='knowledge' only.
  5. Opt-in — no connections → local-only unchanged (gate lives in mcp_server._techne_recall).
  6. techne stands alone — no cabal/agents-repo import in this path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only remote open (sovereignty guard)
# ---------------------------------------------------------------------------


def _open_remote_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a remote techne.db read-only (sovereignty guard, techne#222 §3-A).

    Uses the SQLite URI ?mode=ro flag so writes to the database CONTENT are
    physically impossible — any INSERT/UPDATE/DELETE raises OperationalError:
    attempt to write a readonly database. PRAGMA busy_timeout=3000 so a writer on
    the team store doesn't immediately SQLITE_BUSY-fail a recall.

    **Do NOT** use techne.core.database.connect() for remote opens:
    connect() calls _ensure_initialized() which creates WAL + runs schema
    migrations, writing schema into the foreign directory. ?mode=ro is the guard.

    SIDECAR CAVEAT (empirically verified — the proposal's "?mode=ro prevents WAL-file
    creation" is imprecise): reading a WAL-mode store under ?mode=ro DOES materialize
    transient `<db>-wal` and `<db>-shm` coordination files in the foreign directory,
    and (because a read-only connection cannot checkpoint) they persist after close.
    What ?mode=ro guarantees is that the team's MEMORY — the db content + schema — is
    never modified (verified byte-identical before/after in tests). The -wal/-shm are
    SQLite's required read-coordination mechanism for a WAL database; they carry no
    memory content. Eliminating them entirely needs immutable=1, but immutable asserts
    the file will not change while open — UNSAFE for a live team store being written
    concurrently (the reader could see a torn/stale snapshot), which defeats the whole
    reason the proposal chose WAL (concurrent readers + writer). So we accept the
    transient sidecars as the price of consistent live reads. If the foreign dir is a
    truly read-only mount, the open/read raises SQLITE_READONLY_CANTINIT — handled by
    the caller's degrade-to-local-only guard in fan_out_search.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Remote FTS search (knowledge layer only)
# ---------------------------------------------------------------------------


def _search_remote_knowledge(
    remote_conn: sqlite3.Connection,
    query: str,
    limit: int,
    recency_weight: float,
    bm25_weight: float,
    half_life_days: float,
) -> list[dict]:
    """FTS over a remote's knowledge layer using a pre-opened read-only connection.

    Uses _fts_match_rows + _rerank directly (without calling search_fts) so we
    never call database.connect() on the remote — that would write WAL + schema
    into the foreign dir (see _open_remote_readonly docstring).

    table='knowledge' only — threads/observations are private raw capture and
    never federate (arch §2).
    """
    from techne.core.search import _build_or_fts_query, _fts_match_rows, _rerank, prepare_fts_query

    fts_query = prepare_fts_query(query)
    if fts_query is None:
        return []

    candidate_limit = limit * 2  # expansion_ratio=2
    results = _fts_match_rows(
        remote_conn, fts_query, "knowledge", candidate_limit, include_archived=False
    )

    # #70: OR fallback when AND-pass returns nothing
    if not results:
        or_query = _build_or_fts_query(query)
        if or_query:
            results = _fts_match_rows(
                remote_conn, or_query, "knowledge", candidate_limit, include_archived=False
            )

    # Enrich with the real event_at. The shared _fts_match_rows knowledge SELECT omits
    # event_at (adding it there would change recency ranking for the LOCAL path too —
    # out of scope), so knowledge rows would otherwise fall back to updated_at for the
    # temporal tiebreak. The proposal's "later event_at wins" is load-bearing for the
    # cross-store contradiction verdict, so we backfill event_at scoped to the remote
    # (foreign) path only via one extra read-only query. IDs are trusted (from our own
    # FTS result, never user input) so the interpolated IN-list is safe.
    if results:
        ids = [r["id"] for r in results]
        placeholders = ",".join("?" for _ in ids)
        event_by_id = {
            row["id"]: row["event_at"]
            for row in remote_conn.execute(
                f"SELECT id, event_at FROM knowledge WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        for r in results:
            r["event_at"] = event_by_id.get(r["id"])

    return _rerank(results, limit, recency_weight, bm25_weight, half_life_days=half_life_days)


# ---------------------------------------------------------------------------
# Fan-out (step A)
# ---------------------------------------------------------------------------


def fan_out_search(
    local_db: Path,
    connections: dict,  # str → FederationConnectionSpec
    query: str,
    limit: int,
    half_life_days: float,
    recency_weight: float = 0.3,
    bm25_weight: float = 0.7,
) -> tuple[list[dict], list[dict]]:
    """Fan out recall to local + team/static stores.

    Returns (local_results, remote_results) where every row is tagged with:
      _store: connection name ("local" for local rows)
      _role:  "local" | "team" | "static"

    Local uses the full hybrid search (all tables). Remotes use knowledge FTS only.
    Each remote connection is opened read-only and closed after the query.
    Failed remote opens are logged and skipped (a degraded local-only result is
    better than a hard failure).
    """
    from techne.core.search import search

    # Local — all tables, include_archived=False (same as existing recall)
    local_results = search(
        local_db,
        query,
        table="all",
        limit=limit,
        half_life_days=half_life_days,
        include_archived=False,
    )
    for r in local_results:
        r["_store"] = "local"
        r["_role"] = "local"

    remote_results: list[dict] = []
    for conn_name, spec in connections.items():
        db_path = Path(spec.db_path).expanduser()
        try:
            remote_conn = _open_remote_readonly(db_path)
        except Exception:
            log.warning("federation: failed to open remote %r at %s", conn_name, db_path)
            continue
        # The QUERY block is guarded independently of the OPEN: ?mode=ro opens
        # LAZILY — it succeeds on any existing file and only fails at first
        # execute(). A wrong-schema remote (older techne with no knowledge_fts →
        # "no such table"), a corrupt db, a store locked beyond busy_timeout, or a
        # live-WAL store the RO reader can't init shared memory for
        # (SQLITE_READONLY_CANTINIT) all raise HERE, not at open. Without this
        # except, one bad remote would propagate out of the whole recall and
        # discard the already-computed local results. Degraded local-only beats a
        # hard failure — so we log + skip the bad remote and keep going.
        try:
            rows = _search_remote_knowledge(
                remote_conn,
                query,
                limit,
                recency_weight=recency_weight,
                bm25_weight=bm25_weight,
                half_life_days=half_life_days,
            )
            for r in rows:
                r["_store"] = conn_name
                r["_role"] = spec.role
            remote_results.extend(rows)
            log.debug("federation: %s returned %d knowledge row(s)", conn_name, len(rows))
        except Exception:
            log.warning(
                "federation: query failed on remote %r at %s — skipping (local-only degrade)",
                conn_name,
                db_path,
            )
            continue
        finally:
            remote_conn.close()

    return local_results, remote_results


# ---------------------------------------------------------------------------
# Content-hash dedup (step B)
# ---------------------------------------------------------------------------


def _cross_store_dedup(
    local_results: list[dict],
    remote_results: list[dict],
) -> list[dict]:
    """Drop remote rows whose content fingerprint exactly matches a local row.

    True duplicates are caught here before the contradiction check. This keeps
    identical content out of the LLM pass (dedup ≠ contradiction — coherence-
    layer §4b¶1). Local rows are the authority; exact-match remote rows are
    redundant and dropped.
    """
    from techne.core.knowledge import content_fingerprint

    local_fps = {content_fingerprint(r.get("content") or "") for r in local_results}
    return [
        r
        for r in remote_results
        if content_fingerprint(r.get("content") or "") not in local_fps
    ]


# ---------------------------------------------------------------------------
# Cross-store hash helper
# ---------------------------------------------------------------------------


def _cluster_hash_cross(remote_fp: str, local_ids: list[int]) -> str:
    """Stable hash for a cross-store conflict group.

    Keyed on (content_fingerprint(remote_content), sorted_local_ids) so the
    same foreign content vs the same local rows always maps to the same hash
    regardless of discovery order. Different from _cluster_hash (which keys on
    local ids only) — the remote content fingerprint ensures a changed remote
    fact reopens the conflict even if local ids are unchanged.
    """
    joined = remote_fp + ":" + ",".join(str(i) for i in sorted(local_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Anti-re-proposal guard (federation-conflict origin)
# ---------------------------------------------------------------------------


def _already_proposed_federation_conflict(conn, check_hash: str) -> bool:
    """True if this cross-store conflict group already has a record in the local db.

    Mirrors _already_proposed_conflict from conflict.py but uses
    origin='federation-conflict' so regular conflict proposals don't interfere.
    """
    return (
        conn.execute(
            """SELECT 1 FROM knowledge_candidates
               WHERE prompt_hash = ? AND origin = 'federation-conflict'
                 AND status IN ('pending', 'promoted', 'discarded') LIMIT 1""",
            (check_hash,),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Cross-store contradiction detection (step C)
# ---------------------------------------------------------------------------


def detect_cross_store_contradictions(
    local_db: Path,
    local_results: list[dict],  # noqa: ARG001 — kept for API symmetry / future use
    remote_deduped: list[dict],
    max_candidates: int = 20,
) -> list[dict]:
    """For each deduped remote knowledge row, find contradicting LOCAL knowledge rows.

    Adaptation of the local conflict emit path for the federation call site:
      - id=None proxy: the remote row has no local id, so select_conflict_candidates
        uses id=-1 (effectively a no-op exclusion) to avoid excluding it from the
        candidate set. This is the ONLY adaptation; the function signature is unchanged.
      - Namespaced focus_id: "<conn_name>:<remote_row_id>" (never looked up locally).
      - check_hash keyed on (content_fingerprint(remote_content), sorted_local_ids)
        so the guard key changes if either the remote content or local candidates change.
      - Anti-re-proposal guard against the LOCAL db (origin='federation-conflict').

    Returns a list of conflict-prompt dicts ready for the agent to answer:
      {
        "focus_id":       str,        # namespaced id e.g. "team-archivist:42"
        "focus_store":    str,        # connection name
        "focus_content":  str,        # full remote content (for ingest)
        "focus_event_at": str|None,   # remote row's event_at (for tiebreak)
        "comparison_ids": list[int],  # LOCAL ids only
        "check_hash":     str,
        "prompt":         str,        # conflict prompt for agent to answer
      }
    """
    from techne.core.conflict import run_conflict_check, select_conflict_candidates
    from techne.core.database import connect as _connect
    from techne.core.knowledge import content_fingerprint

    conflicts: list[dict] = []
    conn = _connect(local_db)
    try:
        for r in remote_deduped:
            if r.get("type") != "knowledge":
                # Only knowledge rows can contradict local knowledge
                continue

            # Build a proxy with id=None so select_conflict_candidates doesn't
            # exclude it by id. knowledge_type holds the actual type (fact/decision/…)
            # while "type" in search results is "knowledge" (the table name).
            r_proxy = {
                "id": None,
                "type": r.get("knowledge_type") or "fact",
                "content": r.get("content") or "",
                "tags": r.get("tags") or "",
                "event_at": r.get("event_at") or r.get("updated_at"),
                "created_at": r.get("updated_at"),
            }

            local_matches = select_conflict_candidates(
                local_db, r_proxy, max_candidates=max_candidates
            )
            if not local_matches:
                continue

            store_name = r.get("_store", "remote")
            foreign_id = r.get("id")
            namespaced_id = f"{store_name}:{foreign_id}"
            # NOTE (MVP, LOW): local_ids come from select_conflict_candidates(...)[:max],
            # a truncated list. At scale the truncation set can vary between recalls (a
            # different overlapping row falls inside the cap), so the same foreign-vs-local
            # PAIR could hash differently and re-surface once. Acceptable at MVP scale;
            # a stabler key (e.g. per-pair rather than per-group) is a fast-follow.
            local_ids = sorted(m["id"] for m in local_matches)

            remote_fp = content_fingerprint(r_proxy["content"])
            check_hash = _cluster_hash_cross(remote_fp, local_ids)

            if _already_proposed_federation_conflict(conn, check_hash):
                log.debug(
                    "federation: skipping already-proposed conflict hash=%s", check_hash
                )
                continue

            # Build prompt with namespaced focus_id for display; comparison_set is local
            focus_for_prompt = dict(r_proxy)
            focus_for_prompt["id"] = namespaced_id
            prompt = run_conflict_check(focus_for_prompt, local_matches)

            conflicts.append(
                {
                    "focus_id": namespaced_id,
                    "focus_store": store_name,
                    "focus_content": r_proxy["content"],
                    "focus_event_at": r_proxy["event_at"],
                    "comparison_ids": local_ids,
                    "check_hash": check_hash,
                    "prompt": prompt,
                }
            )
            log.debug(
                "federation: conflict detected %s vs local_ids=%s", namespaced_id, local_ids
            )
    finally:
        conn.close()

    return conflicts


# ---------------------------------------------------------------------------
# Merge + rank (step D)
# ---------------------------------------------------------------------------


def merge_and_rank(
    local_results: list[dict],
    remote_deduped: list[dict],
    limit: int,
    recency_weight: float,
    bm25_weight: float,
    half_life_days: float,
) -> list[dict]:
    """Pool local + deduped remote results and re-rank.

    _rerank's per-type BM25 normalisation (#56) puts local and remote knowledge
    rows into ONE min-max bucket so their (negative) BM25 magnitudes become
    linearly combinable with recency/decay — without it the raw cross-store BM25
    spread would swamp the other signals.

    CAVEAT (LOW, ranking-quality only — NOT the same as #56): #56 normalised
    cross-*table* rows drawn from ONE corpus. Here the two sets come from TWO
    INDEPENDENT corpora (local + remote) with independent IDF / doc-length stats.
    Min-maxing them into a single bucket means a systematically-weaker remote
    corpus gets compressed toward the low end of the combined range — remote rows
    can be under-ranked relative to a from-scratch cross-corpus normalisation.
    This affects ordering only (never inclusion/exclusion), and true cross-corpus
    BM25 calibration is out of MVP scope. Do NOT invent ad-hoc normalisation here.
    """
    from techne.core.search import _rerank

    pooled = list(local_results) + list(remote_deduped)
    return _rerank(pooled, limit, recency_weight, bm25_weight, half_life_days=half_life_days)


# ---------------------------------------------------------------------------
# Candidate insertion helper (federation-conflict origin)
# ---------------------------------------------------------------------------


def _insert_federation_candidate(
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
    """Insert a federation-conflict candidate into the LOCAL knowledge_candidates.

    Mirrors _insert_conflict_candidate from conflict.py but uses
    origin='federation-conflict' so the two candidate types are distinguishable
    and their anti-re-proposal guards don't interfere.

    target_ids = LOCAL ids only (no foreign namespaced ids — promote_candidate
    must never try to look up a foreign id in the local db).

    decision_reason is ALSO run through redact_credentials: the team-newer and
    local-newer branches interpolate the FOREIGN fact's content into it for the
    operator's display, so a credential in the foreign fact would otherwise be
    scrubbed from `content` but persisted verbatim in `decision_reason`. Redact
    at this one chokepoint so every branch is covered.
    """
    from techne.core.redaction import redact_credentials
    from techne.core.sleepdream import SYSTEM_SLEEP_SESSION

    conn.execute(
        """INSERT INTO knowledge_candidates
             (scope, type, content, tags, confidence, source_session_id,
              source_thread_id, model_used, prompt_hash, raw_response,
              action, target_ids, origin, status, decided_by, decision_reason)
           VALUES ('knowledge', ?, ?, '', ?, ?, NULL, 'agent-assisted', ?, ?,
                   ?, ?, 'federation-conflict', ?, ?, ?)""",
        (
            ktype,
            redact_credentials(content),
            confidence,
            SYSTEM_SLEEP_SESSION,
            check_hash,
            redact_credentials(raw) if raw else None,
            action,
            json.dumps(list(target_ids)),
            status,
            decided_by,
            redact_credentials(decision_reason) if decision_reason else decision_reason,
        ),
    )


# ---------------------------------------------------------------------------
# Federation conflict ingest (step E — the only writer, LOCAL db only)
# ---------------------------------------------------------------------------


def federation_conflict_ingest(
    config,  # techne.config.Config — typed loosely to avoid Config import in module
    focus_content: str,
    focus_store: str,
    focus_event_at: str | None,
    comparison_ids: list[int],
    check_hash: str,  # advisory; recomputed from content+ids
    response: str,
) -> dict:
    """Process the agent's verdict on a cross-store contradiction.

    Adapts conflict_check_ingest for the federation call site:
      - The focus row is passed as content+event_at dict (never _fetch_row'd from
        local db — the foreign namespaced id has no local row to look up).
      - Temporal tiebreak via event_at: team newer → pending 'retire' candidate
        targeting the LOCAL row; local newer (team stale) → discarded informational
        marker, NO push to team store.
      - origin='federation-conflict'; target_ids = LOCAL ids only.
      - All candidates status='pending' — never auto-apply.
      - check_hash is RECOMPUTED from content+ids (same defence as conflict_check_ingest).

    Writes the LOCAL db only. The team store is never touched.
    """
    from techne.core.conflict import _effective_event_at, _fetch_row, parse_conflict_response
    from techne.core.database import connect
    from techne.core.knowledge import content_fingerprint
    from techne.core.sleepdream import _ensure_system_session

    counts: dict[str, int] = {
        "supersede": 0,
        "update": 0,
        "noop": 0,
        "distinct": 0,
        "skipped": 0,
        "errors": 0,
    }

    # Recompute check_hash from content + sorted local ids; never trust the caller.
    remote_fp = content_fingerprint(focus_content)
    local_ids = sorted(comparison_ids)
    ch = _cluster_hash_cross(remote_fp, local_ids)

    _ensure_system_session(config.db_path)

    conn = connect(config.db_path)
    try:
        if _already_proposed_federation_conflict(conn, ch):
            counts["skipped"] += 1
            return counts

        decisions = parse_conflict_response(response)

        # The id-namespace gotcha guard: the focus (foreign) row is used ONLY via the
        # passed focus_content / focus_event_at args — we NEVER _fetch_row the namespaced
        # id ("team-archivist:42") from the local db, because it has no local row. Only
        # the LOCAL comparison rows are fetched here.
        comp_by_id = {cid: _fetch_row(conn, cid) for cid in local_ids}

        if not decisions:
            # No parseable verdicts — record discarded marker so we don't re-nag.
            _insert_federation_candidate(
                conn,
                action="add",
                content="(no conflict — agent found no actionable verdict)",
                ktype="fact",
                confidence="low",
                target_ids=local_ids,
                check_hash=ch,
                status="discarded",
                decided_by="agent-triage",
                decision_reason="no-verdicts",
                raw=None,
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
                log.debug(
                    "federation_ingest: comparison id=%s not found (may be superseded)", comp_id
                )
                continue

            log.debug(
                "federation_ingest: verdict=%s focus=%s vs local_id=%s",
                verdict,
                focus_store,
                comp_id,
            )

            if verdict == "SUPERSEDE":
                # Temporal tiebreak: compare team's event_at with local row's event_at.
                # team newer → stage pending 'retire' targeting the LOCAL row.
                # local newer → stage discarded informational marker; no push to team.
                team_effective = focus_event_at or ""
                local_effective = _effective_event_at(comp_row)
                team_newer = team_effective >= local_effective

                if team_newer:
                    surviving_content = (suggested_content or "").strip() or focus_content
                    decision_reason = (
                        f"federation-conflict: team store ({focus_store}) has newer fact. "
                        f"Foreign content: {focus_content[:200]}. Reason: {reason or ''}"
                    )
                    _insert_federation_candidate(
                        conn,
                        action="retire",
                        content=surviving_content,
                        ktype=comp_row.get("type", "fact"),
                        confidence=comp_row.get("confidence", "medium"),
                        target_ids=[comp_id],  # LOCAL id only
                        check_hash=ch,
                        status="pending",
                        decided_by=None,
                        decision_reason=decision_reason,
                        raw=response,
                    )
                    log.debug(
                        "federation_ingest: staged pending RETIRE for local id=%s"
                        " (team newer, store=%s)",
                        comp_id,
                        focus_store,
                    )
                else:
                    # Local is newer — team is stale. Record discarded marker; no team write.
                    decision_reason = (
                        f"federation-conflict: local store is newer than team ({focus_store}). "
                        f"Team fact discarded (no push to foreign store). Reason: {reason or ''}"
                    )
                    _insert_federation_candidate(
                        conn,
                        action="add",
                        content=f"(local newer than team {focus_store} — team is stale, no action)",
                        ktype=comp_row.get("type", "fact"),
                        confidence="low",
                        target_ids=[comp_id],
                        check_hash=ch,
                        status="discarded",
                        decided_by="agent-triage",
                        decision_reason=decision_reason,
                        raw=response,
                    )
                    log.debug(
                        "federation_ingest: discarded (local newer, team=%s stale)", focus_store
                    )
                counts["supersede"] += 1

            elif verdict == "NOOP":
                _insert_federation_candidate(
                    conn,
                    action="add",
                    content=f"(noop — cross-store check: {focus_store} already known locally)",
                    ktype=comp_row.get("type", "fact"),
                    confidence=comp_row.get("confidence", "medium"),
                    target_ids=[comp_id],
                    check_hash=ch,
                    status="discarded",
                    decided_by="agent-triage",
                    decision_reason=f"noop: {reason or ''}",
                    raw=response,
                )
                counts["noop"] += 1

            elif verdict == "DISTINCT":
                _insert_federation_candidate(
                    conn,
                    action="add",
                    content=f"(distinct — {focus_store} and local id={comp_id} differ in topic)",
                    ktype=comp_row.get("type", "fact"),
                    confidence="low",
                    target_ids=[comp_id],
                    check_hash=ch,
                    status="discarded",
                    decided_by="agent-triage",
                    decision_reason=f"distinct: {reason or ''}",
                    raw=response,
                )
                counts["distinct"] += 1

            # UPDATE verdict: not routed for federation — we don't push merged content
            # to the team store. Log it as a discarded informational marker.
            elif verdict == "UPDATE":
                _insert_federation_candidate(
                    conn,
                    action="add",
                    content=f"(update — federation defers cross-store writes; "
                    f"consider storing locally: {(suggested_content or '')[:200]})",
                    ktype=comp_row.get("type", "fact"),
                    confidence=comp_row.get("confidence", "medium"),
                    target_ids=[comp_id],
                    check_hash=ch,
                    status="discarded",
                    decided_by="agent-triage",
                    decision_reason=f"update deferred (federation read-only path): {reason or ''}",
                    raw=response,
                )
                counts["update"] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        counts["errors"] += 1
        log.exception("federation_conflict_ingest: unexpected error")
    finally:
        conn.close()

    return counts
