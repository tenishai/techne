"""techne#1142 — an explicit search quoting a RARE phrase seats one row holding it.

Vec-first (#401) left rows holding a quoted phrase off the page when their vector
neighbours crowded it: on the 2026-09-28 snapshot `litestream replicate techne.db
exec` put neither exact row in the top 200. With ``phrase_seat=True`` one row
holding the longest rare contiguous span of the query takes the seat right after
vec #1. The explicit surfaces ask for it; the per-turn and inbound rails do not.

Detection and rarity run against a real FTS index. The seat tests pin both
channels and the phrase lookup, as test_search_identifier_head.py does: the claim
is about ORDER, and a fixture that fixes the order states it directly.
"""

from __future__ import annotations

import io
import json
import math

import pytest

from techne.core import search as search_mod
from techne.core.database import connect, init_db
from techne.core.search import (
    PHRASE_MIN_DF,
    PHRASE_RARE_FRACTION_DEFAULT,
    PHRASE_SEAT_MAX_TERMS,
    _is_question,
    _phrase_seat_row,
    search,
)
from techne.core.state import ensure_session


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p).close()
    return p


def _obs(db, texts):
    ensure_session(db, "s-1142")
    conn = connect(db)
    try:
        ids = []
        for t in texts:
            cur = conn.execute(
                "INSERT INTO observations (session_id, content, tool_name) VALUES (?, ?, ?)",
                ("s-1142", t, "Bash"),
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def _seat(db, query, table="all", fraction=PHRASE_RARE_FRACTION_DEFAULT):
    conn = connect(db)
    try:
        row = _phrase_seat_row(conn, query, table, fraction)
    finally:
        conn.close()
    return None if row is None else (row["source_table"], row["id"])


def _filler(db, n):
    _obs(db, [f"filler row number {i} about nothing in particular" for i in range(n)])


# --- detection and rarity ----------------------------------------------------------


def test_a_rare_bigram_finds_its_row(db):
    _filler(db, 50)
    (rid,) = _obs(db, ["exec litestream replicate -config /etc/litestream.yml"])

    assert _seat(db, "litestream replicate techne.db exec") == ("observation", rid)


def test_the_phrase_is_matched_after_stemming(db):
    """FTS5's porter tokenizer applies: `replicates` holds the span `replicate`."""
    _filler(db, 50)
    (rid,) = _obs(db, ["litestream replicates the store to s3"])

    assert _seat(db, "litestream replicate techne.db") == ("observation", rid)


def test_the_words_must_be_contiguous(db):
    _filler(db, 50)
    _obs(db, ["litestream can replicate the store"])

    assert _seat(db, "litestream replicate techne.db") is None


def test_a_common_phrase_gets_no_seat_and_the_bar_is_a_fraction_with_a_floor(db):
    """Rare is fewer than max(PHRASE_MIN_DF, ceil(rows * fraction)) rows."""
    _filler(db, 90)
    _obs(db, [f"the retry budget was raised, pass {i}" for i in range(PHRASE_MIN_DF)])
    rows = 90 + PHRASE_MIN_DF

    at_floor = PHRASE_MIN_DF / rows / 2  # ceil(rows * f) below the floor
    assert _seat(db, "retry budget raised", fraction=at_floor) is None, "the floor holds"
    wider = (PHRASE_MIN_DF + 1) / rows
    assert math.ceil(rows * wider) == PHRASE_MIN_DF + 1
    assert _seat(db, "retry budget raised", fraction=wider) is not None


def test_rarity_is_counted_in_the_searched_scope(db):
    """Common across all tables, rare in `knowledge`: the scope decides."""
    from techne.core.knowledge import store

    _filler(db, 20)
    _obs(db, [f"retry budget log line {i}" for i in range(40)])
    kid = store(db, "We set the retry budget to 300ms.", type="fact", source="test")

    assert _seat(db, "retry budget gateway", table="all", fraction=0.01) is None
    assert _seat(db, "retry budget gateway", table="knowledge", fraction=0.01) == (
        "knowledge",
        kid,
    )


def test_a_bigram_of_stop_words_is_not_a_phrase(db):
    _filler(db, 50)
    _obs(db, ["techne does not provide the extra vec"])

    # `does not` is two stop words; `the extra` has one. Neither can seat.
    assert _seat(db, "does not help") is None
    assert _seat(db, "into the extra") is None


def test_the_longest_rare_span_wins_over_a_better_bigram_match(db):
    """`provide the extra vec` beats rows holding only `extra vec`, however often."""
    _filler(db, 50)
    short = _obs(db, ["extra vec extra vec extra vec"])
    (longest,) = _obs(db, ["note: techne does not provide the extra vec dependency on 3.14"])

    assert short
    assert _seat(db, "techne does not provide the extra vec") == ("observation", longest)


@pytest.mark.parametrize(
    "query",
    [
        "what does litestream replicate do",
        "does litestream replicate techne.db",
        "litestream replicate techne.db?",
        "How litestream replicate works",
    ],
)
def test_a_question_gets_no_seat(db, query):
    """A question is asked in the searcher's own words, not a quoted phrase."""
    _filler(db, 50)
    _obs(db, ["exec litestream replicate -config /etc/litestream.yml"])

    assert _is_question(query)
    assert _seat(db, query) is None


def test_a_long_query_gets_no_seat(db):
    _filler(db, 50)
    _obs(db, ["exec litestream replicate -config /etc/litestream.yml"])
    words = "litestream replicate techne.db exec on the backup host tonight".split()
    assert len(words) == PHRASE_SEAT_MAX_TERMS + 1

    assert _seat(db, " ".join(words)) is None
    assert _seat(db, " ".join(words[:PHRASE_SEAT_MAX_TERMS])) is not None


def test_a_superseded_row_is_never_seated(db):
    from techne.core.knowledge import store, supersede

    _filler(db, 50)
    old = store(db, "litestream replicate runs under launchd.", type="fact", source="test")
    new = supersede(db, old, "litestream replicate runs under systemd.", source="test")

    assert _seat(db, "litestream replicate schedule", table="knowledge") == ("knowledge", new)


def test_an_fts_syntax_hazard_does_not_raise(db):
    _filler(db, 10)
    assert _seat(db, 'a "" -- ** NOT') is None


# --- the seat ----------------------------------------------------------------------


def _row(table: str, rid: int, distance: float = 0.5) -> dict:
    return {"source_table": table, "id": rid, "content": f"{table}-{rid}", "distance": distance}


def _scoped(rows):
    def _fn(_db, _query, table="all", limit=20, **_kw):
        return [r for r in rows if table == "all" or table == r["source_table"]][:limit]

    return _fn


def _full_vec():
    return [_row("observations", i, 0.10 + i / 100) for i in range(12)]


def _patch(monkeypatch, vec, fts, phrase=None, calls=None):
    monkeypatch.setattr(search_mod, "search_vec", _scoped(list(vec)))
    monkeypatch.setattr(search_mod, "search_fts", _scoped(list(fts)))
    by_key = {(r["source_table"], r["id"]): r for r in list(vec) + list(fts)}

    def _phrase(_conn, query, _table, _fraction, **_kw):
        if calls is not None:
            calls.append(query)
        return None if phrase is None else dict(by_key.get(phrase, _row(*phrase)))

    monkeypatch.setattr(search_mod, "_phrase_seat_row", _phrase)


def _keys(page):
    return [(r["source_table"], r["id"]) for r in page]


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


Q = "litestream replicate techne.db exec"


def test_the_seat_goes_right_after_vec_one(db, monkeypatch):
    """The reported shape: the phrase row is lexical only and the vector channel
    fills the head. It lands at index 1, vec #1 keeps index 0, the page keeps its
    length and the #827 tail floor keeps the last slot."""
    fts = [_row("observations", 500), _row("observations", 501)]
    _patch(monkeypatch, _full_vec(), fts, phrase=("observations", 500))

    page = search(db, Q, table="all", limit=10, phrase_seat=True)

    assert len(page) == 10
    assert _keys(page)[0] == ("observations", 0), "the top result is never displaced"
    assert _keys(page)[1] == ("observations", 500)
    assert page[1]["reserved_slot"] == "phrase_head"
    assert page[1]["match_source"] == "fts"
    assert _keys(page)[2:9] == [("observations", i) for i in range(1, 8)]
    assert page[-1]["reserved_slot"] == "lexical_tail"
    assert _keys(page)[-1] == ("observations", 501)


def test_the_seat_is_off_by_default(db, monkeypatch):
    calls: list = []
    fts = [_row("observations", 500)]
    _patch(monkeypatch, _full_vec(), fts, phrase=("observations", 500), calls=calls)

    page = search(db, Q, table="all", limit=10)
    off = search(db, Q, table="all", limit=10, phrase_seat=False)

    assert calls == [], "the default path never probes for a phrase"
    assert _same(page, off)
    assert all(r.get("reserved_slot") != "phrase_head" for r in page)


def test_no_rare_phrase_leaves_the_page_byte_identical(db, monkeypatch):
    fts = [_row("observations", 500)]
    _patch(monkeypatch, _full_vec(), fts, phrase=None)

    assert _same(
        search(db, Q, table="all", limit=10, phrase_seat=True),
        search(db, Q, table="all", limit=10),
    )


def test_a_phrase_row_already_first_changes_nothing(db, monkeypatch):
    _patch(monkeypatch, _full_vec(), [_row("observations", 500)], phrase=("observations", 0))

    assert _same(
        search(db, Q, table="all", limit=10, phrase_seat=True),
        search(db, Q, table="all", limit=10),
    )


def test_a_phrase_row_already_second_is_not_marked(db, monkeypatch):
    _patch(monkeypatch, _full_vec(), [_row("observations", 500)], phrase=("observations", 1))

    page = search(db, Q, table="all", limit=10, phrase_seat=True)

    assert _same(page, search(db, Q, table="all", limit=10))
    assert "reserved_slot" not in page[1]


def test_a_row_moved_up_inside_the_head_is_marked_and_bumps_nothing(db, monkeypatch):
    """Vec rank 6 -> index 1. The head keeps the same rows, so the page holds the
    same set and only the order says the seat acted."""
    _patch(monkeypatch, _full_vec(), [_row("observations", 500)], phrase=("observations", 5))

    page = search(db, Q, table="all", limit=10, phrase_seat=True)
    off = search(db, Q, table="all", limit=10)

    assert _keys(page)[:2] == [("observations", 0), ("observations", 5)]
    assert page[1]["reserved_slot"] == "phrase_head"
    assert page[1]["match_source"] == "vec"
    assert sorted(_keys(page)) == sorted(_keys(off))
    assert _keys(page).count(("observations", 5)) == 1


def test_the_identifier_head_stays_first(db, monkeypatch):
    fts = [_row("observations", 500), _row("observations", 600)]
    _patch(monkeypatch, _full_vec(), fts, phrase=("observations", 600))
    monkeypatch.setattr(
        search_mod,
        "_identifier_pool",
        lambda *_a, **_k: [_row("observations", 500)],
    )

    page = search(db, Q, table="all", limit=10, phrase_seat=True)

    assert _keys(page)[:3] == [("observations", 500), ("observations", 0), ("observations", 600)]
    assert [r.get("reserved_slot") for r in page[:3]] == ["identifier_head", None, "phrase_head"]
    assert len(page) == 10


def test_the_curated_reservation_cannot_evict_the_seat(db, monkeypatch):
    """#946 displaces non-curated rows from the BACK of the head; the phrase row
    joins the head only after that, so it survives a full curated promotion."""
    vec = _full_vec() + [_row("knowledge", 90 + j, 0.90 + j / 100) for j in range(4)]
    _patch(monkeypatch, vec, [_row("observations", 500)], phrase=("observations", 500))

    page = search(db, Q, table="all", limit=10, phrase_seat=True)

    assert _keys(page)[1] == ("observations", 500)
    assert sum(r.get("reserved_slot") == "curated_head" for r in page) >= 1
    assert len(set(_keys(page))) == len(page) == 10


def test_a_thin_head_keeps_its_vector_row(db, monkeypatch):
    """The seat needs a vec slot to spare; at limit 2 there is none."""
    _patch(monkeypatch, _full_vec(), [_row("observations", 500)], phrase=("observations", 500))

    page = search(db, Q, table="all", limit=2, phrase_seat=True)

    assert all(r.get("reserved_slot") != "phrase_head" for r in page)


# --- which surfaces ask for it -----------------------------------------------------


@pytest.fixture()
def spy(tmp_path, monkeypatch):
    """Replace search() everywhere it is imported from; record each call's kwargs."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    calls: list[dict] = []

    def _search(_db, query, **kw):
        calls.append(dict(kw, query=query))
        return []

    monkeypatch.setattr("techne.core.search.search", _search)
    monkeypatch.setattr("techne.core.search._VEC_CHANNEL_ERROR", None)
    return calls


def _home_config(tmp_path, hooks=None):
    import yaml

    from techne.config import Config

    if hooks:
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({"hooks": hooks}))
    config = Config.load()
    init_db(config.db_path).close()
    ensure_session(config.db_path, "sess-1142")
    return config


def _mcp(config, name, args):
    from techne.mcp_server import handle_request

    resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        config,
    )
    assert "error" not in resp, resp.get("error")


def test_techne_search_turns_the_seat_on(tmp_path, spy):
    _mcp(_home_config(tmp_path), "techne_search", {"query": Q})
    assert [c.get("phrase_seat") for c in spy] == [True]


def test_techne_recall_turns_the_seat_on(tmp_path, spy):
    _mcp(_home_config(tmp_path), "techne_recall", {"topic": Q})
    assert spy and all(c.get("phrase_seat") is True for c in spy)


def test_cli_search_turns_the_seat_on(tmp_path, spy):
    from click.testing import CliRunner

    from techne.cli import main

    _home_config(tmp_path)
    result = CliRunner().invoke(main, ["search", Q], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert [c.get("phrase_seat") for c in spy] == [True]


def test_the_per_turn_rail_leaves_the_seat_off(tmp_path, spy, monkeypatch, capsys):
    from techne.hooks import user_prompt_submit

    _home_config(tmp_path, {"per_turn_recall": True})
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"prompt": Q, "session_id": "sess-1142"}))
    )
    with pytest.raises(SystemExit):
        user_prompt_submit.main()

    assert spy, "the rail searched"
    assert all(not c.get("phrase_seat", False) for c in spy)


def test_the_inbound_rail_leaves_the_seat_off(tmp_path, spy, monkeypatch, capsys):
    from techne.hooks import post_tool_use, user_prompt_submit

    _home_config(
        tmp_path,
        {
            "inbound_recall": True,
            "inbound_recall_floor_vec_distance": 1.0,
            "per_turn_recall": False,
        },
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"prompt": "next", "session_id": "sess-1142"}))
    )
    with pytest.raises(SystemExit):
        user_prompt_submit.main()
    payload = {
        "session_id": "sess-1142",
        "tool_name": "Agent",
        "tool_input": {},
        "tool_response": {
            "status": "completed",
            "prompt": "brief",
            "content": [{"type": "text", "text": f"Checked {Q} on the backup host."}],
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        post_tool_use.main()
    except SystemExit:
        pass

    assert spy, "the inbound rail searched"
    assert all(not c.get("phrase_seat", False) for c in spy)


# --- small limits: the seat never changes the top result (Prudence, #1143 r1) -----


def _small_vec():
    """12 observation vec rows, then 3 knowledge vec rows: #946 has curated rows to
    promote at every limit."""
    return _full_vec() + [_row("knowledge", 900 + j, 0.90 + j / 100) for j in range(3)]


def test_prudence_repro_limit_3_phrase_row_already_second(db, monkeypatch):
    """Her fixture: the phrase row is vec #2. At limit 3 (floor 1, head 2) the seat
    lowered vec_budget, the #946 promotion displaced from a one-row head, and vec #1
    left the top. Seat on must keep the seat-off page's first row."""
    fts = [_row("observations", 500)]
    _patch(monkeypatch, _small_vec(), fts, phrase=("observations", 1))

    on = search(db, Q, table="all", limit=3, phrase_seat=True)
    off = search(db, Q, table="all", limit=3)

    assert _keys(off)[0] == ("observations", 0)
    assert _keys(on)[0] == ("observations", 0)
    assert len(on) == len(off) == 3
    assert len(set(_keys(on))) == 3


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.parametrize("fts_only", [True, False], ids=["tail_row", "no_fts_only"])
@pytest.mark.parametrize(
    "phrase",
    [("observations", 1), ("observations", 5), ("observations", 777), ("knowledge", 901)],
    ids=["already_second", "inside_head", "lexical_only", "curated"],
)
def test_the_top_result_never_changes_at_small_limits(db, monkeypatch, limit, fts_only, phrase):
    fts = [_row("observations", 500), _row("observations", 777)] if fts_only else []
    _patch(monkeypatch, _small_vec(), fts, phrase=phrase)

    on = search(db, Q, table="all", limit=limit, phrase_seat=True)
    off = search(db, Q, table="all", limit=limit)

    assert _keys(on)[:1] == _keys(off)[:1]
    assert len(on) == len(off)
    assert len(set(_keys(on))) == len(on)
