"""Architectural-patterns markdown corpus importer.

Parses a markdown file structured like
`~/.claude/projects/.../memory/architectural-patterns.md` into knowledge
entries for the techne knowledge store.

Two kinds of sections recognized:

  1. Pattern sections — `## <Pattern Name>` followed by lines
     `- **Source**: <text>` and `- **Surveyed**: YYYY-MM-DD`, then a
     body of paragraphs until the next `---` separator or `## ` heading.
     Stored with type="pattern", source=<url>, tags include the date.

  2. No-lift list — bullets under `## Surveyed (no patterns worth saving)`
     of the form `- <url> — surveyed YYYY-MM-DD — <reason>`. Stored with
     type="pattern", source=<url>, tags include `surveyed-no-lift` and
     the date. Preserves the "don't re-investigate" dedup signal.

Skipped sections: `## Composite observations`, `## Template for new entries`,
and any `## Surveyed` that isn't the no-lift list (defensive — we only
match the exact heading we ship).

Dedup is by source URL — if any existing knowledge entry has the same
source, the importer skips the new entry with a log line. `--force`
overrides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedPattern:
    """A pattern section ready to be stored."""

    name: str
    source_url: str
    surveyed_date: str  # YYYY-MM-DD
    body: str  # full markdown body — everything after the Source/Surveyed lines


@dataclass(frozen=True, slots=True)
class NoLiftEntry:
    """A 'surveyed but nothing to lift' bullet."""

    source_url: str
    surveyed_date: str  # YYYY-MM-DD
    reason: str  # short human-readable why-not


@dataclass(frozen=True, slots=True)
class ParseResult:
    patterns: list[ParsedPattern]
    no_lifts: list[NoLiftEntry]


# Skip these heading titles (case-insensitive prefix match) when scanning
# for pattern sections — they're meta or template content, not patterns.
SKIPPED_HEADINGS = (
    "composite observations",
    "template for new entries",
)

NO_LIFT_HEADING = "surveyed (no patterns worth saving)"


# A pattern's URL line looks like:
#   - **Source**: https://github.com/foo/bar — `path/to/file.ts`
# We grab the first http(s) URL on the line; the trailing path comment is
# decorative and not stored separately.
_URL_RE = re.compile(r"https?://\S+")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse(text: str) -> ParseResult:
    """Parse a markdown document into ParsedPattern + NoLiftEntry lists.

    Tolerant by design: unrecognized sections are skipped silently;
    malformed pattern bodies (missing source/date) are skipped with a log.
    """
    patterns: list[ParsedPattern] = []
    no_lifts: list[NoLiftEntry] = []

    sections = _split_into_sections(text)
    for heading, body in sections:
        heading_lower = heading.lower()
        if heading_lower == NO_LIFT_HEADING:
            no_lifts.extend(_parse_no_lift_bullets(body))
            continue
        if any(heading_lower.startswith(skip) for skip in SKIPPED_HEADINGS):
            continue
        # Anything else is a pattern candidate
        parsed = _parse_pattern_body(heading, body)
        if parsed is not None:
            patterns.append(parsed)

    return ParseResult(patterns=patterns, no_lifts=no_lifts)


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading, body), ...] split on `## ` top-level headings.

    Anything before the first `## ` heading is dropped (frontmatter,
    file-level prose). Headings INSIDE fenced code blocks (```) are
    treated as body content, not section breaks, so example markdown in
    a "Template for new entries" section doesn't fragment the parse.
    """
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    in_fence = False

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if current_heading is not None:
                current_body.append(line)
            continue
        # `## ` heading (exactly two hashes; deeper levels stay in body).
        # Skip detection while inside a fenced code block.
        if not in_fence and line.startswith("## ") and not line.startswith("### "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line[3:].strip()
            current_body = []
        elif current_heading is not None:
            current_body.append(line)

    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_body).strip()))

    return sections


def _parse_pattern_body(heading: str, body: str) -> ParsedPattern | None:
    """Extract URL + date + remaining body for a pattern section.

    Returns None and logs a warning if the section lacks a Source or
    Surveyed line.
    """
    source_line: str | None = None
    date_line: str | None = None
    body_lines: list[str] = []

    state: Literal["preamble", "body"] = "preamble"
    for line in body.splitlines():
        if state == "preamble":
            stripped = line.strip()
            if stripped.startswith("- **Source**:"):
                source_line = stripped
                continue
            if stripped.startswith("- **Surveyed**:"):
                date_line = stripped
                continue
            if stripped == "":
                # Blank line still in preamble — skip
                continue
            # First non-source/date/blank line: body starts
            state = "body"
        if state == "body":
            body_lines.append(line)

    if source_line is None or date_line is None:
        logger.warning(
            "skip_section",
            extra={"heading": heading, "reason": "missing Source or Surveyed line"},
        )
        return None

    url_match = _URL_RE.search(source_line)
    date_match = _DATE_RE.search(date_line)
    if url_match is None or date_match is None:
        logger.warning(
            "skip_section",
            extra={"heading": heading, "reason": "could not extract URL or date"},
        )
        return None

    return ParsedPattern(
        name=heading,
        source_url=url_match.group(0).rstrip("),.;"),
        surveyed_date=date_match.group(1),
        body="\n".join(body_lines).strip(),
    )


