"""Inbound recall: recall on content that arrives through a tool round (techne#1018).

Per-turn recall rides UserPromptSubmit, so a subagent hand-back, a fetched page or
a peer's coord message that lands mid-turn got no recall at all. The inbound rail
runs the same pipeline (`user_prompt_submit.recall_block`) from PostToolUse.

`search()` is replaced with a fake that returns real knowledge rows stamped as
vector hits. The rail drops any row its vector floor cannot judge, and the test
store has no vectors, so without the fake every test here would pass by
injecting nothing.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.hooks import post_tool_use, user_prompt_submit

SID = "sess-inbound"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _config(home, **hooks):
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": hooks}))
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.state import ensure_session

    config = Config.load()
    init_db(config.db_path)
    ensure_session(config.db_path, SID)
    return config


ARMED = {
    "inbound_recall": True,
    "inbound_recall_floor_vec_distance": 1.0,
    "per_turn_recall": False,
}


def _seed(content):
    from techne.config import Config
    from techne.core import knowledge

    return knowledge.store(Config.load().db_path, content, type="fact", confidence="high")


@pytest.fixture()
def fake_search(monkeypatch):
    """Returns ``hits`` (id, distance, match_source) as rows; records every query."""
    state = {"hits": [], "queries": []}

    def _search(db_path, query, **kw):
        import sqlite3

        state["queries"].append(query)
        conn = sqlite3.connect(str(db_path))
        try:
            out = []
            for rid, dist, src in state["hits"]:
                (content,) = conn.execute(
                    "SELECT content FROM knowledge WHERE id = ?", (rid,)
                ).fetchone()
                row = {
                    "id": rid,
                    "content": content,
                    "knowledge_type": "fact",
                    "source_table": "knowledge",
                    "match_source": src,
                }
                if src == "fts":
                    row["rank"] = -5.0
                else:
                    row["distance"] = dist
                out.append(row)
            return out
        finally:
            conn.close()

    monkeypatch.setattr("techne.core.search.search", _search)
    # A vector-channel error left by an earlier test in this worker would read as
    # this rail's failure; in production each hook is a fresh process.
    monkeypatch.setattr("techne.core.search._VEC_CHANNEL_ERROR", None)
    return state


def _prompt(monkeypatch, capsys, text="next question"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": text, "session_id": SID})))
    with pytest.raises(SystemExit):
        user_prompt_submit.main()
    return capsys.readouterr().out


def _tool(monkeypatch, capsys, tool_name, tool_response, **extra):
    payload = {
        "session_id": SID,
        "tool_name": tool_name,
        "tool_input": {},
        "tool_response": tool_response,
    }
    payload.update(extra)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        post_tool_use.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _handback(text):
    return {
        "status": "completed",
        "prompt": "BRIEF-TO-SUBAGENT do the thing",
        "content": [{"type": "text", "text": text}],
    }


def _log(source="inbound_recall"):
    import sqlite3

    from techne.config import Config

    conn = sqlite3.connect(str(Config.load().db_path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "context_injections" not in tables:
            return []
        return [
            (t, json.loads(h) if h else [])
            for t, h in conn.execute(
                "SELECT trigger_snippet, hit_ids FROM context_injections WHERE source = ? "
                "ORDER BY id",
                (source,),
            )
        ]
    finally:
        conn.close()


def _marker():
    from techne.config import Config
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    conn = connect(Config.load().db_path)
    try:
        return get_state(conn, "hook:inbound_recall:last_run") or {}
    finally:
        conn.close()


# ---- acceptance: a hand-back gets recall mid-turn --------------------------------


def test_a_handback_gets_recall_mid_turn(home, monkeypatch, capsys, fake_search):
    _config(home, **ARMED)
    rid = _seed("Knowledge 935: the licensing decision was Apache-2.0.")
    fake_search["hits"] = [(rid, 0.6, "vec")]
    _prompt(monkeypatch, capsys)

    ctx = _tool(monkeypatch, capsys, "Agent", _handback("Checked knowledge 935 and issue #225."))

    assert ctx is not None and f"⟦techne#{rid}⟧" in ctx
    assert "relevant to this tool result" in ctx
    # The hand-back is searched, never our own brief to the subagent.
    assert fake_search["queries"] == ["Checked knowledge 935 and issue #225."]
    assert _log() == [("Checked knowledge 935 and issue #225.", [rid])]
    assert _marker()["outcome"] == "ok"


def test_at_most_one_delivery_per_prompt(home, monkeypatch, capsys, fake_search):
    _config(home, **ARMED)
    a = _seed("row a")
    b = _seed("row b")
    fake_search["hits"] = [(a, 0.5, "vec"), (b, 0.6, "vec")]
    _config(home, **{**ARMED, "inbound_recall_limit": 1})
    _prompt(monkeypatch, capsys)

    assert f"⟦techne#{a}⟧" in _tool(monkeypatch, capsys, "WebFetch", {"result": "page one"})
    # Same prompt: the slot is used, so no search and no injection.
    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page two"}) is None
    assert fake_search["queries"] == ["page one"]
    assert _marker()["outcome"] == "noop:delivered-this-prompt"

    # A new prompt reopens it; row a was already sent, so b comes next.
    _prompt(monkeypatch, capsys)
    ctx = _tool(monkeypatch, capsys, "WebFetch", {"result": "page three"})
    assert f"⟦techne#{b}⟧" in ctx and f"⟦techne#{a}⟧" not in ctx


def test_no_row_is_delivered_twice_across_the_two_rails(home, monkeypatch, capsys, fake_search):
    _config(home, **{**ARMED, "per_turn_recall": True})
    a = _seed("row a, sent per turn")
    b = _seed("row b")
    fake_search["hits"] = [(a, 0.5, "vec")]
    out = _prompt(monkeypatch, capsys, "question about a")
    assert f"⟦techne#{a}⟧" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

    fake_search["hits"] = [(a, 0.5, "vec"), (b, 0.6, "vec")]
    ctx = _tool(monkeypatch, capsys, "Agent", _handback("report mentioning a and b"))
    assert f"⟦techne#{b}⟧" in ctx and f"⟦techne#{a}⟧" not in ctx


# ---- part 4: no floor, no rail ----------------------------------------------------


def test_off_by_default(home, monkeypatch, capsys, fake_search):
    config = _config(home)
    assert config.hooks.inbound_recall is False
    assert config.hooks.inbound_recall_floor_vec_distance is None
    rid = _seed("row")
    fake_search["hits"] = [(rid, 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    fake_search["queries"].clear()

    assert _tool(monkeypatch, capsys, "Agent", _handback("report")) is None
    assert fake_search["queries"] == []
    assert _marker() == {}


def test_flag_without_a_floor_stays_dormant_and_says_so(home, monkeypatch, capsys, fake_search):
    config = _config(home, inbound_recall=True, per_turn_recall=False)
    rid = _seed("row")
    fake_search["hits"] = [(rid, 0.5, "vec")]

    assert _tool(monkeypatch, capsys, "Agent", _handback("report")) is None
    assert fake_search["queries"] == []
    assert _marker()["outcome"] == "noop:dormant-no-floor"

    from techne.core.checks import Verdict, check_hooks

    (res,) = [r for r in check_hooks(config) if r.name == "hook:inbound_recall"]
    assert res.verdict is Verdict.DORMANT
    assert "inbound_recall_floor_vec_distance is unset" in res.detail


def test_the_floor_drops_far_and_unjudgeable_rows(home, monkeypatch, capsys, fake_search):
    """A row beyond the floor, and an FTS-only row the vector floor cannot judge,
    are both dropped here (per-turn keeps the second). Nothing delivered means
    the slot is not used, so a later result in the same prompt can still deliver."""
    _config(home, **ARMED)
    far, fts, near = _seed("far"), _seed("fts only"), _seed("near")
    fake_search["hits"] = [(far, 1.2, "vec"), (fts, None, "fts")]
    _prompt(monkeypatch, capsys)

    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page"}) is None
    assert _marker()["outcome"] == "noop:nothing-to-inject"
    assert _log() == [("page", [])]

    fake_search["hits"] = [(near, 0.4, "vec")]
    assert f"⟦techne#{near}⟧" in _tool(monkeypatch, capsys, "WebFetch", {"result": "page 2"})


def test_no_turn_marker_means_no_delivery(home, monkeypatch, capsys, fake_search):
    """UserPromptSubmit never stamped this session: the cap cannot be kept."""
    _config(home, **ARMED)
    fake_search["hits"] = [(_seed("row"), 0.5, "vec")]
    assert _tool(monkeypatch, capsys, "Agent", _handback("report")) is None
    assert _marker()["outcome"] == "noop:no-turn-marker"


# ---- which tools, and what text -----------------------------------------------------


@pytest.mark.parametrize("tool", ["Bash", "Read", "Edit", "Grep"])
def test_file_and_shell_tools_are_refused_even_when_listed(
    home, monkeypatch, capsys, fake_search, tool
):
    _config(home, **{**ARMED, "inbound_recall_tools": [tool, "Agent"]})
    fake_search["hits"] = [(_seed("row"), 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    assert _tool(monkeypatch, capsys, tool, "some output text") is None
    assert fake_search["queries"] == []


def test_an_unlisted_tool_is_not_searched(home, monkeypatch, capsys, fake_search):
    _config(home, **{**ARMED, "inbound_recall_tools": ["Agent"]})
    fake_search["hits"] = [(_seed("row"), 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page"}) is None
    assert fake_search["queries"] == []


def test_coord_message_bodies_are_searched_once(home, monkeypatch, capsys, fake_search):
    """A peer message about a topic injects that topic's rows once, not on every
    later tool call — even when the tool itself (Bash) is never searched."""
    config = _config(home, **ARMED)
    from techne.core import coord

    rid = _seed("worktree conventions")
    fake_search["hits"] = [(rid, 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    coord.send(config.db_path, "sess-peer", "claiming worktree wt-x", to="coord", from_name="peer")

    ctx = _tool(monkeypatch, capsys, "Bash", "ls output")
    assert "claiming worktree wt-x" in ctx and f"⟦techne#{rid}⟧" in ctx
    assert fake_search["queries"] == ["claiming worktree wt-x"]

    assert _tool(monkeypatch, capsys, "Bash", "ls output") is None
    assert fake_search["queries"] == ["claiming worktree wt-x"]

    # A second message in the same prompt is injected by the coord rail, but
    # the inbound slot is used, so it is not searched.
    coord.send(config.db_path, "sess-peer", "second note", to="coord", from_name="peer")
    ctx = _tool(monkeypatch, capsys, "Bash", "ls output")
    assert "second note" in ctx and "techne —" not in ctx
    assert fake_search["queries"] == ["claiming worktree wt-x"]


def test_a_subagent_tool_call_gets_nothing(home, monkeypatch, capsys, fake_search):
    """#930: a subagent's own tool calls are gated before every rail."""
    _config(home, **ARMED)
    fake_search["hits"] = [(_seed("row"), 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page"}, agent_id="a1") is None
    assert fake_search["queries"] == []


def test_a_handback_of_only_techne_context_is_not_searched(
    home, monkeypatch, capsys, fake_search
):
    _config(home, **ARMED)
    fake_search["hits"] = [(_seed("row"), 0.5, "vec")]
    _prompt(monkeypatch, capsys)
    own = (
        "## techne — durable facts relevant to this prompt\n"
        "> Per-turn recall (techne#109); recalled memory, not ground truth — verify first.\n"
        "\n- ⟦techne#7⟧ [fact] something"
    )
    assert _tool(monkeypatch, capsys, "Agent", _handback(own)) is None
    assert fake_search["queries"] == []
    assert _marker()["outcome"] == "noop:only-own-context"


def test_inbound_text_shapes():
    t = post_tool_use._inbound_text
    assert t(_handback("the report")) == "the report"
    assert t({"result": "page body", "code": 200}) == "page body"
    assert t([{"type": "text", "text": "mcp text"}]) == "mcp text"
    inbox = json.dumps(
        {"session_id": "s", "messages": [{"id": 1, "from_session": "x", "body": "hello peer"}]}
    )
    assert t([{"type": "text", "text": inbox}]) == "hello peer"
    assert t(None) == ""


def test_a_long_page_is_searched_on_its_questions_or_a_capped_opening():
    from techne.hooks._recall_query import build_inbound_query

    page = "intro line\n" + "x " * 5000 + "\nWhat is the retention policy?\nmore"
    assert build_inbound_query(page, 600) == "What is the retention policy?"
    assert len(build_inbound_query("y " * 5000, 600)) <= 600


def test_inbound_recall_tools_must_be_a_list(home):
    import yaml

    from techne.config import Config

    (home / "config.yaml").write_text(
        yaml.safe_dump({"hooks": {"inbound_recall_tools": "Agent"}})
    )
    with pytest.raises(ValueError, match="inbound_recall_tools"):
        Config.load()


def test_a_lost_claim_delivers_and_logs_nothing(home, fake_search):
    """Two tool results finishing together: the loser must not log its rows, or
    `_already_sent_this_session` would suppress rows nobody received."""
    config = _config(home, **ARMED)
    rid = _seed("row")
    fake_search["hits"] = [(rid, 0.5, "vec")]
    block, rows = user_prompt_submit.recall_block(
        config, SID, "q", source="inbound_recall", k=2, claim=lambda: False
    )
    assert (block, rows) == ("", [])
    assert _log() == []


def test_a_task_notification_does_not_reopen_the_slot(home, monkeypatch, capsys, fake_search):
    """A finished background task reaches UserPromptSubmit but is not a prompt."""
    _config(home, **{**ARMED, "inbound_recall_limit": 1})
    fake_search["hits"] = [(_seed("row a"), 0.5, "vec"), (_seed("row b"), 0.6, "vec")]
    _prompt(monkeypatch, capsys)
    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page one"}) is not None
    _prompt(monkeypatch, capsys, "<task-notification><task-id>x</task-id></task-notification>")
    assert _tool(monkeypatch, capsys, "WebFetch", {"result": "page two"}) is None


def test_concurrent_claims_deliver_exactly_once(home, monkeypatch):
    """Two tool results finishing together: BEGIN IMMEDIATE lets one claim win.

    The read is slowed so every thread would see the slot open without the lock;
    with it, the others wait on the write lock and then see it used."""
    import threading
    import time

    from techne.core import extension_state

    config = _config(home, **ARMED)
    user_prompt_submit._stamp_prompt_turn(config.db_path, SID)
    real_get = extension_state.get_state

    def _slow_get(conn, name):
        out = real_get(conn, name)
        time.sleep(0.05)
        return out

    monkeypatch.setattr(extension_state, "get_state", _slow_get)
    results, barrier = [], threading.Barrier(8)

    def _claim():
        barrier.wait()
        results.append(post_tool_use._inbound_slot(config, SID, take=True))

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["open"] + ["used"] * 7
