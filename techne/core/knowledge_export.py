"""Knowledge export — human-readable markdown projection (techne#218, coherence D5).

READ-ONLY. Renders live knowledge (superseded_by IS NULL) to a flat,
git-diffable markdown tree: an index file + one file per knowledge type.

Determinism guarantee: entries are ordered by (type, id) — a stable integer
key that never reorders on re-export. No generated-at timestamps appear in
output bodies, so re-exports on an unchanged DB produce byte-identical files.
"""

from __future__ import annotations

from pathlib import Path

from techne.core.database import connect

# Known types in display order; unknown future types append in sorted order.
EXPORT_TYPES = ("fact", "decision", "procedure", "evaluation", "document", "document_card")

_READ_ONLY_NOTICE = (
    "> **Read-only projection.** Source of truth: `techne.db`. "
    "Re-export with `techne knowledge export` to refresh. "
    "Ordering: type → id (stable; re-exports on an unchanged DB are byte-identical).\n"
)


def query_live_knowledge(db_path: Path, *, include_superseded: bool = False) -> list[dict]:
    """Return knowledge entries in deterministic (type, id) order.

    Live only (superseded_by IS NULL) by default. Pass include_superseded=True
    to include the full history including superseded revisions.
    """
    conn = connect(db_path)
    try:
        where_extra = "" if include_superseded else "AND superseded_by IS NULL"
        rows = conn.execute(
            f"""SELECT id, type, content, tags, confidence, status,
                       event_at, last_confirmed_at, created_at, source
                FROM knowledge
                WHERE 1=1 {where_extra}
                ORDER BY type, id""",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-entry rendering
# ---------------------------------------------------------------------------


def _date_only(raw) -> str:
    """Normalize a timestamp value to a YYYY-MM-DD date string.

    Reuses knowledge._parse_timestamp (handles both SQLite ISO text and
    datetime objects) so a non-string type never silently yields a garbage
    prefix. Unparseable/None → "" (field is then omitted from the metadata line).
    """
    if raw is None:
        return ""
    from techne.core.knowledge import _parse_timestamp

    try:
        return _parse_timestamp(raw).date().isoformat()
    except (ValueError, TypeError):
        # Last-resort: assume ISO text; a bad value drops the field rather than crash.
        text = str(raw)[:10]
        return text if len(text) == 10 else ""


def _render_entry(entry: dict) -> str:
    """Render one knowledge entry as a markdown section.

    Heading anchors use the numeric id so in-page links from the index are
    stable across content edits (the id never changes once written).
    """
    eid = entry["id"]
    content = (entry.get("content") or "").strip()
    confidence = entry.get("confidence") or "medium"
    status = entry.get("status") or "current"
    tags = (entry.get("tags") or "").strip()
    # Date-only (YYYY-MM-DD) — avoids sub-second churn on re-export.
    last_confirmed = _date_only(entry.get("last_confirmed_at"))
    event_at = _date_only(entry.get("event_at"))

    # Heading: id + first line of content (truncated for readability).
    # Status badge only when non-current — avoids churn on `current` entries.
    first_line = content.split("\n")[0]
    heading_title = first_line[:72] + ("…" if len(first_line) > 72 else "")
    status_badge = f" `⚠ {status}`" if status != "current" else ""
    heading = f"## [{eid}]{status_badge} {heading_title}"

    # Explicit anchor GitHub honors. GitHub auto-slugifies the `##` heading to
    # something like `#1-use-sqlite-…`, NOT `#1`, so an index link keyed on the id
    # alone would load the file but never scroll to the entry. Emit a stable,
    # id-prefixed anchor (`e{id}`) that the index links to — it never collides with
    # the auto-slug and never changes when content changes (deterministic per id).
    anchor = f'<a id="e{eid}"></a>'

    parts = [anchor, "", heading, "", content, ""]

    # Metadata line — only include fields that carry a non-empty value.
    meta: list[str] = [f"**Confidence:** {confidence}"]
    if tags:
        tag_parts = [f"`{t.strip()}`" for t in tags.split(",") if t.strip()]
        if tag_parts:
            meta.append(f"**Tags:** {', '.join(tag_parts)}")
    if event_at:
        meta.append(f"**Event:** {event_at}")
    if last_confirmed:
        meta.append(f"**Confirmed:** {last_confirmed}")

    parts.append(" · ".join(meta))
    parts.append("")
    parts.append("---")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-type file rendering
# ---------------------------------------------------------------------------


def _render_type_file(ktype: str, entries: list[dict]) -> str:
    """Render a per-type markdown file for *entries* (already sorted by id)."""
    label = ktype.replace("_", " ").title() + "s"
    count = len(entries)
    count_str = f"{count} {'entry' if count == 1 else 'entries'}"
    lines: list[str] = [
        f"# {label}",
        "",
        _READ_ONLY_NOTICE,
        f"**{count_str}.**",
        "",
    ]
    for entry in entries:
        lines.append(_render_entry(entry))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Index file rendering
# ---------------------------------------------------------------------------


def _render_index(
    by_type: dict[str, list[dict]],
    *,
    include_superseded: bool,
) -> str:
    """Render `index.md` — type summary table + per-type entry stubs."""
    total = sum(len(v) for v in by_type.values())
    scope = (
        "all entries (including superseded)"
        if include_superseded
        else "live entries (superseded_by IS NULL)"
    )

    lines: list[str] = [
        "# techne knowledge",
        "",
        _READ_ONLY_NOTICE,
        f"**Scope:** {scope}  ",
        f"**Total entries:** {total}",
        "",
        "## Contents by type",
        "",
        "| Type | Count | File |",
        "|------|------:|------|",
    ]

    # Known types first (stable display order), then any unknown types alphabetically.
    ordered_types: list[str] = [t for t in EXPORT_TYPES if t in by_type]
    extra_types = sorted(k for k in by_type if k not in EXPORT_TYPES)
    for ktype in ordered_types + extra_types:
        count = len(by_type[ktype])
        lines.append(f"| {ktype} | {count} | [{ktype}.md]({ktype}.md) |")

    lines.append("")

    # Entry stubs grouped by type — brief summary + anchor link into the type file.
    for ktype in ordered_types + extra_types:
        entries = by_type[ktype]
        label = ktype.replace("_", " ").title() + "s"
        lines.append(f"## {label}")
        lines.append("")
        for e in entries:
            content = (e.get("content") or "").strip()
            first_line = content.split("\n")[0]
            stub = first_line[:100] + ("…" if len(first_line) > 100 else "")
            conf = e.get("confidence") or "medium"
            # Anchor href targets the explicit `e{id}` anchor emitted by
            # _render_entry — GitHub honors it and scrolls to the entry (the
            # auto-slug of the heading would not match a plain `#{id}`).
            eid = e["id"]
            lines.append(
                f"- **[{eid}]** ({conf}) {stub}"
                f" → [{ktype}.md#e{eid}]({ktype}.md#e{eid})"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def export_knowledge(
    db_path: Path,
    output_dir: Path | str,
    *,
    include_superseded: bool = False,
) -> dict:
    """Export live knowledge to a flat markdown tree under *output_dir*.

    Returns::

        {
            "total": int,                   # total entries exported
            "by_type": {type: count, ...},  # per-type counts
            "files_written": [str, ...],    # absolute paths written
        }

    The tree is deterministic: same DB + same inputs → byte-identical files.
    Re-exports only produce git diffs when the underlying data actually changed.
    No generated-at timestamps appear in output — only the knowledge entries'
    own ``last_confirmed_at`` / ``event_at`` fields, which only change when
    the data changes.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    entries = query_live_knowledge(db_path, include_superseded=include_superseded)

    # Group by type, preserving the (type, id) ORDER BY from the query.
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e["type"], []).append(e)

    files_written: list[str] = []

    # Per-type files — written before the index so a reader can follow links.
    for ktype, type_entries in by_type.items():
        text = _render_type_file(ktype, type_entries)
        path = output_dir / f"{ktype}.md"
        path.write_text(text, encoding="utf-8")
        files_written.append(str(path))

    # Index file (written last; complete picture of what's on disk).
    index_text = _render_index(by_type, include_superseded=include_superseded)
    index_path = output_dir / "index.md"
    index_path.write_text(index_text, encoding="utf-8")
    files_written.append(str(index_path))

    return {
        "total": len(entries),
        "by_type": {k: len(v) for k, v in by_type.items()},
        "files_written": files_written,
    }
