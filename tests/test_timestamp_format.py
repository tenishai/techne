"""One on-disk timestamp shape (#515).

`observations.timestamp` is TEXT and compared LEXICALLY, so the storage format IS
the ordering primitive. It held three shapes at once — the column default
(`2026-06-09 23:25:10`, 18,327 rows), bare ISO (18), and ISO with fractional
seconds and a Z (3,439) — all written concurrently across the whole history.
Because 'T' (0x54) sorts after ' ' (0x20) at position 10, every ISO row compared
as LATER than every space-separated row on the same date: ~12% wrong on a live
range query, with nothing raised.

THE GUARD HERE EXERCISES THE REAL WRITERS. A test that asserts "no 'T' in the
column" over rows a test wrote itself passes just as happily on unfixed code —
it cannot distinguish the failing state from the passing one, which is the
discriminator that catches a hollow guard before mutation testing has to.
"""

from __future__ import annotations

import json

import pytest

from techne.core.database import (
    TIMESTAMP_FORMAT,
    backfill_timestamp_formats,
    connect,
    init_db,
    normalize_timestamp,
)

# Every shape observed in the operator's store, plus the one the fix targets.
REAL_SHAPES = [
    ("2026-08-08T00:19:00.965Z", "2026-08-08 00:19:00"),  # 3,439 rows
    ("2026-06-09T14:00:00", "2026-06-09 14:00:00"),  # 18 rows
    ("2026-06-09 23:25:10", "2026-06-09 23:25:10"),  # 18,327 rows — already right
]


@pytest.mark.parametrize("raw,expected", REAL_SHAPES)
def test_normalize_produces_one_shape(raw, expected):
    assert normalize_timestamp(raw) == expected


def test_normalize_strips_the_fraction_and_zone_not_just_the_separator():
    """A 'T' -> ' ' substitution is NOT enough, and this is why.

    `2026-08-08 00:19:00.965Z` is 24 characters. Compared lexically against a
    19-character bound like `2026-08-08 00:19:00`, the trailing `.965Z` makes it
    sort AFTER — so the row is still misplaced, by a fix that looks complete.
    """
    naive = "2026-08-08T00:19:00.965Z".replace("T", " ")
    assert naive > "2026-08-08 00:19:00", "precondition: the naive fix still mis-sorts"
    assert normalize_timestamp("2026-08-08T00:19:00.965Z") == "2026-08-08 00:19:00"
    assert len(normalize_timestamp("2026-08-08T00:19:00.965Z")) == 19


def test_normalize_converts_an_offset_to_utc():
    """Both writers are UTC today; an offset-bearing value must not slip through
    as wall-clock text, which would misplace it by the offset."""
    assert normalize_timestamp("2026-08-08T00:19:00-04:00") == "2026-08-08 04:19:00"


def test_normalize_passes_none_through():
    assert normalize_timestamp(None) is None


def test_normalize_leaves_an_unparseable_value_alone():
    """Return it UNCHANGED rather than dropping it or coercing to now().

    Inventing a timestamp fabricates history; dropping one loses the row's place
    in it. An unknown shape should show up in the data AS ITSELF, so it can be
    found, rather than as a plausible lie.
    """
    assert normalize_timestamp("not a timestamp") == "not a timestamp"


def test_lexical_order_matches_chronological_order_once_normalised():
    """The property the column actually has to have.

    Sorting the raw values gives the WRONG order; sorting the normalised ones
    gives the right one. Asserting both halves is what makes this a test of the
    fix rather than a restatement of it.
    """
    raw = ["2026-08-08T00:19:00.965Z", "2026-08-08 00:21:54", "2026-08-08T00:25:00.100Z"]
    chronological = ["2026-08-08 00:19:00", "2026-08-08 00:21:54", "2026-08-08 00:25:00"]

    assert sorted(raw) != sorted(raw, key=normalize_timestamp), (
        "precondition: raw lexical order must differ from chronological order"
    )
    assert sorted(normalize_timestamp(r) for r in raw) == chronological


# ---- the writers themselves -------------------------------------------------


def _seed_session(conn, sid="s"):
    """observations.session_id is a FOREIGN KEY — the parent row must exist."""
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (sid,))
    conn.commit()


