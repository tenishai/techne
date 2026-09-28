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
    set_verdict,
    stats,
    sweep_legacy_verdicts,
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


def test_search_patterns_or_fallback_on_empty_and_pass(db):
    """#557: a problem-phrased query must not AND-miss a pattern that answers it.

    The corpus is queried in problem language but titled metaphorically, so under
    FTS5 implicit-AND every term must appear or the caller is told "no patterns"
    while an on-point pattern sits in the store.
    """
    inv = upsert_investigation(db, url="u", surveyed_at="d", verdict="patterns-only")
    add_pattern(
        db,
        investigation_id=inv,
        name="Two-gate alerting: relative tier AND absolute floor",
        source_url="u",
        body=(
            "An alert fires only when a file clears a repo-relative tier AND trips an "
            "absolute factor, so a healthy repo can return no findings at all."
        ),
    )

    # "stop" appears nowhere in the corpus. A single-term query takes no fallback
    # (build_or_fts_query returns "" for <2 terms), so this empty result proves the
    # term is genuinely absent — and therefore that the ANDed multi-term query below
    # cannot match, which is what the pre-#557 caller saw as "no patterns matched".
    assert search_patterns(db, "stop") == []

    hits = search_patterns(db, "how do I stop an alert firing when the repo is healthy")
    assert hits, "OR fallback should recover a pattern the AND pass misses"
    assert hits[0]["name"].startswith("Two-gate alerting")


def test_search_patterns_and_row_keeps_precedence_over_topped_up_or_row(db):
    """techne#1079: the AND pass still resolves first — an exact match stays the
    top hit — but a decoy sharing only one query term is no longer suppressed.
    Before this fix the OR pass ran ONLY when AND matched nothing, so a single
    exact match silently hid every partial match in the store; now it's topped
    up after the exact match instead.
    """
    inv = upsert_investigation(db, url="u", surveyed_at="d", verdict="patterns-only")
    add_pattern(
        db,
        investigation_id=inv,
        name="Quarantine-fence untrusted content",
        source_url="u",
        body="Preserve, do not drop; defuse, do not trust.",
    )
    # LOAD-BEARING FIXTURE DESIGN: the decoy shares EXACTLY ONE of the two query
    # terms ("quarantine", not "untrusted"), so AND("quarantine","untrusted")
    # matches only the first pattern while OR("quarantine" OR "untrusted") also
    # matches this decoy — that's what gives the top-up assertion below teeth.
    add_pattern(
        db,
        investigation_id=inv,
        name="Unrelated durable execution runtime",
        source_url="u2",
        body="Swap the runtime without changing agent code. Nothing to do with quarantine.",
    )

    # The invariant above, made executable so a reworded fixture cannot silently
    # re-vacuate this test: both patterns must share the term "quarantine". If the
    # decoy stops matching it, this fails here rather than passing hollowly below.
    assert len(search_patterns(db, "quarantine")) == 2, "decoy must share one query term"

    hits = search_patterns(db, "quarantine untrusted")
    names = [h["name"] for h in hits]
    assert names[0] == "Quarantine-fence untrusted content", "AND row keeps precedence"
    assert names[1:] == ["Unrelated durable execution runtime"], "partial match topped up"
    assert len(names) == len(set(names)), f"duplicate row in {names}"


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


def test_no_lift_bullets_ignore_the_callers_verdict(db):
    """A --verdict meant for the pattern-carrying URLs must not leak onto a
    no-lift bullet: the section header already establishes that this URL was
    surveyed and nothing was lifted.

    That is as far as "correct by construction" goes, and no further — it does not
    establish whether the judgment is `skip` or `skip-with-flag`, which is why the
    no-lift write is insert-only rather than authoritative. See
    test_no_lift_reverdict_also_survives_a_reingest.
    """
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
    """Fail closed: a bad verdict must not leave a half-ingested corpus.

    The fixture is deliberately a NO-LIFT-ONLY document, and that is the whole
    point of the test. With a document containing patterns, the first
    upsert_investigation would reject the bad verdict anyway, so the assertion
    passes with or without the pre-flight and proves nothing — verified by
    deleting the pre-flight and watching all 20 tests still pass (Prudence,
    PR #432).

    A no-lift-only document is the case the pre-flight actually exists for:
    `parsed.patterns` is empty, the no-lift loop writes its own hardcoded
    "skip-with-flag", and nothing downstream ever looks at the caller's verdict —
    so without the pre-flight a bogus verdict is silently accepted.
    """
    md = (
        "## Surveyed (no patterns worth saving)\n\n"
        "- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD\n"
    )
    with pytest.raises(ValueError, match="verdict must be one of"):
        ingest_patterns_md(db, md, verdict="nonsense")
    assert stats(db)["patterns"] == 0
    assert stats(db)["investigations"] == 0
    assert stats(db)["skips"] == 0


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


