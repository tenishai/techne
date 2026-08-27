"""Tests for the architectural-patterns markdown parser (techne#137 removed the
techne.db importer; `parse` now feeds only the Architect store)."""

from __future__ import annotations

from textwrap import dedent

import pytest

from techne.import_md import (
    NoLiftEntry,
    ParsedPattern,
    parse,
)

# ---- Parser ----------------------------------------------------------------


SAMPLE_DOC = dedent("""\
    ---
    name: patterns-fixture
    description: test corpus
    ---

    Some preamble that should be skipped.

    ## First pattern

    - **Source**: https://github.com/foo/bar — `path/file.ts`
    - **Surveyed**: 2026-06-09

    Body paragraph one. Spans multiple sentences and explains the pattern.

    **Why interesting**: it tests parsing.

    **Where to apply in techne**: nowhere actually.

    ---

    ## Second pattern

    - **Source**: https://github.com/baz/qux
    - **Surveyed**: 2026-06-10

    Different body.

    ---

    ## Composite observations (across patterns)

    This section is meta and should be skipped.

    ---

    ## Surveyed (no patterns worth saving)

    URLs we've looked at and decided nothing was novel enough to lift.

    - https://github.com/skip-1/repo — surveyed 2026-06-11 — trivial wrapper
    - https://github.com/skip-2/repo — surveyed 2026-06-12 — all standard idioms

    <!-- - <url> — surveyed YYYY-MM-DD — <reason> -->

    ---

    ## Template for new entries

    ```markdown
    ## <Pattern name>
    ...
    ```
""")


def test_parser_extracts_patterns() -> None:
    result = parse(SAMPLE_DOC)
    assert len(result.patterns) == 2

    first = result.patterns[0]
    assert isinstance(first, ParsedPattern)
    assert first.name == "First pattern"
    assert first.source_url == "https://github.com/foo/bar"
    assert first.surveyed_date == "2026-06-09"
    assert "Body paragraph one" in first.body
    assert "Why interesting" in first.body

    second = result.patterns[1]
    assert second.name == "Second pattern"
    assert second.source_url == "https://github.com/baz/qux"
    assert second.surveyed_date == "2026-06-10"


def test_parser_extracts_no_lifts() -> None:
    result = parse(SAMPLE_DOC)
    assert len(result.no_lifts) == 2
    first = result.no_lifts[0]
    assert isinstance(first, NoLiftEntry)
    assert first.source_url == "https://github.com/skip-1/repo"
    assert first.surveyed_date == "2026-06-11"
    assert first.reason == "trivial wrapper"


def test_parser_skips_composite_and_template_sections() -> None:
    result = parse(SAMPLE_DOC)
    names = {p.name for p in result.patterns}
    assert "Composite observations (across patterns)" not in names
    assert "Template for new entries" not in names


def test_parser_skips_section_missing_source() -> None:
    doc = dedent("""\
        ## Half-baked

        - **Surveyed**: 2026-06-10

        Body but no source link.
    """)
    assert parse(doc).patterns == []


def test_parser_skips_section_missing_date() -> None:
    doc = dedent("""\
        ## Half-baked

        - **Source**: https://github.com/foo/bar

        Body but no surveyed date.
    """)
    assert parse(doc).patterns == []


def test_parser_skips_html_comment_in_no_lift_list() -> None:
    """The HTML comment template line must NOT parse as a no-lift entry."""
    doc = dedent("""\
        ## Surveyed (no patterns worth saving)

        - https://github.com/real/entry — surveyed 2026-06-12 — actual reason

        <!-- - <url> — surveyed YYYY-MM-DD — <reason> -->
    """)
    result = parse(doc)
    assert len(result.no_lifts) == 1
    assert result.no_lifts[0].source_url == "https://github.com/real/entry"