def _transcript(tmp_path, session_id, stamps):
    """A minimal Claude Code transcript carrying Z-suffixed ISO timestamps —
    the shape the real JSONL uses, which is where the ISO rows came from."""
    path = tmp_path / f"{session_id}.jsonl"
    with path.open("w") as fh:
        for i, ts in enumerate(stamps):
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": ts,
                        "message": {"content": [{"type": "text", "text": f"line {i}"}]},
                    }
                )
                + "\n"
            )
    return path


def test_the_import_writer_stores_the_canonical_shape(tmp_path, monkeypatch):
    """EXERCISES THE REAL WRITER, not a hand-built row.

    This is the test that fails on unfixed code: before the fix the importer
    passed the JSONL's `...Z` value straight through, so the stored row carried
    a 'T'. A fixture-only assertion would pass either way.
    """
    from techne.platforms.claude_code import transcript_import

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    db = tmp_path / "t.db"
    init_db(db).close()

    sid = "sess-515"
    path = _transcript(tmp_path, sid, ["2026-08-08T00:19:00.965Z", "2026-08-08T00:25:00.100Z"])
    result = transcript_import.import_transcript(db, path)
    assert not result.get("skipped"), f"import was skipped, nothing exercised: {result}"

    conn = connect(db)
    try:
        rows = [
            r[0]
            for r in conn.execute("SELECT timestamp FROM observations WHERE session_id = ?", (sid,))
        ]
    finally:
        conn.close()

    assert rows, "the importer wrote nothing; this test would pass vacuously"
    offenders = [r for r in rows if r and "T" in r]
    assert not offenders, f"the importer stored non-canonical timestamps: {offenders}"


def test_the_default_writer_already_matches_the_canonical_shape(tmp_path):
    """The column default is the format being standardised on.

    Pinning it means a future schema change that swaps the default to ISO breaks
    HERE, rather than silently reintroducing the split from the other side.
    """
    import datetime

    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        _seed_session(conn)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier) "
            "VALUES ('s', 'Bash', 'x', 'hot')"
        )
        conn.commit()
        stored = conn.execute("SELECT timestamp FROM observations").fetchone()[0]
    finally:
        conn.close()

    datetime.datetime.strptime(stored, TIMESTAMP_FORMAT)  # raises if the shape drifted
    assert "T" not in stored


# ---- the backfill -----------------------------------------------------------


def test_migration_23_backfills_both_columns(tmp_path):
    """Rows written before the fix must be repaired, not just new ones."""
    db = tmp_path / "t.db"
    init_db(db).close()

    conn = connect(db)
    try:
        _seed_session(conn)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp, event_at) "
            "VALUES ('s', 'Bash', 'x', 'hot', '2026-08-08T00:19:00.965Z', "
            "'2026-08-08T00:18:00.100Z')"
        )
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp, event_at) "
            "VALUES ('s', 'Bash', 'y', 'hot', '2026-08-08 00:21:54', NULL)"
        )
        conn.commit()
        backfill_timestamp_formats(conn)
        conn.commit()

        rows = conn.execute("SELECT timestamp, event_at FROM observations ORDER BY id").fetchall()
    finally:
        conn.close()

    assert rows[0][0] == "2026-08-08 00:19:00"
    assert rows[0][1] == "2026-08-08 00:18:00"
    # These two assert VALUE preservation, which is all they can detect:
    # substr(NULL) is NULL regardless, so no mutation of the UPDATE makes the
    # event_at line fire, and a value-preserving rewrite passes the first. The
    # ROWCOUNT property they used to claim is genuinely covered by the
    # `changed ==` assertions in the idempotence and sessions tests.
    assert rows[1][0] == "2026-08-08 00:21:54", "an already-correct row's VALUE changed"
    assert rows[1][1] is None, "a NULL event_at gained a value"


def test_migration_23_is_idempotent(tmp_path):
    """It must be safe to re-run — `techne reconcile` can invoke it again."""
    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        _seed_session(conn)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
            "VALUES ('s', 'Bash', 'x', 'hot', '2026-08-08T00:19:00.965Z')"
        )
        conn.commit()
        first = backfill_timestamp_formats(conn)
        once = conn.execute("SELECT timestamp FROM observations").fetchone()[0]
        second = backfill_timestamp_formats(conn)
        twice = conn.execute("SELECT timestamp FROM observations").fetchone()[0]
        conn.commit()
        assert first == 1, "precondition: the first pass must actually change a row"
        assert second == 0, "the second pass rewrote rows again — not idempotent"
    finally:
        conn.close()

    assert once == twice == "2026-08-08 00:19:00"