# A no-lift bullet looks like:
#   - https://github.com/foo/bar — surveyed 2026-06-12 — short reason here
# Em-dashes (—) separate the URL, the surveyed clause, and the reason.
_NO_LIFT_RE = re.compile(
    r"^\s*-\s+(?P<url>https?://\S+)\s*—\s*surveyed\s+(?P<date>\d{4}-\d{2}-\d{2})\s*—\s*(?P<reason>.+)$"
)


def _parse_no_lift_bullets(body: str) -> list[NoLiftEntry]:
    """Walk the body, pulling out each `- <url> — surveyed <date> — <reason>` line."""
    out: list[NoLiftEntry] = []
    for line in body.splitlines():
        match = _NO_LIFT_RE.match(line)
        if match is None:
            continue
        out.append(
            NoLiftEntry(
                source_url=match.group("url").rstrip("),.;"),
                surveyed_date=match.group("date"),
                reason=match.group("reason").strip(),
            )
        )
    return out


# ---- Importer (knowledge.store integration) -------------------------------


@dataclass(slots=True)
class ImportResult:
    """Counts returned from import_into."""

    patterns_added: int = 0
    patterns_skipped_existing: int = 0
    no_lifts_added: int = 0
    no_lifts_skipped_existing: int = 0


def import_into(
    db_path: Path,
    parsed: ParseResult,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ImportResult:
    """Apply a ParseResult to a techne knowledge corpus.

    Dedup by `source` URL — if any active knowledge entry has the same
    source as a new pattern, the new entry is skipped with a log line.
    `force=True` writes anyway (still creates a new row; supersession is
    not used here because the corpus is append-only with re-runs).

    `dry_run=True` parses + computes the would-add/would-skip counts
    without writing.
    """
    from techne.core import knowledge

    # Dedup is per (source_url, pattern-name) for patterns and
    # (source_url, "<no-lift>") for no-lifts. Two patterns lifted from
    # the same repo (e.g., OpenLtm has three) are kept separate.
    existing_keys = _existing_keys(db_path)

    result = ImportResult()

    for pattern in parsed.patterns:
        key = (pattern.source_url, pattern.name)
        if not force and key in existing_keys:
            result.patterns_skipped_existing += 1
            logger.info(
                "skip_existing_pattern",
                extra={"source": pattern.source_url, "name": pattern.name},
            )
            continue
        if not dry_run:
            content = f"# {pattern.name}\n\n{pattern.body}".strip()
            knowledge.store(
                db_path,
                content=content,
                type="pattern",
                tags=f"surveyed:{pattern.surveyed_date}",
                confidence="medium",
                source=pattern.source_url,
            )
            existing_keys.add(key)
        result.patterns_added += 1

    for no_lift in parsed.no_lifts:
        key = (no_lift.source_url, "<no-lift>")
        if not force and key in existing_keys:
            result.no_lifts_skipped_existing += 1
            logger.info(
                "skip_existing_no_lift",
                extra={"source": no_lift.source_url},
            )
            continue
        if not dry_run:
            knowledge.store(
                db_path,
                content=no_lift.reason,
                type="pattern",
                tags=f"surveyed-no-lift,surveyed:{no_lift.surveyed_date}",
                confidence="low",
                source=no_lift.source_url,
            )
            existing_keys.add(key)
        result.no_lifts_added += 1

    return result


def _existing_keys(db_path: Path) -> set[tuple[str, str]]:
    """Return every (source_url, identifier) tuple already in the corpus.

    For patterns, the identifier is the pattern name extracted from the
    first H1-style line in content. For no-lifts (tagged
    `surveyed-no-lift`), the identifier is the literal "<no-lift>".
    """
    from techne.core.database import connect

    if not db_path.exists():
        return set()
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, content, tags FROM knowledge "
            "WHERE source IS NOT NULL AND superseded_by IS NULL"
        ).fetchall()
    finally:
        conn.close()

    out: set[tuple[str, str]] = set()
    for row in rows:
        source = row["source"]
        tags = row["tags"] or ""
        if "surveyed-no-lift" in tags:
            out.add((source, "<no-lift>"))
            continue
        name = _extract_pattern_name(row["content"])
        if name is not None:
            out.add((source, name))
    return out


def _extract_pattern_name(content: str) -> str | None:
    """Pull the `# <Name>` heading from the first non-blank line of content."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip()
        return None
    return None
