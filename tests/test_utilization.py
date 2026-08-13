"""Tests for the utilization audit (techne#329) — techne.core.utilization.

Read-only descriptive panels over the observation ledger + injection log. These
seed a temp db with the real writers (add_observation, log_context_injection)
and assert the aggregates, ratios, windowing, and empty-db behaviour.
"""

from __future__ import annotations

import pytest

from techne.core.database import init_db
from techne.core.state import add_observation, ensure_session
from techne.core.utilization import (
    edit_rail_attribution,
    injection_utilization,
    read_write_ledger,
    recall_attribution,
)
from techne.hooks._injection_log import log_context_injection


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p)
    ensure_session(p, "s1")
    return p


def _obs(db, tool, n=1):
    for _ in range(n):
        add_observation(db, "s1", tool, "x")


def _backdate(db, tool, days):
    """Shift a tool's rows to `days` ago so the recency window excludes them."""
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    conn.execute(
        "UPDATE observations SET timestamp = datetime('now', ?) WHERE tool_name = ?",
        (f"-{days} days", tool),
    )
    conn.commit()
    conn.close()


# ---- injection_utilization -------------------------------------------------


def test_injection_utilization_aggregates_per_source(db):
    # session_start: 2 events, one with 3 hits (real), one empty (fired, 0 hits).
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[{"id": 1}, {"id": 2}, {"id": 3}], injected_text="x" * 40,
    )
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[], injected_text="",
    )
    # codex_edit_rail: 1 event, 2 hits, a different session.
    log_context_injection(
        db, source="codex_edit_rail", session_id="s2", trigger="sym",
        rows=[{"id": "a"}, {"id": "b"}], injected_text="y" * 20,
    )

    rows = injection_utilization(db)
    by_source = {r["source"]: r for r in rows}

    ss = by_source["session_start"]
    assert ss["events"] == 2
    assert ss["total_hits"] == 3
    assert ss["empty_events"] == 1  # the fired-but-selected-nothing event
    assert ss["sessions"] == 1
    assert ss["total_tokens"] == 10  # 40 chars ≈ 10 tokens (empty event adds 0)
    assert ss["avg_tokens"] == 5  # 10 tokens over 2 events

    er = by_source["codex_edit_rail"]
    assert er["events"] == 1 and er["total_hits"] == 2 and er["sessions"] == 1

    # Ordered by event count desc.
    assert rows[0]["source"] == "session_start"


def test_injection_utilization_empty_when_no_table(db):
    """No injection ever logged → the table may not exist → [] (not a crash)."""
    assert injection_utilization(db) == []


def test_injection_utilization_reports_avg_radius(db):
    """techne#343: avg_radius = mean pre-cap total_hits for a capping source;
    None for a source that never sets it."""
    log_context_injection(
        db, source="codex_edit_rail", session_id="s1", trigger="sym",
        rows=[{"id": 1}, {"id": 2}], injected_text="x" * 20, total_hits=14,
    )
    log_context_injection(
        db, source="codex_edit_rail", session_id="s1", trigger="sym2",
        rows=[{"id": 3}], injected_text="y" * 10, total_hits=6,
    )
    # session_start never sets total_hits → avg_radius stays None.
    log_context_injection(
        db, source="session_start", session_id="s1", trigger=None,
        rows=[{"id": 9}], injected_text="z" * 8,
    )
    by = {r["source"]: r for r in injection_utilization(db)}
    assert by["codex_edit_rail"]["avg_radius"] == 10.0  # (14 + 6) / 2
    assert by["codex_edit_rail"]["total_hits"] == 3  # sum of hit_count (2 + 1)
    assert by["session_start"]["avg_radius"] is None


# ---- read_write_ledger -----------------------------------------------------


_OLD_INJECTION_DDL = (
    "CREATE TABLE context_injections (id INTEGER PRIMARY KEY, source TEXT, "
    "session_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, trigger_snippet TEXT, "
    "hit_count INTEGER, hit_ids TEXT, scores TEXT, approx_tokens INTEGER)"  # no total_hits
)


