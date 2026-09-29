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


def _resolve_conn_db_path(raw: str) -> Path:
    """Resolve a federation connection's db_path to an absolute Path (techne#332).

    A relative db_path is resolved against ``techne_home()`` — mirroring how the
    primary db_path/data_dir already default there — so a *committed* config.yaml
    can carry a portable relative path (e.g. ``db_path: team-mirror.db``) and each
    machine resolves it locally, no per-machine templating (the pain surfaced
    wiring the prudence read-back, tenishai/agents-archive#2). Absolute paths and ``~``
    are unchanged. Resolution happens here at open time, not at config load, so
    the stored spec keeps the portable string for save round-trips.
    """
    from techne.config import techne_home

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = techne_home() / path
    return path


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
# Schema-compat check (techne#253)
# ---------------------------------------------------------------------------

# Minimum columns required in the remote's ``knowledge`` table for the FTS + rerank
# path to produce correct results.  _fts_match_rows SELECTs all of these; the
# event_at enrichment in search_knowledge_conn also needs it.  A missing column
# would raise OperationalError at query time (caught by the existing degrade guard),
# but checking BEFORE the query makes the degrade reason explicit and prevents the
# "silently wrong ranking" case where a drifted schema causes a partially-populated
# row to be returned without error — e.g. a renamed column that SQLite silently
# returns as NULL instead of raising.
_REQUIRED_KNOWLEDGE_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "type",
        "content",
        "tags",
        "confidence",
        "confidence_numeric",
        "last_confirmed_at",
        "status",
        "source",
        "updated_at",
        "superseded_by",
        "event_at",
    }
)