def test_migration_23_leaves_an_unrecognised_shape_alone(tmp_path):
    """The WHERE clause is the safety: an unrecognised value must survive intact.

    The fixture is deliberately LONGER THAN 19 CHARACTERS. A shorter one cannot
    distinguish the failing state from the passing one — `substr(x, 1, 19)` is a
    no-op on it either way, so the test would pass with the guard removed. Found
    by mutation: dropping the WHERE clause left an earlier version of this test
    green.
    """
    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        _seed_session(conn)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
            "VALUES ('s', 'Bash', 'x', 'hot', 'garbage-value-that-exceeds-19-chars')"
        )
        conn.commit()
        backfill_timestamp_formats(conn)
        conn.commit()
        stored = conn.execute("SELECT timestamp FROM observations").fetchone()[0]
    finally:
        conn.close()

    assert stored == "garbage-value-that-exceeds-19-chars"


def test_backfill_covers_sessions_not_just_observations(tmp_path):
    """`sessions` carries the same split from the same importer.

    Not tidiness: #516 resolves a moment to a session with
    `started_at <= T AND (ended_at IS NULL OR ended_at >= T)`, which is exactly
    the lexical comparison this bug breaks. Leaving `sessions` alone would fix
    the ledger and leave the index into it wrong.
    """
    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at) "
            "VALUES ('s1', '2026-08-08T00:19:00.965Z', '2026-08-08T00:25:00.100Z')"
        )
        conn.execute("INSERT INTO sessions (id, started_at) VALUES ('s2', '2026-08-08 00:30:00')")
        conn.commit()
        changed = backfill_timestamp_formats(conn)
        conn.commit()
        rows = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute("SELECT id, started_at, ended_at FROM sessions")
        )
    finally:
        conn.close()

    assert changed == 2, f"expected both s1 columns rewritten, got {changed}"
    assert rows["s1"] == ("2026-08-08 00:19:00", "2026-08-08 00:25:00")
    assert rows["s2"] == ("2026-08-08 00:30:00", None), "an already-correct row was rewritten"


def test_backfill_map_does_not_claim_clean_tables():
    """The map is scoped to what the importers actually write.

    Measured on the operator's store: knowledge.created_at/updated_at,
    threads.created_at/updated_at, outbox.CREATED_AT and coord_messages.created_at
    are uniformly space-separated, because each is written by CURRENT_TIMESTAMP or
    the house strftime helper. Listing them would make the migration scan tables
    it has no reason to touch and imply a defect measured not to exist.

    `outbox.SYNCED_AT` is in the map for the opposite reason — it was 100%
    non-conforming (isoformat with fraction and offset), which made the module's
    "one on-disk shape" claim false.
    """
    from techne.core.database import _TIMESTAMP_BACKFILL_COLUMNS

    assert set(_TIMESTAMP_BACKFILL_COLUMNS) == {"observations", "sessions", "outbox"}


def test_step_23_actually_runs_the_backfill(tmp_path):
    """The WIRING, not the function.

    Every other backfill test calls `backfill_timestamp_formats` directly, so
    the call site inside `_migrate` was outside the test boundary: deleting it
    left the whole suite green while every existing store went unrepaired. That
    is this PR's own discriminator applied one level up — extracting the named
    function made idempotence testable and moved the invocation out of cover.

    Drives a REAL v21 -> current upgrade through `connect()`, so it also serves
    as the only end-to-end proof that `event_at` is repaired on a live upgrade
    path rather than only by a direct call.
    """
    import sqlite3

    db = tmp_path / "t.db"
    connect(db).close()

    raw = sqlite3.connect(str(db))
    raw.execute("DELETE FROM schema_version")
    raw.execute("INSERT INTO schema_version (version) VALUES (21)")
    raw.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s')")
    raw.execute(
        "INSERT INTO observations (session_id, tool_name, content, tier, timestamp, event_at) "
        "VALUES ('s','Bash','x','hot','2026-08-08T00:19:00.965Z','2026-08-08T00:18:00.100Z')"
    )
    raw.commit()
    raw.close()

    connect(db).close()  # triggers the guarded migrate 21 -> SCHEMA_VERSION

    post = sqlite3.connect(str(db))
    try:
        ts, ea = post.execute("SELECT timestamp, event_at FROM observations").fetchone()
    finally:
        post.close()

    assert ts == "2026-08-08 00:19:00", f"step 23 did not backfill timestamp: {ts!r}"
    assert ea == "2026-08-08 00:18:00", f"step 23 did not backfill event_at: {ea!r}"


