"""Knowledge store — durable facts, decisions, patterns, evaluations, procedures."""

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from techne.core.database import connect, like_escape
from techne.core.redaction import redact_credentials

VALID_TYPES = (
    "fact",
    "decision",
    "evaluation",
    "procedure",
    # Documents — third-party authoritative content mirrored into techne
    # by the Librarian persona (cabal#5). Tags carry source-specific
    # metadata (e.g. `source:confluence,kind:runbook`).
    "document",
    # Document card (Librarian Option D, techne#91) — a compact GIST + pointer to
    # a row in the `documents` table; full content is fetched on demand, never
    # injected. Pull-only (recall-able, never auto-injected). tags carry
    # doc:/ver:/backing:/pending.
    "document_card",
)
VALID_CONFIDENCE = ("high", "medium", "low")

# Team-domain types that must NOT be written to personal techne.db (techne#137).
# `pattern` lives in the Architect store (architect.db); writing it here leaked team
# patterns into personal recall (recall doesn't filter by type). store() rejects
# these with a pointer to the right store, so the boundary is a write-time invariant,
# not something a periodic cleanup has to mop up after.
REDIRECTED_TYPES = {
    "pattern": "the Architect pattern store (architect.db) — use `techne architect ingest`",
}

# Types that are reference material — recall-able (pull), but never auto-injected
# into session-start context (push). egress filters these out. `pattern` stays
# listed so any legacy rows on not-yet-cleaned installs are still pull-only.
PULL_ONLY_TYPES = ("pattern", "document", "document_card")

# Mapping the legacy text enum to canonical numeric values (0..1).
# The migration in core/database.py uses the same values for backfill.
CONFIDENCE_NUMERIC = {"high": 0.9, "medium": 0.6, "low": 0.3}

# Default half-life for confidence decay (days). Operators can override per
# call site; agents reading via recall use config.knowledge_confidence_half_life_days.
DEFAULT_CONFIDENCE_HALF_LIFE_DAYS = 30.0

# Down-rank multiplier applied to an `uncertain` entry's effective confidence
# (techne#117 recall gate). An uncertain row is live and still surfaced, but a
# dependency it rests on was superseded — so it must rank below an otherwise-
# equal `current` row until reconfirmed. 0.5 halves its weight (one decay
# half-life's worth), enough to sink it without erasing it.
UNCERTAIN_CONFIDENCE_PENALTY = 0.5