def test_log_context_injection_self_migrates_total_hits(tmp_path):
    """techne#343: writing to a pre-total_hits table ADDs the column so the value
    persists rather than erroring (the guarded ALTER)."""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_OLD_INJECTION_DDL)
    c.commit()
    c.close()

    log_context_injection(
        p, source="codex_edit_rail", session_id="s", trigger="x",
        rows=[{"id": 1}], injected_text="x" * 8, total_hits=9,
    )
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT total_hits FROM context_injections").fetchone()
    c.close()
    assert row["total_hits"] == 9


def test_injection_utilization_handles_pre_migration_table(tmp_path):
    """The audit must not crash on a table lacking total_hits (a row written before
    #343) — avg_radius is guarded to None."""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_OLD_INJECTION_DDL)
    c.execute(
        "INSERT INTO context_injections (source, hit_count, approx_tokens) "
        "VALUES ('session_start', 3, 40)"
    )
    c.commit()
    c.close()

    rows = injection_utilization(p)
    assert rows[0]["avg_radius"] is None  # column absent → NULL, no crash


def test_read_write_ledger_ratios(db):
    _obs(db, "mcp__techne__techne_store", 10)
    _obs(db, "mcp__techne__techne_recall", 3)
    _obs(db, "mcp__techne__techne_search", 1)  # reads = 4
    _obs(db, "Edit", 80)
    _obs(db, "mcp__techne__codex_search", 2)  # codex reads = 2

    led = read_write_ledger(db)
    assert led["memory"]["write"] == 10
    assert led["memory"]["read"] == 4
    assert led["memory"]["writes_per_read"] == 2.5
    assert led["codex"]["read"] == 2
    assert led["codex"]["edit"] == 80
    assert led["codex"]["edits_per_read"] == 40.0


def test_read_write_ledger_never_queried_is_none_not_zerodiv(db):
    _obs(db, "mcp__techne__techne_store", 5)  # writes but no reads
    led = read_write_ledger(db)
    assert led["memory"]["read"] == 0
    assert led["memory"]["writes_per_read"] is None  # not a ZeroDivisionError
    assert led["codex"]["edits_per_read"] is None  # no codex reads
    assert led["architect"]["read"] == 0
    assert led["librarian"]["read"] == 0


def test_librarian_flagged_not_mcp_exposed(db):
    """techne exposes no librarian MCP tool, so the ledger can't measure it —
    the row must be flagged mcp_read_tool=False (not reported as 'never queried',
    which would imply an ignored tool). architect DOES have MCP tools → True.
    (techne#329 review.)"""
    led = read_write_ledger(db)
    assert led["librarian"]["mcp_read_tool"] is False
    assert led["architect"]["mcp_read_tool"] is True


def test_read_write_ledger_counts_librarian_by_prefix(db):
    """The prefix match still lights up if a librarian_* MCP tool is ever added."""
    _obs(db, "mcp__techne__librarian_search", 2)
    _obs(db, "mcp__techne__librarian_show", 1)
    led = read_write_ledger(db)
    assert led["librarian"]["read"] == 3


def test_since_days_windows_the_ledger(db):
    _obs(db, "mcp__techne__techne_store", 4)
    _backdate(db, "mcp__techne__techne_store", days=10)  # push them out of a 2d window
    _obs(db, "mcp__techne__techne_recall", 1)  # recent

    all_time = read_write_ledger(db)
    assert all_time["memory"]["write"] == 4

    recent = read_write_ledger(db, since_days=2)
    assert recent["memory"]["write"] == 0  # backdated writes excluded
    assert recent["memory"]["read"] == 1  # recent read kept


# ---- edit_rail_attribution (techne#374) ------------------------------------


def _stamp(db, table, ts, where, params=()):
    """Force a row's timestamp (both tables default CURRENT_TIMESTAMP)."""
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    conn.execute(f"UPDATE {table} SET timestamp = ? WHERE {where}", (ts, *params))
    conn.commit()
    conn.close()


