"""Pattern store data layer over architect.db (techne#87).

CRUD + dedup + search for the Architect's pattern domain. Backend-portable in
spirit (Postgres is P3) but SQLite-direct for now via plain_connect — architect.db
is a domain db, so it gets only its own schema (no techne core tables).

Dedup is lens-aware: `investigations` keyed (url × surveyed_by × lens) makes the
SRE-vs-infra comparison a join. Patterns hang off an investigation.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from techne.core.stores import plain_connect


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


# --- investigation verdicts (techne#428) -------------------------------------
#
# The verdict is meant to record the OPERATOR'S judgment about a surveyed URL.
# It did not. `ingest_patterns_md` hardcoded "full-eval" for any URL group that
# carried patterns and "skip-with-flag" for a no-lift bullet, and the CLI had no
# way to pass one — so the column recorded *whether the markdown contained
# patterns*, which the patterns table already says, and `patterns-only` was
# literally unreachable. Live corpus when this was found: 202 full-eval,
# 70 skip-with-flag, 0 patterns-only, against 19 eval docs on disk.
#
# That made the field worse than empty: reading `verdict=full-eval` on a
# re-investigated URL invited the inference "a prior pass judged this
# decision-forcing but never wrote its eval doc", which was an artifact of the
# default.
VERDICTS = ("skip", "skip-with-flag", "patterns-only", "full-eval", "unknown")

# What an ingest records when the caller does not say. `patterns-only` is the
# documented modal outcome and `full-eval` is documented as ~5-10%, so defaulting
# here is right most of the time where the old hardcode was right rarely. It is
# still a default, not a judgment — pass --verdict to state one.
DEFAULT_INGEST_VERDICT = "patterns-only"

# Reserved for rows whose verdict was auto-assigned before this existed and so
# never meant anything. Honest about not knowing rather than guessing.
UNKNOWN_VERDICT = "unknown"

# surveyed_at is TEXT holding a zero-padded ISO date (import_md enforces
# \d{4}-\d{2}-\d{2} at parse time), so `surveyed_at < ?` is a sound string
# comparison — but ONLY if the bound value is the same shape. Validated where it
# enters, because the date scoping is the sweep's only safety property.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- graph extraction (techne#87 P2) ----------------------------------------
#
# The relationship signals are already embedded in pattern *bodies* — lifted by
# the investigate-url skill — they just were never materialized into rows:
#   * `[[architectural-patterns#<name>]]` wikilinks  → pattern_relations edges
#   * `**in <target>**:` markers in the where-to-apply line → pattern_applies
# Backfill parses those out and writes the edges; resolution mirrors codex#89
# (resolve by name, keep `unresolved` + the raw name when it doesn't match).

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# A pattern's target project is named on its "Where to apply" line as
# `…where to apply in <target>…` (the live corpus folds it into the bold heading,
# `**Where to apply in techne**:`). We anchor on that exact phrase and capture the
# single token that follows "apply in" — so neither descriptive prose elsewhere
# (`**built in techne**`) nor a comparison later in the same heading
# (`…in techne (vs cabal)`) manufactures a spurious applies row (Prudence, PR#130).
# A target named only in non-bold prose after the colon is not extracted — an
# honest under-reach, not a false positive.
_APPLY_RE = re.compile(
    r"where\s+(?:to|it might|this)\s+appl\w*\s+in\s+([a-z][a-z0-9-]*)", re.IGNORECASE
)
# The projects a pattern may declare itself relevant to. NOT hardcoded prose:
# every name here is a real target named in the /investigate-url skill's context
# block, so a pattern that says "apply in prudence" resolves instead of being
# silently dropped (techne#674 — 14 pattern bodies named Prudence and none could
# ever link, which made `architect applies prudence` read as "we hold nothing").
_KNOWN_TARGETS = {
    "techne",
    "cabal",
    "liminal",
    "agent-infra",
    "prudence",
    "daedalus",
    "kaminos",
    "argus",
    "ladon",
    "agora",
}

# Pre-rename spellings still present in older pattern bodies. Extraction
# canonicalises through this map so ONE PROJECT CANNOT OCCUPY TWO TARGETS —
# `limenal` was renamed to `liminal` on 2026-08-11 and the set was not updated,
# so backfill emitted `limenal` while the direct-tag path wrote `liminal`,
# leaving 11 patterns double-listed under both (techne#674).
_TARGET_ALIASES = {"limenal": "liminal"}

# Outcome lifecycle (techne#426). 'filed' → work exists; 'shipped' → it landed;
# 'rejected' → considered and declined, which is a real signal, not an absence.
_OUTCOME_STATES = {"filed", "shipped", "rejected"}


def _wikilink_targets(body: str) -> list[str]:
    """Extract the referenced pattern names from `[[...]]` links in *body*.

    Links look like `[[architectural-patterns#Hybrid rank fusion: BM25 + vec]]`
    (an Obsidian file#heading ref). We want the heading after the last `#`; a
    bare `[[Name]]` (no `#`) is taken whole. Self/empty refs are dropped by the
    caller via name resolution. Deduped, order-preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _WIKILINK_RE.findall(body or ""):
        name = raw.rsplit("#", 1)[-1].strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _apply_targets(body: str) -> list[str]:
    """Extract canonical project targets from `where to apply in <target>` phrases
    in *body*. Only the token immediately after "apply in" is taken; unknown tokens
    are dropped. Deduped, order-preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _APPLY_RE.findall(body or ""):
        t = _TARGET_ALIASES.get(raw.lower(), raw.lower())
        if t in _KNOWN_TARGETS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _norm(name: str) -> str:
    """Normalize a pattern name for fuzzy resolution: lowercase, collapse runs of
    non-alphanumerics to a single space, strip. So `BM25 + vec` ~ `bm25 vec`."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def upsert_investigation(
    db_path: Path,
    *,
    url: str,
    surveyed_at: str,
    verdict: str,
    surveyed_by: str = "raven",
    lens: str = "infra",
    summary: str | None = None,
    skip_reason: str | None = None,
    maturity: str | None = None,
    overwrite_verdict: bool = True,
    clears_skip: bool = False,
) -> str:
    """Insert/update one investigation (the dedup unit). Returns its id.

    `verdict` must be one of VERDICTS. It is validated here rather than by a
    schema CHECK because the constraint would need a full SQLite table rebuild to
    add to an existing db, and this is the only write path — a bad value should
    fail loudly at the call site, not land as a silent out-of-vocabulary string
    that every downstream query then has to tolerate (techne#428).

    `overwrite_verdict=False` makes the verdict INSERT-ONLY: a new row gets it, an
    existing row keeps whatever it already has. Callers pass False when the value
    is a fallback rather than a stated judgment. Without this, a default that
    overwrites on every re-ingest is techne#428 again with a nicer constant — and
    concretely it undid `set_verdict`: state a judgment with `reverdict`, re-ingest
    the same markdown, and a routine ingest silently reverts it (Prudence, PR #432).

    `clears_skip=True` additionally displaces an existing `skip*` verdict even when
    the write is insert-only. Callers pass it when the write itself proves the URL
    DID yield patterns, which contradicts any skip verdict — "no patterns worth
    saving" cannot survive a pattern being saved. Without it, insert-only stranded
    a URL first seen as a no-lift bullet and later carrying patterns: the row kept
    `skip-with-flag` while owning a pattern, so `stats()` reported it under both
    `patterns` and `skips` at once.
    """
    if verdict not in VERDICTS:
        raise ValueError(
            f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}. "
            "The verdict is the operator's judgment about the URL — see techne#428."
        )
    inv_id = _hash(url, surveyed_by, lens)
    conn = plain_connect(db_path)
    try:
        # Named parameters, not positional: the overwrite flag governs a clause ten
        # `?` slots away from where it would have been bound, and a future edit that
        # inserts a column would silently misbind it (Prudence, PR #432).
        conn.execute(
            """INSERT INTO investigations
                 (id, url, surveyed_by, lens, surveyed_at, verdict, summary, skip_reason, maturity)
               VALUES (:id, :url, :surveyed_by, :lens, :surveyed_at, :verdict,
                       :summary, :skip_reason, :maturity)
               ON CONFLICT(url, surveyed_by, lens) DO UPDATE SET
                 surveyed_at = excluded.surveyed_at,
                 verdict = CASE
                             WHEN :overwrite THEN excluded.verdict
                             WHEN :clears_skip AND investigations.verdict LIKE 'skip%'
                                  THEN excluded.verdict
                             ELSE investigations.verdict END,
                 summary = COALESCE(excluded.summary, investigations.summary),
                 skip_reason = COALESCE(excluded.skip_reason, investigations.skip_reason),
                 maturity = COALESCE(excluded.maturity, investigations.maturity)""",
            {
                "id": inv_id,
                "url": url,
                "surveyed_by": surveyed_by,
                "lens": lens,
                "surveyed_at": surveyed_at,
                "verdict": verdict,
                "summary": summary,
                "skip_reason": skip_reason,
                "maturity": maturity,
                "overwrite": 1 if overwrite_verdict else 0,
                "clears_skip": 1 if clears_skip else 0,
            },
        )
        conn.commit()
        return inv_id
    finally:
        conn.close()


