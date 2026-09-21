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
        db,
        source="session_start",
        session_id="s1",
        trigger=None,
        rows=[{"id": 1}, {"id": 2}, {"id": 3}],
        injected_text="x" * 40,
    )
    log_context_injection(
        db,
        source="session_start",
        session_id="s1",
        trigger=None,
        rows=[],
        injected_text="",
    )
    # codex_edit_rail: 1 event, 2 hits, a different session.
    log_context_injection(
        db,
        source="codex_edit_rail",
        session_id="s2",
        trigger="sym",
        rows=[{"id": "a"}, {"id": "b"}],
        injected_text="y" * 20,
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
        db,
        source="codex_edit_rail",
        session_id="s1",
        trigger="sym",
        rows=[{"id": 1}, {"id": 2}],
        injected_text="x" * 20,
        total_hits=14,
    )
    log_context_injection(
        db,
        source="codex_edit_rail",
        session_id="s1",
        trigger="sym2",
        rows=[{"id": 3}],
        injected_text="y" * 10,
        total_hits=6,
    )
    # session_start never sets total_hits → avg_radius stays None.
    log_context_injection(
        db,
        source="session_start",
        session_id="s1",
        trigger=None,
        rows=[{"id": 9}],
        injected_text="z" * 8,
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
        p,
        source="codex_edit_rail",
        session_id="s",
        trigger="x",
        rows=[{"id": 1}],
        injected_text="x" * 8,
        total_hits=9,
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


def test_mcp_read_tool_is_derived_not_hardcoded(db):
    """`mcp_read_tool` separates "0 because agents ignore an existing tool" from
    "0 because there is nothing to call". Getting it wrong inverts the meaning of
    every zero in the report.

    THIS TEST USED TO ASSERT `librarian: False` AND WAS RIGHT TO (techne#329): there
    was no librarian MCP tool. #724 added `librarian_search`, the prefix matcher
    auto-lit-up exactly as its comment promised — and the hardcoded boolean beside it
    did not, so the ledger HAD the number and printed "no MCP read tool" anyway, for a
    tool the tutorial tells operators to call. Prudence ranked that the one finding not
    to let reach the release notes (#727).

    The old assertion is why the suite stayed green through it: a test pinning a
    constant cannot notice the constant going stale. So this asserts the DERIVATION —
    every domain with a name in the vocabulary has a read tool, and a made-up one does
    not — which stays true as the surface grows.
    """
    from techne.core.domains import DOMAIN_NAMES
    from techne.core.utilization import _has_mcp_read_tool

    led = read_write_ledger(db)
    assert led["librarian"]["mcp_read_tool"] is True, (
        "librarian_search exists and the prefix matcher counts it, but the ledger "
        "still reports no MCP read tool"
    )
    assert led["architect"]["mcp_read_tool"] is True

    for domain in DOMAIN_NAMES:
        assert _has_mcp_read_tool(domain) is True, domain
    assert _has_mcp_read_tool("not-a-domain") is False, (
        "the derivation answers True for anything — it is not deriving"
    )


def test_has_mcp_read_tool_actually_consults_the_registry(monkeypatch):
    """PRUDENCE'S MUTATION, MADE PERMANENT — and the previous version of this file
    failed it.

    `_has_mcp_read_tool` was `domain in DOMAIN_NAMES`, called only with literals that
    are in `DOMAIN_NAMES` forever: a constant wearing a derivation's clothes. She proved
    it by REMOVING `librarian_search` from the registry — the tool was gone, the
    function still said True, and the whole suite stayed green. The assertion above is
    `x in S for x in S` against that body, which is why it could not notice.

    So this asserts the only thing that makes it a derivation: take the tool away, and
    the answer changes.
    """
    from techne import mcp_server
    from techne.core.utilization import _has_mcp_read_tool

    assert _has_mcp_read_tool("librarian") is True

    without = {k: v for k, v in mcp_server.TOOL_HANDLERS.items() if k != "librarian_search"}
    monkeypatch.setattr(mcp_server, "TOOL_HANDLERS", without)

    assert _has_mcp_read_tool("librarian") is False, (
        "the tool was removed from the registry and the ledger still claims it exists "
        "— the function is not consulting anything"
    )
    assert _has_mcp_read_tool("architect") is True, "unrelated domains must be unaffected"


def test_the_ledger_counts_archivist_reads(db):
    """PRUDENCE, #725 r4. `archivist_search` was counted by no tuple — three of the four
    domain-named tools landed in the ledger and the archivist's vanished.

    The bite does not need an archivist row: when a read moves from `techne_recall` to
    `archivist_search` — which the docs tell operators to do — `memory.read` falls and
    the panel reports a read-path DECLINE that is actually a read-path MIGRATION.
    """
    _obs(db, "mcp__techne__archivist_search", 5)
    _obs(db, "mcp__techne__techne_archivist_query", 2)
    led = read_write_ledger(db)
    assert led["archivist"]["read"] == 7, "archivist reads are invisible to the ledger"
    assert led["archivist"]["mcp_read_tool"] is True


def test_the_ledger_counts_the_domain_named_architect_tool(db):
    """`_ARCHITECT_READ` is an EXACT tuple, so a new tool must be added to it or
    successful adoption reads as `never queried` — the ledger reporting neglect at
    precisely the moment the tool is being used. (Prudence, #727.)"""
    _obs(db, "mcp__techne__architect_search", 4)
    led = read_write_ledger(db)
    assert led["architect"]["read"] == 4, (
        "architect_search was not counted — the exact-match tuple is stale"
    )


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
        db,
        source="codex_edit_rail",
        session_id=session,
        trigger=trigger,
        rows=[{"id": d} for d in deps],
        injected_text="x" * 20,
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
        {"file_path": path, "edits": [edit]}
        if file_path_first
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
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")  # 30s later, same file
    a = edit_rail_attribution(db)
    assert a["injections"] == 1
    assert a["file_attributed"] == 1
    assert a["file_attributed_pct"] == 100.0


def test_edit_rail_attribution_excludes_trigger_file(db):
    # The only dependent shares the trigger's OWN file — editing it is not
    # "used a dependent" (you were editing that symbol anyway).
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/foo.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 0


def test_edit_rail_attribution_window(db):
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _edit(db, "2026-01-01 12:10:00", "/repo/techne/foo.py")  # 10 min later
    tight = edit_rail_attribution(db, window_minutes=2.0)
    assert tight["no_edits_in_window"] == 1
    assert tight["file_attributed"] == 0
    wide = edit_rail_attribution(db, window_minutes=15.0)
    assert wide["file_attributed"] == 1


def test_edit_rail_attribution_next_edits_cap(db):
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    # Two unrelated edits first, then the dependent — beyond next_edits=2, so uncredited.
    _edit(db, "2026-01-01 12:00:10", "/repo/techne/a.py")
    _edit(db, "2026-01-01 12:00:20", "/repo/techne/b.py")
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    assert edit_rail_attribution(db, next_edits=2)["file_attributed"] == 0
    assert edit_rail_attribution(db, next_edits=5)["file_attributed"] == 1


def test_edit_rail_attribution_symbol_name(db):
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::compute_total::function::10"],
    )
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", change="compute_total(x)")
    a = edit_rail_attribution(db)
    assert a["symbol_attributed"] == 1  # the edit text names the dependent