def _inject(db, ts, *, trigger, deps, session="s1"):
    """Log a codex_edit_rail injection (deps = dependent symbol-id strings) at `ts`."""
    log_context_injection(
        db, source="codex_edit_rail", session_id=session, trigger=trigger,
        rows=[{"id": d} for d in deps], injected_text="x" * 20,
    )
    _stamp(db, "context_injections", ts, "trigger_snippet = ?", (trigger,))


def _edit(db, ts, path, *, change="", session="s1"):
    """Record an Edit observation targeting `path` at `ts` (post_tool_use shape)."""
    content = f"Edited: {path}\nChange: {change}"
    add_observation(db, session, "Edit", content)
    _stamp(db, "observations", ts, "tool_name = 'Edit' AND content = ?", (content,))


def _write(db, ts, path, *, body="", session="s1"):
    """Record a Write observation (post_tool_use shape: 'Wrote: <path>\\nContent: …')."""
    content = f"Wrote: {path}\nContent: {body}..."
    add_observation(db, session, "Write", content)
    _stamp(db, "observations", ts, "tool_name = 'Write' AND content = ?", (content,))


def _multiedit(db, ts, path, *, body="", session="s1", file_path_first=True):
    """Record a MultiEdit observation, faithfully mirroring post_tool_use.summarize:
    the generic branch renders 'Tool: MultiEdit\\nInput: {json.dumps(input)[:500]}…'.
    `file_path_first` controls key order so the 500-char truncation boundary can be
    exercised (file_path at the front survives; at the back can be truncated away)."""
    import json as _json

    edit = {"old_string": body, "new_string": body}
    tool_input = (
        {"file_path": path, "edits": [edit]} if file_path_first
        else {"edits": [edit], "file_path": path}
    )
    inp = _json.dumps(tool_input)[:500]  # summarize() truncation
    content = f"Tool: MultiEdit\nInput: {inp}\nOutput: ok"
    add_observation(db, session, "MultiEdit", content)
    _stamp(db, "observations", ts, "tool_name = 'MultiEdit' AND content = ?", (content,))


def test_edit_rail_attribution_empty_db(db):
    a = edit_rail_attribution(db)
    assert a["injections"] == 0
    assert a["file_attributed_pct"] is None
    assert a["payload"]["dependents"] == 0


