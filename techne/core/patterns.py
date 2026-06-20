"""Pattern store data layer over architect.db (techne#87).

CRUD + dedup + search for the Architect's pattern domain. Backend-portable in
spirit (Postgres is P3) but SQLite-direct for now via plain_connect — architect.db
is a domain db, so it gets only its own schema (no techne core tables).

Dedup is lens-aware: `investigations` keyed (url × surveyed_by × lens) makes the
SRE-vs-infra comparison a join. Patterns hang off an investigation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from techne.core.stores import plain_connect


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


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
    from techne.core.search import _sanitize_fts_query

    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT p.id, p.name, p.source_url, p.body, p.confidence, bm25(patterns_fts) AS rank
               FROM patterns_fts f JOIN patterns p ON p.rowid = f.rowid
               WHERE patterns_fts MATCH ? AND p.status = 'active'
               ORDER BY rank LIMIT ?""",
            (_sanitize_fts_query(query), limit),
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
        "Generated export from techne architect.db (`techne architect export`). "
        "Re-import with `techne architect ingest`. architect.db is canonical — "
        "edit there (via the architect persona), not here.",
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
    for nl in parsed.no_lifts:
        upsert_investigation(
            db_path,
            url=nl.source_url,
            surveyed_at=nl.surveyed_date,
            verdict="skip-with-flag",
            skip_reason=nl.reason,
        )
    return {"patterns_added": patterns_added, **stats(db_path)}