# --- PR #432 review findings (Prudence) --------------------------------------


def test_reverdict_survives_a_plain_reingest(db):
    """Important #2: the default must be INSERT-ONLY.

    The sharp case she named: `reverdict` records a judgment, then a routine
    re-ingest of the same markdown silently reverts it — the command this change
    adds for stating a judgment, undone by the command that prompted it. A default
    that overwrites on every re-ingest is techne#428 again with a nicer constant.
    """
    ingest_patterns_md(db, _MD_ONE)  # fresh row -> default
    url = "https://github.com/x/repo"
    assert _verdict_for(db, url) == "patterns-only"

    set_verdict(db, url=url, verdict="full-eval")  # operator states a judgment
    ingest_patterns_md(db, _MD_ONE)  # plain re-ingest, no --verdict
    assert _verdict_for(db, url) == "full-eval"  # judgment survives


def test_stated_verdict_still_overwrites_on_reingest(db):
    """Insert-only applies to the FALLBACK only — an explicit --verdict must win,
    or you could never correct a row by re-ingesting."""
    ingest_patterns_md(db, _MD_ONE, verdict="patterns-only")
    ingest_patterns_md(db, _MD_ONE, verdict="full-eval")
    assert _verdict_for(db, "https://github.com/x/repo") == "full-eval"


@pytest.mark.parametrize("bad", ["2026-7-1", "20260701", "yesterday", "", "2026-07-01T00:00"])
def test_sweep_rejects_a_malformed_before(db, bad):
    """Critical #1: --before is the sweep's only safety property. An unparseable
    value would compare as a plain string and could select the whole table."""
    with pytest.raises(ValueError, match="ISO date"):
        sweep_legacy_verdicts(db, before=bad, dry_run=False)


def test_sweep_dry_run_exposes_the_urls(db):
    """Critical #1: a count-only preview looks equally correct whether the scope
    is right or has swallowed everything, so the URLs are the actual protection."""
    upsert_investigation(db, url="old-a", surveyed_at="2026-06-01", verdict="full-eval")
    upsert_investigation(db, url="old-b", surveyed_at="2026-06-02", verdict="full-eval")
    upsert_investigation(db, url="new", surveyed_at="2026-07-30", verdict="full-eval")

    res = sweep_legacy_verdicts(db, before="2026-07-01")
    assert sorted(res["urls"]) == ["old-a", "old-b"]
    assert "new" not in res["urls"]


def test_sweep_applies_and_is_idempotent(db):
    """Important #3, partial cover — and an honest note about what this does NOT prove.

    The fix swapped `applied` from the SELECT's len(rows) to the UPDATE's
    cur.rowcount, so the audit number for a destructive rewrite is measured
    against the write rather than the read that predicted it.

    This test canNOT detect that swap: mutation-checked, reverting to len(rows)
    leaves the whole file green.

    I previously wrote here that it was untestable and that BEGIN IMMEDIATE was
    "covered by the fact that this file's sweeps commit atomically at all". Both
    claims were wrong, and the second was the same overclaim shape as the toothless
    test this replaced. Prudence showed (a) a BEFORE UPDATE … RAISE(IGNORE) trigger
    diverges the two single-threaded — so "equal by construction" is really "equal
    unless something suppresses the write" — and (b) removing BEGIN IMMEDIATE and
    racing a writer yields matched=1, applied=0, which is correct ONLY because
    `applied` comes from the UPDATE; len(rows) would have claimed a rewrite that
    never happened. The lock and the rowcount are one guard, not two: the lock
    prevents the race, the rowcount keeps the report honest if prevention lapses.

    Deliberately NOT adding the trigger-based test: no triggers exist in this
    schema, so it would trade an honest gap for a dishonest green. The lock itself
    IS covered, properly, by
    test_sweep_holds_the_write_lock_across_select_and_update.

    What this test covers: the sweep writes, and re-running is a no-op.
    """
    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")
    res = sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    assert res["applied"] == 1
    assert _verdict_for(db, "old") == "unknown"
    again = sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    assert again["matched"] == 0 and again["applied"] == 0