def test_edit_rail_attribution_file_hit(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")  # 30s later, same file
    a = edit_rail_attribution(db)
    assert a["injections"] == 1
    assert a["file_attributed"] == 1
    assert a["file_attributed_pct"] == 100.0


def test_edit_rail_attribution_excludes_trigger_file(db):
    # The only dependent shares the trigger's OWN file — editing it is not
    # "used a dependent" (you were editing that symbol anyway).
    _inject(db, "2026-01-01 12:00:00", trigger="techne/foo.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 0


def test_edit_rail_attribution_window(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _edit(db, "2026-01-01 12:10:00", "/repo/techne/foo.py")  # 10 min later
    tight = edit_rail_attribution(db, window_minutes=2.0)
    assert tight["no_edits_in_window"] == 1
    assert tight["file_attributed"] == 0
    wide = edit_rail_attribution(db, window_minutes=15.0)
    assert wide["file_attributed"] == 1


def test_edit_rail_attribution_next_edits_cap(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    # Two unrelated edits first, then the dependent — beyond next_edits=2, so uncredited.
    _edit(db, "2026-01-01 12:00:10", "/repo/techne/a.py")
    _edit(db, "2026-01-01 12:00:20", "/repo/techne/b.py")
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    assert edit_rail_attribution(db, next_edits=2)["file_attributed"] == 0
    assert edit_rail_attribution(db, next_edits=5)["file_attributed"] == 1


def test_edit_rail_attribution_symbol_name(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::compute_total::function::10"])
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", change="compute_total(x)")
    a = edit_rail_attribution(db)
    assert a["symbol_attributed"] == 1  # the edit text names the dependent


def test_edit_rail_attribution_payload_composition(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=[
                "techne/foo.py::bar::function::10",     # real
                "tests/test_foo.py::baz::function::3",  # test file
                "techne/foo.py::<module>::module::0",   # synthetic module node
            ])
    pay = edit_rail_attribution(db)["payload"]
    assert pay["dependents"] == 3
    assert pay["real_pct"] == round(100 / 3, 1)
    assert pay["module_pct"] == round(100 / 3, 1)
    assert pay["test_pct"] == round(100 / 3, 1)


def test_edit_rail_attribution_noise_only(db):
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=[
                "techne/foo.py::<module>::module::0",   # module noise
                "tests/test_x.py::a::function::1",      # test noise
            ])
    a = edit_rail_attribution(db)
    assert a["noise_only_injections"] == 1
    assert a["noise_only_pct"] == 100.0
    assert a["payload"]["real_pct"] == 0.0


# ---- techne#374 review regressions -----------------------------------------


def test_edit_rail_attribution_tz_aware_timestamp_no_crash(db):
    """Transcript-imported edits carry ISO '…Z' (tz-AWARE) timestamps while the
    injection's is naive — subtracting them used to raise TypeError and take down
    `techne audit` (review issue #1). Must not raise, and must still attribute."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _edit(db, "2026-01-01T12:00:30.000Z", "/repo/techne/foo.py")  # tz-aware shape
    a = edit_rail_attribution(db)  # no TypeError
    assert a["file_attributed"] == 1


def test_edit_rail_attribution_write_follow_up(db):
    """Write ('Wrote: <path>') follow-ups must be counted, not silently dropped
    (review issue #2)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _write(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    assert edit_rail_attribution(db)["file_attributed"] == 1


def test_edit_rail_attribution_multiedit_follow_up(db):
    """MultiEdit fires the rail, so a MultiEdit fix-up to a dependent must attribute
    at both file and symbol granularity (review issue #2)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::compute_total::function::10"])
    _multiedit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", body="compute_total(x)")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 1
    assert a["symbol_attributed"] == 1  # dependent name in the change body


def test_edit_rail_attribution_symbol_not_credited_from_path(db):
    """A dependent whose name also appears in its own filename must NOT earn symbol
    credit when the change body doesn't touch it (review nit — match body, not path)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::foo::function::10"])  # symbol 'foo' also in foo.py
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", change="unrelated_change()")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 1   # file still credited
    assert a["symbol_attributed"] == 0  # symbol not — name only in the path


def test_edit_rail_attribution_attributable_denominator(db):
    """Rate over 'injections that had a follow-up edit' is reported alongside the
    over-all rate (review suggestion)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/t1.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")  # hits the dep
    _inject(db, "2026-01-01 13:00:00", trigger="techne/t2.py::t::function::5",
            deps=["techne/baz.py::qux::function::7"])  # no follow-up edit
    a = edit_rail_attribution(db)
    assert a["injections"] == 2 and a["no_edits_in_window"] == 1
    assert a["attributable"] == 1
    assert a["file_attributed"] == 1
    assert a["file_attributed_pct"] == 50.0             # over all injections
    assert a["file_attributed_pct_attributable"] == 100.0  # over those w/ a follow-up


def test_edit_rail_attribution_multiedit_truncation_boundary(db):
    """MultiEdit's Input JSON is truncated to 500 chars by summarize(). With
    file_path FIRST (schema order), it survives even a large edits payload →
    still attributed (re-review boundary case)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    # body >500 chars so the serialised Input is truncated; file_path is first.
    _multiedit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py",
               body="z" * 600, file_path_first=True)
    assert edit_rail_attribution(db)["file_attributed"] == 1


def test_edit_rail_attribution_multiedit_truncation_degrades_gracefully(db):
    """The pathological case — file_path serialised AFTER a large edits array is
    truncated away. Documents that this degrades to a silent DROP (undercount),
    never a crash (re-review boundary case)."""
    _inject(db, "2026-01-01 12:00:00", trigger="techne/trig.py::t::function::5",
            deps=["techne/foo.py::bar::function::10"])
    _multiedit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py",
               body="z" * 600, file_path_first=False)
    a = edit_rail_attribution(db)  # must not raise
    assert a["file_attributed"] == 0  # dropped, not crashed


# ---- techne#413: per-hit retrieval provenance -------------------------------


_PRE_413_DDL = (
    "CREATE TABLE context_injections (id INTEGER PRIMARY KEY, source TEXT, "
    "session_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, trigger_snippet TEXT, "
    "hit_count INTEGER, hit_ids TEXT, scores TEXT, approx_tokens INTEGER, "
    "total_hits INTEGER)"  # post-#343, pre-#413
)


def _provenance(p):
    """Read back the #413 arrays + k for the single logged row."""
    import json
    import sqlite3

    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute(
            "SELECT scores, native_scores, score_kinds, match_sources, k_requested "
            "FROM context_injections"
        ).fetchone()
    finally:
        c.close()
    return {
        "scores": json.loads(r["scores"]),
        "native": json.loads(r["native_scores"]),
        "kinds": json.loads(r["score_kinds"]),
        "channels": json.loads(r["match_sources"]),
        "k": r["k_requested"],
    }


def _log_one(p, row, **kw):
    log_context_injection(
        p,
        source="per_turn_recall",
        session_id="s",
        trigger="t",
        rows=[row],
        injected_text="x" * 8,
        **kw,
    )


def test_vec_row_records_distance_and_its_scale(tmp_path):
    """A vector-channel hit is PLACED by distance, so that's what gets recorded —
    tagged `vec_distance` so nothing compares it to a BM25 rank.

    Fail-without-fix: pre-#413 only `scores` existed and was None here, so the
    signal that ordered the row was lost."""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 7, "match_source": "vec", "distance": 0.1234})
    got = _provenance(p)
    assert got["native"] == [0.1234]
    assert got["kinds"] == ["vec_distance"]
    assert got["channels"] == ["vec"]
    assert got["scores"] == [None]  # legacy FTS-only column unchanged


def test_fts_only_row_records_bm25_rank(tmp_path):
    """An FTS-only hit reports its raw BM25 rank, tagged as such. Note this is an
    INPUT to placement, not the placement value — _rerank blends and then pops the
    real ordering score, so it never reaches the logger (#419)."""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 8, "match_source": "fts", "rank": -3.5})
    got = _provenance(p)
    assert got["native"] == [-3.5]
    assert got["kinds"] == ["bm25"]
    assert got["channels"] == ["fts"]


