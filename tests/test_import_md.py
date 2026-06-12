"""Tests for the architectural-patterns markdown importer."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from techne.core.database import init_db
from techne.core.knowledge import list_entries
from techne.import_md import (
    NoLiftEntry,
    ParsedPattern,
    import_into,
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


# ---- Importer roundtrip ---------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test_corpus.db"
    init_db(p).close()
    return p


def test_import_writes_entries(db_path: Path) -> None:
    parsed = parse(SAMPLE_DOC)
    result = import_into(db_path, parsed)
    assert result.patterns_added == 2
    assert result.no_lifts_added == 2
    assert result.patterns_skipped_existing == 0
    assert result.no_lifts_skipped_existing == 0

    entries = list_entries(db_path, type="pattern", limit=100)
    assert len(entries) == 4
    sources = {e["source"] for e in entries}
    assert sources == {
        "https://github.com/foo/bar",
        "https://github.com/baz/qux",
        "https://github.com/skip-1/repo",
        "https://github.com/skip-2/repo",
    }


def test_import_is_idempotent(db_path: Path) -> None:
    """Re-running with the same content adds nothing."""
    parsed = parse(SAMPLE_DOC)
    import_into(db_path, parsed)
    second = import_into(db_path, parsed)
    assert second.patterns_added == 0
    assert second.no_lifts_added == 0
    assert second.patterns_skipped_existing == 2
    assert second.no_lifts_skipped_existing == 2

    # Corpus size unchanged after the second pass.
    entries = list_entries(db_path, type="pattern", limit=100)
    assert len(entries) == 4


def test_import_dry_run_writes_nothing(db_path: Path) -> None:
    parsed = parse(SAMPLE_DOC)
    result = import_into(db_path, parsed, dry_run=True)
    assert result.patterns_added == 2  # counts what WOULD be added
    assert result.no_lifts_added == 2
    assert list_entries(db_path, type="pattern", limit=100) == []


def test_import_force_writes_duplicate(db_path: Path) -> None:
    parsed = parse(SAMPLE_DOC)
    import_into(db_path, parsed)
    second = import_into(db_path, parsed, force=True)
    assert second.patterns_added == 2  # force = re-add
    entries = list_entries(db_path, type="pattern", limit=100)
    assert len(entries) == 8  # original 4 + 4 forced duplicates


def test_no_lift_entries_tagged_correctly(db_path: Path) -> None:
    parsed = parse(SAMPLE_DOC)
    import_into(db_path, parsed)

    no_lifts = list_entries(db_path, tags="surveyed-no-lift", limit=100)
    assert len(no_lifts) == 2
    for entry in no_lifts:
        assert "surveyed-no-lift" in entry["tags"]
        assert entry["confidence"] == "low"


def test_pattern_entries_have_pattern_type(db_path: Path) -> None:
    parsed = parse(SAMPLE_DOC)
    import_into(db_path, parsed)

    patterns = list_entries(db_path, type="pattern", limit=100)
    for entry in patterns:
        assert entry["type"] == "pattern"


def test_multiple_patterns_same_source_url(db_path: Path) -> None:
    """Two patterns lifted from the same repo (e.g. three OpenLtm files)
    must both be stored — dedup is by (URL + pattern name), not URL alone."""
    doc = dedent("""\
        ## Pattern A from OpenLtm

        - **Source**: https://github.com/RohiRIK/OpenLtm — `file1.ts`
        - **Surveyed**: 2026-06-09

        Body A.

        ---

        ## Pattern B from OpenLtm

        - **Source**: https://github.com/RohiRIK/OpenLtm — `file2.ts`
        - **Surveyed**: 2026-06-09

        Body B.
    """)
    parsed = parse(doc)
    assert len(parsed.patterns) == 2

    result = import_into(db_path, parsed)
    assert result.patterns_added == 2
    assert result.patterns_skipped_existing == 0

    # Re-run: both should be skipped (idempotent per (URL, name)).
    second = import_into(db_path, parsed)
    assert second.patterns_added == 0
    assert second.patterns_skipped_existing == 2


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
