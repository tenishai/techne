"""Knowledge store — durable facts, decisions, patterns, evaluations, procedures."""

import hashlib
from datetime import datetime, timezone
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
        # Local named `anchor_content_hash` (not `content_hash`) to avoid shadowing
        # the module-level content_hash() function, matching store().
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

        conn.execute(
            "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_id, old_id),
        )
        conn.commit()
        return new_id
    finally:
        conn.close()


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
        query = "SELECT id, type, content, tags, confidence, source, created_at, updated_at FROM knowledge WHERE 1=1"
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


def content_hash(content: str) -> str:
    """Generate a dedup hash for knowledge content."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---- Decay-aware confidence ----------------------------------------------


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
    patterns) where staleness isn't meaningful.

    Returns None if no such entry exists.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT confidence_numeric, last_confirmed_at
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    base = float(row["confidence_numeric"])
    if half_life_days <= 0:
        return base
    last = _parse_timestamp(row["last_confirmed_at"])
    current = now or datetime.now(timezone.utc)
    if last.tzinfo is None:
        # SQLite returns naive timestamps; treat them as UTC.
        last = last.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    days_since = max(0.0, (current - last).total_seconds() / 86400.0)
    return base * (0.5 ** (days_since / half_life_days))


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


def _parse_timestamp(value) -> datetime:
    """Parse a SQLite timestamp value into a datetime. Accepts both string
    and datetime forms because connect() has detect_types enabled in some
    paths but not others."""
    if isinstance(value, datetime):
        return value
    # SQLite ISO format: "YYYY-MM-DD HH:MM:SS" (optionally with fractional secs)
    s = str(value).replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"could not parse timestamp {value!r}")
