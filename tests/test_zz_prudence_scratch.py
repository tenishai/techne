"""SCRATCH — Prudence review probes for PR #432. Delete before merge."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from techne.core.architect import ARCHITECT_SCHEMA
from techne.core.patterns import (
    ingest_patterns_md,
    set_verdict,
    sweep_legacy_verdicts,
    upsert_investigation,
)
from techne.core.stores import SqliteStore


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "architect.db"
    SqliteStore("architect", p).migrate(ARCHITECT_SCHEMA)
    return p


def _verdict_for(db, url):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT verdict FROM investigations WHERE url = ?", (url,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- (a) flakiness


def _run_lock_race(db, monkeypatch, load_threads=0):
    """Verbatim mechanism from the PR's test, instrumented with timings."""
    from techne.core import patterns as P

    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")

    at_select = threading.Event()
    resume = threading.Event()
    contender: dict[str, bool] = {}
    timing: dict[str, float] = {}
    real_connect = P.plain_connect

    class _Stalling:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            cur = self._inner.execute(sql, *a, **kw)
            if sql.lstrip().upper().startswith("SELECT"):
                at_select.set()
                t0 = time.monotonic()
                got = resume.wait(3)
                timing["stall"] = time.monotonic() - t0
                timing["resume_signalled"] = got
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(P, "plain_connect", lambda p: _Stalling(real_connect(p)))

    def contend():
        at_select.wait(3)
        conn = sqlite3.connect(db, timeout=0.25)
        t0 = time.monotonic()
        try:
            conn.execute("UPDATE investigations SET verdict = 'skip' WHERE url = 'old'")
            conn.commit()
            contender["wrote"] = True
        except sqlite3.OperationalError:
            contender["wrote"] = False
        finally:
            timing["contend"] = time.monotonic() - t0
            conn.close()
            resume.set()

    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x += sum(i * i for i in range(2000))

    loaders = [threading.Thread(target=burn, daemon=True) for _ in range(load_threads)]
    for ldr in loaders:
        ldr.start()

    t = threading.Thread(target=contend)
    t.start()
    try:
        P.sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    finally:
        resume.set()
        t.join(5)
        stop.set()
    return contender, timing


@pytest.mark.parametrize("i", range(40))
def test_race_repeat_clean(db, monkeypatch, i):
    contender, timing = _run_lock_race(db, monkeypatch)
    assert contender.get("wrote") is False, timing
    # margin: how close did the contender's 0.25s busy-timeout come to the 3s stall?
    assert timing["stall"] < 2.9, f"stall nearly timed out: {timing}"


@pytest.mark.parametrize("i", range(15))
def test_race_repeat_under_gil_load(db, monkeypatch, i):
    contender, timing = _run_lock_race(db, monkeypatch, load_threads=12)
    assert contender.get("wrote") is False, timing
    print(f"\nloaded run {i}: {timing}")


def test_what_the_sweep_calls_on_the_wrapper(db, monkeypatch):
    """Enumerate every attribute the sweep pulls through __getattr__, and confirm
    no implicit dunder (with-block / iteration) is used, which would bypass it."""
    from techne.core import patterns as P

    upsert_investigation(db, url="old", surveyed_at="2026-06-01", verdict="full-eval")
    seen: list[str] = []
    real_connect = P.plain_connect

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            seen.append(f"execute:{sql.split()[0].upper()}")
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            seen.append(f"getattr:{name}")
            return getattr(self._inner, name)

    monkeypatch.setattr(P, "plain_connect", lambda p: _Spy(real_connect(p)))
    P.sweep_legacy_verdicts(db, before="2026-07-01", dry_run=False)
    print("\nSWEEP TOUCHED:", seen)
    assert _verdict_for(db, "old") == "unknown"  # the commit really landed


# ------------------------------------------------- (b) can a row be stranded?


PATTERNS_MD = (
    "## Bulkhead isolation\n\n"
    "- **Source**: https://github.com/z/nope\n"
    "- **Surveyed**: 2026-06-20\n\n"
    "Isolate failure domains so one pool cannot exhaust another.\n\n"
    "---\n"
)

NO_LIFT_MD = (
    "## Surveyed (no patterns worth saving)\n\n"
    "- https://github.com/z/nope — surveyed 2026-06-10 — generic CRUD\n"
)


def test_no_lift_then_patterns_bare_reingest(db):
    """A URL first seen as a no-lift bullet, later carrying patterns."""
    ingest_patterns_md(db, NO_LIFT_MD)
    assert _verdict_for(db, "https://github.com/z/nope") == "skip-with-flag"

    res = ingest_patterns_md(db, PATTERNS_MD)  # bare re-ingest, no --verdict
    print("\nafter bare re-ingest:", _verdict_for(db, "https://github.com/z/nope"), res)

    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT count(*) FROM patterns WHERE source_url = ?", ("https://github.com/z/nope",)
    ).fetchone()[0]
    conn.close()
    print("patterns rows for that url:", n)
    # Record the actual state rather than asserting a belief:
    assert n >= 0


def test_no_lift_then_patterns_with_stated_verdict(db):
    ingest_patterns_md(db, NO_LIFT_MD)
    ingest_patterns_md(db, PATTERNS_MD, verdict="full-eval")
    print("\nwith --verdict full-eval:", _verdict_for(db, "https://github.com/z/nope"))


def test_stated_verdict_clobbers_other_urls_reverdicts(db):
    """Is `ingest --verdict` a safe repair tool, or a shotgun?"""
    md = (
        "## Alpha probe\n\n"
        "- **Source**: https://github.com/a/one\n"
        "- **Surveyed**: 2026-06-20\n\n"
        "Body one.\n\n"
        "---\n\n"
        "## Beta probe\n\n"
        "- **Source**: https://github.com/b/two\n"
        "- **Surveyed**: 2026-06-20\n\n"
        "Body two.\n\n"
        "---\n"
    )
    ingest_patterns_md(db, md)
    set_verdict(db, url="https://github.com/a/one", verdict="full-eval")  # operator judgment
    ingest_patterns_md(db, md, verdict="patterns-only")  # repairing some OTHER url
    print(
        "\na/one after someone else's --verdict:",
        _verdict_for(db, "https://github.com/a/one"),
    )


def test_patterns_then_no_lift_reverse_transition(db):
    """The direction edbe155 actually changed: a URL that carried patterns and
    later moves INTO the no-lift section. Before this commit the no-lift write
    overwrote and self-corrected to skip-with-flag; now it is insert-only."""
    ingest_patterns_md(db, PATTERNS_MD)
    assert _verdict_for(db, "https://github.com/z/nope") == "patterns-only"
    ingest_patterns_md(db, NO_LIFT_MD)  # URL demoted to a no-lift bullet
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT verdict, skip_reason FROM investigations WHERE url = ?",
        ("https://github.com/z/nope",),
    ).fetchone()
    conn.close()
    print("\nafter demotion to no-lift:", row)


def test_sweep_cannot_reach_a_frozen_row(db):
    """If a row is stuck, does the sweep offer a way out?"""
    ingest_patterns_md(db, NO_LIFT_MD)
    res = sweep_legacy_verdicts(db, before="2026-07-01", dry_run=True)
    print("\nsweep sees:", res)