def set_verdict(
    db_path: Path,
    *,
    url: str,
    verdict: str,
    surveyed_by: str = "raven",
    lens: str = "infra",
) -> bool:
    """Correct one investigation's verdict in place. True if a row was updated.

    Deliberately does NOT touch surveyed_at — correcting a judgment is not a
    re-survey, and conflating the two is the adjacent bug in techne#429.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE investigations SET verdict = ? WHERE id = ?",
            (verdict, _hash(url, surveyed_by, lens)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def sweep_legacy_verdicts(db_path: Path, *, before: str, dry_run: bool = True) -> dict:
    """Retire pre-techne#428 auto-assigned verdicts to 'unknown'.

    Every `full-eval` written before the fix was assigned by the ingester purely
    because the markdown carried patterns, so none of those values is a judgment.
    Leaving them reads as 202 deliberate full-evals; 'unknown' is the honest state.

    Scoped by `before` (a surveyed_at date) rather than sweeping the whole table,
    so a genuine operator-supplied full-eval recorded after the fix is never
    clobbered. Caveat: because re-ingest currently overwrites surveyed_at
    (techne#429), a legacy row that has since been re-investigated carries the
    newer date and will fall outside the sweep — it will need `set_verdict`.

    dry_run=True by default: this rewrites the operator's corpus, so the caller
    has to ask for it twice.
    """
    if not _ISO_DATE_RE.match(before):
        raise ValueError(
            f"--before must be an ISO date (YYYY-MM-DD); got {before!r}. "
            "It is this function's only safety property — an unparseable value would "
            "compare as a plain string and could select every row in the table."
        )

    conn = plain_connect(db_path)
    try:
        # Take the write lock BEFORE the read (BEGIN IMMEDIATE): with the default
        # isolation a bare SELECT opens no transaction, so a concurrent writer can
        # land between the SELECT that builds the preview/audit list and the UPDATE
        # that rewrites those rows — reporting rows as swept that were changed by
        # someone else. Same read→write TOCTOU the rest of this codebase already
        # guards (curated.py:105, cards.py:106, documents.py:71); reproducing it in
        # a *destructive* path is worse than in an idempotent one.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT url, surveyed_at FROM investigations "
            "WHERE verdict = 'full-eval' AND surveyed_at < ? ORDER BY surveyed_at",
            (before,),
        ).fetchall()
        applied = 0
        if not dry_run and rows:
            cur = conn.execute(
                "UPDATE investigations SET verdict = ? "
                "WHERE verdict = 'full-eval' AND surveyed_at < ?",
                (UNKNOWN_VERDICT, before),
            )
            # rowcount from the UPDATE, not len(rows) from the SELECT: the audit
            # number for a destructive rewrite must be measured against the write
            # that happened, not the read that predicted it.
            applied = cur.rowcount
        conn.commit()
        return {
            "matched": len(rows),
            "applied": applied,
            "before": before,
            "urls": [r[0] for r in rows],
        }
    finally:
        conn.close()


def add_pattern(
    db_path: Path,
    *,
    investigation_id: str,
    name: str,
    source_url: str,
    body: str,
    source_path: str | None = None,
    lens_kind: str | None = None,
    confidence: str = "medium",
) -> tuple[str, bool]:
    """Insert a lifted pattern (content-hash id, idempotent). Returns (id, stored)."""
    pid = _hash(name, source_url, body)
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pid,)).fetchone():
            return pid, False
        conn.execute(
            """INSERT INTO patterns
                 (id, investigation_id, name, source_url, source_path, lens_kind, body, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, investigation_id, name, source_url, source_path, lens_kind, body, confidence),
        )
        conn.commit()
        return pid, True
    finally:
        conn.close()


