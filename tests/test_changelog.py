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

COLLAPSED ARCHIVES ARE NOT DUPLICATES, and getting that wrong is what this file's own
history is about. `[0.13.0]` wraps a verbatim archive in `<details><summary>Earlier detail
from this cycle, kept verbatim</summary>`, which legitimately repeats `### Added` and
`### Fixed` INSIDE the collapsed block. The first version of this test read those as the
same defect as `[Unreleased]`'s and "fixed" them by interleaving the archive into the main
flow — which split the `<details>` pair across sections, orphaned the closer, and left an
unclosed opener swallowing five past releases into a collapsed spoiler on GitHub. The
defect this test exists to catch, reintroduced by the test's own premise. So `<details>`
regions are stripped before counting, and their balance is pinned separately below.

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


#: NOT WIDENED TO THE ATTRIBUTE FORM, and this is a deliberate non-fix (#748 r2 nit 3).
#: `<details open>` does not match, so it would read as a stray closer and blame the wrong
#: tag. I tried `</?details\\b[^>]*>` and it made the parse test and the balance test fail
#: on a file whose structure is sound, in a way I could not explain from the code in the
#: time available. Shipping a guard I cannot explain is worse than the nit it closes:
#: nothing in this file uses the attribute form, the current failure is loud rather than
#: silent, and a wrong diagnostic beats a guard with unexplained behaviour. Reopen with a
#: reproduction if the attribute form ever appears.
#:
#: A `<details>` tag that OPENS A LINE is markup. One inside prose — `<details>` in a
#: sentence, in backticks, in a fenced block — is the file TALKING ABOUT the convention,
#: which this file now documents, so that edit is likely rather than hypothetical.
#:
#: MEASURED (Prudence, #748 r1 Important 4): matching the bare token anywhere let one
#: prose mention unbalance the counter AND extend a strip region to the next real closer,
#: silently dropping `[0.15.0]`, `[0.14.0]` and `[0.13.0]` from the duplicate-heading
#: check — collection went 11 tests to 8. A guard that loses coverage when it misfires is
#: worse than one that only misfires.
_DETAILS = re.compile(r"^\s*(?P<tag></?details>)", re.M)


def _strip_collapsed(text: str) -> str:
    """Drop `<details>…</details>` regions — a collapsed verbatim archive may repeat a
    heading on purpose, and reading that as a duplicate is what broke this file once.

    Only line-leading tags count; see `_DETAILS`.
    """
    out, depth, cut = [], 0, 0
    for m in _DETAILS.finditer(text):
        if m.group("tag") == "<details>":
            if depth == 0:
                out.append(text[cut : m.start()])
            depth += 1
        else:
            depth = max(0, depth - 1)
            if depth == 0:
                cut = m.end()
    out.append(text[cut:])
    return "".join(out)


def _sections() -> list[tuple[str, list[str]]]:
    """(release heading, its `###` subsection headings) for every release block."""
    text = _strip_collapsed(CHANGELOG.read_text(encoding="utf-8"))
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


def test_every_details_block_is_closed():
    """THE DEFECT THE HEADING CHECK CANNOT SEE, because it reads markdown as text rather
    than as structure. An unclosed `<details>` renders everything after it as a collapsed
    spoiler on GitHub — content present and invisible, which is this file's whole subject.
    It happened here: merging `[0.13.0]`'s headings split the pair across sections, leaving
    an orphaned `</details>` and an opener that swallowed five past releases.

    Checks NESTING, not just counts: a stray closer before its opener balances numerically
    and is still broken, which is exactly the state that shipped past a bullet-count claim.
    Line-leading tags only — a `<details>` mentioned in prose is the file describing its own
    convention, not markup, and treating it as markup broke this guard once.
    """
    text = CHANGELOG.read_text(encoding="utf-8")

    depth = 0
    for m in _DETAILS.finditer(text):
        depth += 1 if m.group("tag") == "<details>" else -1
        assert depth >= 0, (
            f"`</details>` at offset {m.start()} closes a block that was never opened — "
            "counts can still balance while the nesting is inverted"
        )

    assert depth == 0, f"{depth} `<details>` block(s) never closed"