def test_overlap_row_is_placed_by_vector_distance_not_bm25(tmp_path):
    """The load-bearing case. An `fts+vec` row carries BOTH signals, but search()'s
    vec-first union placed it by DISTANCE and merely carries `rank` along as
    provenance (#402). Recording the rank as the ordering signal would misattribute
    why the row was there — and a floor built on it would threshold the wrong scale.
    """
    p = tmp_path / "t.db"
    _log_one(p, {"id": 9, "match_source": "fts+vec", "distance": 0.42, "rank": -9.9})
    got = _provenance(p)
    assert got["native"] == [0.42], "ordering signal must be the vector distance"
    assert got["kinds"] == ["vec_distance"]
    assert got["channels"] == ["fts+vec"]
    assert got["scores"] == [-9.9], "legacy column still reports the comparable BM25"


def test_scoreless_row_records_nulls_not_zeros(tmp_path):
    """Session-start rows are selected against a token budget, not a retrieval
    score. Absence must read as None — a 0.0 would look like a perfect vec hit."""
    p = tmp_path / "t.db"
    log_context_injection(
        p,
        source="session_start",
        session_id="s",
        trigger=None,
        rows=[{"id": 1}],
        injected_text="x" * 8,
    )
    got = _provenance(p)
    assert got["native"] == [None]
    assert got["kinds"] == [None]
    assert got["channels"] == [None]
    assert got["k"] is None, "session_start doesn't retrieve by k"