def test_parser_handles_url_with_trailing_punctuation() -> None:
    """If a URL ends with `,` or `.`, we strip it so it's a clean source."""
    doc = dedent("""\
        ## Pattern

        - **Source**: https://github.com/foo/bar.
        - **Surveyed**: 2026-06-10

        Body
    """)
    result = parse(doc)
    assert result.patterns[0].source_url == "https://github.com/foo/bar"


def test_parser_skips_template_inside_code_block() -> None:
    """`## <Name>` inside a fenced code block must not split the parent
    'Template for new entries' section into its own pattern."""
    doc = dedent("""\
        ## Template for new entries

        ```markdown
        ## <Short pattern name>

        - **Source**: <url>
        - **Surveyed**: YYYY-MM-DD
        ```
    """)
    result = parse(doc)
    # Both Template and the inner placeholder must NOT produce a pattern;
    # Template is skipped (meta), placeholder never becomes a section.
    assert result.patterns == []


# --- the entry separator, and what must NOT be mistaken for one (techne#770) ---

@pytest.mark.parametrize(
    "label,body,expected",
    [
        ("trailing separator", "Alpha beta.\n\n---", "Alpha beta."),
        ("accreted separators", "Alpha beta.\n\n---\n\n---\n\n---", "Alpha beta."),
        ("no separator", "Alpha beta.", "Alpha beta."),
        ("asterisk rule", "Alpha beta.\n\n***", "Alpha beta."),
        ("underscore rule", "Alpha beta.\n\n___", "Alpha beta."),
        # A setext H2 underline is ATTACHED to its heading — no blank line above.
        # Popping it demotes a heading to a paragraph (Prudence, #778 Nit 2).
        ("setext H2 underline", "Alpha beta.\n\nCaveats\n---", "Alpha beta.\n\nCaveats\n---"),
        ("setext H1 underline", "Alpha beta.\n\nCaveats\n===", "Alpha beta.\n\nCaveats\n==="),
        # export writes the separator at COLUMN 0; an indented rule is a divider
        # inside a list item, not an entry separator (Prudence, #778 r2 Nit 1).
        ("indented rule in a list", "- alpha\n\n  ---", "- alpha\n\n  ---"),
        ("indented rule after prose", "Alpha beta.\n\n   ---", "Alpha beta.\n\n   ---"),
    ],
)
def test_strip_trailing_rule_removes_separators_but_not_setext_headings(label, body, expected):
    """`export_patterns_md` ALWAYS writes the separator after a blank line
    (`body`, `""`, `"---"`, `""`), so requiring a blank line above the rule
    separates a thematic break from a setext underline using the format's own
    signal rather than a guess about what bodies contain."""
    from techne.import_md import _strip_trailing_rule

    assert _strip_trailing_rule(body) == expected


def test_a_body_that_is_only_a_rule_collapses_to_empty():
    """Guards the `len(lines) == 1` arm of the precondition — with nothing above
    it, a lone rule cannot be a setext underline, so it is a separator."""
    from techne.import_md import _strip_trailing_rule

    assert _strip_trailing_rule("---") == ""


def test_a_malformed_no_lift_bullet_is_reported_not_dropped() -> None:
    """A bullet one word wrong must name ITSELF, not the heading (techne#787).

    THE DEFECT THIS REPLACES, measured on main. `_parse_no_lift_bullets` returned
    only the entries it matched, so a bullet reading `— RE-SURVEYED 2026-08-26 —`
    instead of `— surveyed 2026-08-26 —` vanished with `skipped` left empty. That
    empty list is what routes `architect ingest` into its heading-level branch, so
    the operator was told "sections must start with `## ` (two hashes)" about a file
    whose heading was EXACTLY that. Being sent to fix something that is not broken
    is worse than a vague error — the same sentence `_parse_pattern_body` already
    carries about misplaced markers, which the no-lift path never got.

    Asserts the KIND and that the offending line is quoted back, because those are
    what make the report actionable; the reason wording is free to change.
    """
    result = parse(
        dedent(
            """\
            ## Surveyed (no patterns worth saving)

            - https://github.com/foo/bar — RE-SURVEYED 2026-08-26 — one word wrong
            """
        )
    )

    assert result.no_lifts == []
    assert len(result.skipped) == 1
    (skip,) = result.skipped
    assert skip.kind == "no-lift bullet"
    assert "RE-SURVEYED" in skip.label, "the offending line is not quoted back"
    assert "surveyed" in skip.reason, "the reason does not name the required form"