def test_edit_rail_attribution_payload_composition(db):
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=[
            "techne/foo.py::bar::function::10",  # real
            "tests/test_foo.py::baz::function::3",  # test file
            "techne/foo.py::<module>::module::0",  # synthetic module node
        ],
    )
    pay = edit_rail_attribution(db)["payload"]
    assert pay["dependents"] == 3
    assert pay["real_pct"] == round(100 / 3, 1)
    assert pay["module_pct"] == round(100 / 3, 1)
    assert pay["test_pct"] == round(100 / 3, 1)


def test_edit_rail_attribution_noise_only(db):
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=[
            "techne/foo.py::<module>::module::0",  # module noise
            "tests/test_x.py::a::function::1",  # test noise
        ],
    )
    a = edit_rail_attribution(db)
    assert a["noise_only_injections"] == 1
    assert a["noise_only_pct"] == 100.0
    assert a["payload"]["real_pct"] == 0.0


# ---- techne#374 review regressions -----------------------------------------


def test_edit_rail_attribution_tz_aware_timestamp_no_crash(db):
    """Transcript-imported edits carry ISO '…Z' (tz-AWARE) timestamps while the
    injection's is naive — subtracting them used to raise TypeError and take down
    `techne audit` (review issue #1). Must not raise, and must still attribute."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _edit(db, "2026-01-01T12:00:30.000Z", "/repo/techne/foo.py")  # tz-aware shape
    a = edit_rail_attribution(db)  # no TypeError
    assert a["file_attributed"] == 1


def test_edit_rail_attribution_write_follow_up(db):
    """Write ('Wrote: <path>') follow-ups must be counted, not silently dropped
    (review issue #2)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _write(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")
    assert edit_rail_attribution(db)["file_attributed"] == 1


def test_edit_rail_attribution_multiedit_follow_up(db):
    """MultiEdit fires the rail, so a MultiEdit fix-up to a dependent must attribute
    at both file and symbol granularity (review issue #2)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::compute_total::function::10"],
    )
    _multiedit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", body="compute_total(x)")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 1
    assert a["symbol_attributed"] == 1  # dependent name in the change body


def test_edit_rail_attribution_symbol_not_credited_from_path(db):
    """A dependent whose name also appears in its own filename must NOT earn symbol
    credit when the change body doesn't touch it (review nit — match body, not path)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::foo::function::10"],
    )  # symbol 'foo' also in foo.py
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py", change="unrelated_change()")
    a = edit_rail_attribution(db)
    assert a["file_attributed"] == 1  # file still credited
    assert a["symbol_attributed"] == 0  # symbol not — name only in the path


