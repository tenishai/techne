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
