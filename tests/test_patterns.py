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
    outcome_stats,
    outcomes_for_pattern,
    record_outcome,
    search_patterns,
    stats,
    supersede,
    unrealized_patterns,
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


# --- outcome provenance (techne#426) ----------------------------------------


@pytest.fixture()
def pattern(db):
    """One stored pattern to hang outcomes off."""
    inv = upsert_investigation(
        db, url="https://example.test/x", surveyed_at="2026-07-28", verdict="patterns-only"
    )
    pid, _ = add_pattern(
        db, investigation_id=inv, name="P", source_url="https://example.test/x", body="b"
    )
    return pid


def test_record_outcome_inserts_then_promotes_state(db, pattern):
    assert record_outcome(db, pattern_id=pattern, ref="o/r#1") is True
    rows = outcomes_for_pattern(db, pattern)
    assert len(rows) == 1 and rows[0]["state"] == "filed" and rows[0]["kind"] == "issue"

    # same ref, new state → upsert promotes rather than duplicating
    assert record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped") is True
    rows = outcomes_for_pattern(db, pattern)
    assert len(rows) == 1 and rows[0]["state"] == "shipped"

    # identical re-record is a no-op
    assert record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped") is False


def test_record_outcome_is_many_to_many(db, pattern):
    """One pattern can become several things — that's why it's a junction."""
    record_outcome(db, pattern_id=pattern, ref="o/techne#1")
    record_outcome(db, pattern_id=pattern, ref="o/limenal#2", state="shipped")
    refs = {r["ref"] for r in outcomes_for_pattern(db, pattern)}
    assert refs == {"o/techne#1", "o/limenal#2"}


def test_record_outcome_note_is_preserved_when_omitted(db, pattern):
    record_outcome(db, pattern_id=pattern, ref="o/r#1", note="why it mattered")
    record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped")  # no note passed
    assert outcomes_for_pattern(db, pattern)[0]["note"] == "why it mattered"


def test_record_outcome_rejects_unknown_pattern_and_state(db, pattern):
    with pytest.raises(ValueError, match="unknown pattern id"):
        record_outcome(db, pattern_id="deadbeefdeadbeef", ref="o/r#1")
    with pytest.raises(ValueError, match="unknown outcome state"):
        record_outcome(db, pattern_id=pattern, ref="o/r#2", state="bogus")


def test_outcome_stats_counts_distinct_patterns_not_rows(db, pattern):
    """A pattern that spawned three issues is ONE realized pattern, not three."""
    for i in range(3):
        record_outcome(db, pattern_id=pattern, ref=f"o/r#{i}")
    s = outcome_stats(db)
    assert s["realized_patterns"] == 1
    assert s["shipped_patterns"] == 0
    assert s["outcome_rows_by_state"] == {"filed": 3}

    record_outcome(db, pattern_id=pattern, ref="o/r#0", state="shipped")
    assert outcome_stats(db)["shipped_patterns"] == 1


def test_unrealized_excludes_patterns_with_outcomes(db, pattern):
    inv = upsert_investigation(
        db, url="https://example.test/y", surveyed_at="2026-07-28", verdict="patterns-only"
    )
    other, _ = add_pattern(
        db, investigation_id=inv, name="Q", source_url="https://example.test/y", body="b2"
    )

    ids = {p["id"] for p in unrealized_patterns(db)}
    assert {pattern, other} <= ids

    record_outcome(db, pattern_id=pattern, ref="o/r#1")
    ids = {p["id"] for p in unrealized_patterns(db)}
    assert pattern not in ids and other in ids


# --- Prudence PR#427 regressions -------------------------------------------


def test_bare_re_record_cannot_demote_a_shipped_outcome(db, pattern):
    """The #426 backfill re-records every ref it finds; if an omitted --state
    carried a 'filed' default it would silently undo every 'shipped' in the corpus."""
    record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped")
    assert record_outcome(db, pattern_id=pattern, ref="o/r#1") is False
    assert outcomes_for_pattern(db, pattern)[0]["state"] == "shipped"


def test_kind_change_is_reported_as_changed(db, pattern):
    """kind was written on conflict but excluded from the comparison, so the CLI
    printed 'unchanged' while mutating the row."""
    record_outcome(db, pattern_id=pattern, ref="o/r#1", kind="issue", state="filed")
    assert record_outcome(db, pattern_id=pattern, ref="o/r#1", kind="pr") is True
    assert outcomes_for_pattern(db, pattern)[0]["kind"] == "pr"


def test_omitted_fields_default_only_on_insert(db, pattern):
    """Both halves of the name. The INSERT half alone passed even with the fix
    fully reverted — the old signature defaults produced an identical row, so it
    characterised behaviour without protecting it (Prudence, PR#427)."""
    record_outcome(db, pattern_id=pattern, ref="o/r#1")
    row = outcomes_for_pattern(db, pattern)[0]
    assert (row["kind"], row["state"]) == ("issue", "filed")  # defaults applied at INSERT

    # ...and NOT on update: with non-default values stored, omitting the args must
    # leave them alone. Under the old `state="filed"` / `kind="issue"` signature
    # defaults this reverts both columns, so this half has real teeth.
    record_outcome(db, pattern_id=pattern, ref="o/r#1", kind="pr", state="shipped")
    record_outcome(db, pattern_id=pattern, ref="o/r#1", note="unrelated edit")
    row = outcomes_for_pattern(db, pattern)[0]
    assert (row["kind"], row["state"]) == ("pr", "shipped")