def _name_index(conn) -> dict[str, str]:
    """Map normalized pattern name → id for resolution. On a name collision the
    first (lowest rowid) wins — deterministic, and name dupes are rare."""
    index: dict[str, str] = {}
    for row in conn.execute("SELECT id, name FROM patterns WHERE status = 'active' ORDER BY rowid"):
        key = _norm(row["name"])
        index.setdefault(key, row["id"])
    return index


def add_relation(
    db_path: Path, *, src_pattern_id: str, dst_name: str, kind: str = "related"
) -> tuple[str | None, bool]:
    """Record one pattern→pattern edge, resolving *dst_name* against active
    patterns. Returns (dst_pattern_id_or_None, resolved). Idempotent on
    (src, dst_name, kind); re-running upgrades unresolved→resolved as the corpus
    grows. A self-edge (dst resolves back to src) is dropped."""
    conn = plain_connect(db_path)
    try:
        dst_id = _name_index(conn).get(_norm(dst_name))
        if dst_id == src_pattern_id:  # self-reference — not an edge
            return src_pattern_id, True
        status = "resolved" if dst_id else "unresolved"
        conn.execute(
            """INSERT INTO pattern_relations
                 (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                 dst_pattern_id = excluded.dst_pattern_id,
                 resolution_status = excluded.resolution_status""",
            (src_pattern_id, dst_name, dst_id, kind, status),
        )
        conn.commit()
        return dst_id, dst_id is not None
    finally:
        conn.close()


def add_apply(db_path: Path, *, pattern_id: str, target: str) -> bool:
    """Tag a pattern as applying to a project target (see _KNOWN_TARGETS).
    Idempotent. Returns True if a new row was inserted."""
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pattern_applies (pattern_id, target) VALUES (?, ?)",
            (pattern_id, target),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def record_outcome(
    db_path: Path,
    *,
    pattern_id: str,
    ref: str,
    kind: str = "issue",
    state: str = "filed",
    note: str | None = None,
) -> bool:
    """Record what a pattern BECAME — the return path of provenance (techne#426).

    A pattern already carries where it came from (`source_url`, `investigation_id`).
    This carries the other direction: the issue/PR/commit it turned into and whether
    that shipped. *ref* is free-form but conventionally `owner/repo#123` for issues.

    Upserts on (pattern_id, ref) so re-recording promotes state — the normal
    lifecycle is 'filed' at file-time, then 'shipped' when the issue closes as
    completed. Returns True if a row was inserted or its state/note changed.
    Raises ValueError on unknown pattern id."""
    if state not in _OUTCOME_STATES:
        raise ValueError(
            f"unknown outcome state: {state} (expected one of {sorted(_OUTCOME_STATES)})"
        )
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone() is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
        before = conn.execute(
            "SELECT state, note FROM pattern_outcomes WHERE pattern_id = ? AND ref = ?",
            (pattern_id, ref),
        ).fetchone()
        now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        conn.execute(
            "INSERT INTO pattern_outcomes (pattern_id, ref, kind, state, recorded_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(pattern_id, ref) DO UPDATE SET"
            "   state = excluded.state,"
            "   kind = excluded.kind,"
            "   note = COALESCE(excluded.note, pattern_outcomes.note)",
            (pattern_id, ref, kind, state, now, note),
        )
        conn.commit()
        if before is None:
            return True
        return before[0] != state or (note is not None and before[1] != note)
    finally:
        conn.close()


