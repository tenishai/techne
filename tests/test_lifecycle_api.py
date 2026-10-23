"""Tests for techne.lifecycle — library-callable lifecycle operations."""

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.state import add_observation, get_observations, start_session


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


def fake_generate(prompt: str) -> str:
    """Fake LLM generate function that returns a minimal valid thread response."""
    return """## Thread 1: Test work
Observations: 0,1,2
Summary: Ran some edits and bash commands in the test session.
Resume: None"""


def test_run_session_start_returns_session_id(config, db_path):
    """run_session_start returns a non-empty session_id and persists a row."""
    from techne.lifecycle import run_session_start

    session_id = run_session_start(config, project_path="/tmp/test", agent_identity="test-agent")

    assert isinstance(session_id, str)
    assert len(session_id) > 0

    # Verify row exists in sessions table
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["id"] == session_id
    assert row["project_path"] == "/tmp/test"
    assert row["agent_identity"] == "test-agent"


def test_run_session_end_full_lifecycle(config, db_path, monkeypatch):
    """run_session_end returns expected keys and persists at least one thread."""
    import techne.core.threads as threads_mod
    from techne.lifecycle import run_session_end, run_session_start

    # Monkeypatch _make_generate_fn so no real LLM call is made
    monkeypatch.setattr(threads_mod, "_make_generate_fn", lambda cfg: fake_generate)

    # Enqueue is gated on a live consumer (non-noop adapter); opt in so the
    # queued>=1 assertion below exercises the enqueue path.
    config.sync.adapter = "filesystem"
    config.sync.path = str(db_path.parent / "sync-out")

    session_id = run_session_start(config)

    # Add enough observations to be interesting (above MIN_NEW_OBSERVATIONS=10 in hook,
    # but lifecycle.run_session_end itself has no minimum — just needs some obs)
    for i in range(5):
        add_observation(db_path, session_id, "Edit", f"Edited: file_{i}.py")
    for i in range(5):
        add_observation(db_path, session_id, "Bash", f"Ran: ls {i}")

    result = run_session_end(config, session_id)

    # Check the returned dict has all expected keys
    # Exact-set, not superset: this is the library-callable API surface, so a new
    # key is a deliberate change that must be acknowledged here. It did that job
    # for techne#514's `queued_for_reflection`.
    assert set(result.keys()) == {
        "threads", "linked", "aged_completed", "aged_stale", "queued",
        "queued_for_reflection", "knowledge_pushed", "knowledge_published", "reflect",
    }
    assert isinstance(result["threads"], int)
    assert isinstance(result["linked"], int)
    assert isinstance(result["aged_completed"], int)
    assert isinstance(result["aged_stale"], int)
    assert isinstance(result["queued"], int)
    # reflect is always present; counts are all zero when reflect.enabled=False
    assert isinstance(result["reflect"], dict)
    assert result["reflect"]["threads_reflected"] == 0
    assert result["reflect"]["candidates_written"] == 0

    # At least one thread was detected and saved
    assert result["threads"] >= 1
    assert result["queued"] >= 1

    # Verify thread row was persisted
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM threads WHERE session_id = ?", (session_id,)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) >= 1

    # Verify session was ended
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    assert row["ended_at"] is not None


def test_capture_tool_use_filtered(config, db_path):
    """capture_tool_use returns False for filtered tools and writes no observation row."""
    from techne.core.state import observation_count
    from techne.lifecycle import capture_tool_use, run_session_start

    session_id = run_session_start(config)

    # "Read" is in SKIP_TOOLS in hooks/post_tool_use.py — should be filtered out
    captured = capture_tool_use(
        config,
        session_id,
        tool_name="Read",
        tool_input={"file_path": "/tmp/foo.txt"},
        tool_output="file contents",
    )

    assert captured is False
    assert observation_count(db_path, session_id) == 0


def test_capture_tool_use_captured(config, db_path):
    """capture_tool_use returns True for non-filtered tools and writes an observation."""
    from techne.core.state import observation_count
    from techne.lifecycle import capture_tool_use, run_session_start

    session_id = run_session_start(config)

    # "Edit" is not filtered
    captured = capture_tool_use(
        config,
        session_id,
        tool_name="Edit",
        tool_input={"file_path": "/tmp/foo.py", "old_string": "x", "new_string": "y"},
        tool_output=None,
    )

    assert captured is True
    assert observation_count(db_path, session_id) == 1

    obs = get_observations(db_path, session_id)
    assert obs[0]["tool_name"] == "Edit"
    assert "/tmp/foo.py" in obs[0]["content"]


def test_capture_tool_use_hook_disabled(config, db_path):
    """capture_tool_use returns False when config.hooks.post_tool_use is False."""
    from techne.core.state import observation_count
    from techne.lifecycle import capture_tool_use, run_session_start

    config.hooks.post_tool_use = False
    session_id = run_session_start(config)

    captured = capture_tool_use(
        config,
        session_id,
        tool_name="Edit",
        tool_input={"file_path": "/tmp/foo.py"},
        tool_output=None,
    )

    assert captured is False
    assert observation_count(db_path, session_id) == 0
