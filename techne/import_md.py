"""Architectural-patterns markdown corpus parser.

`parse()` turns an `architectural-patterns.md`-style corpus into a `ParseResult`
(patterns + no-lift entries). It is the shared front end for the Architect store:
`patterns.ingest_patterns_md` seeds `architect.db` from a parsed corpus. This module
is pure parsing — it does NOT write to any store. (The old `import_into` writer that
dumped `type="pattern"` rows into personal techne.db was removed in techne#137; the
pattern corpus lives only in architect.db now.)

Two kinds of sections recognized:

  1. Pattern sections — `## <Pattern Name>` followed by lines
     `- **Source**: <text>` and `- **Surveyed**: YYYY-MM-DD`, then a
     body of paragraphs until the next `---` separator or `## ` heading.

  2. No-lift list — bullets under `## Surveyed (no patterns worth saving)`
     of the form `- <url> — surveyed YYYY-MM-DD — <reason>`. Preserves the
     "don't re-investigate" dedup signal.

Skipped sections: `## Composite observations`, `## Template for new entries`,
and any `## Surveyed` that isn't the no-lift list (defensive — we only
match the exact heading we ship).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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
