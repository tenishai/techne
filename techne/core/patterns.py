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
_KNOWN_TARGETS = {"techne", "cabal", "limenal", "agent-infra"}


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
        t = raw.lower()
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
) -> str:
    """Insert/update one investigation (the dedup unit). Returns its id."""
    inv_id = _hash(url, surveyed_by, lens)
    conn = plain_connect(db_path)
    try:
        conn.execute(
            """INSERT INTO investigations
                 (id, url, surveyed_by, lens, surveyed_at, verdict, summary, skip_reason, maturity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url, surveyed_by, lens) DO UPDATE SET
                 surveyed_at = excluded.surveyed_at, verdict = excluded.verdict,
                 summary = COALESCE(excluded.summary, investigations.summary),
                 skip_reason = COALESCE(excluded.skip_reason, investigations.skip_reason),
                 maturity = COALESCE(excluded.maturity, investigations.maturity)""",
            (inv_id, url, surveyed_by, lens, surveyed_at, verdict, summary, skip_reason, maturity),
        )
        conn.commit()
        return inv_id
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
    """Tag a pattern as applying to a project target (techne|cabal|limenal|…).
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


def supersede(
    db_path: Path, *, old_id: str, new_id: str, absorbed: str | None = None
) -> bool:
    """Mark *old_id* as superseded by *new_id*, atomically (techne#308, closes #307).

    In ONE transaction: stamp `old.status='superseded'`, `old.superseded_by=new_id`,
    `old.absorbed=<salvage note>` (kept if not re-supplied), AND upsert a resolved
    `supersedes` edge (old → new) so the relation graph reflects the supersession.
    Both patterns must exist and differ. Idempotent. Returns True if the row changed.

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
        with conn:  # atomic: both writes commit together, or neither does
            cur = conn.execute(
                """UPDATE patterns
                     SET status = 'superseded', superseded_by = ?,
                         absorbed = COALESCE(?, absorbed)
                   WHERE id = ?""",
                (new_id, absorbed, old_id),
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
    """All active patterns tagged as applying to *target* (techne|cabal|limenal|…)."""
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
    """FTS over active patterns (name + body)."""
    from techne.core.search import prepare_fts_query

    fts = prepare_fts_query(query)  # None when empty/all-punctuation → MATCH '' crash (#143)
    if fts is None:
        return []
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT p.id, p.name, p.source_url, p.body, p.confidence, bm25(patterns_fts) AS rank
               FROM patterns_fts f JOIN patterns p ON p.rowid = f.rowid
               WHERE patterns_fts MATCH ? AND p.status = 'active'
               ORDER BY rank LIMIT ?""",
            (fts, limit),
        ).fetchall()
        return [dict(r) for r in rows]
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


def ingest_patterns_md(db_path: Path, md_text: str) -> dict:
    """Seed architect.db from architectural-patterns.md (reuses import_md.parse):
    patterns → grouped by url into investigations(verdict=full-eval) + patterns;
    no-lift bullets → investigations(verdict=skip-with-flag). Idempotent."""
    from techne.import_md import parse

    parsed = parse(md_text)
    patterns_added = 0
    for p in parsed.patterns:
        inv_id = upsert_investigation(
            db_path, url=p.source_url, surveyed_at=p.surveyed_date, verdict="full-eval"
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
        )
    # Materialize the relation/applies graph from the bodies we just stored, and
    # re-resolve any edges that earlier ingests left dangling (P2 write-path).
    graph = backfill_graph(db_path)
    return {"patterns_added": patterns_added, "graph": graph, **stats(db_path)}