def _check_remote_schema_compat(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Check whether a remote db has a compatible knowledge schema (techne#253).

    Compat rule (two gates, both must pass):
      1. ``knowledge_fts`` virtual table exists — without it no FTS search is
         possible and the remote is useless to the recall path.
      2. ``knowledge`` table contains every column in _REQUIRED_KNOWLEDGE_COLUMNS —
         a missing/renamed column means the FTS JOIN SELECT would either fail or
         silently return NULL for that field, causing mis-ranking with no error.

    Returns ``(True, "")`` when compat, or ``(False, reason_str)`` on failure.
    The caller should log a warning using ``reason_str`` and skip the remote.

    Never raises — any unexpected exception is caught and returned as a failure
    reason so the caller's degrade path is always taken on error.
    """
    try:
        fts_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        if not fts_exists:
            return False, "missing knowledge_fts table (remote schema too old or not a techne db)"

        col_rows = conn.execute("PRAGMA table_info(knowledge)").fetchall()
        if not col_rows:
            return False, "knowledge table absent or PRAGMA returned no rows"

        # PRAGMA table_info row shape: (cid, name, type, notnull, dflt_value, pk)
        present_cols = {row[1] for row in col_rows}
        missing = _REQUIRED_KNOWLEDGE_COLUMNS - present_cols
        if missing:
            return False, f"knowledge table missing required columns: {sorted(missing)}"

        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"schema-compat check failed with unexpected error: {exc}"


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

    Delegates to ``search.search_knowledge_conn`` — the stable public entrypoint
    (techne#253) — so federation never reaches into search.py private helpers.
    ``search_knowledge_conn`` handles AND/OR-fallback FTS, event_at enrichment
    (so we no longer hand-backfill it here), and rerank.

    table='knowledge' only — threads/observations are private raw capture and
    never federate (arch §2).

    The caller must have already passed _check_remote_schema_compat; this function
    does not re-check.
    """
    from techne.core.search import search_knowledge_conn

    return search_knowledge_conn(
        remote_conn,
        query,
        limit,
        half_life_days=half_life_days,
        recency_weight=recency_weight,
        bm25_weight=bm25_weight,
        include_event_at=True,
    )


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
    *,
    local_results: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fan out recall to local + team/static stores.

    Returns (local_results, remote_results) where every row is tagged with:
      _store: connection name ("local" for local rows)
      _role:  "local" | "team" | "static"

    Local uses the full hybrid search (all tables). Remotes use knowledge FTS only.
    Each remote connection is opened read-only and closed after the query.
    Failed remote opens and schema-incompatible remotes are logged and skipped
    (a degraded local-only result is better than a hard failure).

    ``local_results`` (techne#254): when the caller has already computed the local
    search (e.g. mcp_server._techne_recall which runs it standalone first so it
    survives any remote failure), pass it here to avoid the 2× local search cost.
    When ``None`` (the default), local search is computed internally — unchanged
    behaviour for any caller that doesn't hold a precomputed set.

    Rows in the provided ``local_results`` must already be tagged with
    ``_store="local"`` and ``_role="local"`` — _techne_recall does this before
    passing them in.
    """
    # Local — all tables, include_archived=False (same as existing recall).
    # When the caller passes a precomputed set we skip the search entirely.
    if local_results is None:
        from techne.core.search import search

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
        db_path = _resolve_conn_db_path(spec.db_path)
        try:
            remote_conn = _open_remote_readonly(db_path)
        except Exception:
            log.warning("federation: failed to open remote %r at %s", conn_name, db_path)
            continue

        # Schema-compat check (techne#253): verify the remote has the knowledge
        # columns we need BEFORE running the query.  The existing query-block
        # except already catches "no such table: knowledge_fts" (lazy ?mode=ro
        # open), but doesn't catch drifted columns — a renamed or missing column
        # in an older remote would either raise OperationalError or, worse, return
        # NULL values that silently mis-rank results.  By checking explicitly we
        # emit a descriptive warning early and always degrade gracefully.
        compat, reason = _check_remote_schema_compat(remote_conn)
        if not compat:
            log.warning(
                "federation: skipping remote %r at %s — incompatible schema: %s",
                conn_name,
                db_path,
                reason,
            )
            remote_conn.close()
            continue

        # The QUERY block is guarded independently of the OPEN: ?mode=ro opens
        # LAZILY — it succeeds on any existing file and only fails at first
        # execute().  A corrupt db, a store locked beyond busy_timeout, or a
        # live-WAL store the RO reader can't init shared memory for
        # (SQLITE_READONLY_CANTINIT) all raise HERE, not at open.  Without this
        # except, one bad remote would propagate out of the whole recall and
        # discard the already-computed local results.  Degraded local-only beats
        # a hard failure — so we log + skip the bad remote and keep going.
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
        r for r in remote_results if content_fingerprint(r.get("content") or "") not in local_fps
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
# Cross-store contradiction detection (step C)
# ---------------------------------------------------------------------------


def detect_cross_store_contradictions(
    local_db: Path,
    local_results: list[dict],  # noqa: ARG001 — kept for API symmetry / future use
    remote_deduped: list[dict],
    max_candidates: int = 20,
    min_shared_keywords: int = 1,
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
    from techne.core.conflict import (
        _already_proposed,
        run_conflict_check,
        select_conflict_candidates,
    )
    from techne.core.database import connect as _connect
    from techne.core.knowledge import content_fingerprint

    conflicts: list[dict] = []
    conn = _connect(local_db)
    try:
        for r in remote_deduped:
            if r.get("source_table") != "knowledge":
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
                local_db,
                r_proxy,
                max_candidates=max_candidates,
                min_shared_keywords=min_shared_keywords,
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

            if _already_proposed(conn, check_hash, "federation-conflict"):
                log.debug("federation: skipping already-proposed conflict hash=%s", check_hash)
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
            log.debug("federation: conflict detected %s vs local_ids=%s", namespaced_id, local_ids)
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

    AND ROWS FROM EVERY STORE ARE RERANKED TOGETHER, THEN OR ROWS FILL WHAT'S
    LEFT (techne#1079 review Important 1). A single pooled `_rerank` call over
    `local_results + remote_deduped` recreates the #1081 bug one hop further
    out: `search_fts` and `search_knowledge_conn` already keep their own AND
    and OR rows in separate blocks, tagged `_match_pass`, precisely so a short
    OR row's BM25+recency can't outscore a long exact AND row — but pooling
    everyone's rows back into one list here and reranking ONCE erases that
    separation, since `_rerank` re-normalises BM25 per `source_table` across
    the reunited AND+OR rows. Measured (Prudence's probe): a stale long
    exact-match row and a fresh short partial-match row, correctly ordered
    `[and, or]` by both `search_knowledge_conn` and `search()`, came out
    `[or, and]` through this function. Fixed the same way as the two
    producers: rerank every store's AND-tagged rows as one block, then fill
    the remaining slots with every store's OR-tagged (or untagged) rows as a
    second block, and concatenate AND-first.

    A ROW WITHOUT `_match_pass` — a local vec-only match (never went through
    the FTS AND/OR split at all: `search()`'s vec-first fusion builds most
    page rows from `search_vec`, not `search_fts`, and copies only `rank`
    across an fts+vec overlap, not this tag), or a remote on a version that
    predates this tag — is treated as OR-tier, same as an explicit `"or"`.
    Precedence here means "confirmed to match every query term, lexically";
    a vector-only match carries no such confirmation, so it gets no more
    benefit of the doubt than a partial lexical match does. This never drops
    a row, only orders it: an untagged row still competes for, and can win,
    an OR-tier slot on its own recency/BM25/decay score.
    """
    from techne.core.search import _rerank

    and_rows = [r for r in local_results if r.get("_match_pass") == "and"]
    and_rows += [r for r in remote_deduped if r.get("_match_pass") == "and"]
    or_rows = [r for r in local_results if r.get("_match_pass") != "and"]
    or_rows += [r for r in remote_deduped if r.get("_match_pass") != "and"]

    and_ranked = _rerank(
        and_rows, limit, recency_weight, bm25_weight, half_life_days=half_life_days
    )
    remaining = limit - len(and_ranked)
    or_ranked = (
        _rerank(or_rows, remaining, recency_weight, bm25_weight, half_life_days=half_life_days)
        if remaining > 0 and or_rows
        else []
    )
    return and_ranked + or_ranked


# ---------------------------------------------------------------------------
# Federation conflict ingest (step E — the only writer, LOCAL db only)
# ---------------------------------------------------------------------------


#: Producer label for a candidate written by a RESIDENT agent (techne#555). ONE shared
#: definition — `model_used` is the only record of which producer judged a row, so a
#: second copy of the literal here would drift from the rails' and re-open the exact
#: blindness that let the automatic reflect rail fail unnoticed for two months.
from techne.core.sleepdream import RESIDENT_PRODUCER  # noqa: E402


def federation_conflict_ingest(
    config,  # techne.config.Config — typed loosely to avoid Config import in module
    focus_content: str,
    focus_store: str,
    focus_event_at: str | None,
    comparison_ids: list[int],
    check_hash: str,  # advisory; recomputed from content+ids
    response: str,
    *,
    model_used: str = RESIDENT_PRODUCER,
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
    from techne.core.conflict import (
        _already_proposed,
        _effective_event_at,
        _fetch_row,
        _insert_candidate,
        parse_conflict_response,
    )
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
        if _already_proposed(conn, ch, "federation-conflict"):
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
            _insert_candidate(
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
                origin="federation-conflict",
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
                # team newer → stage pending 'supersede' targeting the LOCAL row.
                # local newer → stage discarded informational marker; no push to team.
                #
                # Normalize the 'T' ISO separator to a space before comparing: agent-
                # supplied event_at may be ISO-8601 ("…T…") while SQLite stores dates
                # with a space separator ("… …").  'T' (0x54) > ' ' (0x20), so without
                # normalization a same-date ISO timestamp compares as GREATER than its
                # space-separated equivalent, flipping the tiebreak. (techne#245)
                team_effective = (focus_event_at or "").replace("T", " ")
                local_effective = _effective_event_at(comp_row).replace("T", " ")
                team_newer = team_effective >= local_effective

                if team_newer:
                    surviving_content = (suggested_content or "").strip() or focus_content
                    decision_reason = (
                        f"federation-conflict: team store ({focus_store}) has newer fact. "
                        f"Foreign content: {focus_content[:200]}. Reason: {reason or ''}"
                    )
                    _insert_candidate(
                        conn,
                        # supersede — not retire — because the foreign survivor has no
                        # local id. _apply_supersede mints a new local row from
                        # surviving_content (the team's newer fact) and sets
                        # superseded_by on comp_id. _apply_retire requires two LOCAL
                        # ids [old, survivor] and would ValueError with only one.
                        # (techne#245)
                        action="supersede",
                        content=surviving_content,
                        ktype=comp_row.get("type", "fact"),
                        confidence=comp_row.get("confidence", "medium"),
                        target_ids=[comp_id],  # LOCAL id only
                        check_hash=ch,
                        status="pending",
                        decided_by=None,
                        decision_reason=decision_reason,
                        raw=response,
                        origin="federation-conflict",
                        model_used=model_used,
                    )
                    log.debug(
                        "federation_ingest: staged pending SUPERSEDE for local id=%s"
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
                    _insert_candidate(
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
                        origin="federation-conflict",
                        model_used=model_used,
                    )
                    log.debug(
                        "federation_ingest: discarded (local newer, team=%s stale)", focus_store
                    )
                counts["supersede"] += 1

            elif verdict == "NOOP":
                _insert_candidate(
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
                    origin="federation-conflict",
                    model_used=model_used,
                )
                counts["noop"] += 1

            elif verdict == "DISTINCT":
                _insert_candidate(
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
                    origin="federation-conflict",
                    model_used=model_used,
                )
                counts["distinct"] += 1

            # UPDATE verdict: not routed for federation — we don't push merged content
            # to the team store. Log it as a discarded informational marker.
            elif verdict == "UPDATE":
                _insert_candidate(
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
                    origin="federation-conflict",
                    model_used=model_used,
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
