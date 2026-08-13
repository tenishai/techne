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


def test_search_patterns_or_fallback_does_not_disturb_a_matching_and_query(db):
    """#557: the fallback fires only on an empty AND pass — a working query is unchanged.

    This is the no-regression half: adding the fallback must not widen results for
    queries that already resolve, or precise searches would silently get noisier.
    """
    inv = upsert_investigation(db, url="u", surveyed_at="d", verdict="patterns-only")
    add_pattern(
        db,
        investigation_id=inv,
        name="Quarantine-fence untrusted content",
        source_url="u",
        body="Preserve, do not drop; defuse, do not trust.",
    )
    # LOAD-BEARING FIXTURE DESIGN: the decoy must contain EXACTLY ONE of the two
    # query terms ("quarantine", not "untrusted"). That is what gives this test teeth.
    # AND("quarantine","untrusted") matches only the first pattern, so the guard holds
    # the fallback back — but OR("quarantine" OR "untrusted") WOULD match this decoy,
    # so if `if not results` is ever removed the OR pass runs, returns two rows, and
    # the assertion below breaks. A decoy sharing NO query term makes the test vacuous:
    # it then passes with or without the guard (verified by mutation — the first
    # version of this test survived replacing the guard with `if True`).
    add_pattern(
        db,
        investigation_id=inv,
        name="Unrelated durable execution runtime",
        source_url="u2",
        body="Swap the runtime without changing agent code. Nothing to do with quarantine.",
    )

    hits = search_patterns(db, "quarantine untrusted")
    assert len(hits) == 1, "AND pass matched, so the OR fallback must not have widened it"
    assert hits[0]["name"] == "Quarantine-fence untrusted content"


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