def outcomes_for_pattern(db_path: Path, pattern_id: str) -> list[dict]:
    """Every recorded outcome for one pattern, newest first."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ref, kind, state, recorded_at, note FROM pattern_outcomes"
            " WHERE pattern_id = ? ORDER BY recorded_at DESC",
            (pattern_id,),
        ).fetchall()
        return [
            {"ref": r[0], "kind": r[1], "state": r[2], "recorded_at": r[3], "note": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def outcome_stats(db_path: Path) -> dict:
    """Corpus-level conversion: did this investigation programme actually pay?

    `realized` counts DISTINCT patterns with at least one outcome — not outcome
    rows — so a pattern that spawned three issues counts once. `shipped` is the
    subset with at least one outcome in state 'shipped'. The rate is the number
    that matters: what fraction of the corpus produced downstream work."""
    conn = plain_connect(db_path)
    try:
        active = conn.execute("SELECT COUNT(*) FROM patterns WHERE status = 'active'").fetchone()[0]
        realized = conn.execute(
            "SELECT COUNT(DISTINCT pattern_id) FROM pattern_outcomes"
        ).fetchone()[0]
        shipped = conn.execute(
            "SELECT COUNT(DISTINCT pattern_id) FROM pattern_outcomes WHERE state = 'shipped'"
        ).fetchone()[0]
        by_state = dict(
            conn.execute("SELECT state, COUNT(*) FROM pattern_outcomes GROUP BY state").fetchall()
        )
        return {
            "active_patterns": active,
            "realized_patterns": realized,
            "shipped_patterns": shipped,
            "realized_rate": round(realized / active, 3) if active else 0.0,
            "shipped_rate": round(shipped / active, 3) if active else 0.0,
            "outcome_rows_by_state": by_state,
        }
    finally:
        conn.close()


def unrealized_patterns(db_path: Path, *, topic: str | None = None, limit: int = 100) -> list[dict]:
    """Active patterns with no recorded outcome — the candidate queue.

    These are patterns we lifted and then never turned into anything. Not
    inherently waste (a pattern can be reference material), but this is the list
    to read when asking "what did we learn and never act on?"."""
    conn = plain_connect(db_path)
    try:
        sql = (
            "SELECT p.id, p.name, p.topic, p.source_url FROM patterns p"
            " LEFT JOIN pattern_outcomes o ON o.pattern_id = p.id"
            " WHERE p.status = 'active' AND o.pattern_id IS NULL"
        )
        args: list = []
        if topic:
            sql += " AND p.topic = ?"
            args.append(topic)
        sql += " ORDER BY p.created_at DESC LIMIT ?"
        args.append(limit)
        return [
            {"id": r[0], "name": r[1], "topic": r[2], "source_url": r[3]}
            for r in conn.execute(sql, args).fetchall()
        ]
    finally:
        conn.close()


def supersede(
    db_path: Path,
    *,
    old_id: str,
    new_id: str,
    absorbed: str | None = None,
    invalid_at: str | None = None,
) -> bool:
    """Mark *old_id* as superseded by *new_id*, atomically (techne#308, closes #307).

    In ONE transaction: stamp `old.status='superseded'`, `old.superseded_by=new_id`,
    `old.absorbed=<salvage note>` (kept if not re-supplied), record the validity
    window (techne#306) — `old.invalid_at` and `new.valid_at` get the SAME handoff
    timestamp (default now; *invalid_at* overrides it) so the read-path can ask
    "what was current as of T?" — AND upsert a resolved `supersedes` edge (old → new)
    so the relation graph reflects it. Both patterns must exist and differ. Idempotent
    (COALESCE keeps the original window on a re-run). Returns True if the row changed.

    Columns are the source of truth for supersession; the edge is written by the
    same atomic op purely so the graph view (techne#308 → #305) stays consistent."""
    if old_id == new_id:
        raise ValueError("a pattern cannot supersede itself")
    conn = plain_connect(db_path)
    try:
        old = conn.execute("SELECT id FROM patterns WHERE id = ?", (old_id,)).fetchone()
        new = conn.execute("SELECT id, name FROM patterns WHERE id = ?", (new_id,)).fetchone()
        if old is None:
            raise ValueError(f"unknown pattern id (old): {old_id}")
        if new is None:
            raise ValueError(f"unknown pattern id (new): {new_id}")
        with conn:  # atomic: all writes commit together, or none do
            # One timestamp for the handoff: old stops being current and new becomes
            # current at the same instant (techne#306 validity window).
            ts = invalid_at or conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            cur = conn.execute(
                """UPDATE patterns
                     SET status = 'superseded', superseded_by = ?,
                         absorbed = COALESCE(?, absorbed),
                         invalid_at = COALESCE(invalid_at, ?)
                   WHERE id = ?""",
                (new_id, absorbed, ts, old_id),
            )
            conn.execute(
                "UPDATE patterns SET valid_at = COALESCE(valid_at, ?) WHERE id = ?",
                (ts, new_id),
            )
            conn.execute(
                """INSERT INTO pattern_relations
                     (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
                   VALUES (?, ?, ?, 'supersedes', 'resolved')
                   ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                     dst_pattern_id = excluded.dst_pattern_id,
                     resolution_status = excluded.resolution_status""",
                (old_id, new["name"], new_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def tag_pattern(
    db_path: Path,
    pattern_id: str,
    *,
    topic: str | None = None,
    status: str | None = None,
    superseded_by: str | None = None,
    absorbed: str | None = None,
) -> bool:
    """Curation mutator for a single pattern (techne#308 `architect tag`).

    If *superseded_by* is given, routes to the atomic `supersede()` (status is
    forced to 'superseded' and the `supersedes` edge written); a *topic* supplied
    alongside is applied too. Otherwise updates whichever of topic/status/absorbed
    are provided. Returns True if a row changed. Raises ValueError on unknown id."""
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone() is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
    finally:
        conn.close()

    if superseded_by is not None:
        changed = supersede(db_path, old_id=pattern_id, new_id=superseded_by, absorbed=absorbed)
        if topic is not None:
            changed = _set_fields(db_path, pattern_id, {"topic": topic}) or changed
        return changed

    fields: dict[str, str] = {}
    if topic is not None:
        fields["topic"] = topic
    if status is not None:
        fields["status"] = status
    if absorbed is not None:
        fields["absorbed"] = absorbed
    if not fields:
        return False
    return _set_fields(db_path, pattern_id, fields)


def _set_fields(db_path: Path, pattern_id: str, fields: dict[str, str]) -> bool:
    """Update a whitelisted set of columns on one pattern. Column names are from a
    fixed internal allowlist (never user input), so the f-string is injection-safe."""
    allowed = {"topic", "status", "absorbed", "superseded_by"}
    cols = [c for c in fields if c in allowed]
    if not cols:
        return False
    assignments = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols] + [pattern_id]
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(f"UPDATE patterns SET {assignments} WHERE id = ?", values)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def backfill_graph(db_path: Path) -> dict:
    """Populate pattern_relations + pattern_applies from the signals already in
    pattern bodies (techne#87 P2). Parses `[[wikilinks]]` → relations and
    `**in <target>**` → applies for every active pattern, in one transaction.

    Idempotent and incremental: edges upsert on (src, dst_name, kind), so a
    re-run after new patterns land upgrades previously-`unresolved` edges to
    `resolved`. Returns counts for reporting."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, body FROM patterns WHERE status = 'active'"
        ).fetchall()
        index = _name_index(conn)
        rel_total = rel_resolved = applies_total = 0
        for r in rows:
            src = r["id"]
            for dst_name in _wikilink_targets(r["body"]):
                dst_id = index.get(_norm(dst_name))
                if dst_id == src:  # self-reference
                    continue
                status = "resolved" if dst_id else "unresolved"
                conn.execute(
                    """INSERT INTO pattern_relations
                         (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
                       VALUES (?, ?, ?, 'related', ?)
                       ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                         dst_pattern_id = excluded.dst_pattern_id,
                         resolution_status = excluded.resolution_status""",
                    (src, dst_name, dst_id, status),
                )
                rel_total += 1
                rel_resolved += int(dst_id is not None)
            for target in _apply_targets(r["body"]):
                conn.execute(
                    "INSERT OR IGNORE INTO pattern_applies (pattern_id, target) VALUES (?, ?)",
                    (src, target),
                )
                applies_total += 1
        conn.commit()
        # All counts are TOTALS over the corpus (not per-run deltas), so the
        # numbers are stable across idempotent re-runs.
        return {
            "relations": rel_total,
            "resolved": rel_resolved,
            "unresolved": rel_total - rel_resolved,
            "applies": applies_total,
        }
    finally:
        conn.close()


def related_patterns(db_path: Path, pattern_id: str) -> dict:
    """Edges touching *pattern_id*, both directions. Returns
    {'outbound': [...], 'inbound': [...]}. Outbound includes unresolved edges
    (dst_id NULL, dst_name carries the dangling reference); inbound is resolved
    edges only (an unresolved edge has no known source-of-truth target)."""
    conn = plain_connect(db_path)
    try:
        outbound = [
            dict(r)
            for r in conn.execute(
                """SELECT r.dst_name, r.dst_pattern_id, r.kind, r.resolution_status,
                          p.name AS dst_pattern_name
                   FROM pattern_relations r
                   LEFT JOIN patterns p ON p.id = r.dst_pattern_id
                   WHERE r.src_pattern_id = ?
                   ORDER BY r.resolution_status, r.dst_name""",
                (pattern_id,),
            )
        ]
        inbound = [
            dict(r)
            for r in conn.execute(
                """SELECT r.src_pattern_id, r.kind, p.name AS src_pattern_name
                   FROM pattern_relations r JOIN patterns p ON p.id = r.src_pattern_id
                   WHERE r.dst_pattern_id = ? ORDER BY p.name""",
                (pattern_id,),
            )
        ]
        return {"outbound": outbound, "inbound": inbound}
    finally:
        conn.close()


def patterns_for_target(db_path: Path, target: str, *, limit: int = 100) -> list[dict]:
    """All active patterns tagged as applying to *target* (see _KNOWN_TARGETS).

    Takes the target VERBATIM — no aliasing. A pre-rename spelling such as
    `limenal` therefore returns [] rather than `liminal`'s rows; canonicalisation
    happens on the WRITE side (`_apply_targets`), so the stored value is already
    canonical and a miss here means the caller asked for something real that is
    not tagged, which is a true answer (techne#674)."""
    conn = plain_connect(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT p.id, p.name, p.source_url, p.confidence
                   FROM pattern_applies a JOIN patterns p ON p.id = a.pattern_id
                   WHERE a.target = ? AND p.status = 'active'
                   ORDER BY p.name LIMIT ?""",
                (target, limit),
            )
        ]
    finally:
        conn.close()


def topic_list(db_path: Path) -> list[dict]:
    """The topic INDEX (techne#597): every topic with its active-pattern count,
    plus an `(untopiced)` bucket for patterns not yet grouped. Count-desc so the
    crowded topics — the ones worth curating first — surface at the top."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT COALESCE(topic, '(untopiced)') AS topic, COUNT(*) AS count
               FROM patterns WHERE status = 'active'
               GROUP BY COALESCE(topic, '(untopiced)')
               ORDER BY count DESC, topic"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def topic_show(db_path: Path, topic: str) -> list[dict]:
    """The read-path for one topic (techne#597): active patterns in *topic*, each
    with its superseded predecessors folded in under a `superseded` list (their
    `absorbed` salvage notes carried along). `(untopiced)` selects the ungrouped
    bucket, matching `topic_list`."""
    conn = plain_connect(db_path)
    try:
        where = "topic IS NULL" if topic == "(untopiced)" else "topic = ?"
        params: tuple = () if topic == "(untopiced)" else (topic,)
        actives = conn.execute(
            f"""SELECT id, name, source_url, confidence FROM patterns
                WHERE status = 'active' AND {where} ORDER BY name""",
            params,
        ).fetchall()
        out = []
        for a in actives:
            superseded = conn.execute(
                """SELECT id, name, absorbed FROM patterns
                   WHERE status = 'superseded' AND superseded_by = ? ORDER BY name""",
                (a["id"],),
            ).fetchall()
            row = dict(a)
            row["superseded"] = [dict(s) for s in superseded]
            out.append(row)
        return out
    finally:
        conn.close()


def current_as_of(db_path: Path, when: str, *, topic: str | None = None) -> list[dict]:
    """Patterns that were CURRENT at timestamp *when* (techne#306 validity windows).

    A pattern is current at T when it had become valid — COALESCE(valid_at,
    created_at) <= T — and had not yet been superseded — invalid_at IS NULL OR
    invalid_at > T. *when* is a SQLite datetime string ('YYYY-MM-DD[ HH:MM:SS]').
    Optional *topic* scope ('(untopiced)' = the NULL bucket). This is the temporal
    read-path: "what was the current pattern for this topic as of D?"."""
    conn = plain_connect(db_path)
    try:
        clauses = [
            "COALESCE(valid_at, created_at) <= ?",
            "(invalid_at IS NULL OR invalid_at > ?)",
        ]
        params: list = [when, when]
        if topic is not None:
            if topic == "(untopiced)":
                clauses.append("topic IS NULL")
            else:
                clauses.append("topic = ?")
                params.append(topic)
        rows = conn.execute(
            f"""SELECT id, name, topic, status, valid_at, invalid_at FROM patterns
                WHERE {" AND ".join(clauses)} ORDER BY name""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def graph_data(db_path: Path, topic: str | None = None) -> dict:
    """Nodes + resolved edges for a relation-graph render (techne#305).

    Full store when *topic* is None, else scoped to that topic ('(untopiced)' =
    the NULL bucket). Each node carries `status` so a renderer can de-emphasize
    superseded ones. Edges are RESOLVED relations whose BOTH endpoints are in the
    node set — so a topic-scoped subgraph has no dangling edges. Unresolved edges
    are omitted (they point at a name with no node yet)."""
    conn = plain_connect(db_path)
    try:
        if topic is None:
            node_rows = conn.execute(
                "SELECT id, name, topic, status FROM patterns ORDER BY name"
            ).fetchall()
        else:
            where = "topic IS NULL" if topic == "(untopiced)" else "topic = ?"
            params: tuple = () if topic == "(untopiced)" else (topic,)
            node_rows = conn.execute(
                f"SELECT id, name, topic, status FROM patterns WHERE {where} ORDER BY name",
                params,
            ).fetchall()
        nodes = [dict(r) for r in node_rows]
        node_ids = {n["id"] for n in nodes}
        edges = [
            {"src": r["src_pattern_id"], "dst": r["dst_pattern_id"], "kind": r["kind"]}
            for r in conn.execute(
                """SELECT src_pattern_id, dst_pattern_id, kind FROM pattern_relations
                   WHERE resolution_status = 'resolved'"""
            )
            if r["src_pattern_id"] in node_ids and r["dst_pattern_id"] in node_ids
        ]
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


def investigation_exists(
    db_path: Path, url: str, *, surveyed_by: str = "raven", lens: str = "infra"
) -> bool:
    """Lens-aware dedup probe — the fast SELECT the flat md couldn't do."""
    conn = plain_connect(db_path)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM investigations WHERE url = ? AND surveyed_by = ? AND lens = ?",
                (url, surveyed_by, lens),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def investigations_for_url(db_path: Path, url: str) -> list[dict]:
    """All lenses/verdicts for a URL side by side — the comparison query."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT surveyed_by, lens, verdict, summary FROM investigations
               WHERE url = ? ORDER BY surveyed_by, lens""",
            (url,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_patterns(db_path: Path, query: str, *, limit: int = 20) -> list[dict]:
    """FTS over active patterns (name + body), with an OR fallback on an empty AND pass.

    The corpus is queried in problem language ("resume work after a crash without
    losing the task") while patterns are titled metaphorically ("Rebuild-not-reference
    handle across durable-workflow step boundaries"). Under FTS5 implicit-AND every
    term must appear, so a natural-language question matches nothing and the empty
    result reads as "we have no pattern for this" while a directly on-point one sits
    in the store. Measured on the 477-pattern corpus (#557): 1/15 problem-phrased
    queries found their target, 14/15 returned zero; with this fallback, 14/15 and
    zero empties.

    Same #70 empty-result fallback that :func:`techne.core.search` and
    :func:`techne.core.codegraph.search_symbols` already apply — this was the one
    FTS caller that never got it. The fallback fires ONLY when the AND pass returns
    nothing, so a query that already resolves is returned unchanged.
    """
    from techne.core.search import build_or_fts_query, prepare_fts_query

    fts = prepare_fts_query(query)  # None when empty/all-punctuation → MATCH '' crash (#143)
    if fts is None:
        return []
    conn = plain_connect(db_path)
    try:

        def _match(match_str: str) -> list[dict]:
            rows = conn.execute(
                """SELECT p.id, p.name, p.source_url, p.body, p.confidence,
                          bm25(patterns_fts) AS rank
                   FROM patterns_fts f JOIN patterns p ON p.rowid = f.rowid
                   WHERE patterns_fts MATCH ? AND p.status = 'active'
                   ORDER BY rank LIMIT ?""",
                (match_str, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        results = _match(fts)
        if not results:
            # The AND pass matched nothing — retry as OR-of-terms so a multi-term
            # question returns the ranked union rather than a false "no patterns".
            # build_or_fts_query returns "" for <2 terms (AND==OR → no re-query).
            or_query = build_or_fts_query(query)
            if or_query:
                results = _match(or_query)
        return results
    finally:
        conn.close()


def get_pattern(db_path: Path, pattern_id: str) -> dict | None:
    conn = plain_connect(db_path)
    try:
        row = conn.execute("SELECT * FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_patterns(db_path: Path, *, limit: int = 100) -> list[dict]:
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, name, source_url, confidence FROM patterns
               WHERE status = 'active' ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    conn = plain_connect(db_path)
    try:
        return {
            "investigations": conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0],
            "patterns": conn.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'active'"
            ).fetchone()[0],
            "skips": conn.execute(
                "SELECT COUNT(*) FROM investigations WHERE verdict LIKE 'skip%'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def retire_legacy_pattern_knowledge(techne_db_path: Path, *, dry_run: bool = False) -> int:
    """Delete the legacy `type='pattern'` rows from techne.db (techne#87 P2).

    Before the pattern store existed, lifted patterns were dumped into personal
    memory as `knowledge` rows with `type='pattern'`. They're now fully owned by
    architect.db, but recall/search don't filter by type — so these dupes leak
    *team* patterns into *personal* recall. They're superseded; delete them. Uses
    the migrated core connection so the knowledge FTS triggers stay consistent.
    Returns the count removed (or that *would* be removed under dry_run)."""
    from techne.core.database import connect

    conn = connect(techne_db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM knowledge WHERE type = 'pattern'").fetchone()[0]
        if not dry_run and n:
            conn.execute("DELETE FROM knowledge WHERE type = 'pattern'")
            conn.commit()
        return n
    finally:
        conn.close()


def export_patterns_md(db_path: Path) -> str:
    """Regenerate an architectural-patterns.md from architect.db — the shareable
    export (the md becomes a generated artifact; architect.db is canonical). Round-
    trips with ingest_patterns_md."""
    conn = plain_connect(db_path)
    try:
        pats = conn.execute(
            """SELECT p.name, p.source_url, p.body, i.surveyed_at
               FROM patterns p JOIN investigations i ON i.id = p.investigation_id
               WHERE p.status = 'active'
               ORDER BY i.surveyed_at, p.name"""
        ).fetchall()
        no_lifts = conn.execute(
            """SELECT url, surveyed_at, skip_reason FROM investigations
               WHERE verdict LIKE 'skip%' ORDER BY surveyed_at, url"""
        ).fetchall()
    finally:
        conn.close()

    out = [
        "# Architectural patterns",
        "",
        "Shared team corpus of design patterns lifted from other people's code. "
        "**How to use**: grep for a source URL before investigating it — if it's "
        "below, we've seen it. **To add one**: append a block using the *Template "
        "for new entries* at the bottom, then `git commit`. Load into your local "
        "store to query with `techne architect ingest patterns/architectural-patterns.md`.",
        "",
        "This file round-trips with the Architect store (`architect.db`): "
        "`architect ingest` loads it, `architect export` regenerates it. Hand-edits "
        "survive a regenerate **as long as you `ingest` before you `export`** (ingest "
        "pulls your additions into the store first). See `patterns/README.md`.",
        "",
        "---",
        "",
    ]
    for p in pats:
        out.append(f"## {p['name']}")
        out.append("")
        out.append(f"- **Source**: {p['source_url']}")
        out.append(f"- **Surveyed**: {p['surveyed_at']}")
        out.append("")
        out.append((p["body"] or "").strip())
        out.append("")
        out.append("---")
        out.append("")
    if no_lifts:
        out.append("## Surveyed (no patterns worth saving)")
        out.append("")
        for nl in no_lifts:
            reason = nl["skip_reason"] or "nothing to lift"
            out.append(f"- {nl['url']} — surveyed {nl['surveyed_at']} — {reason}")
        out.append("")

    # The format/schema, embedded so the file is self-documenting. This heading is
    # in import_md.SKIPPED_HEADINGS (and the body is indented), so it is NOT ingested
    # as a pattern.
    out += [
        "## Template for new entries",
        "",
        "Append a pattern as a block like this (parsed on `architect ingest`):",
        "",
        "    ## <Short pattern name>",
        "",
        "    - **Source**: <url> — <optional file:line or §section>",
        "    - **Surveyed**: <YYYY-MM-DD>",
        "",
        "    <What it does — 1–2 paragraphs.>",
        "",
        "    **Why interesting**: <the non-obvious design choice / trade-off>.",
        "",
        "    **Where to apply**: <where in our stack it fits>.",
        "",
        "    ---",
        "",
        "For a URL you investigated but found nothing to lift, add a bullet under "
        '"## Surveyed (no patterns worth saving)": `- <url> — surveyed <date> — <reason>`.',
        "",
    ]
    return "\n".join(out)


def ingest_patterns_md(db_path: Path, md_text: str, verdict: str | None = None) -> dict:
    """Seed architect.db from architectural-patterns.md (reuses import_md.parse):
    patterns → grouped by url into investigations + patterns;
    no-lift bullets → investigations(verdict=skip-with-flag). Idempotent.

    `verdict` is the operator's judgment for the URLs carrying patterns. When
    omitted it falls back to DEFAULT_INGEST_VERDICT ("patterns-only"), NOT the old
    hardcoded "full-eval" — see techne#428 and the VERDICTS comment above.

    No-lift bullets ignore the caller's `verdict` and write "skip-with-flag", but
    they do NOT write it unconditionally — that write is insert-only, so an
    operator who narrowed the judgment to "skip" via `reverdict` keeps it. The
    document's structure establishes that the URL was surveyed and nothing was
    lifted; it does not establish which of the two skip verdicts the operator
    means. (This paragraph previously claimed "unconditionally" and "correct by
    construction", contradicting the call-site comment 30 lines below in the very
    commit that made it insert-only — Prudence, PR #432.)

    KNOWN LIMITATION — verdicts have no provenance, so the earliest fallback wins
    forever, and two transitions are frozen (tracked in techne#433):
      * patterns → later demoted to a no-lift bullet: the row keeps its
        pattern-era verdict, because the no-lift write is insert-only. Pinned by
        test_demotion_to_no_lift_keeps_the_pattern_era_verdict so it changes
        deliberately rather than silently.
      * `--verdict` is per-INGEST, not per-URL, so passing one to correct a single
        URL also overwrites every other pattern-carrying URL in the same document,
        including ones whose judgment was set by `reverdict`. Use `reverdict` for
        single-row corrections.
    The opposite transition — a no-lift URL that later DOES yield a pattern — is
    handled, via `clears_skip`: a skip cannot survive a pattern being saved.
    """
    from techne.import_md import parse

    # `is None`, not falsy: `verdict or DEFAULT` silently coerced "" to the
    # default, so the empty string that upsert_investigation explicitly rejects
    # was quietly accepted on this path (Prudence, PR #432).
    stated = verdict is not None
    verdict = DEFAULT_INGEST_VERDICT if verdict is None else verdict
    if verdict not in VERDICTS:  # fail before any write, not partway through
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")

    parsed = parse(md_text)
    patterns_added = 0
    for p in parsed.patterns:
        inv_id = upsert_investigation(
            db_path,
            url=p.source_url,
            surveyed_at=p.surveyed_date,
            verdict=verdict,
            # A stated --verdict overwrites; the fallback is insert-only, so a
            # judgment recorded via `reverdict` survives a later plain re-ingest.
            overwrite_verdict=stated,
            # …but this write proves the URL DID yield a pattern, so a stale skip
            # verdict must not survive it, or the row is counted as a skip while
            # owning a pattern.
            clears_skip=True,
        )
        _pid, stored = add_pattern(
            db_path, investigation_id=inv_id, name=p.name, source_url=p.source_url, body=p.body
        )
        patterns_added += int(stored)
        # Typed graph edges declared as md fields (techne#308). Resolution is
        # idempotent and upgrades unresolved→resolved on later re-ingests, same as
        # the wikilink `related` edges backfilled below.
        for kind, dst_name in p.relations:
            add_relation(db_path, src_pattern_id=_pid, dst_name=dst_name, kind=kind)
    for nl in parsed.no_lifts:
        upsert_investigation(
            db_path,
            url=nl.source_url,
            surveyed_at=nl.surveyed_date,
            verdict="skip-with-flag",
            skip_reason=nl.reason,
            # Insert-only here too. "Correct by construction" is only half true: the
            # section header proves the URL was surveyed and nothing was lifted, but
            # NOT whether the operator's judgment is `skip` or `skip-with-flag` —
            # and `skip` is offered in reverdict's choices. Left overwriting, this
            # was Important #2 surviving on the second call site: `reverdict
            # --verdict skip` then a plain re-ingest reverted it (Prudence, PR #432).
            overwrite_verdict=False,
        )
    # Materialize the relation/applies graph from the bodies we just stored, and
    # re-resolve any edges that earlier ingests left dangling (P2 write-path).
    graph = backfill_graph(db_path)
    return {"patterns_added": patterns_added, "graph": graph, **stats(db_path)}
