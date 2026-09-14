"""PostToolUse must inject nothing when it fires inside a subagent (techne#929).

A subagent presents the PARENT'S session_id, so every session-keyed lookup in the
hook resolves to the parent's state — `_inject_coord_messages` reads the parent's
inbox and hands a peer's message to the subagent. The sandbox around
`research-readonly` is built by REMOVING tools (no Bash, no store, no MCP, no
Read); this rail writes into that agent's context by a route none of those
removals touch, and the agent keeps WebFetch.

The discriminator is `agent_id` / `agent_type`, documented as present only inside
a subagent. It cannot be session_id — that is the same value, which is the whole
bug.

The first test here is the control: without those fields the message IS injected.
Without it the rest would pass against a hook that had simply stopped working.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.hooks import post_tool_use

PARENT = "sess-parent"
PEER = "sess-peer"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _seed_coord_message(home):
    """A peer message waiting in PARENT's inbox, as on any live session."""
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": {}}))

    from techne.config import Config
    from techne.core import coord
    from techne.core.database import init_db
    from techne.core.state import ensure_session

    config = Config.load()
    init_db(config.db_path)
    ensure_session(config.db_path, PARENT)
    coord.send(config.db_path, PEER, "worktree /tmp/wt-x, branch fix/x", to="coord",
               from_name="peer")
    return config


def _payload(**extra):
    p = {
        "session_id": PARENT,
        "tool_name": "WebFetch",
        "tool_input": {"url": "https://example.org"},
        "tool_response": "ok",
        "cwd": "/tmp",
    }
    p.update(extra)
    return p


def _run(monkeypatch, capsys, payload):
    """Returns stdout. The guard exits via sys.exit(0), like main()'s other early
    exits, so SystemExit is an expected outcome here rather than a failure."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        post_tool_use.main()
    except SystemExit as exc:
        assert exc.code in (0, None), f"hook exited {exc.code}"
    return capsys.readouterr().out


def _injected(out):
    if not out.strip():
        return None
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext")


def test_control_main_session_does_receive_the_coord_message(home, monkeypatch, capsys):
    """CONTROL. Without agent_id/agent_type the message is injected — so the
    suppression tests below are testing suppression, not a dead rail."""
    _seed_coord_message(home)
    ctx = _injected(_run(monkeypatch, capsys, _payload()))
    assert ctx is not None, "coord rail injected nothing for a main session"
    assert "worktree /tmp/wt-x" in ctx


def test_agent_id_suppresses_injection(home, monkeypatch, capsys):
    _seed_coord_message(home)
    out = _run(monkeypatch, capsys, _payload(agent_id="a585d495d3728378d"))
    assert _injected(out) is None


def test_agent_type_suppresses_injection(home, monkeypatch, capsys):
    """agent_type alone is enough — the fields are documented as a pair, and
    relying on both being present would fail open on either arriving alone."""
    _seed_coord_message(home)
    out = _run(monkeypatch, capsys, _payload(agent_type="research-readonly"))
    assert _injected(out) is None


def test_suppressed_message_is_still_unread_for_the_parent(home, monkeypatch, capsys):
    """Suppression must LOSE NOTHING. The message stays unread in the ledger, so
    the parent still gets it via the session-start banner and techne_inbox — the
    subagent simply is not handed a copy."""
    config = _seed_coord_message(home)
    from techne.core import coord

    _run(monkeypatch, capsys, _payload(agent_id="a585d495d3728378d"))
    still = [m["body"] for m in coord.inbox(config.db_path, PARENT)]
    assert still == ["worktree /tmp/wt-x, branch fix/x"]


def test_parent_still_gets_it_after_a_subagent_call(home, monkeypatch, capsys):
    """The real sequence: a subagent makes a tool call (suppressed), then the
    parent makes one. The parent must still receive the message — a naive fix that
    marked it surfaced during the subagent call would silently eat it."""
    _seed_coord_message(home)
    _run(monkeypatch, capsys, _payload(agent_id="a585d495d3728378d"))
    ctx = _injected(_run(monkeypatch, capsys, _payload()))
    assert ctx is not None, "the parent lost the message to the subagent's call"
    assert "worktree /tmp/wt-x" in ctx


def test_capture_still_happens_for_a_subagent_call(home, monkeypatch, capsys):
    """THE PROPERTY THE WHOLE CHANGE RESTS ON, and nothing else pins it.

    The gate suppresses INJECTION, not CAPTURE. It sits below the capture block for
    that reason, and the comment says so — but a comment does not hold a line in
    place. The natural future tidy-up is "exit early, exit sooner": hoist the gate
    above the capture block. Do that and all the tests above stay green while every
    subagent tool call stops being recorded (Prudence demonstrated it on #930:
    5 passed, observations 0).

    That is the silent shape this repo is organised around — post_tool_use calls the
    capture rail "techne is functionally dead without it", and techne#931 presupposes
    these rows exist in order to argue about their attribution. So the assertion is
    on the ROW, not on the ordering: a test that pins line numbers would pass on a
    refactor that broke this.
    """
    config = _seed_coord_message(home)
    from techne.core.database import connect

    _run(monkeypatch, capsys, _payload(agent_id="a585d495d3728378d"))

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT tool_name FROM observations WHERE session_id = ?", (PARENT,)
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["WebFetch"], (
        "a subagent's tool call was not captured — the gate suppresses injection "
        "only, and must sit BELOW the capture block"
    )