def test_sweep_holds_the_write_lock_across_select_and_update(db, monkeypatch):
    """Important #3, the guard that actually carries the weight.

    `sweep_legacy_verdicts` takes BEGIN IMMEDIATE before its SELECT so a writer
    cannot land between the read that builds the audit list and the UPDATE that
    rewrites those rows. Deleting that one line previously left the whole file
    green — the guard I called load-bearing had zero cover, which is the same
    overclaim shape as the toothless test it replaced (Prudence, PR #432).

    Mechanism: stall the sweep's connection just after its SELECT, then race a
    second connection at the same row. With the lock held the contender is blocked
    (OperationalError); without it, the contender writes.
    """
    import sqlite3
    import threading

    from techne.core import patterns as P

    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")

    # Generous caps. Both waits are released by an Event the moment the other side
    # is ready, so a large cap costs a passing run nothing — it only exists to stop
    # a hang. Small caps were the one way this could invert on a loaded box: if the
    # contender were not scheduled inside the stall window, the sweep would release
    # the lock and the contender would write, reporting the guard as broken when the
    # only thing that failed was scheduling. Measured 0/40 idle and 0/15 under 8
    # spinning CPU hogs before widening these anyway.
    STALL_S = 15.0
    at_select = threading.Event()
    resume = threading.Event()
    contender: dict[str, bool] = {}
    real_connect = P.plain_connect

    class _Stalling:
        """Delegates everything to the real connection; stalls once, after the
        sweep's SELECT, so a competing writer has a window to try. Only the FIRST
        SELECT stalls — a future edit adding a SELECT to the write path must not
        re-enter and deadlock against the contender."""

        def __init__(self, inner):
            self._inner = inner
            self._stalled = False

        def execute(self, sql, *a, **kw):
            cur = self._inner.execute(sql, *a, **kw)
            if not self._stalled and sql.lstrip().upper().startswith("SELECT"):
                self._stalled = True
                at_select.set()
                resume.wait(STALL_S)  # hold the transaction open across the race
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)  # commit/close/etc. hit the real conn

    monkeypatch.setattr(P, "plain_connect", lambda p: _Stalling(real_connect(p)))

    def contend():
        if not at_select.wait(STALL_S):
            return  # sweep never reached its SELECT; reported as setup failure below
        conn = sqlite3.connect(db, timeout=0.25)
        try:
            conn.execute("UPDATE investigations SET verdict = 'skip' WHERE url = 'old'")
            conn.commit()
            contender["wrote"] = True
        except sqlite3.OperationalError:
            contender["wrote"] = False
        finally:
            conn.close()
            resume.set()

    t = threading.Thread(target=contend)
    t.start()
    try:
        P.sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    finally:
        resume.set()
        t.join(STALL_S + 5)

    # Distinguish "the race never happened" from "the lock failed to hold", so a
    # scheduling stall can never masquerade as a broken guard.
    assert "wrote" in contender, "test setup failed: the contender never attempted a write"

    assert contender.get("wrote") is False, "a writer got in between the SELECT and the UPDATE"


def test_no_lift_reverdict_also_survives_a_reingest(db):
    """Important #2 was still alive on the SECOND call site.

    The no-lift loop hardcodes "skip-with-flag", and I defended that as "correct
    by construction". Half true: the section header proves the URL was surveyed
    and nothing was lifted, but not whether the operator's judgment is `skip` or
    `skip-with-flag` — and `skip` is one of reverdict's choices. Left overwriting,
    `reverdict --verdict skip` was reverted by a plain re-ingest (Prudence, PR #432).
    """
    md = (
        "## Surveyed (no patterns worth saving)\n\n"
        "- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD\n"
    )
    ingest_patterns_md(db, md)
    url = "https://github.com/z/nope"
    assert _verdict_for(db, url) == "skip-with-flag"

    set_verdict(db, url=url, verdict="skip")  # operator narrows the judgment
    ingest_patterns_md(db, md)  # plain re-ingest
    assert _verdict_for(db, url) == "skip"


def test_a_skip_verdict_does_not_survive_the_url_yielding_patterns(db):
    """A trap I introduced with the insert-only fix, caught by probing my own
    answer to a review question rather than waiting to be told.

    A URL first seen as a no-lift bullet gets `skip-with-flag`. If a later ingest
    shows it DID yield a pattern, insert-only left the skip in place — so
    `stats()` counted the same row under BOTH `patterns` and `skips`, and the
    corpus asserted "no patterns worth saving" about a row owning a pattern.

    `clears_skip` displaces a skip verdict when the write itself contradicts it.
    A stated judgment is still never clobbered — see the assertion at the end.
    """
    url = "https://github.com/x/repo"
    nolift = f"## Surveyed (no patterns worth saving)\n\n- {url} — surveyed 2026-06-01 — nope\n"
    withpat = (
        f"## Real pattern\n\n- **Source**: {url}\n- **Surveyed**: 2026-07-01\n\nBody.\n\n---\n"
    )

    ingest_patterns_md(db, nolift)
    assert _verdict_for(db, url) == "skip-with-flag"
    assert stats(db)["skips"] == 1

    ingest_patterns_md(db, withpat)  # same URL now yields a pattern, no --verdict
    assert _verdict_for(db, url) == "patterns-only"
    s = stats(db)
    assert s["patterns"] == 1 and s["skips"] == 0, "a row was both a pattern and a skip"