def store(
    db_path: Path,
    content: str,
    type: str = "fact",
    tags: str = "",
    confidence: str = "medium",
    source: str = "session",
    anchor: str | None = None,
) -> int:
    """Store a knowledge entry. Returns the new entry's id.

    Writes both the legacy `confidence` text enum (for display + backward
    compat) and the canonical `confidence_numeric` derived from it. The
    `last_confirmed_at` defaults to CURRENT_TIMESTAMP so a fresh entry
    starts at full strength.

    `anchor` (techne#90) optionally pins the memory to evidence in a repo. Pass
    a path, or "path::symbol" to also record the symbol. When given, the file's
    sha256 + the git HEAD sha are captured so recall can grade staleness. If the
    path doesn't resolve to a file in a git repo, the anchor_path/symbol are
    still recorded but the hash/sha stay NULL → recall reports `unknown` (off-git
    or portable host) rather than falsely asserting freshness. Omitting `anchor`
    leaves all anchor columns NULL and behaves exactly as before — non-anchored
    memory is never staleness-checked.
    """
    if type in REDIRECTED_TYPES:
        raise ValueError(
            f"knowledge type '{type}' is not stored in personal memory (techne#137); "
            f"it belongs in {REDIRECTED_TYPES[type]}"
        )
    if type not in VALID_TYPES:
        raise ValueError(f"Invalid knowledge type '{type}'. Must be one of: {VALID_TYPES}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence '{confidence}'. Must be one of: {VALID_CONFIDENCE}")

    anchor_path: str | None = None
    anchor_symbol: str | None = None
    anchor_content_hash: str | None = None
    anchor_git_sha: str | None = None
    if anchor:
        from techne.core.anchor import resolve_anchor

        anchor_path, _, symbol = anchor.partition("::")
        anchor_path = anchor_path or None
        anchor_symbol = symbol or None
        if anchor_path:
            resolved = resolve_anchor(anchor_path)
            if resolved is not None:
                anchor_content_hash = resolved["content_hash"]
                anchor_git_sha = resolved["git_sha"]

    numeric = CONFIDENCE_NUMERIC[confidence]
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO knowledge
                 (type, content, source, tags, confidence, confidence_numeric,
                  anchor_path, anchor_symbol, content_hash, anchor_git_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                type,
                redact_credentials(content),
                source,
                tags,
                confidence,
                numeric,
                anchor_path,
                anchor_symbol,
                anchor_content_hash,
                anchor_git_sha,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def supersede(db_path: Path, old_id: int, new_content: str, source: str = "session") -> int:
    """Replace an existing knowledge entry with updated content.

    Marks the old entry as superseded and creates a new one inheriting the old
    entry's type, tags, and confidence — including `confidence_numeric` (the value
    that actually drives ranking/decay), which must stay in sync with the text
    enum (techne#131; a prior version dropped it, silently demoting every
    superseded fact to the 0.6 default). `last_confirmed_at` is intentionally NOT
    carried: superseding is a fresh confirmation, so the decay clock resets to now.

    An evidence anchor (techne#90) is carried forward to the new revision, but its
    hash/sha are RE-RESOLVED against the current file state (mirroring store()) —
    the new content is a fresh snapshot of the same anchored evidence, not an
    inheritance of the old, now-stale snapshot. Off-git/missing → NULL → `unknown`.
    """
    conn = connect(db_path)
    try:
        # Take the write lock before the liveness read (read→write TOCTOU; matches
        # curated.ingest_memory_file's guard, techne#108 staff review). The promote
        # dispatch uses _supersede_in_conn directly under its own held lock, so this
        # only guards direct public callers.
        conn.execute("BEGIN IMMEDIATE")
        new_id = _supersede_in_conn(conn, old_id, new_content, source)
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _supersede_in_conn(conn, old_id: int, new_content: str, source: str) -> int:
    """Core of supersede() on a caller-owned connection — no commit/close — so it can
    run inside another transaction (e.g. promote_candidate's action dispatch, #108).
    Body is verbatim from the public supersede(); the caller owns the transaction."""
    old = conn.execute(
        "SELECT type, tags, confidence, anchor_path, anchor_symbol, superseded_by "
        "FROM knowledge WHERE id = ?",
        (old_id,),
    ).fetchone()
    if old is None:
        raise ValueError(f"Knowledge entry {old_id} not found")
    if old["superseded_by"] is not None:
        raise ValueError(
            f"Knowledge entry {old_id} is already superseded by {old['superseded_by']}; "
            "supersede the latest revision instead"
        )

    # Re-derive numeric from the enum so the two can't drift (self-heals any
    # row a pre-#131 supersede left inconsistent). Fall back to medium if the
    # stored enum is somehow unknown.
    numeric = CONFIDENCE_NUMERIC.get(old["confidence"], CONFIDENCE_NUMERIC["medium"])

    # Re-resolve the carried anchor (if any) against the current working tree.
    # `anchor_content_hash` is the anchored FILE's hash (→ the content_hash column),
    # distinct from the content_fingerprint() of the knowledge text.
    anchor_content_hash: str | None = None
    anchor_git_sha: str | None = None
    if old["anchor_path"]:
        from techne.core.anchor import resolve_anchor

        resolved = resolve_anchor(old["anchor_path"])
        if resolved is not None:
            anchor_content_hash = resolved["content_hash"]
            anchor_git_sha = resolved["git_sha"]

    cur = conn.execute(
        """INSERT INTO knowledge
             (type, content, source, tags, confidence, confidence_numeric,
              anchor_path, anchor_symbol, content_hash, anchor_git_sha)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            old["type"],
            redact_credentials(new_content),
            source,
            old["tags"],
            old["confidence"],
            numeric,
            old["anchor_path"],
            old["anchor_symbol"],
            anchor_content_hash,
            anchor_git_sha,
        ),
    )
    new_id = cur.lastrowid

    # Atomic liveness guard (techne#390): `AND superseded_by IS NULL` so a
    # concurrent supersede that already retired old_id — between our liveness read
    # above and this write, which the Python check can't cover for _in_conn callers
    # not holding BEGIN IMMEDIATE — can't ALSO succeed and leave two live 'current'
    # rows. rowcount 0 = we lost the race: raise so the caller's transaction rolls
    # back the revision we just inserted (no orphan) rather than silently no-op.
    cur = conn.execute(
        "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND superseded_by IS NULL",
        (new_id, old_id),
    )
    if cur.rowcount == 0:
        won = conn.execute(
            "SELECT superseded_by FROM knowledge WHERE id = ?", (old_id,)
        ).fetchone()
        won_by = won["superseded_by"] if won else None
        raise ValueError(
            f"Knowledge entry {old_id} was concurrently superseded"
            + (f" by {won_by}" if won_by else "")
            + "; supersede the latest revision instead"
        )
    # Dependency cascade (techne#117): a fact that depended on the now-superseded
    # entry can no longer be trusted at face value — flip live dependents to
    # `uncertain`, atomically in this transaction. The replacement value is NOT
    # auto-propagated; a dependent stays uncertain until reconfirm()ed. supersede
    # is a deliberate update, so this is off the hot ingest path.
    _cascade_uncertain(conn, old_id)
    return new_id


def _retire_in_conn(conn, old_id: int, survivor_id: int) -> int:
    """Retire *old_id* by pointing its `superseded_by` at an EXISTING live *survivor_id*
    row — without minting a new revision (techne#213 contradiction detection).

    Unlike `_supersede_in_conn`, which mints a fresh row from *new_content*, retire is
    used when BOTH the loser and the survivor already exist as live `knowledge` rows
    (the two entries a contradiction check compared). Minting here would duplicate the
    survivor's fact into a second live row with an independent confidence/decay clock
    that can drift — the integrity bug this helper avoids. The survivor keeps its own
    row, id, and clock untouched; we only repoint the loser at it.

    Bookkeeping parity with `_supersede_in_conn` MINUS the mint: sets
    `superseded_by` + `updated_at` on *old_id* and runs the #117 dependency cascade
    (live dependents of *old_id* → `uncertain`). It does NOT repoint knowledge_deps
    edges — neither does `_supersede_in_conn`: per `cascade_uncertain`, an edge
    references a specific revision and is re-pointed by re-extraction, not auto-migrated.
    It does NOT touch the survivor's `last_confirmed_at` (retire is not a reaffirmation
    of the survivor; that would be a separate deliberate act).

    Preserve-not-delete: *old_id* is retained (retrievable with include_superseded),
    never hard-deleted. Returns *survivor_id* (the id that stays live).
    """
    if old_id == survivor_id:
        raise ValueError("cannot retire a knowledge entry into itself")
    old = conn.execute(
        "SELECT superseded_by FROM knowledge WHERE id = ?", (old_id,)
    ).fetchone()
    if old is None:
        raise ValueError(f"Knowledge entry {old_id} not found")
    if old["superseded_by"] is not None:
        raise ValueError(
            f"Knowledge entry {old_id} is already superseded by {old['superseded_by']}; "
            "retire the latest revision instead"
        )
    survivor = conn.execute(
        "SELECT superseded_by FROM knowledge WHERE id = ?", (survivor_id,)
    ).fetchone()
    if survivor is None:
        raise ValueError(f"Survivor knowledge entry {survivor_id} not found")
    if survivor["superseded_by"] is not None:
        raise ValueError(
            f"Survivor {survivor_id} is itself superseded by {survivor['superseded_by']}; "
            "retire into a live entry"
        )
    # Atomic liveness guard (techne#390), mirroring _supersede_in_conn: the Python
    # check above is a read→write TOCTOU for callers not under BEGIN IMMEDIATE, so
    # gate the write on `AND superseded_by IS NULL` too. rowcount 0 = a concurrent
    # supersede/retire already won → raise (caller rolls back) rather than
    # double-write.
    cur = conn.execute(
        "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND superseded_by IS NULL",
        (survivor_id, old_id),
    )
    if cur.rowcount == 0:
        won = conn.execute(
            "SELECT superseded_by FROM knowledge WHERE id = ?", (old_id,)
        ).fetchone()
        won_by = won["superseded_by"] if won else None
        raise ValueError(
            f"Knowledge entry {old_id} was concurrently superseded"
            + (f" by {won_by}" if won_by else "")
            + "; retire the latest revision instead"
        )
    # #117 cascade — identical to _supersede_in_conn: live dependents of the retired
    # entry go `uncertain` until reconfirmed. The survivor is NOT auto-propagated.
    _cascade_uncertain(conn, old_id)
    return survivor_id


def merge_into(
    db_path: Path,
    old_ids: list[int],
    merged_content: str,
    *,
    type: str,
    confidence: str,
    tags: str | None = None,
    source: str = "consolidate",
) -> int:
    """Collapse N live knowledge rows into one merged row (techne#108 consolidate).

    Mints ONE new row carrying the *explicitly chosen* type/confidence/tags (the olds
    may differ — the proposal decides, we never silently pick), then points every old
    at it via `superseded_by` (N→1 is a plain per-row FK; legal and indexed). Each old
    runs the #117 cascade (its dependents go `uncertain`).

    Reversible: `UPDATE knowledge SET superseded_by=NULL WHERE superseded_by=new_id`
    then delete the merged row — the N back-pointers ARE the merge provenance, so no
    extra table. Raises if any old is missing or already superseded (no merging a
    chain). Does NOT add a knowledge_deps edge merged→olds: the olds are superseded,
    so the cascade would immediately flip the merged row to uncertain.
    """
    if not old_ids:
        raise ValueError("merge_into requires at least one old id")
    if type not in VALID_TYPES:
        raise ValueError(f"merge_into: invalid type {type!r}")
    numeric = CONFIDENCE_NUMERIC.get(confidence, CONFIDENCE_NUMERIC["medium"])
    conn = connect(db_path)
    try:
        # Write lock before the liveness reads (TOCTOU guard, matches supersede above).
        conn.execute("BEGIN IMMEDIATE")
        new_id = _merge_in_conn(
            conn,
            old_ids,
            merged_content,
            ktype=type,
            confidence=confidence,
            numeric=numeric,
            tags=tags,
            source=source,
        )
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _merge_in_conn(
    conn,
    old_ids: list[int],
    merged_content: str,
    *,
    ktype: str,
    confidence: str,
    numeric: float,
    tags: str | None,
    source: str,
) -> int:
    """Core of merge_into() on a caller-owned connection (no commit/close), for the
    promote dispatch (#108). Asserts every old is live, mints the merged row, repoints
    the olds, cascades each. Caller owns the transaction."""
    seen: set[int] = set()
    for oid in old_ids:
        if oid in seen:
            raise ValueError(f"merge_into: duplicate old id {oid}")
        seen.add(oid)
        row = conn.execute("SELECT superseded_by FROM knowledge WHERE id = ?", (oid,)).fetchone()
        if row is None:
            raise ValueError(f"merge_into: knowledge entry {oid} not found")
        if row["superseded_by"] is not None:
            raise ValueError(
                f"merge_into: entry {oid} is already superseded — merge live rows only"
            )

    cur = conn.execute(
        """INSERT INTO knowledge (type, content, source, tags, confidence, confidence_numeric)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ktype, redact_credentials(merged_content), source, tags or "", confidence, numeric),
    )
    new_id = cur.lastrowid
    for oid in old_ids:
        conn.execute(
            "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_id, oid),
        )
        _cascade_uncertain(conn, oid)
    return new_id


# --- dependency-driven staleness (techne#117) -------------------------------


def _add_dep_in_conn(
    conn, dependent_id: int, trigger_id: int, kind: str = "derived_from"
) -> None:
    """Write one dependency edge inside a caller-owned transaction (no commit/close).

    Idempotent (INSERT OR IGNORE). The sole mechanism for writing dep edges — called
    only from promote_candidate's dream-promote dispatch (_apply_add in reflect.py)
    so the edge write stays in the same atomic transaction as the knowledge row insert
    (techne#215). Self-edges are silently skipped (dependent_id == trigger_id).

    Design decision (techne#251, 2026-07-06): dependency edges are created only on the
    dream-promote path. Supersession is replacement, not derivation, so no dep-edge is
    written on the conflict/supersede path. The former public add_dependency() API was
    removed as dead code (zero non-test callers); revisit if agent-declared dependencies
    become a concrete need.
    """
    if dependent_id == trigger_id:
        return  # no self-edges; silent skip mirrors INSERT OR IGNORE spirit
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_deps (dependent_id, trigger_id, kind) "
        "VALUES (?, ?, ?)",
        (dependent_id, trigger_id, kind),
    )


def _cascade_uncertain(conn, trigger_id: int) -> int:
    """Flip live (non-superseded, currently-current) dependents of *trigger_id* to
    `uncertain`. Operates on a caller-owned connection so supersede() can run it in
    its own transaction. Returns the count changed."""
    cur = conn.execute(
        """UPDATE knowledge SET status = 'uncertain', updated_at = CURRENT_TIMESTAMP
           WHERE superseded_by IS NULL AND status = 'current'
             AND id IN (SELECT dependent_id FROM knowledge_deps WHERE trigger_id = ?)""",
        (trigger_id,),
    )
    return cur.rowcount


def cascade_uncertain(db_path: Path, trigger_id: int) -> int:
    """Standalone/reconcile entry point for the dependency cascade (techne#117) —
    mark live dependents of *trigger_id* `uncertain`. supersede() runs the same
    cascade inline when it supersedes an entry. Returns the count flipped.

    Two deliberate Stage-1 boundaries (a junction table, not a graph engine):
      * ONE-HOP: flipping a dependent to `uncertain` is not itself a supersession,
        so it does not re-fire — if A depends on B depends on C, superseding C marks
        B, not A. Transitive propagation is out of scope.
      * Edges reference a specific revision id and do NOT auto-migrate across a
        supersession chain; re-extraction re-points them at the current revision.
    """
    conn = connect(db_path)
    try:
        n = _cascade_uncertain(conn, trigger_id)
        conn.commit()
        return n
    finally:
        conn.close()


def reconfirm(db_path: Path, knowledge_id: int) -> bool:
    """Clear an `uncertain` flag — the dependent was re-verified (techne#117). Sets
    status back to `current` and bumps last_confirmed_at. Returns True if a row
    changed (i.e. it was uncertain)."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge SET status = 'current',
                 last_confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'uncertain'""",
            (knowledge_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _uncertainty_payload() -> dict:
    """Build the uncertainty marker attached to a recalled `uncertain` row.

    Deliberately mirrors anchor.py's `_freshness_payload` shape (machine-readable
    `state` + human `tag` + `note`) so a consumer renders staleness and
    dependency-uncertainty through one code path rather than two vocabularies.
    """
    return {
        "state": "uncertain",
        "tag": "[uncertain — a fact this rests on changed; reconfirm before trusting]",
        "note": (
            "a dependency was superseded (techne#117); this entry is untrustworthy "
            "until reconfirmed"
        ),
    }


def annotate_uncertain(db_path, rows: Iterable[dict]) -> list[dict]:
    """Attach an `uncertainty` payload to recalled knowledge rows whose status is
    `uncertain` (techne#117 cascade-to-uncertain, Stage-2 recall gate).

    The dependency cascade (Stage 1) flips a derived row to `uncertain` when its
    basis is superseded; the row stays live (``superseded_by IS NULL``) so search
    still returns it. This annotates that subset so the surface never presents an
    uncertain row as confidently current — the recall-side mirror of #90's
    `annotate_freshness`.

    Scope guarantees (mirrors annotate_freshness):
      - Only knowledge rows with status='uncertain' are touched; `current` rows
        get no `uncertainty` field. No row is ever dropped — recall annotates.
      - The `uncertainty` key is distinct from `freshness`, so a row that is both
        anchor-stale and dependency-uncertain carries both, independently.
      - Surfacing-path only; never fires on touch/relevance updates.

    Mutates and returns *rows*.
    """
    from techne.core.database import connect

    rows = list(rows)
    knowledge_ids = [
        r["id"] for r in rows if r.get("type") == "knowledge" and r.get("id") is not None
    ]
    if not knowledge_ids:
        return rows

    placeholders = ",".join("?" * len(knowledge_ids))
    conn = connect(db_path)
    try:
        uncertain_ids = {
            row["id"]
            for row in conn.execute(
                f"""SELECT id FROM knowledge
                    WHERE id IN ({placeholders}) AND status = 'uncertain'""",
                knowledge_ids,
            )
        }
    finally:
        conn.close()

    if not uncertain_ids:
        return rows

    for r in rows:
        if r.get("type") == "knowledge" and r.get("id") in uncertain_ids:
            r["uncertainty"] = _uncertainty_payload()
    return rows


def list_entries(
    db_path: Path,
    type: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    include_superseded: bool = False,
) -> list[dict]:
    """List knowledge entries with optional filters."""
    conn = connect(db_path)
    try:
        query = (
            "SELECT id, type, content, tags, confidence, source, created_at, updated_at "
            "FROM knowledge WHERE 1=1"
        )
        params: list = []

        if not include_superseded:
            query += " AND superseded_by IS NULL"
        if type:
            query += " AND type = ?"
            params.append(type)
        if tags:
            for tag in tags.split(","):
                query += " AND tags LIKE ? ESCAPE '\\'"
                params.append(f"%{like_escape(tag.strip())}%")

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get(db_path: Path, entry_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT id, type, content, tags, confidence, source,
                      superseded_by, created_at, updated_at
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete(db_path: Path, entry_id: int) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM knowledge WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def content_fingerprint(content: str) -> str:
    """Stable hash of knowledge CONTENT — used for dedup + version compare.

    Renamed from ``content_hash`` (techne#self-review): that collided with the
    ``content_hash`` *column*, which stores the anchored FILE's hash (a different
    thing) — see anchor.py. The two are now lexically distinct.
    """
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---- Decay-aware confidence ----------------------------------------------


def decay_score(
    confidence_numeric: float,
    last_confirmed_at: "str | datetime | None",
    half_life_days: float,
    status: "str | None" = None,
    *,
    now: "datetime | None" = None,
) -> float:
    """Pure decay formula — no DB access.

    Formula: ``confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)``

    When ``half_life_days <= 0`` or ``last_confirmed_at`` is absent/unparseable,
    returns ``confidence_numeric`` directly (decay disabled; no timestamp → treat
    as fresh). The ``UNCERTAIN_CONFIDENCE_PENALTY`` (×0.5) is applied afterwards
    when ``status == 'uncertain'``, regardless of whether time decay was computed
    — it is a trust penalty, not a decay penalty (techne#117).

    Args:
        confidence_numeric: The base stored confidence (0..1).
        last_confirmed_at:  ISO/space-separated timestamp of last confirmation,
                            or a ``datetime`` object, or None.
        half_life_days:     Half-life for exponential decay.  0 or negative
                            disables time decay.
        status:             Row status string; ``'uncertain'`` triggers the
                            ``UNCERTAIN_CONFIDENCE_PENALTY`` multiplier.
        now:                Reference instant for age calculation.  Defaults to
                            ``datetime.now(timezone.utc)``; pass an explicit
                            value in tests to pin time.
    """
    base = float(confidence_numeric)
    if half_life_days <= 0 or not last_confirmed_at:
        value = base
    else:
        try:
            if isinstance(last_confirmed_at, datetime):
                last = last_confirmed_at
            else:
                last = _parse_timestamp(last_confirmed_at)
            if last.tzinfo is None:
                # SQLite returns naive timestamps; treat them as UTC.
                last = last.replace(tzinfo=timezone.utc)
            _now = now or datetime.now(timezone.utc)
            if _now.tzinfo is None:
                _now = _now.replace(tzinfo=timezone.utc)
            days_since = max(0.0, (_now - last).total_seconds() / 86400.0)
            value = base * (0.5 ** (days_since / half_life_days))
        except (ValueError, TypeError):
            value = base
    # techne#117 recall gate: an uncertain row (a dependency it rests on was
    # superseded) ranks below an otherwise-equal current row until reconfirmed.
    # Applied regardless of whether time decay ran — this is a trust penalty.
    if status == "uncertain":
        value *= UNCERTAIN_CONFIDENCE_PENALTY
    return value


def effective_confidence(
    db_path: Path,
    entry_id: int,
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_CONFIDENCE_HALF_LIFE_DAYS,
) -> float | None:
    """Return the decay-adjusted confidence of an entry.

    Formula: ``confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)``

    A re-affirmed entry (where ``last_confirmed_at`` was bumped recently)
    holds most of its weight. An entry that hasn't been seen in
    ``half_life_days`` is at half strength; double that, quarter strength;
    etc.

    ``half_life_days=0`` disables decay entirely — useful for operators
    who want the raw stored value, or for entries (like architectural
    patterns) where staleness isn't meaningful. The `uncertain` down-rank
    (techne#117) still applies in that mode: it is a trust penalty, not decay.

    Returns None if no such entry exists.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT confidence_numeric, last_confirmed_at, status
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return decay_score(
        row["confidence_numeric"],
        row["last_confirmed_at"],
        half_life_days,
        row["status"],
        now=now,
    )


def reaffirm(db_path: Path, entry_id: int) -> bool:
    """Bump ``last_confirmed_at`` to NOW without creating a new revision.

    Use this when an agent re-encounters a fact and finds it still true —
    refreshes the entry's effective confidence without supersession. Returns
    True iff the entry exists.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge
               SET last_confirmed_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (entry_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- Soft-archive lifecycle (techne#216 / coherence D2) --------------------

# Conservative defaults — archiving should be hard to trigger accidentally.
# Evidence: aggressive lifecycle costs precision; bias the floor low and the
# reaffirmation window long so only genuinely stale, uncertain entries qualify.
DEFAULT_ARCHIVE_CONFIDENCE_FLOOR = 0.1
DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS = 180


def archive_scan(
    db_path: Path,
    *,
    confidence_floor: float = DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,
    reaffirmation_window_days: int = DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
    half_life_days: float = DEFAULT_CONFIDENCE_HALF_LIFE_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Soft-archive scan — move stale, uncertain, unreaffirmed entries out of default recall.

    An entry qualifies for archiving when ALL THREE of these hold:
      1. status = 'uncertain'         (a dependency it rests on was superseded)
      2. effective_confidence() < confidence_floor  (decayed below the floor)
      3. last_confirmed_at older than reaffirmation_window_days (not recently reaffirmed)

    Action: sets status = 'archived'. This is a RECALL VISIBILITY change, not a
    content edit — the row is NEVER hard-deleted. Archived entries are:
      - Excluded from Tier-0 egress (select_tier0_injection — the default session-start
        injection). Bounds growth of the default working set.
      - Excluded from techne_recall (operator-topic-scoped pull).
      - Still returned by explicit knowledge search (techne_search / search_fts).
      - Fully reversible via unarchive().

    This is an EXPLICIT operator-invoked action (not a silent background sweep) because:
      - It changes recall visibility, which affects every future session.
      - It's reversible but the change has immediate operational effect.
      - The codebase precedent for reversible status changes (reconfirm, reaffirm)
        is explicit — mirrored here.

    Returns a list of archived-entry dicts: {id, content, status, effective_confidence}.
    When dry_run=True, returns the same list without writing to the DB (no-op).

    Config levers (conservative defaults):
      - confidence_floor: 0.1  (only archive deeply decayed entries)
      - reaffirmation_window_days: 180  (6 months without reaffirmation)
    """
    _now = now or datetime.now(timezone.utc)
    cutoff_dt = _now - timedelta(days=reaffirmation_window_days)
    # SQLite stores timestamps as "YYYY-MM-DD HH:MM:SS"; format the cutoff to match.
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = connect(db_path)
    try:
        # Fetch all uncertain, live (non-superseded) entries whose last_confirmed_at
        # predates the reaffirmation window. We pull their decay columns so we can
        # compute effective_confidence() in Python (same formula as the public API)
        # without an extra per-row DB round-trip.
        candidates = conn.execute(
            """SELECT id, content, confidence_numeric, last_confirmed_at, status
               FROM knowledge
               WHERE superseded_by IS NULL
                 AND status = 'uncertain'
                 AND last_confirmed_at < ?""",
            (cutoff_str,),
        ).fetchall()
    finally:
        conn.close()

    # Filter by effective_confidence floor.
    to_archive: list[dict] = []
    for row in candidates:
        eff = decay_score(
            row["confidence_numeric"],
            row["last_confirmed_at"],
            half_life_days,
            row["status"],
            now=_now,
        )
        if eff < confidence_floor:
            to_archive.append({
                "id": row["id"],
                "content": row["content"],
                "status": row["status"],
                "effective_confidence": eff,
            })

    if not to_archive or dry_run:
        return to_archive

    # TOCTOU note: candidates are read on one connection above and UPDATEd on a
    # fresh connection below. A concurrent reaffirm() landing between the two could
    # archive a just-reaffirmed row. Negligible for a single-operator CLI (the only
    # caller); the miss self-heals on the next scan or via unarchive(). Documented
    # rather than defended (a held write-lock across the whole scan isn't worth it).
    # Write: flip status to 'archived' for each qualifying row.
    ids = [r["id"] for r in to_archive]
    placeholders = ",".join("?" * len(ids))
    conn = connect(db_path)
    try:
        conn.execute(
            f"""UPDATE knowledge
                SET status = 'archived', updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})""",
            ids,
        )
        conn.commit()
    finally:
        conn.close()

    return to_archive


def unarchive(db_path: Path, entry_id: int) -> bool:
    """Reverse a soft-archive: restore an archived entry to 'uncertain' status.

    Archived entries were moved out of default recall by archive_scan(). This is
    the EXPLICIT reversal — it does not reconfirm the entry (the dependency that
    caused it to become uncertain is still unresolved). Use reconfirm() separately
    if the dependency has been resolved and the entry should return to 'current'.

    Returns True if the entry was archived and has now been restored, False if it
    was not found or was not in 'archived' status.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge
               SET status = 'uncertain', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'archived'""",
            (entry_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _parse_timestamp(value) -> datetime:
    """Parse a SQLite timestamp value into a datetime. Accepts both string
    and datetime forms because connect() has detect_types enabled in some
    paths but not others.

    Handles all timestamp formats seen in practice:
    - Timezone-aware ISO 8601:  ``"2024-07-06T21:09:39.617094+00:00"``
    - SQLite space-separated:   ``"2024-07-06 21:09:39"`` / ``"2024-07-06 21:09:39.123456"``
    - UTC sentinel "Z" suffix:  ``"2024-07-06T21:09:39Z"`` (stripped before strptime)
    """
    if isinstance(value, datetime):
        return value
    s = str(value)
    # fromisoformat handles timezone-aware strings (e.g. "+00:00") and the
    # standard space separator; try it first before the strptime fallback.
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        pass
    # Fallback: strip "Z" suffix and parse with explicit strptime formats.
    s_stripped = s.replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s_stripped, fmt)
        except ValueError:
            continue
    raise ValueError(f"could not parse timestamp {value!r}")
