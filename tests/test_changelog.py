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


#: WIDENED TO THE ATTRIBUTE FORM in #748 r3, and the earlier failed attempt is worth
#: recording because it explains the shape of the pattern rather than just its history.
#: An earlier round tried `</?details\\b[^>]*>` — the same widening, WITHOUT the `^\\s*`
#: anchor — and the balance and parse tests failed on a file whose structure is sound,
#: for a reason I could not explain at the time. It is explainable now (Prudence, r3 I1):
#:
#:     unanchored  </?details\\b[^>]*>          -> 3 matches  (odd -> unbalanced)
#:     anchored    ^\\s*<(?P<slash>/?)details\\b[^>]*>  -> 2 matches  (balanced)
#:
#: The third match is CHANGELOG.md:515 — a `<details>` mentioned in PROSE, inside
#: backticks. So widening was never the problem; dropping the anchor was. Both halves of
#: this pattern are load-bearing and neither can be removed without the other's failure
#: reappearing.
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
_DETAILS = re.compile(r"^\s*<(?P<slash>/?)details\b[^>]*>", re.M)


def _details_depth(text: str) -> int:
    """Net `<details>` nesting depth, raising if a closer ever precedes its opener.

    A module-level helper rather than a loop inside the test, so the `slash`
    conditional is EXERCISED by anything that pins it. When the test re-implemented
    this loop, reverting this conditional to a literal `== "<details>"` comparison
    left the suite fully green (Prudence, #748 r3 I2, mutation M2): the real
    CHANGELOG.md contains no attribute form, so the literal comparison still worked
    there, and the only test covering the attribute form was checking its own copy
    of the logic.
    """
    depth = 0
    for m in _DETAILS.finditer(text):
        depth += 1 if not m.group("slash") else -1
        assert depth >= 0, (
            f"`</details>` at offset {m.start()} closes a block that was never opened — "
            "counts can still balance while the nesting is inverted"
        )
    return depth


def _strip_collapsed(text: str) -> str:
    """Drop `<details>…</details>` regions — a collapsed verbatim archive may repeat a
    heading on purpose, and reading that as a duplicate is what broke this file once.

    Only line-leading tags count; see `_DETAILS`.
    """
    out, depth, cut = [], 0, 0
    for m in _DETAILS.finditer(text):
        if not m.group("slash"):
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
    assert _details_depth(text) == 0, "`<details>` block(s) never closed"


def test_an_attribute_bearing_details_tag_is_read_as_an_opener():
    """`<details open>` is legal, common, and was matched only on its CLOSER.

    `_DETAILS` was `</?details>`, requiring the immediate `>`. A single
    well-formed `<details open>…</details>` block therefore contributed one tag
    instead of two, counted as -1, and failed with *"closes a block that was
    never opened"* — an error naming the closer when the opener is what the
    pattern could not see. Worse for `_strip_collapsed`: with no opener the
    region is never cut, so a collapsed archive's repeated headings flow into
    the duplicate-heading check, which is the exact failure the strip exists to
    prevent (Prudence, #748 r2 N3).

    Nothing in CHANGELOG.md uses the attribute form today. That is why this is
    pinned rather than left to the file: the guard would misfire on the first
    one added, and the message would send the reader to the wrong tag.
    """
    for markup in ("<details open>", '<details class="archive">', "<details>"):
        text = f"# Head\n\n{markup}\n## [0.1.0]\n</details>\n\n## [0.1.0]\n"

        assert _details_depth(text) == 0, (
            f"{markup} did not balance — read as a closer, or never closed"
        )

        stripped = _strip_collapsed(text)
        assert stripped.count("## [0.1.0]") == 1, (
            f"{markup}: the collapsed region was not cut, so its repeated "
            f"heading survives into the duplicate-heading check"
        )
        # THE COUNT ALONE CANNOT TELL THE TWO FAILURES APART (Prudence r3 I2, M3).
        # Misread the opener as a closer and `cut` jumps past it: the heading INSIDE
        # the block is dropped and the one OUTSIDE survives — count 1 either way, for
        # opposite reasons, and everything before the block is silently deleted.
        assert "# Head" in stripped, (
            f"{markup}: text BEFORE the block was deleted — the opener was read as "
            f"a closer, which the heading count cannot distinguish"
        )


#: Matches a RELEASED block heading — `## [0.16.1] — 2026-08-30`. `[Unreleased]` is
#: deliberately not matched: it is in flight, and the section it needs is written at
#: cut time, which is exactly the moment the test below starts applying to it.
_RELEASED = re.compile(r"^## \[\d+\.\d+\.\d+\]")


def test_the_newest_released_block_carries_an_upgrading_section():
    """A release must SAY what the operator has to run, or say that nothing is needed.

    WHY THIS EXISTS, and why it is a test rather than a habit. v0.16.0 shipped with no
    upgrade guidance in this file and none in its GitHub Release body: four headings,
    Added/Changed/Fixed/Docs, and in 925 lines the string `vectors backfill` appears
    once, inside a narrative aside. Two external users installed that tag, ran no
    `techne init` and no `techne vectors backfill`, and got an install whose hook chain
    was whatever their previous version wrote and whose vector index was empty. Recall
    returned nothing and nothing said why.

    That failure was already known. techne#575 — "no upgrade path for tag-installing
    users" — was filed, fixed, and CLOSED on 2026-08-18. v0.15.0 shipped the next day
    with an `### Upgrading` section. v0.16.0 shipped nine days later without one. The
    practice survived exactly one release, because a closed issue is a claim that
    something was fixed, not a mechanism that keeps it fixed. Two of eleven blocks in
    this file carry the section; the check below is what makes the third onward.

    SCOPED TO THE NEWEST RELEASED BLOCK ON PURPOSE. The nine older blocks cannot be
    repaired — their releases are published and their bodies are history — so a check
    that spanned the file would fire on nine permanent failures and be suppressed within
    a day. This file already argues that case for the commit-coverage check it declines
    to make: "a check that fires on legitimate omissions gets trained into noise." One
    block, one assertion, firing exactly once per release cut.

    WHAT THIS CANNOT SEE, stated so nobody reads it as broader cover than it is: the
    PUBLISHED GitHub Release body. That is the surface that actually failed for v0.16.0,
    and no test can reach it. The link is procedural — the release step sources the body
    from this section — so this test pins the source and the SOP has to carry the rest.
    If the body ever stops being generated from here, this check goes decorative and the
    only warning will be another follow-up release.
    """
    for heading, subsections in _sections():
        if not _RELEASED.match(heading):
            continue
        assert "Upgrading" in subsections, (
            f"{heading} has no `### Upgrading` section. Every release states what the "
            f"operator must run after upgrading — `techne init` to rewrite the hook "
            f"chain, `techne vectors backfill` to embed — or states plainly that "
            f"nothing is needed. v0.16.0 omitted this and shipped a silently broken "
            f"recall path to two external users (techne#575, techne#814). Found: "
            f"{subsections}"
        )
        return

    pytest.fail("no released version block found — the heading format changed")