def test_edit_rail_attribution_attributable_denominator(db):
    """Rate over 'injections that had a follow-up edit' is reported alongside the
    over-all rate (review suggestion)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/t1.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _edit(db, "2026-01-01 12:00:30", "/repo/techne/foo.py")  # hits the dep
    _inject(
        db,
        "2026-01-01 13:00:00",
        trigger="techne/t2.py::t::function::5",
        deps=["techne/baz.py::qux::function::7"],
    )  # no follow-up edit
    a = edit_rail_attribution(db)
    assert a["injections"] == 2 and a["no_edits_in_window"] == 1
    assert a["attributable"] == 1
    assert a["file_attributed"] == 1
    assert a["file_attributed_pct"] == 50.0  # over all injections
    assert a["file_attributed_pct_attributable"] == 100.0  # over those w/ a follow-up


def test_edit_rail_attribution_multiedit_truncation_boundary(db):
    """MultiEdit's Input JSON is truncated to 500 chars by summarize(). With
    file_path FIRST (schema order), it survives even a large edits payload →
    still attributed (re-review boundary case)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    # body >500 chars so the serialised Input is truncated; file_path is first.
    _multiedit(
        db, "2026-01-01 12:00:30", "/repo/techne/foo.py", body="z" * 600, file_path_first=True
    )
    assert edit_rail_attribution(db)["file_attributed"] == 1