def test_a_well_formed_no_lift_bullet_reports_nothing() -> None:
    """The other direction, and the one that keeps the report trustworthy.

    A guard that fires on correct input is worse than no guard: operators learn to
    ignore it, and this whole change exists because a WRONG message is worse than a
    vague one. Pinned so a widening of the malformed-bullet predicate cannot start
    flagging entries that parse.
    """
    result = parse(
        dedent(
            """\
            ## Surveyed (no patterns worth saving)

            - https://github.com/foo/bar — surveyed 2026-08-26 — perfectly fine
            """
        )
    )

    assert len(result.no_lifts) == 1
    assert result.skipped == ()


#: The boundary of `_URL_BULLET_RE`, pinned line by line (Prudence, #788 r1).
#: The first version matched a URL ANYWHERE in a bullet and flagged the three
#: `False` rows below. That is not merely noise: `architect ingest` gates its
#: heading-level advice on `if not counts["skipped"]`, so a false positive
#: SUPPRESSES a correct diagnosis — the #787 defect inverted.
_BULLET_BOUNDARY = [
    # (label, line, should_be_flagged)
    ("motivating case", "- https://github.com/foo/bar — RE-SURVEYED 2026-08-26 — r", True),
    ("en dashes", "- https://github.com/foo/bar – surveyed 2026-08-26 – r", True),
    ("hyphens", "- https://github.com/foo/bar - surveyed 2026-08-26 - r", True),
    ("no reason clause", "- https://github.com/foo/bar — surveyed 2026-08-26", True),
    ("bad date format", "- https://github.com/foo/bar — surveyed 26-08-2026 — r", True),
    ("angle-bracketed url", "- <https://github.com/foo/bar> — RE-SURVEYED 2026-08-26 — r", True),
    # THE FOUR DECORATED SPELLINGS (Prudence, #788 r2). The `<?`-only anchor dropped
    # all of these, and the give-up comment declared only the first. Their cost was
    # not a silent miss either: measured through the real CLI, a link-wrapped entry
    # under a correct `## ` heading made ingest report "sections must start with
    # `## `" — the #787 misdiagnosis this predicate exists to remove.
    (
        "link-wrapped url",
        "- [foo](https://github.com/foo/bar) — surveyed 2026-08-26 — r",
        True,
    ),
    ("backticked url", "- `https://github.com/foo/bar` — surveyed 2026-08-26 — r", True),
    ("bold url", "- **https://github.com/foo/bar** — surveyed 2026-08-26 — r", True),
    ("italic url", "- *https://github.com/foo/bar* — surveyed 2026-08-26 — r", True),
    # CommonMark has FOUR emphasis spellings, not two (Prudence, #788 r3). The r2
    # comment said "both emphasis forms" and named `*` and `**`; `_`, `__` and `~~`
    # were all still dropped, i.e. still misdiagnosed.
    ("underscore italic url", "- _https://github.com/foo/bar_ — surveyed 2026-08-26 — r", True),
    ("underscore bold url", "- __https://github.com/foo/bar__ — surveyed 2026-08-26 — r", True),
    ("strikethrough url", "- ~~https://github.com/foo/bar~~ — surveyed 2026-08-26 — r", True),
    # AND THE SPELLINGS STILL NOT COVERED, pinned as False so the gap is a recorded
    # fact rather than a surprise. The predicate is deliberately not exhaustive; an
    # uncovered decoration falls back to main's #787 misdiagnosis.
    (
        "quoted url (not covered)",
        '- "https://github.com/foo/bar" — surveyed 2026-08-26 — r',
        False,
    ),
    (
        "parenthesised url (not covered)",
        "- (https://github.com/foo/bar) — surveyed 2026-08-26 — r",
        False,
    ),
    # THE ACCEPTED TRADE (Prudence, #788 r3 Important 1). Link-FIRST commentary is
    # structurally identical to a link-wrapped entry up to the first token, so it is
    # accused. Recorded as a deliberate True, not an oversight; separating them means
    # keying on the remainder rather than the prefix, which is filed, not done here.
    (
        "link-first prose (accepted false positive)",
        "- [the upstream tracker](https://github.com/x/y/issues/787) explains why.",
        True,
    ),
    # AND THE FALSE POSITIVES THE WIDENING MUST NOT REINTRODUCE. `](` has to sit
    # immediately after the bullet, which is what keeps prose and task items out.
    ("task-list DONE with url first", "- [x] https://example.com was checked", False),
    (
        "commentary with a markdown link",
        "- Context lives in [the notes](https://example.com/n).",
        False,
    ),
    ("nested sub-bullet note", "  - the near-miss: https://github.com/foo/bar/auth.ts", False),
    ("task-list TODO", "- [ ] revisit https://example.com next quarter", False),
    ("bare note, no URL at all", "- a bare note with no URL in it at all", False),
]