def test_clears_skip_does_not_clobber_a_stated_non_skip_judgment(db):
    """clears_skip must only displace skip* — insert-only still protects a real
    judgment, or the fix for the trap would undo Important #2."""
    url = "https://github.com/x/repo"
    withpat = (
        f"## Real pattern\n\n- **Source**: {url}\n- **Surveyed**: 2026-07-01\n\nBody.\n\n---\n"
    )
    ingest_patterns_md(db, withpat)
    set_verdict(db, url=url, verdict="full-eval")
    ingest_patterns_md(db, withpat)  # plain re-ingest
    assert _verdict_for(db, url) == "full-eval"


def test_demotion_to_no_lift_keeps_the_pattern_era_verdict(db):
    """PINS a known limitation rather than asserting it is correct (techne#433).

    The reverse of the trap `clears_skip` fixes: a URL that once carried patterns,
    later demoted to a no-lift bullet, KEEPS its pattern-era verdict because the
    no-lift write is insert-only. Prudence found this direction untested in either
    direction and explicitly did not ask for a behaviour change — the insert-only
    trade is the right one, and the root cause is that verdicts carry no
    provenance, so the earliest fallback wins forever.

    This test exists so the frozen behaviour is deliberate and visible. If a future
    change makes demotion move the verdict, this test SHOULD fail and be updated —
    that is the point of pinning it.
    """
    url = "https://github.com/x/repo"
    withpat = (
        f"## Real pattern\n\n- **Source**: {url}\n- **Surveyed**: 2026-06-01\n\nBody.\n\n---\n"
    )
    nolift = f"## Surveyed (no patterns worth saving)\n\n- {url} — surveyed 2026-07-01 — thin\n"

    ingest_patterns_md(db, withpat)
    assert _verdict_for(db, url) == "patterns-only"

    ingest_patterns_md(db, nolift)  # demoted later
    assert _verdict_for(db, url) == "patterns-only", "frozen by design — see techne#433"


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


# --- the ladder itself (Prudence r1 on #702) --------------------------------
#
# She deleted the new v4->v5 step entirely and the full suite stayed green at 3104,
# exit 0; `if current < 4` instead of `< 5` also left 78/78 green. #674's step was
# equally uncovered, so this is pre-existing practice rather than something the
# renumbering introduced — which is why it is worth closing now.
#
# Uses SqliteStore().migrate() like the `db` fixture above: `_reconcile_relations`
# runs the LADDER only, so a bare path has no `patterns` table for the steps to alter.


def _laddered(path, start_version):
    """A store as it would REALLY look at `start_version`, then re-migrated.

    Removes only the artifacts of steps ABOVE the stamp. That fidelity is the point in
    both directions:

    * Asserting straight after `migrate()` proves nothing — `ARCHITECT_SCHEMA` already
      creates every column and table the ladder adds (architect.py:57-61), so Prudence
      r2 replaced the whole body of `_architect_run_steps` with `pass` and all five
      parameters still passed.
    * Stripping EVERYTHING and then claiming stamp 3 is equally wrong in the other
      direction — a store at 3 legitimately has the v1-v3 columns, and steps 1-3 are
      correctly skipped, so the assertion would fail against correct code.

    SQLite cannot DROP COLUMN on these files, so columns go by rebuilding `patterns`.
    """
    import sqlite3

    from techne.core import architect

    SqliteStore("architect", path).migrate(ARCHITECT_SCHEMA)
    conn = sqlite3.connect(path)
    # step -> what it leaves behind, so we can remove exactly the future ones
    drop_cols: set[str] = set()
    if start_version < 2:
        drop_cols |= {"topic", "absorbed"}
    if start_version < 3:
        drop_cols |= {"valid_at", "invalid_at"}
    if start_version < 5:
        conn.execute("DROP TABLE IF EXISTS pattern_outcomes")
    if drop_cols:
        # Rebuild with EXPLICIT DDL, not `CREATE TABLE AS SELECT`: the latter drops the
        # primary key, and `pattern_applies` has a foreign key onto `patterns(id)`, so
        # the next step fails with "foreign key mismatch" — a fault in the harness that
        # looks exactly like a fault in the ladder.
        #
        # ALSO LOST, and named so nobody trusts this to be a faithful copy: the FK to
        # `investigations` (PRAGMA table_info cannot see FKs), the three FTS triggers,
        # and the two schema indexes. The rebuilt table is strictly LAXER, and no ladder
        # step writes to `patterns`, so none of that can manufacture a pass — but a
        # future step that does write here would not be exercised faithfully.
        info = [r for r in conn.execute("PRAGMA table_info(patterns)") if r[1] not in drop_cols]
        defs = ", ".join(
            f"{r[1]} {r[2]}"
            + (" NOT NULL" if r[3] else "")
            + (f" DEFAULT {r[4]}" if r[4] is not None else "")
            + (" PRIMARY KEY" if r[5] else "")
            for r in info
        )
        names = ", ".join(r[1] for r in info)
        conn.execute(f"CREATE TABLE _p ({defs})")
        conn.execute(f"INSERT INTO _p ({names}) SELECT {names} FROM patterns")
        conn.execute("DROP TABLE patterns")
        conn.execute("ALTER TABLE _p RENAME TO patterns")
    conn.execute(f"PRAGMA user_version = {start_version}")
    conn.commit()
    conn.close()
    architect._reconcile_relations(path)
    return sqlite3.connect(path)