def test_cli_realized_omits_state_by_default(db, pattern, monkeypatch):
    """The defect's ORIGIN was Click's `default="filed"`, not the data layer.
    Reverting cli.py alone left the whole suite green, so pin the CLI contract
    directly (Prudence, PR#427). Config injected via the house pattern in
    tests/test_cli_audit.py."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cfg = Config()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("techne.core.architect.architect_db", lambda _config: db)

    record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped")
    res = CliRunner().invoke(main, ["architect", "realized", pattern, "--ref", "o/r#1"])
    assert res.exit_code == 0, res.output
    assert outcomes_for_pattern(db, pattern)[0]["state"] == "shipped"
    assert "shipped" in res.output  # echoes the STORED state, not the argument


def test_shipped_rate_also_excludes_superseded_patterns(db, pattern):
    """Reverting only the `shipped` half of the realized_rate fix broke nothing —
    both halves need their own assertion (Prudence, PR#427)."""
    inv = upsert_investigation(
        db, url="https://example.test/z", surveyed_at="2026-07-29", verdict="patterns-only"
    )
    other, _ = add_pattern(
        db, investigation_id=inv, name="R", source_url="https://example.test/z", body="b3"
    )
    record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped")
    supersede(db, old_id=pattern, new_id=other)
    assert outcome_stats(db)["shipped_patterns"] == 0


def test_realized_rate_cannot_exceed_one_when_a_pattern_is_superseded(db, pattern):
    """Superseded patterns keep their outcomes; counting them against an
    active-only denominator pushed the headline metric above 1.0."""
    inv = upsert_investigation(
        db, url="https://example.test/y", surveyed_at="2026-07-28", verdict="patterns-only"
    )
    other, _ = add_pattern(
        db, investigation_id=inv, name="Q", source_url="https://example.test/y", body="b2"
    )
    record_outcome(db, pattern_id=pattern, ref="o/r#1")
    record_outcome(db, pattern_id=other, ref="o/r#2")

    supersede(db, old_id=pattern, new_id=other)

    s = outcome_stats(db)
    assert s["active_patterns"] == 1
    assert s["realized_patterns"] == 1  # the superseded one no longer counts
    assert s["realized_rate"] <= 1.0


def test_concurrent_record_outcome_does_not_raise_integrityerror(db, pattern):
    """Two writers racing the same (pattern_id, ref). Without BEGIN IMMEDIATE both
    read `before is None`, both take INSERT, and the loser raises an uncaught
    IntegrityError the CLI does not catch (it only handles ValueError).
    Reproduced by Prudence on PR#427.

    A SINGLE round is only a ~62% detector — Prudence replayed one round 40x
    against reverted code and saw red just 25 times, and on one full revert it
    passed with the bug entirely restored. Ten rounds of three writers makes
    missing it effectively impossible while staying <1s. Three writers rather
    than two because the losing pair is what collides.
    """
    import threading

    errors: list[BaseException] = []

    for round_no in range(10):
        ref = f"race#{round_no}"
        start = threading.Barrier(3)

        def writer(state: str, ref: str = ref) -> None:
            start.wait()
            try:
                record_outcome(db, pattern_id=pattern, ref=ref, state=state)
            except BaseException as exc:  # noqa: BLE001 — we want to see ANY failure
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(s,))
            for s in ("filed", "shipped", "rejected")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            # A hung writer must fail loudly: join() returns on timeout either way,
            # so without this a deadlock passes green (Prudence, PR#427).
            assert not t.is_alive(), f"writer hung in round {round_no}"

        assert not errors, f"round {round_no}: concurrent writers raised: {errors!r}"
        rows = [r for r in outcomes_for_pattern(db, pattern) if r["ref"] == ref]
        assert len(rows) == 1, f"round {round_no}: expected 1 row, got {len(rows)}"


def test_cli_realized_omits_kind_by_default(db, pattern, monkeypatch):
    """`--kind` got the same default=None treatment as `--state` but no test —
    reverting that one line left all 19 tests green while genuinely demoting data
    ('pr' back to 'issue'). Same gap class as the shipped_rate half (Prudence,
    PR#427)."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cfg = Config()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("techne.core.architect.architect_db", lambda _config: db)

    record_outcome(db, pattern_id=pattern, ref="o/r#1", kind="pr", state="shipped")
    res = CliRunner().invoke(main, ["architect", "realized", pattern, "--ref", "o/r#1"])
    assert res.exit_code == 0, res.output
    assert outcomes_for_pattern(db, pattern)[0]["kind"] == "pr"


def test_noop_re_record_does_not_bump_recorded_at(db, pattern):
    """`unchanged` must mean no write happened. Bumping recorded_at on a no-op
    silently reordered outcomes_for_pattern, which sorts by recorded_at DESC —
    so a re-record could reshuffle history without reporting a change
    (Prudence, PR#427)."""
    from techne.core.stores import plain_connect

    record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped")

    # Backdate the row. CURRENT_TIMESTAMP has ONE-SECOND resolution, so simply
    # re-recording and comparing timestamps is not a detector — both calls land in
    # the same second and match whether or not the write happened. (My first
    # version of this test passed with the guard fully reverted.) An obviously-old
    # stamp makes any write visible immediately and deterministically.
    old = "2001-01-01 00:00:00"
    conn = plain_connect(db)
    try:
        conn.execute("UPDATE pattern_outcomes SET recorded_at = ?", (old,))
        conn.commit()
    finally:
        conn.close()

    assert record_outcome(db, pattern_id=pattern, ref="o/r#1", state="shipped") is False
    assert outcomes_for_pattern(db, pattern)[0]["recorded_at"] == old

    # ...and a real change still stamps it.
    record_outcome(db, pattern_id=pattern, ref="o/r#1", note="now with a reason")
    assert outcomes_for_pattern(db, pattern)[0]["note"] == "now with a reason"