def test_arrays_stay_aligned_with_hit_ids_across_mixed_channels(tmp_path):
    """Positional alignment is the whole contract — a consumer joins by index."""
    import json
    import sqlite3

    p = tmp_path / "t.db"
    log_context_injection(
        p,
        source="per_turn_recall",
        session_id="s",
        trigger="t",
        rows=[
            {"id": 1, "match_source": "vec", "distance": 0.1},
            {"id": 2, "match_source": "fts", "rank": -2.0},
            {"id": 3},
        ],
        injected_text="x" * 8,
        k_requested=5,
    )
    got = _provenance(p)
    c = sqlite3.connect(p)
    hit_ids = json.loads(c.execute("SELECT hit_ids FROM context_injections").fetchone()[0])
    c.close()
    assert hit_ids == [1, 2, 3]
    for key in ("native", "kinds", "channels"):
        assert len(got[key]) == len(hit_ids), f"{key} misaligned with hit_ids"
    assert got["kinds"] == ["vec_distance", "bm25", None]
    assert got["k"] == 5


def test_k_requested_distinguishes_a_bound_result_from_a_coincidence(tmp_path):
    """hit_count == k is ambiguous without k. Record what was asked for."""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 1, "match_source": "vec", "distance": 0.1}, k_requested=5)
    assert _provenance(p)["k"] == 5


def test_self_migrates_413_columns_onto_a_pre_413_table(tmp_path):
    """A db written before #413 lacks the four new columns; the guarded ALTER adds
    them so the values persist rather than the INSERT erroring (and the write is
    best-effort, so an error would silently DROP the row — a hole in the very
    audit data this table exists to produce)."""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_PRE_413_DDL)
    c.commit()
    c.close()

    _log_one(p, {"id": 4, "match_source": "vec", "distance": 0.25}, k_requested=5)

    got = _provenance(p)
    assert got["native"] == [0.25]
    assert got["kinds"] == ["vec_distance"]
    assert got["k"] == 5


def test_fts_fallback_rows_record_bm25_with_a_null_channel(tmp_path):
    """search()'s early returns (vectors off, or requested-but-unavailable) hand
    back FTS rows WITHOUT a match_source stamp — the shape a vectorless install
    produces. The channel is null but the kind still says bm25, which is what lets
    a consumer tell this apart from "no retrieval score at all"."""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 1, "rank": -5.4884})  # no match_source key at all
    got = _provenance(p)
    assert got["native"] == [-5.4884]
    assert got["kinds"] == ["bm25"]
    assert got["channels"] == [None]


def test_bool_ranks_are_not_logged_as_scores(tmp_path):
    """bool is a subclass of int, so an unguarded isinstance check would log a
    stray True as the score 1.0 — a fabricated relevance value in the table the
    #414 floor will be tuned from. Both score columns must reject it. (#419)"""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 1, "rank": True})
    got = _provenance(p)
    assert got["native"] == [None]
    assert got["kinds"] == [None]
    assert got["scores"] == [None]


def test_bool_distance_is_not_logged_as_a_vec_score(tmp_path):
    """Sibling of the rank guard, on the vec branch. #419 review found this one
    untested — the guard could be deleted with the whole suite still green."""
    p = tmp_path / "t.db"
    _log_one(p, {"id": 1, "match_source": "vec", "distance": True})
    got = _provenance(p)
    assert got["native"] == [None]
    assert got["kinds"] == [None], "a True distance must not become vec_distance 1.0"


# ---- recall_attribution (techne#329 step 1's deferred join) -----------------


def _stamp_last(db, table, ts):
    """Force the most recently inserted row's timestamp (both tables default now)."""
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    conn.execute(
        f"UPDATE {table} SET timestamp = ? WHERE id = (SELECT MAX(id) FROM {table})", (ts,)
    )
    conn.commit()
    conn.close()


