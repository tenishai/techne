"""Tests for the Architect pattern store over architect.db (techne#87)."""

from __future__ import annotations

import pytest

from techne.core.architect import ARCHITECT_SCHEMA
from techne.core.patterns import (
    add_pattern,
    get_pattern,
    ingest_patterns_md,
    investigation_exists,
    investigations_for_url,
    list_patterns,
    search_patterns,
    set_verdict,
    stats,
    sweep_legacy_verdicts,
    upsert_investigation,
)
from techne.core.stores import SqliteStore


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def test_upsert_investigation_dedup_and_lens(db):
    a = upsert_investigation(db, url="u", surveyed_at="2026-06-01", verdict="full-eval")
    b = upsert_investigation(db, url="u", surveyed_at="2026-06-02", verdict="patterns-only")
    assert a == b  # same (url, surveyed_by, lens) → one row, updated
    # A different lens on the same URL is a DISTINCT row (the comparison axis).
    c = upsert_investigation(db, url="u", surveyed_at="2026-06-01", verdict="skip", lens="sre")
    assert c != a
    assert investigation_exists(db, "u")  # default lens=infra
    assert investigation_exists(db, "u", lens="sre")
    assert not investigation_exists(db, "u", lens="product")


def test_investigations_for_url_is_the_comparison(db):
    upsert_investigation(
        db, url="u", surveyed_at="d", verdict="full-eval", surveyed_by="raven", lens="infra"
    )
    upsert_investigation(
        db, url="u", surveyed_at="d", verdict="skip", surveyed_by="rook", lens="sre"
    )
    rows = investigations_for_url(db, "u")
    assert {(r["surveyed_by"], r["verdict"]) for r in rows} == {
        ("raven", "full-eval"),
        ("rook", "skip"),
    }


def test_add_pattern_dedup_and_search(db):
    inv = upsert_investigation(db, url="u", surveyed_at="d", verdict="full-eval")
    pid, stored = add_pattern(
        db,
        investigation_id=inv,
        name="Capability probing",
        source_url="u",
        body="probe for sqlite-vec at startup and branch on what's available",
    )
    assert stored
    # Same content → idempotent.
    _pid2, stored2 = add_pattern(
        db,
        investigation_id=inv,
        name="Capability probing",
        source_url="u",
        body="probe for sqlite-vec at startup and branch on what's available",
    )
    assert not stored2

    hits = search_patterns(db, "sqlite-vec capability")
    assert [h["id"] for h in hits] == [pid]
    assert get_pattern(db, pid)["name"] == "Capability probing"
    assert len(list_patterns(db)) == 1


def test_search_patterns_blank_query_returns_empty(db):
    """#103: blank/whitespace must not crash (FTS5 MATCH '' is a syntax error)."""
    inv = upsert_investigation(db, url="u", surveyed_at="d", verdict="full-eval")
    add_pattern(db, investigation_id=inv, name="P", source_url="u", body="some body text")
    assert search_patterns(db, "") == []
    assert search_patterns(db, "   ") == []
    # techne#143: a non-blank query that sanitizes to "" (lone backslash) reached
    # MATCH '' on this path and crashed — now guarded via prepare_fts_query.
    assert search_patterns(db, "\\") == []


def test_ingest_patterns_md(db):
    md = """---
name: architectural-patterns
---

## Capability probing

- **Source**: https://github.com/x/repo — `src/a.ts`
- **Surveyed**: 2026-06-09

Probe for libsqlite3 and sqlite-vec; branch on availability.

**Why interesting**: runtime capability negotiation.

---

## Two-stage classification

- **Source**: https://github.com/y/repo
- **Surveyed**: 2026-06-09

Heuristic first, LLM tiebreaker, graceful fallback.

---

## Surveyed (no patterns worth saving)

- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD, nothing novel
"""
    counts = ingest_patterns_md(db, md)
    assert counts["patterns_added"] == 2
    assert counts["patterns"] == 2
    assert counts["skips"] == 1
    # the skip is an investigation with no patterns
    assert investigation_exists(db, "https://github.com/z/nope")
    assert search_patterns(db, "tiebreaker")  # the lifted pattern is searchable

    # Idempotent re-ingest.
    counts2 = ingest_patterns_md(db, md)
    assert counts2["patterns_added"] == 0
    assert stats(db)["patterns"] == 2


def test_export_roundtrips_with_ingest(db, tmp_path):
    from techne.core.architect import ARCHITECT_SCHEMA
    from techne.core.patterns import export_patterns_md

    md = (
        "## Capability probing\n\n"
        "- **Source**: https://github.com/x/repo\n"
        "- **Surveyed**: 2026-06-09\n\n"
        "Probe for sqlite-vec; branch on availability.\n\n"
        "---\n\n"
        "## Surveyed (no patterns worth saving)\n\n"
        "- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD\n"
    )
    ingest_patterns_md(db, md)
    exported = export_patterns_md(db)

    # Re-ingest the export into a FRESH db → same shape (the md is a faithful transport).
    db2 = tmp_path / "architect2.db"
    SqliteStore("architect", db2).migrate(ARCHITECT_SCHEMA)
    ingest_patterns_md(db2, exported)
    assert stats(db2)["patterns"] == 1
    assert stats(db2)["skips"] == 1
    assert search_patterns(db2, "sqlite-vec")


