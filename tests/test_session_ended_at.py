"""sessions.ended_at for sessions that died unclean (#516).

`end_session` is the ONLY writer of `ended_at` and it runs from
`run_session_end`, which a crash, kill, or Remote Control timeout never reaches.
Coverage was 52% of June sessions, 19% of July, 7% of August — decaying, not
static. With `ended_at` NULL the interval is open-ended, so "which session was
live at T" matched every session ever started before T.

THE IDLE WINDOW IS THE WHOLE SAFETY. Several sessions run concurrently against
this store, so a sweep keyed on anything other than SILENCE would close its
peers. `test_a_live_peer_session_is_not_closed` is the guard, and it is the one
that would matter in production.
"""

from __future__ import annotations

import pytest

from techne.core.database import connect, init_db
from techne.core.state import close_abandoned_sessions


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    return p


def _session(db, sid, started, ended=None):
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
            (sid, started, ended),
        )
        conn.commit()
    finally:
        conn.close()


def _obs(db, sid, ts):
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
            "VALUES (?, 'Bash', 'x', 'hot', ?)",
            (sid, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _ended(db, sid):
    conn = connect(db)
    try:
        return conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
    finally:
        conn.close()


def test_derives_ended_at_from_last_activity(db):
    _session(db, "dead", "2026-08-01 10:00:00")
    _obs(db, "dead", "2026-08-01 10:05:00")
    _obs(db, "dead", "2026-08-01 10:30:00")

    assert close_abandoned_sessions(db, now="2026-08-02 00:00:00") == 1
    assert _ended(db, "dead") == "2026-08-01 10:30:00", "must be LAST activity, not first"


def test_a_live_peer_session_is_not_closed(db):
    """THE GUARD THAT MATTERS. Concurrent sessions are the normal case here.

    A sweep keyed on 'older than the newest session' would close every peer.
    Keying on silence is what makes it safe to run from session_start while
    other sessions are mid-work.
    """
    _session(db, "peer", "2026-08-01 10:00:00")
    _obs(db, "peer", "2026-08-01 23:58:00")  # active two minutes before the cutoff

    assert close_abandoned_sessions(db, now="2026-08-01 23:00:00") == 0
    assert _ended(db, "peer") is None


def test_a_session_with_no_observations_falls_back_to_started_at(db):
    """Better than leaving it NULL: an empty session's interval is a point, and
    a point that excludes every other T is more useful than one that matches
    every T."""
    _session(db, "empty", "2026-08-01 10:00:00")

    assert close_abandoned_sessions(db, now="2026-08-02 00:00:00") == 1
    assert _ended(db, "empty") == "2026-08-01 10:00:00"


def test_a_cleanly_ended_session_is_left_alone(db):
    _session(db, "clean", "2026-08-01 10:00:00", ended="2026-08-01 11:00:00")
    _obs(db, "clean", "2026-08-01 10:30:00")

    assert close_abandoned_sessions(db, now="2026-08-02 00:00:00") == 0
    assert _ended(db, "clean") == "2026-08-01 11:00:00"


def test_a_stale_ended_at_is_moved_forward_not_left_wrong(db):
    """SELF-CORRECTING. A session swept, then resumed, then abandoned again would
    otherwise keep an ended_at earlier than its own last observation — a row
    asserting the session was over while the ledger shows it working."""
    _session(db, "resumed", "2026-08-01 10:00:00", ended="2026-08-01 10:30:00")
    _obs(db, "resumed", "2026-08-01 12:00:00")

    assert close_abandoned_sessions(db, now="2026-08-02 00:00:00") == 1
    assert _ended(db, "resumed") == "2026-08-01 12:00:00"


def test_sweep_is_idempotent(db):
    _session(db, "dead", "2026-08-01 10:00:00")
    _obs(db, "dead", "2026-08-01 10:30:00")

    first = close_abandoned_sessions(db, now="2026-08-02 00:00:00")
    second = close_abandoned_sessions(db, now="2026-08-02 00:00:00")

    assert (first, second) == (1, 0), "a second pass rewrote rows that were already correct"


def test_mixed_timestamp_formats_do_not_break_the_derivation(db):
    """Must be correct whether or not #515's backfill has run.

    The ISO row is chronologically EARLIER but sorts LATER lexically, so a naive
    MAX() picks it and stamps ended_at before the session's real last activity.
    """
    # THE SAME INSTANT IN TWO SHAPES. An earlier version used 10:05 vs 10:30 —
    # different MINUTES — so a plain replace('T',' ') already picked the right
    # row and the assertion could not separate the states: removing the
    # substr(...,1,19) truncation left the whole suite green. With both rows at
    # 10:30, only full normalisation gives the canonical 19-char value; bare
    # replace leaves the 24-char '...10:30:00.965Z', which sorts AFTER it and
    # wins the MAX.
    _session(db, "mixed", "2026-08-01 10:00:00")
    _obs(db, "mixed", "2026-08-01T10:30:00.965Z")
    _obs(db, "mixed", "2026-08-01 10:30:00")

    conn = connect(db)
    try:
        naive = conn.execute(
            "SELECT MAX(timestamp) FROM observations WHERE session_id = 'mixed'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert naive.startswith("2026-08-01T10:30"), "precondition: raw MAX picks the ISO row"

    close_abandoned_sessions(db, now="2026-08-02 00:00:00")
    assert _ended(db, "mixed") == "2026-08-01 10:30:00"


# ---- the window query the whole issue exists to enable ----------------------


def test_a_timestamp_resolves_to_exactly_one_session(db):
    """#516's actual acceptance test.

    Before the fix every session ever started before T matched, because a NULL
    ended_at leaves the interval open. Asserting the BROKEN behaviour first is
    what makes this a test of the fix rather than a restatement of it.
    """
    _session(db, "june", "2026-06-09 10:00:00")
    _obs(db, "june", "2026-06-09 11:00:00")
    _session(db, "august", "2026-08-07 13:00:00")
    _obs(db, "august", "2026-08-07 15:00:00")

    window = (
        "SELECT id FROM sessions WHERE started_at <= :t "
        "AND (ended_at IS NULL OR ended_at >= :t) ORDER BY id"
    )
    t = {"t": "2026-08-07 14:00:00"}

    conn = connect(db)
    try:
        before = [r[0] for r in conn.execute(window, t)]
        assert before == ["august", "june"], (
            f"precondition: the open interval must match the June session too, got {before}"
        )
    finally:
        conn.close()

    close_abandoned_sessions(db, now="2026-08-08 00:00:00")

    conn = connect(db)
    try:
        after = [r[0] for r in conn.execute(window, t)]
    finally:
        conn.close()
    assert after == ["august"], f"T must resolve to one session, got {after}"


# ---- wiring -----------------------------------------------------------------


def test_session_start_sweeps(tmp_path, monkeypatch):
    """The sweep has to run on ordinary use.

    A maintenance verb nobody remembers to run is the failure mode #514 and #488
    are both about — the mechanism exists and never binds. Exercised through
    run_session_start rather than by calling the sweep directly, because the
    wiring is the thing under test.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.lifecycle import run_session_start

    config = Config.load()
    init_db(config.db_path).close()
    _session(config.db_path, "dead", "2026-08-01 10:00:00")
    _obs(config.db_path, "dead", "2026-08-01 10:30:00")

    run_session_start(config)

    assert _ended(config.db_path, "dead") == "2026-08-01 10:30:00"


def test_session_start_survives_a_failing_sweep(tmp_path, monkeypatch):
    """A missing ended_at is the status quo; it must never cost a session start."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core import state
    from techne.lifecycle import run_session_start

    config = Config.load()
    init_db(config.db_path).close()

    def boom(*a, **k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(state, "close_abandoned_sessions", boom)
    sid = run_session_start(config)
    assert sid, "session start must succeed even when the sweep fails"


def test_migration_24_backfills_history(tmp_path):
    """Step 24 runs the SAME sweep the live path does.

    Calls the shared function rather than `_migrate`, which cannot be re-invoked
    on a fresh DB (schema_version is UNIQUE and init_db already stamped it) —
    the same constraint noted at tests/test_audit.py:304.

    idle_hours=0 mirrors the step: every session in history is finished, and the
    `< now` bound still excludes the session performing the upgrade.
    """
    from techne.core.state import close_abandoned_sessions_in_conn

    db = tmp_path / "t.db"
    init_db(db).close()
    _session(db, "dead", "2026-08-01 10:00:00")
    _obs(db, "dead", "2026-08-01 10:30:00")

    conn = connect(db)
    try:
        assert close_abandoned_sessions_in_conn(conn, idle_hours=0) == 1
        conn.commit()
    finally:
        conn.close()

    assert _ended(db, "dead") == "2026-08-01 10:30:00"


def test_a_malformed_observation_does_not_suppress_its_session(db):
    """One garbage row must not make the whole session invisible.

    MAX() takes the lexically greatest value, so 'not-a-timestamp' beats every
    real timestamp and then never compares < cutoff — the session drops out of
    the target set entirely and is absent from the returned count, so nothing
    reports it and no later sweep recovers it. Fails safe, but invisibly.
    """
    _session(db, "dirty", "2026-08-01 10:00:00")
    _obs(db, "dirty", "2026-08-01 10:30:00")
    _obs(db, "dirty", "not-a-timestamp")

    assert close_abandoned_sessions(db, now="2026-08-02 00:00:00") == 1
    assert _ended(db, "dirty") == "2026-08-01 10:30:00"


def test_derived_and_clean_ends_are_distinguishable(db):
    """ended_at now carries two meanings; a row must say which it holds.

    For a cleanly-ended session it is the true end. For a derived one it is a
    LOWER BOUND — last known activity — understating duration by however long
    the process lived after its final observation. check_outbox_targets depends
    on the difference.
    """
    from techne.core.state import end_session

    _session(db, "crashed", "2026-08-01 10:00:00")
    _obs(db, "crashed", "2026-08-01 10:30:00")
    _session(db, "clean", "2026-08-01 10:00:00")
    end_session(db, "clean", summary="done")

    close_abandoned_sessions(db, now="2026-08-02 00:00:00")

    conn = connect(db)
    try:
        got = dict(conn.execute("SELECT id, ended_at_source FROM sessions").fetchall())
    finally:
        conn.close()

    assert got["crashed"] == "derived"
    assert got["clean"] == "clean"


def test_a_crashed_session_is_not_evidence_the_producer_is_dead(tmp_path, monkeypatch):
    """The check_outbox_targets consequence, asserted against its real query.

    Those lanes are fed AT SESSION END, so only a session that actually ENDED is
    evidence the producer failed to feed one. A crashed session never had the
    opportunity. Before the provenance column, deriving ended_at made every crash
    count as evidence — and with 93% of August sessions never reaching
    run_session_end, that reports STALE on a healthy store.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    db = tmp_path / "t.db"
    init_db(db).close()
    _session(db, "crashed", "2026-08-01 10:00:00")
    _obs(db, "crashed", "2026-08-07 10:30:00")
    close_abandoned_sessions(db, now="2026-08-08 00:00:00")

    conn = connect(db)
    try:
        count, newest = conn.execute(
            "SELECT COUNT(*), MAX(ended_at) FROM sessions "
            "WHERE ended_at IS NOT NULL AND COALESCE(ended_at_source, 'clean') = 'clean'"
        ).fetchone()
    finally:
        conn.close()

    assert (count, newest) == (0, None), (
        "a derived end counted as a cleanly-ended session — check_outbox_targets "
        "will report 'producer may be dead' on a store that merely crashes"
    )


def test_migration_24_does_not_close_a_live_peer(tmp_path):
    """The migration path had the idle window switched OFF.

    Step 24 passed idle_hours=0 on the reasoning that history is finished — false
    whenever a peer is live, which this store's own docstring says is normal.
    `connect()` IS the migration trigger, so a post_tool_use hook writing an
    observation runs the step with that session's earlier observations already in
    the past; a peer five seconds idle got stamped. `test_a_live_peer_session_is
    _not_closed` could not see it, because the migration was the one caller that
    disabled the guard it tests.
    """
    import sqlite3

    db = tmp_path / "t.db"
    connect(db).close()

    raw = sqlite3.connect(str(db))
    raw.execute("DELETE FROM schema_version")
    raw.execute("INSERT INTO schema_version (version) VALUES (21)")
    raw.execute("INSERT INTO sessions (id, started_at) VALUES ('peer', datetime('now','-2 hours'))")
    raw.execute(
        "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
        "VALUES ('peer','Bash','x','hot', datetime('now','-5 seconds'))"
    )
    raw.commit()
    raw.close()

    connect(db).close()  # triggers the pending migration through step 24

    post = sqlite3.connect(str(db))
    try:
        ended = post.execute("SELECT ended_at FROM sessions WHERE id='peer'").fetchone()[0]
    finally:
        post.close()

    assert ended is None, f"the migration closed a session active 5s ago: {ended!r}"