def _kinject(db, ts, ids, *, source="per_turn_recall", session="s1"):
    """Log a knowledge-rail injection of `ids` (knowledge row ids) at `ts`."""
    log_context_injection(
        db, source=source, session_id=session, trigger="t",
        rows=[{"id": i} for i in ids], injected_text="x" * 20,
    )
    _stamp_last(db, "context_injections", ts)


def _act(db, ts, content, *, tool="Bash", session="s1"):
    """Record an arbitrary tool observation carrying `content` at `ts`."""
    add_observation(db, session, tool, content)
    _stamp_last(db, "observations", ts)


def test_recall_attribution_empty_db(db):
    r = recall_attribution(db)
    assert r["injections"] == 0
    assert r["cited_in_window_pct"] is None
    assert r["items"] == []
    assert r["per_source"]["per_turn_recall"]["injections"] == 0


def test_recall_attribution_credits_citation_in_window(db):
    _kinject(db, "2026-01-01 12:00:00", [237, 812])
    _act(db, "2026-01-01 12:00:30", "as techne#237 says, do the thing")
    r = recall_attribution(db)
    assert r["injections"] == 1
    assert r["cited_in_window"] == 1
    assert r["cited_in_window_pct"] == 100.0
    assert r["cited_in_session"] == 1


def test_recall_attribution_ignores_uninjected_id(db):
    # Citing some OTHER knowledge id is not evidence this injection was used.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:30", "unrelated: techne#999")
    r = recall_attribution(db)
    assert r["cited_in_window"] == 0
    assert r["cited_in_session"] == 0


def test_recall_attribution_matches_both_spellings_and_brackets(db):
    _kinject(db, "2026-01-01 12:00:00", [956])
    _act(db, "2026-01-01 12:00:10", "CORRECTION to knowledge#956 — see ⟦techne#956⟧")
    assert recall_attribution(db)["cited_in_window"] == 1


def test_recall_attribution_echo_tool_cannot_credit(db):
    # techne_recall's captured Output contains the ids it RETURNED — crediting
    # that would be the store citing itself.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:30", "Output: techne#237",
         tool="mcp__techne__techne_recall")
    r = recall_attribution(db)
    assert r["cited_in_window"] == 0
    assert r["cited_in_session"] == 0


def test_recall_attribution_echo_tool_still_consumes_a_window_slot(db):
    # An echo tool is a real action: it can't earn credit, but it does push a
    # later genuine citation out of a 1-action window.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:10", "Output: techne#237",
         tool="mcp__techne__techne_recall")
    _act(db, "2026-01-01 12:00:20", "now I use techne#237")
    assert recall_attribution(db, next_actions=1)["cited_in_window"] == 0
    assert recall_attribution(db, next_actions=2)["cited_in_window"] == 1


def test_recall_attribution_window_vs_session_ceiling(db):
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:30", "warm-up, no ids here")
    _act(db, "2026-01-01 13:00:00", "much later: techne#237")
    r = recall_attribution(db, window_minutes=10.0)
    assert r["cited_in_window"] == 0
    assert r["cited_in_session"] == 1  # ceiling catches what the window drops


def test_recall_attribution_next_actions_cap(db):
    _kinject(db, "2026-01-01 12:00:00", [237])
    for i in range(3):
        _act(db, f"2026-01-01 12:00:0{i + 1}", "filler")
    _act(db, "2026-01-01 12:00:05", "techne#237")
    assert recall_attribution(db, next_actions=3)["cited_in_window"] == 0
    assert recall_attribution(db, next_actions=5)["cited_in_window"] == 1


def test_recall_attribution_only_counts_actions_after_the_injection(db):
    _act(db, "2026-01-01 11:59:00", "techne#237 mentioned BEFORE the injection")
    _kinject(db, "2026-01-01 12:00:00", [237])
    r = recall_attribution(db)
    assert r["cited_in_window"] == 0
    assert r["cited_in_session"] == 0