# --- verdict is the operator's judgment, not a side effect (techne#428) -------
#
# Regression cover for the bug where `ingest_patterns_md` hardcoded
# verdict="full-eval" for any URL carrying patterns and the CLI had no way to
# pass one — so the column recorded *whether the markdown had patterns*, which
# the patterns table already says, and `patterns-only` was unreachable. Live
# corpus when found: 202 full-eval / 70 skip-with-flag / 0 patterns-only.

_MD_ONE = (
    "## Capability probing\n\n"
    "- **Source**: https://github.com/x/repo\n"
    "- **Surveyed**: 2026-06-09\n\n"
    "Probe for sqlite-vec; branch on availability.\n\n"
    "---\n"
)


def _verdict_for(db, url):
    return investigations_for_url(db, url)[0]["verdict"]


def test_ingest_defaults_to_patterns_only_not_full_eval(db):
    """THE regression: the old code wrote full-eval here."""
    ingest_patterns_md(db, _MD_ONE)
    assert _verdict_for(db, "https://github.com/x/repo") == "patterns-only"


def test_ingest_records_the_verdict_it_is_given(db):
    ingest_patterns_md(db, _MD_ONE, verdict="full-eval")
    assert _verdict_for(db, "https://github.com/x/repo") == "full-eval"


def test_patterns_only_is_reachable(db):
    """It was not, before #428 — no code path could produce this value."""
    ingest_patterns_md(db, _MD_ONE, verdict="patterns-only")
    assert _verdict_for(db, "https://github.com/x/repo") == "patterns-only"


def test_no_lift_bullets_stay_skip_with_flag(db):
    """Derived from document structure, so it is correct by construction and the
    caller's --verdict must NOT override it."""
    md = (
        "## Surveyed (no patterns worth saving)\n\n"
        "- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD\n"
    )
    ingest_patterns_md(db, md, verdict="full-eval")
    assert _verdict_for(db, "https://github.com/z/nope") == "skip-with-flag"


@pytest.mark.parametrize("bad", ["", "FULL-EVAL", "maybe", "patterns_only", None])
def test_out_of_vocabulary_verdict_is_rejected(db, bad):
    with pytest.raises(ValueError, match="verdict must be one of"):
        upsert_investigation(db, url="u", surveyed_at="2026-06-01", verdict=bad)


def test_ingest_rejects_bad_verdict_before_writing_anything(db):
    """Fail closed: a bad verdict must not leave a half-ingested corpus."""
    with pytest.raises(ValueError, match="verdict must be one of"):
        ingest_patterns_md(db, _MD_ONE, verdict="nonsense")
    assert stats(db)["patterns"] == 0
    assert stats(db)["investigations"] == 0


def test_set_verdict_corrects_without_touching_surveyed_at(db):
    """Correcting a judgment is not a re-survey (cf. techne#429).

    surveyed_at is read straight from the table because investigations_for_url
    projects only (surveyed_by, lens, verdict, summary).
    """
    import sqlite3

    upsert_investigation(db, url="u", surveyed_at="2026-06-01", verdict="full-eval")
    assert set_verdict(db, url="u", verdict="patterns-only") is True
    assert investigations_for_url(db, "u")[0]["verdict"] == "patterns-only"

    conn = sqlite3.connect(db)
    try:
        got = conn.execute(
            "SELECT surveyed_at FROM investigations WHERE url = ?", ("u",)
        ).fetchone()
    finally:
        conn.close()
    assert got[0] == "2026-06-01"


def test_set_verdict_reports_miss(db):
    assert set_verdict(db, url="never-surveyed", verdict="skip") is False


def test_sweep_legacy_is_dry_by_default_and_scoped_by_date(db):
    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")
    upsert_investigation(db, url="new", surveyed_at="2026-07-30", verdict="full-eval")

    dry = sweep_legacy_verdicts(db, before="2026-07-01")
    assert dry["matched"] == 1 and dry["applied"] == 0
    assert _verdict_for(db, "old") == "full-eval"  # unchanged by a dry run

    applied = sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    assert applied["matched"] == 1 and applied["applied"] == 1
    assert _verdict_for(db, "old") == "unknown"
    # A post-fix full-eval is a real judgment and must survive the sweep.
    assert _verdict_for(db, "new") == "full-eval"


def test_sweep_legacy_is_idempotent(db):
    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")
    sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    again = sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    assert again["matched"] == 0
    assert _verdict_for(db, "old") == "unknown"