def test_edit_rail_attribution_multiedit_truncation_degrades_gracefully(db):
    """The pathological case — file_path serialised AFTER a large edits array is
    truncated away. Documents that this degrades to a silent DROP (undercount),
    never a crash (re-review boundary case)."""
    _inject(
        db,
        "2026-01-01 12:00:00",
        trigger="techne/trig.py::t::function::5",
        deps=["techne/foo.py::bar::function::10"],
    )
    _multiedit(
        db, "2026-01-01 12:00:30", "/repo/techne/foo.py", body="z" * 600, file_path_first=False
    )
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
        db,
        source=source,
        session_id=session,
        trigger="t",
        rows=[{"id": i} for i in ids],
        injected_text="x" * 20,
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
    _act(db, "2026-01-01 12:00:30", "Output: techne#237", tool="mcp__techne__techne_recall")
    r = recall_attribution(db)
    assert r["cited_in_window"] == 0
    assert r["cited_in_session"] == 0


def test_recall_attribution_echo_tool_still_consumes_a_window_slot(db):
    # An echo tool is a real action: it can't earn credit, but it does push a
    # later genuine citation out of a 1-action window.
    _kinject(db, "2026-01-01 12:00:00", [237])
    _act(db, "2026-01-01 12:00:10", "Output: techne#237", tool="mcp__techne__techne_recall")
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
    assert by_id[812]["encounters"] == 2  # shown twice…
    assert by_id[812]["cited_in_session"] == 0  # …never named
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
        db,
        source="codex_edit_rail",
        session_id="s1",
        trigger="sym",
        rows=[{"id": "techne/foo.py::bar::function::10"}],
        injected_text="x",
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


def test_techne_audit_renders_without_crashing(db, monkeypatch, tmp_path):
    """A SMOKE TEST FOR THE COMMAND ITSELF, because nothing exercised its output.

    While fixing the ledger I bound the archivist row from a variable that does not
    exist in that scope — `techne audit` died with NameError partway through the
    panel — and the FULL SUITE STAYED GREEN at 3367. Every ledger test asserts the
    dict `read_write_ledger` returns; none of them renders it. The panel is the only
    thing an operator actually sees.

    Deliberately shallow: asserts it runs and names every domain, not the numbers.
    """
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    result = CliRunner().invoke(main, ["audit"])

    assert result.exit_code == 0, f"techne audit crashed: {result.output[-600:]}"
    for domain in ("memory", "codex", "architect", "librarian", "archivist"):
        assert domain in result.output, f"{domain} is missing from the ledger panel"


def test_the_flag_and_the_count_cannot_disagree(db, monkeypatch):
    """PRUDENCE, #725 r5 — HER REPRODUCTION, MADE PERMANENT.

    The predicate asked "is a tool named `<domain>_*` registered?" — a naming
    convention — while the counters name tools that do not match their own domain
    prefix: `techne_archivist_query`, `pattern_search`, `pattern_related`. Two facts
    that agreed only by luck of which tools existed.

    Her demonstration: seed reads through `techne_archivist_query`, drop
    `archivist_search` from the registry, and the panel printed "no MCP read tool —
    not visible to this ledger" while the dict held `read: 7`. The count discarded and
    the operator told the opposite of the truth.
    """
    from techne import mcp_server
    from techne.core.utilization import _has_mcp_read_tool

    _obs(db, "mcp__techne__techne_archivist_query", 7)

    without = {k: v for k, v in mcp_server.TOOL_HANDLERS.items() if k != "archivist_search"}
    monkeypatch.setattr(mcp_server, "TOOL_HANDLERS", without)

    led = read_write_ledger(db)
    assert led["archivist"]["read"] == 7, "the counter stopped seeing the SQL surface"
    assert _has_mcp_read_tool("archivist") is True, (
        "the flag says no tool exists while the counter is counting 7 reads through "
        "one — the panel would print 'not visible to this ledger' over a real number"
    )
    assert led["archivist"]["mcp_read_tool"] is True


def test_a_domain_with_no_registered_read_tool_reports_false(db, monkeypatch):
    """The other direction, so the flag is not simply always True: strip EVERY tool the
    archivist's counter names and the answer must change."""
    from techne import mcp_server
    from techne.core.utilization import _ARCHIVIST_READ, _has_mcp_read_tool

    bare = {n.removeprefix("mcp__techne__") for n in _ARCHIVIST_READ}
    without = {k: v for k, v in mcp_server.TOOL_HANDLERS.items() if k not in bare}
    monkeypatch.setattr(mcp_server, "TOOL_HANDLERS", without)

    assert _has_mcp_read_tool("archivist") is False
    assert _has_mcp_read_tool("librarian") is True, "unrelated domains must be unaffected"