@pytest.mark.parametrize(
    "label,line,flagged", _BULLET_BOUNDARY, ids=[c[0] for c in _BULLET_BOUNDARY]
)
def test_only_a_url_first_bullet_is_treated_as_a_malformed_entry(label, line, flagged) -> None:
    """Both halves of the predicate, one row at a time (techne#787, #788 r1).

    The `True` rows are malformed ENTRIES and must be reported — losing any of them
    reopens #787. The `False` rows are ordinary prose under the same heading and
    must NOT be, because a wrong accusation is the failure this whole change exists
    to remove, and because flagging one silences the heading-level advice that would
    otherwise be correct.

    Parametrised rather than bundled so a regression names WHICH construction broke;
    the previous single test asserted `skipped == ()` on a no-URL bullet, which
    guarded only half the boundary and passed happily while all three false
    positives were live.
    """
    body = "## Surveyed (no patterns worth saving)\n\n" + line + "\n"
    result = parse(body)
    was_flagged = any(sk.kind == "no-lift bullet" for sk in result.skipped)
    assert was_flagged is flagged, (
        f"{label!r}: expected flagged={flagged}, got {was_flagged} for {line!r}"
    )


def test_a_fenced_counter_example_is_not_flagged() -> None:
    """A `what not to write` block is not an entry (Prudence, #788 r1 Nit).

    `_split_into_sections` already tracks fence state; this was the one line-walker
    in the module that did not inherit it. The URL-first anchoring does NOT cover
    this — a fenced counter-example is usually a well-formed-LOOKING entry, which is
    the entire point of showing it — so it needs its own guard and its own witness.
    """
    result = parse(
        dedent(
            """\
            ## Surveyed (no patterns worth saving)

            Do not write it like this:

            ```
            - https://example.com - surveyed 2026-01-01 - hyphens, not em-dashes
            ```

            - https://github.com/foo/bar — surveyed 2026-08-26 — a real entry
            """
        )
    )

    assert len(result.no_lifts) == 1
    assert result.skipped == (), "a fenced counter-example was reported as an entry"


def test_a_skipped_pattern_section_still_reports_as_a_section() -> None:
    """`kind` defaults so the pre-techne#787 case is unchanged.

    `architect ingest` prints `skipped {kind} "{label}"`, and one test pins the
    exact string `skipped section "My pattern"`. Widening `SkippedSection` to carry
    bullets must not quietly re-word the message for the case that already worked.
    """
    result = parse(
        dedent(
            """\
            ## My pattern

            No metadata bullets at all, so this cannot be parsed.
            """
        )
    )

    assert result.patterns == []
    assert len(result.skipped) == 1
    (skip,) = result.skipped
    assert skip.kind == "section"
    assert skip.label == "My pattern"