@pytest.mark.parametrize("start", [0, 1, 2, 3, 4])
def test_every_starting_stamp_reaches_the_current_shape(tmp_path, start):
    """ASSERTS BY EFFECT, NOT BY STAMP. `_architect_run_steps` writes the version
    unconditionally after running, so a store that skipped a step still ends up stamped
    correctly — the stamp cannot tell you the step ran. Check what each step leaves."""
    from techne.core import architect

    conn = _laddered(tmp_path / "architect.db", start)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patterns)")}
        assert {"topic", "absorbed", "valid_at", "invalid_at"} <= cols, "v1-v3 steps"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pattern_outcomes'"
        ).fetchone(), "v4->v5 step"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == architect.ARCHITECT_VERSION
    finally:
        conn.close()


def test_the_v4_to_v5_step_is_gated_at_5_not_4(tmp_path):
    """The realistic slip in a renumbering: `if current < 4` for a v5 step. A store
    stamped exactly 4 must still receive it — and that store is the live one.

    `pattern_outcomes` is dropped AFTER the base schema is applied, so only the ladder
    step can put it back. Without that drop the base schema creates it and the assertion
    passes whether or not the step ran.
    """
    import sqlite3

    from techne.core import architect

    db_path = tmp_path / "architect.db"
    SqliteStore("architect", db_path).migrate(ARCHITECT_SCHEMA)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS pattern_outcomes")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    architect._reconcile_relations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pattern_outcomes'"
        ).fetchone(), "a store stamped 4 is exactly the live case; it must migrate"
    finally:
        conn.close()


def test_outcome_stats_counts_one_population(db):
    """Prudence r1 on #702: the denominator filtered `status='active'` and neither
    numerator did, so a SUPERSEDED pattern carrying an outcome inflated the rate —
    demonstrated at realized_rate 2.0. `unrealized_patterns` right beside it does
    filter, so the two were presented as complements while counting different sets."""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO patterns (id,name,source_url,body,status,created_at)"
        " VALUES ('live','a','u','b','active','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO patterns (id,name,source_url,body,status,created_at)"
        " VALUES ('dead','b','u','b','superseded','2026-01-01')"
    )
    conn.commit()
    conn.close()

    record_outcome(db, pattern_id="dead", ref="r1", state="shipped")
    stats = outcome_stats(db)
    assert stats["active_patterns"] == 1
    assert stats["realized_patterns"] == 0, "a superseded pattern is not a realized active one"
    # BOTH numerators. Prudence r2 reverted only the `shipped` one and the full suite
    # passed 3111 — the fix guarded in half. #427's tests do assert shipped_patterns,
    # but every pattern in that fixture is active, so status is the one axis they
    # structurally cannot see.
    assert stats["shipped_patterns"] == 0
    assert stats["realized_rate"] <= 1.0 and stats["shipped_rate"] <= 1.0

    record_outcome(db, pattern_id="live", ref="r2", state="shipped")
    stats = outcome_stats(db)
    assert stats["realized_patterns"] == 1 and stats["realized_rate"] == 1.0
    assert stats["shipped_patterns"] == 1 and stats["shipped_rate"] == 1.0