def test_recall_attribution_unobserved_session_is_excluded_not_scored(db):
    # A session the ledger never captured can't show a citation either way. It
    # must not land in the attributable denominator as a "miss" (the live corpus
    # is dominated by these for session_start).
    ensure_session(db, "s2")
    _kinject(db, "2026-01-01 12:00:00", [237], source="session_start", session="s2")
    r = recall_attribution(db)
    assert r["injections"] == 1
    assert r["unobserved_sessions"] == 1
    assert r["no_actions_in_window"] == 0
    assert r["attributable"] == 0
    assert r["cited_in_window_pct_attributable"] is None
    assert r["per_source"]["session_start"]["unobserved_session"] == 1


def test_recall_attribution_in_ledger_but_nothing_in_window_is_a_real_miss(db):
    # Distinct from the unobserved case: the session IS captured, actions exist,
    # they just fall outside the window. That IS a miss and stays in the base.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 14:00:00", "way outside the window")
    r = recall_attribution(db)
    assert r["unobserved_sessions"] == 0
    assert r["no_actions_in_window"] == 1
    assert r["attributable"] == 0


def test_recall_attribution_attributable_denominator(db):
    # Two injections: one with a following action (attributable, cited), one into
    # an unobserved session. The *_attributable rate must use only the former.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:10", "techne#237")
    ensure_session(db, "s2")
    _kinject(db, "2026-01-01 12:00:00", [237], session="s2")
    r = recall_attribution(db)
    assert r["injections"] == 2
    assert r["attributable"] == 1
    assert r["cited_in_window_pct"] == 50.0
    assert r["cited_in_window_pct_attributable"] == 100.0


def test_recall_attribution_per_item_table(db):
    _kinject(db, "2026-01-01 12:00:00", [237, 812])
    _act(db, "2026-01-01 12:00:10", "techne#237")
    _kinject(db, "2026-01-01 12:10:00", [812])
    by_id = {r["id"]: r for r in recall_attribution(db)["items"]}
    assert by_id[237]["encounters"] == 1
    assert by_id[237]["cited_in_session"] == 1
    assert by_id[812]["encounters"] == 2          # shown twice…
    assert by_id[812]["cited_in_session"] == 0    # …never named
    assert by_id[812]["sessions"] == 1
    r = recall_attribution(db)
    assert r["items_injected"] == 2
    assert r["items_never_cited"] == 1
    assert r["items_never_cited_pct"] == 50.0


def test_recall_attribution_items_sorted_by_encounters(db):
    for _ in range(3):
        _kinject(db, "2026-01-01 12:00:00", [812])
    _kinject(db, "2026-01-01 12:00:00", [237])
    ids = [r["id"] for r in recall_attribution(db)["items"]]
    assert ids == [812, 237]


def test_recall_attribution_ignores_codex_style_string_ids(db):
    # codex_edit_rail writes symbol-id STRINGS into the same column. If such a
    # source is passed explicitly it must degrade to "no citable ids", not raise.
    log_context_injection(
        db, source="codex_edit_rail", session_id="s1", trigger="sym",
        rows=[{"id": "techne/foo.py::bar::function::10"}], injected_text="x",
    )
    _stamp_last(db, "context_injections", "2026-01-01 12:00:00")
    _act(db, "2026-01-01 12:00:10", "techne#237")
    r = recall_attribution(db, sources=("codex_edit_rail",))
    assert r["injections"] == 1
    assert r["cited_in_window"] == 0
    assert r["items"] == []


def test_recall_attribution_tz_aware_timestamp_no_crash(db):
    # Transcript-imported observations carry tz-AWARE ISO times; mixing them with
    # naive stored times used to raise TypeError (techne#374).
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01T12:00:30Z", "techne#237")
    r = recall_attribution(db)  # no TypeError
    assert r["cited_in_window"] == 1


def test_recall_attribution_since_days_windows_injections(db):
    _kinject(db, "2026-01-01 12:00:00", [237])  # far in the past
    assert recall_attribution(db, since_days=7)["injections"] == 0
    assert recall_attribution(db)["injections"] == 1