def test_log_context_injection_self_migrates_truncation_columns(tmp_path):
    """A db written before techne#853 gains the two columns on next write.

    `context_injections` is created lazily by the hook, not by the core
    schema-reconcile path, so an existing db has whatever columns existed when it
    was first written. This is the fourth lazy migration of this table (#343,
    #413, now #853) and the failure mode is silent: without the ADD COLUMN the
    INSERT raises, the log swallows it by contract, and the rail goes quiet with
    no error anywhere.
    """
    import sqlite3

    from techne.hooks._injection_log import log_context_injection

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # the pre-#853 shape, exactly
    conn.executescript(
        """
        CREATE TABLE context_injections (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            session_id      TEXT,
            timestamp       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            trigger_snippet TEXT,
            hit_count       INTEGER NOT NULL,
            hit_ids         TEXT,
            scores          TEXT,
            approx_tokens   INTEGER NOT NULL,
            total_hits      INTEGER,
            native_scores   TEXT,
            score_kinds     TEXT,
            match_sources   TEXT,
            k_requested     INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

    log_context_injection(
        db,
        source="per_turn_recall",
        session_id="s",
        trigger="t",
        rows=[{"id": 1}],
        injected_text="x" * 400,
        rows_truncated=1,
        chars_dropped=97,
    )

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(context_injections)")}
    assert {"rows_truncated", "chars_dropped"} <= cols, "lazy migration did not fire"
    assert conn.execute(
        "SELECT rows_truncated, chars_dropped FROM context_injections"
    ).fetchone() == (1, 97), "migrated columns did not receive the values"


def test_chars_dropped_counts_content_lost_not_the_marker(tmp_path):
    """`chars_dropped` must exclude the cut marker the renderer ADDS.

    `_truncate_for_injection` appends `_CUT_SUFFIX` to a row it cut, so the
    delivered string is longer than the cap. Measuring `len(original) -
    len(delivered)` without accounting for it under-reports every cut row by the
    marker's width — a small, systematic bias that would make the cap look
    gentler than it is on exactly the rows it bit hardest.
    """
    from techne.hooks.user_prompt_submit import _build_context

    stats: dict = {}
    _build_context([{"id": 1, "knowledge_type": "fact", "content": "y" * 700}], 450, stats)
    assert stats == {"rows_truncated": 1, "chars_dropped": 250}, (
        "a 700-char row at cap 450 loses exactly 250 characters of content; "
        f"got {stats}"
    )


def test_stats_are_populated_even_when_nothing_is_cut(tmp_path):
    """"Fired and cut nothing" and "never fired" must be distinguishable.

    If the out-param were only written on the truncating path, a caller reading
    `.get("rows_truncated")` would see None for both a clean injection and a rail
    that never ran, and the resulting NULLs would be indistinguishable in the
    audit table.
    """
    from techne.hooks.user_prompt_submit import _build_context

    stats: dict = {}
    out = _build_context([{"id": 1, "knowledge_type": "fact", "content": "short"}], 450, stats)
    assert out, "fixture should render"
    assert stats == {"rows_truncated": 0, "chars_dropped": 0}

    # THE EMPTY EXIT, which is the one the source comment names and this test
    # did not reach (#854 r1 Important 1). Asserting only the rendering path
    # proved the weaker claim "populated on a row that was not cut"; moving the
    # stats block below the `if not lines: return ""` guard left the whole suite
    # green. At a non-positive cap every row is severed to nothing and dropped
    # from the block, so `lines` is empty and the function returns "" — and the
    # counters must still report what was cut, or the telemetry says the
    # opposite of what happened (#854 r1 Important 2).
    empty_stats: dict = {}
    empty_out = _build_context(
        [{"id": 1, "knowledge_type": "fact", "content": "x" * 700}], 0, empty_stats
    )
    assert empty_out == "", "a non-positive cap must render no block"
    assert empty_stats == {"rows_truncated": 1, "chars_dropped": 700}, (
        "the empty exit must still report what it severed"
    )


# ---- recall_attribution: current citation forms + per-origin split (techne#995) --


def test_recall_attribution_matches_sigil_and_spaced_forms(db):
    # 🧠N is the interactive convention since 2026-08-31 and "knowledge #N" the
    # machine-readable one; before #995 neither matched, so both went uncounted.
    _kinject(db, "2026-01-01 12:00:00", [2862])
    _act(db, "2026-01-01 12:00:10", "per 🧠2862 (sigils), ship it")
    _kinject(db, "2026-01-01 13:00:00", [877])
    _act(db, "2026-01-01 13:00:10", "Relates to knowledge #877.")
    assert recall_attribution(db)["cited_in_window"] == 2


def test_recall_attribution_sigil_does_not_match_a_longer_id(db):
    _kinject(db, "2026-01-01 12:00:00", [286])
    _act(db, "2026-01-01 12:00:10", "see 🧠2862")
    assert recall_attribution(db)["cited_in_window"] == 0


def test_recall_attribution_splits_rows_by_origin(db):
    from techne.core.knowledge import store

    hand = store(db, "hand-stored fact", source="agent")
    refl = store(db, "reflect fact", source="reflect-promote:sess:1")
    cons = store(db, "merged fact", source="consolidate-merge:7")
    _kinject(db, "2026-01-01 12:00:00", [hand, refl, cons])
    _act(db, "2026-01-01 12:00:10", f"following 🧠{refl}")

    po = recall_attribution(db)["per_origin"]
    assert po["reflect"]["pairs"] == 1 and po["reflect"]["cited_in_window"] == 1
    assert po["reflect"]["cited_in_window_pct"] == 100.0
    assert po["hand"]["cited_in_window"] == 0 and po["hand"]["items_never_cited"] == 1
    assert po["consolidate"]["pairs"] == 1 and po["consolidate"]["cited_in_window_pct"] == 0.0


def test_recall_attribution_origin_rate_excludes_ledger_blind_pairs(db):
    from techne.core.knowledge import store

    refl = store(db, "reflect fact", source="reflect-promote:sess:1")
    _kinject(db, "2026-01-01 12:00:00", [refl])
    _act(db, "2026-01-01 12:00:10", f"techne#{refl}")
    ensure_session(db, "blind")
    _kinject(db, "2026-01-01 12:00:00", [refl], session="blind")  # no observations

    r = recall_attribution(db)["per_origin"]["reflect"]
    assert r["pairs"] == 2 and r["unobserved"] == 1
    assert r["cited_in_window_pct"] == 100.0  # 1 of 1 observable, not 1 of 2


def test_recall_attribution_empty_db_has_per_origin(db):
    po = recall_attribution(db)["per_origin"]
    assert set(po) == {"reflect", "consolidate", "hand", "unknown"}
    assert po["reflect"]["cited_in_window_pct"] is None


def test_recall_attribution_does_not_credit_spaced_techne_form(db):
    # "techne #412" is how a GitHub issue gets written in prose, not a citation form.
    _kinject(db, "2026-01-01 12:00:00", [412])
    _act(db, "2026-01-01 12:00:10", "tracked in techne #412")
    assert recall_attribution(db)["cited_in_window"] == 0


def test_recall_attribution_buckets_a_missing_row_as_unknown_not_hand(db):
    _kinject(db, "2026-01-01 12:00:00", [987654])  # no knowledge row with this id
    _act(db, "2026-01-01 12:00:10", "unrelated")
    po = recall_attribution(db)["per_origin"]
    assert po["unknown"]["pairs"] == 1 and po["hand"]["pairs"] == 0