def test_the_v3_to_v4_step_collapses_the_pre_rename_target(tmp_path):
    """#674's step was uncovered too — Prudence r2 stubbed it and 84 tests passed.
    Pre-existing rather than introduced here, but the harness above makes it one test.

    The step is DATA, not schema: it folds `limenal` rows into `liminal` after the
    2026-08-11 rename. So the fixture plants the old spelling at a stamp below 4."""
    import sqlite3

    from techne.core import architect

    db_path = tmp_path / "architect.db"
    SqliteStore("architect", db_path).migrate(ARCHITECT_SCHEMA)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO patterns (id,name,source_url,body,status,created_at)"
        " VALUES ('p1','a','u','b','active','2026-01-01')"
    )
    conn.execute("INSERT INTO pattern_applies (pattern_id, target) VALUES ('p1','limenal')")
    conn.execute("INSERT INTO pattern_applies (pattern_id, target) VALUES ('p1','liminal')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    architect._reconcile_relations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        targets = {r[0] for r in conn.execute("SELECT target FROM pattern_applies")}
        assert targets == {"liminal"}, "the pre-rename spelling must be collapsed, not kept"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# techne#429 — the ordinary ingest IS the declined path
# ---------------------------------------------------------------------------


def _one_pattern_md(surveyed: str) -> str:
    return f"""---
name: architectural-patterns
---

## Capability probing

- **Source**: https://github.com/x/repo — `src/a.ts`
- **Surveyed**: {surveyed}

Probe for libsqlite3 and sqlite-vec; branch on availability.
"""


def _surveyed_at(db, url="https://github.com/x/repo"):
    # Direct SQL: `investigations_for_url` projects (surveyed_by, lens, verdict,
    # summary) and deliberately does not expose the date this test is about.
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT surveyed_at FROM investigations WHERE url = ?", (url,)
        ).fetchone()
        return row[0][:10] if row else None
    finally:
        conn.close()


def test_the_ordinary_ingest_path_does_not_advance_the_survey_date(db):
    """THE BINDING BETWEEN "THE CLI DEFAULT" AND "THE DECLINED PATH" (Prudence, #738 r2).

    The comment block at patterns.py and the whole of techne#741 rest on one fact: the
    ordinary `techne architect ingest` declines the verdict, so `surveyed_at` no longer
    advances. Nothing held that in place. Prudence mutated `cli.py`'s `--verdict`
    default from None to "patterns-only" — which falsifies the recorded mechanism and
    inverts the sweep_legacy_verdicts caveat straight back — and the full suite stayed
    green at 3393 passed. No test invokes the architect CLI group at all, and every
    existing test calls upsert_investigation or ingest_patterns_md with an explicit
    verdict, so the default was invisible to all of them.

    This pins the `stated` half in the file that changes most. A failing assertion here
    points straight at the comment that would otherwise have gone stale."""
    ingest_patterns_md(db, _one_pattern_md("2026-01-01"))
    assert _surveyed_at(db) == "2026-01-01"

    ingest_patterns_md(db, _one_pattern_md("2026-08-24"))
    assert _surveyed_at(db) == "2026-01-01", (
        "the ordinary ingest declines the verdict, so the date must not advance — if "
        "this fails, the trade recorded in patterns.py is no longer the trade being made"
    )


def test_an_ingest_that_states_a_verdict_does_advance_the_date(db):
    """The other half, so the pair discriminates: when the verdict IS accepted the date
    follows it. Without this, freezing the date unconditionally would also pass."""
    ingest_patterns_md(db, _one_pattern_md("2026-01-01"))
    ingest_patterns_md(db, _one_pattern_md("2026-08-24"), verdict="full-eval")
    assert _surveyed_at(db) == "2026-08-24"