def test_backfill_converts_a_non_utc_offset(tmp_path):
    """SQL CONVERTS the offset; it does not truncate, and must not refuse.

    An earlier version used substr(...,1,19)+replace, which keeps WALL CLOCK and
    drops the zone, so it had to REFUSE offset-bearing rows. That rested on
    "Python can convert, SQL cannot" — false: strftime agrees with
    normalize_timestamp on every shape, including -04:00 and +05:30. The refusal
    was also silent AND permanent: _migrate discards the return value and can
    never re-run, so a refused row stayed non-conforming forever in a column that
    exists to sort lexically.
    """
    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        _seed_session(conn)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
            "VALUES ('s','Bash','x','hot','2026-08-08T00:19:00-04:00')"
        )
        conn.commit()
        backfill_timestamp_formats(conn)
        conn.commit()
        stored = conn.execute("SELECT timestamp FROM observations").fetchone()[0]
    finally:
        conn.close()

    assert stored == "2026-08-08 04:19:00", f"offset not converted to UTC: {stored!r}"
    assert stored == normalize_timestamp("2026-08-08T00:19:00-04:00"), (
        "the SQL backfill and normalize_timestamp disagree on this shape"
    )


def test_backfill_does_not_null_out_an_unparseable_value(tmp_path):
    """The strftime IS NOT NULL guard is load-bearing.

    strftime returns NULL on 'not a timestamp' and on '2026-13-45T99:99:99'
    (both checked), so without the guard the UPDATE would NULL those rows out —
    strictly worse than leaving them alone, because it destroys the row's place
    in the ledger rather than merely failing to improve it.
    """
    db = tmp_path / "t.db"
    init_db(db).close()
    conn = connect(db)
    try:
        _seed_session(conn)
        for bad in ("2026-13-45T99:99:99", "garbage-value-that-exceeds-19-chars"):
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
                "VALUES ('s','Bash','x','hot',?)",
                (bad,),
            )
        conn.commit()
        backfill_timestamp_formats(conn)
        conn.commit()
        rows = [r[0] for r in conn.execute("SELECT timestamp FROM observations ORDER BY id")]
    finally:
        conn.close()

    assert rows == ["2026-13-45T99:99:99", "garbage-value-that-exceeds-19-chars"], (
        f"an unparseable value was nulled out or rewritten: {rows}"
    )


def test_the_documented_api_path_stores_the_canonical_shape(tmp_path, monkeypatch):
    """event_at is CALLER-SUPPLIED, and both public surfaces ask for ISO-8601.

    The MCP tool schema and the `--event-at` CLI flag both document it as
    "ISO-8601 event time" — the exact shape migration 023 exists to eliminate.
    The four transcript-importer sites normalised; this one did not, so a
    supported path re-introduced the defect into a column this migration
    repairs. It is invisible in a census of existing rows because event_at is
    usually NULL on live capture, so it only appears when a caller supplies it,
    and `_migrate` can never re-run to clean it up.

    Exercised through `record_observation`, not by calling the normaliser.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.state import record_observation, start_session

    cfg = Config.load()
    init_db(cfg.db_path).close()
    sid = start_session(cfg.db_path)

    # record_observation takes the CONFIG, and everything after content is
    # keyword-only — this is the shape the MCP tool and the CLI call.
    record_observation(cfg, sid, "ran a thing", tool_name="Bash", event_at="2026-08-08T00:19:00Z")

    conn = connect(cfg.db_path)
    try:
        stored = conn.execute(
            "SELECT event_at FROM observations WHERE session_id = ?", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert stored == "2026-08-08 00:19:00", (
        f"the documented API stored {stored!r} — a 00:19 ISO row sorts ABOVE a "
        f"23:00 canonical row on the same date, so health.py's "
        f"MAX(COALESCE(event_at, timestamp)) reads it as the latest activity"
    )
