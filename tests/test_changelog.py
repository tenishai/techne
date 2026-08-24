"""The release notes must describe the whole release, not half of it.

WHY THIS FILE EXISTS. At the 0.16.0 audit, `[Unreleased]` was missing entries for TEN
of the twenty-one commits merged since v0.15.0 — including a schema renumber
(architect.db v4 -> v5) and every one of the five fixes merged that day. Had we tagged,
the notes would have described half the release.

The structural half of that is what this pins, because it is the part a test can see:
the section carried **two** `### Added` blocks and **two** `### Fixed` blocks. A reader
scanning for "Fixed" finds the first one and stops, so the second was invisible even
though its content was present. That is not a content judgement — it is a duplicate
heading, and a duplicate heading is checkable.

DELIBERATELY NOT CHECKED HERE: whether every merged commit has an entry. That needs git
history at test time, it cannot distinguish a docs-only commit from a missed feature,
and a check that fires on legitimate omissions gets trained into noise. The audit is a
release step; this is the guard that stops the file from silently hiding what it does
carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _sections() -> list[tuple[str, list[str]]]:
    """(release heading, its `###` subsection headings) for every release block."""
    text = CHANGELOG.read_text(encoding="utf-8")
    blocks = re.split(r"^(## \[.+?\].*)$", text, flags=re.M)
    out = []
    for i in range(1, len(blocks), 2):
        out.append((blocks[i].strip(), re.findall(r"^### (.+)$", blocks[i + 1], flags=re.M)))
    return out


def test_the_changelog_parses_into_release_blocks():
    """A precondition, not a formality: every assertion below is vacuously true if the
    split finds nothing, which is the way a structural test quietly stops testing."""
    blocks = _sections()

    assert blocks, "no release blocks found — the heading format changed"
    assert blocks[0][0].startswith("## [Unreleased]"), blocks[0][0]


@pytest.mark.parametrize(
    "release,subsections", _sections(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_release_block_repeats_a_section_heading(release, subsections):
    """Two `### Fixed` blocks under one release means a reader who scans to the first
    one never learns the second exists. Measured on `[Unreleased]` at the 0.16.0 audit:
    Added / Changed / Fixed / **Added** / **Fixed**."""
    duplicates = [h for h in set(subsections) if subsections.count(h) > 1]

    assert not duplicates, (
        f"{release} repeats {duplicates}. Merge them — a second block with the same "
        "heading is invisible to anyone scanning the notes."
    )