def test_the_cli_verdict_default_is_what_makes_ingest_the_declined_path(db, tmp_path, monkeypatch):
    """THE OTHER HALF OF THE BINDING — through the real CLI (Prudence, #738 r2).

    The test above pins `stated = verdict is not None`, but it calls
    `ingest_patterns_md` directly, so `cli.py`'s `--verdict` default stays invisible to
    it. Prudence measured that NOTHING in the repo covers the architect CLI group, and
    that changing that default from None to "patterns-only" falsifies the recorded
    mechanism, inverts the sweep_legacy_verdicts caveat back to its old form, and
    undermines techne#741 — while leaving 3393 tests green.

    So this drives `techne architect ingest` itself with no --verdict. It is the first
    CliRunner case against this group; the door is worth opening for a one-line default
    that three artefacts depend on."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    class _Cfg:
        architect_db_path = db
        stores = {}
        # `architect ingest` reads config.architect for surveyed_by/lens so that it
        # and `architect add` write the same half of UNIQUE(url, surveyed_by, lens).
        # A stub missing it fails with AttributeError, not a wrong verdict.
        architect = Config().architect

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: _Cfg())
    monkeypatch.setattr("techne.core.architect.architect_db", lambda *a, **k: db)

    corpus = tmp_path / "corpus.md"
    runner = CliRunner()

    corpus.write_text(_one_pattern_md("2026-01-01"))
    first = runner.invoke(main, ["architect", "ingest", str(corpus)])
    assert first.exit_code == 0, first.output
    assert _surveyed_at(db) == "2026-01-01", first.output

    corpus.write_text(_one_pattern_md("2026-08-24"))
    second = runner.invoke(main, ["architect", "ingest", str(corpus)])
    assert second.exit_code == 0, second.output
    assert _surveyed_at(db) == "2026-01-01", (
        "`techne architect ingest` with no --verdict must DECLINE the verdict and "
        "therefore leave the date alone. If this fails, cli.py's --verdict default "
        "changed and the mechanism recorded in patterns.py is no longer true."
    )


# --- round-trip idempotence (techne#770) --------------------------------------
#
# `test_export_roundtrips_with_ingest` above re-ingests into a FRESH db, which is
# the one arrangement where this defect is invisible: with nothing already stored,
# the first cycle's wrong id is indistinguishable from the right one. The drift
# only shows on the SECOND cycle, against a store that already holds the pattern.
#
# The oracle matters as much as the arrangement. A COUNT can be right while every
# id changed, so these assert IDS, which is what an id-addressed store is keyed on.

def _pattern_ids(db_path):
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM patterns")}


_ROUNDTRIP_MD = (
    "## Capability probing\n\n"
    "- **Source**: https://github.com/x/repo\n"
    "- **Surveyed**: 2026-06-09\n\n"
    "Probe for sqlite-vec; branch on availability.\n\n"
    "---\n\n"
)


def test_export_ingest_is_idempotent_against_a_populated_store(db):
    """export -> ingest against a NON-EMPTY store must be a no-op, forever.

    Before techne#770 the `---` separator export writes after each entry was
    absorbed into that entry's body. Pattern ids are content hashes of
    (name, source_url, body), so every cycle minted a new id and the store grew:
    1 -> 2 -> 3 -> 4 patterns for one pattern, each body carrying one more
    trailing rule than the last. An operator round-tripping their corpus to
    reformat or move it accreted near-duplicates silently.
    """
    from techne.core.patterns import export_patterns_md

    ingest_patterns_md(db, _ROUNDTRIP_MD)
    original = _pattern_ids(db)
    assert len(original) == 1

    for cycle in range(1, 4):
        ingest_patterns_md(db, export_patterns_md(db))
        assert _pattern_ids(db) == original, (
            f"cycle {cycle}: ids changed, so the store accreted a near-duplicate "
            f"instead of recognising the pattern it already held"
        )


def test_ingest_normalises_a_corpus_already_damaged_by_earlier_cycles(db, tmp_path):
    """The fix is CONVERGENT, not merely a stop to the growth.

    A store round-tripped before techne#770 holds bodies ending in `---\\n\\n---\\n\\n---`.
    Stripping ALL trailing rules rather than one means re-ingesting such a corpus
    lands on the SAME id as the clean body, so the damage heals instead of being
    frozen at whatever depth it reached.
    """
    from techne.core.architect import ARCHITECT_SCHEMA

    head = (
        "## Capability probing\n\n"
        "- **Source**: https://github.com/x/repo\n"
        "- **Surveyed**: 2026-06-09\n\n"
        "Probe for sqlite-vec; branch on availability.\n\n"
    )
    ingest_patterns_md(db, head)

    damaged = tmp_path / "damaged.db"
    SqliteStore("architect", damaged).migrate(ARCHITECT_SCHEMA)
    ingest_patterns_md(damaged, head + "---\n\n---\n\n---\n\n")

    assert _pattern_ids(damaged) == _pattern_ids(db)


def test_a_rule_inside_a_fenced_block_is_not_stripped(db):
    """Only the TRAILING separator goes. A `---` inside a fence is content.

    The closing ``` is the last line of such a body, so the strip loop stops
    before reaching the rule — this pins that reasoning rather than trusting it.
    """
    md = (
        "## Fenced rule\n\n"
        "- **Source**: https://github.com/y/repo\n"
        "- **Surveyed**: 2026-06-09\n\n"
        "Example:\n\n"
        "```\n"
        "---\n"
        "```\n\n"
        "---\n\n"
    )
    ingest_patterns_md(db, md)
    import sqlite3

    with sqlite3.connect(db) as conn:
        body = conn.execute("SELECT body FROM patterns").fetchone()[0]
    assert "```" in body and "---" in body, "the fenced rule was stripped as a separator"
    assert not body.rstrip().endswith("\n---"), "the trailing separator survived"


def test_stats_reports_superseded_patterns_beside_the_active_count(db):
    """`stats()["patterns"]` COUNTS ACTIVE ONLY, and the excluded rows are now
    reported rather than left to be inferred.

    The regression this pins is a reporting one, not an arithmetic one. `patterns`
    has always excluded supersessions; nothing in the output said so, so checking
    it against a plain `SELECT COUNT(*) FROM patterns` shows a gap that reads as a
    broken counter. Measured on the live store 2026-09-02: 555 vs 561, and the
    6-row difference was reported as a bug in the summary.
    """
    from techne.core.patterns import tag_pattern

    md = """## Alpha

- **Source**: https://github.com/x/alpha
- **Surveyed**: 2026-09-02

First.

---

## Beta

- **Source**: https://github.com/x/beta
- **Surveyed**: 2026-09-02

Second.

---
"""
    ingest_patterns_md(db, md)
    ids = {name: pid for pid, name in _pattern_ids(db)}
    assert stats(db)["patterns"] == 2
    assert stats(db)["patterns_superseded"] == 0

    tag_pattern(db, ids["Alpha"], superseded_by=ids["Beta"], absorbed="the earlier framing")

    s = stats(db)
    assert s["patterns"] == 1, "active count must drop when a pattern is superseded"
    assert s["patterns_superseded"] == 1, "the excluded row must be visible, not inferred"

    # The whole point: active + superseded reconciles with the raw table count, so a
    # reader comparing the summary against SELECT COUNT(*) lands on agreement.
    from techne.core.patterns import plain_connect

    conn = plain_connect(db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
    finally:
        conn.close()
    assert s["patterns"] + s["patterns_superseded"] == total == 2


def _pattern_ids(db):
    from techne.core.patterns import plain_connect

    conn = plain_connect(db)
    try:
        return conn.execute("SELECT id, name FROM patterns").fetchall()
    finally:
        conn.close()


# --- issue #549: date alone cannot express the legacy set ---------------------


def test_sweep_spares_rows_with_an_eval_doc_or_a_skip_url(db, tmp_path):
    """Measured: real operator full-evals sat among the legacy rows in both June
    and July, so every --before value destroyed some. The predicate is "no eval
    doc", which the sweep can now evaluate."""
    evals = tmp_path / "evaluations"
    evals.mkdir()
    (evals / "goose-evaluation.md").write_text("x")
    (evals / "precisionmembench-evaluation-2026-07-01.md").write_text("x")
    (evals / "claude-pulse-someowner-evaluation.md").write_text("x")
    (evals / "notes.md").write_text("not an eval doc")  # must not spare `notes`
    for url in (
        "https://github.com/a/goose",
        "https://github.com/t/precisionMemBench",
        "https://github.com/n/claude-pulse",
        "https://github.com/x/Dolt",
        "https://github.com/x/notes",
        "https://github.com/x/legacy",
    ):
        upsert_investigation(db, url=url, surveyed_at="2026-06-15", verdict="full-eval")

    res = sweep_legacy_verdicts(
        db,
        before="2026-07-01",
        dry_run=False,
        spare_urls=["https://GitHub.com/x/dolt.git/"],  # same repo, pasted differently
        eval_doc_dirs=[evals],
    )
    assert sorted(res["urls"]) == ["https://github.com/x/legacy", "https://github.com/x/notes"]
    assert res["applied"] == 2
    assert sorted((r["url"].rsplit("/", 1)[1], r["reason"]) for r in res["spared"]) == [
        ("Dolt", "skip-url"),
        ("claude-pulse", "eval-doc"),
        ("goose", "eval-doc"),
        ("precisionMemBench", "eval-doc"),
    ]
    assert _verdict_for(db, "https://github.com/a/goose") == "full-eval"
    assert _verdict_for(db, "https://github.com/x/Dolt") == "full-eval"
    assert _verdict_for(db, "https://github.com/x/legacy") == "unknown"


def test_sweep_cli_refuses_a_date_only_apply_and_labels_the_dry_run(db, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("techne.core.architect.architect_db", lambda _c: db)
    upsert_investigation(db, url="https://github.com/x/real", surveyed_at="2026-06-15",
                         verdict="full-eval")
    upsert_investigation(db, url="https://github.com/x/legacy", surveyed_at="2026-06-15",
                         verdict="full-eval")
    run = CliRunner().invoke

    refused = run(main, ["architect", "sweep-legacy-verdicts", "--before", "2026-07-01",
                         "--apply"])
    assert refused.exit_code != 0 and "--spare-with-eval-doc" in refused.output
    assert _verdict_for(db, "https://github.com/x/legacy") == "full-eval"  # nothing written

    dry = run(main, ["architect", "sweep-legacy-verdicts", "--before", "2026-07-01",
                     "--skip-url", "https://github.com/x/real"])
    assert dry.exit_code == 0, dry.output
    assert "legacy  https://github.com/x/legacy" in dry.output
    assert "spare   https://github.com/x/real  (skip-url)" in dry.output

    done = run(main, ["architect", "sweep-legacy-verdicts", "--before", "2026-07-01",
                      "--skip-url", "https://github.com/x/real", "--apply"])
    assert done.exit_code == 0, done.output
    assert _verdict_for(db, "https://github.com/x/real") == "full-eval"
    assert _verdict_for(db, "https://github.com/x/legacy") == "unknown"
