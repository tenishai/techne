"""The post_tool_use hook stores observations untagged; `vectors backfill` tags
them later (issue 1192).

Inference in the hook cost a numpy import and a model load on every tool call,
about 195 ms of a 360 ms hook. A NULL `kind` marks a row as pending, and
`backfill_tags(pending_only=True)` fills it, once. Other callers of
`capture_tool_use` and `add_observation` keep write-time tagging, because only
the Claude Code platform runs the backfill.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.core import inference
from techne.core.inference import InferenceResult, backfill_tags
from techne.core.state import add_observation, ensure_session, get_observations

SID = "sess-defer"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"hooks": {}}))
    from techne.config import Config
    from techne.core.database import init_db

    config = Config.load()
    init_db(config.db_path)
    ensure_session(config.db_path, SID)
    return config


def _forbid_inference(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("infer() ran on a deferred write")

    monkeypatch.setattr(inference, "infer", _boom)


def _stub_inference(monkeypatch, *, prototypes=True):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
    monkeypatch.setattr(
        inference,
        "_get_prototype_embeddings",
        lambda *a, **kw: {"alpha": object()} if prototypes else {},
    )
    monkeypatch.setattr(
        inference,
        "infer",
        lambda content, cfg=None, **kw: InferenceResult(tags=["alpha"], kind="fact-shaped"),
    )


def test_add_observation_can_defer_inference(home, monkeypatch):
    _forbid_inference(monkeypatch)
    add_observation(home.db_path, SID, "Bash", "git status", infer_tags=False)
    (row,) = get_observations(home.db_path, SID)
    assert row["tags"] is None and row["kind"] is None


def test_add_observation_still_infers_by_default(home, monkeypatch):
    _stub_inference(monkeypatch)
    add_observation(home.db_path, SID, "Bash", "git status")
    (row,) = get_observations(home.db_path, SID)
    assert (row["tags"], row["kind"]) == ("alpha", "fact-shaped")


def test_capture_tool_use_defers_only_when_asked(home, monkeypatch):
    from techne.lifecycle import capture_tool_use

    _stub_inference(monkeypatch)
    capture_tool_use(home, SID, "Edit", {"file_path": "/tmp/a.py"}, None)
    capture_tool_use(home, SID, "Edit", {"file_path": "/tmp/b.py"}, None, defer_inference=True)
    kinds = {r["content"].split("/tmp/")[1][:4]: r["kind"] for r in get_observations(home.db_path, SID)}
    assert kinds == {"a.py": "fact-shaped", "b.py": None}


def test_the_hook_stores_rows_untagged(home, monkeypatch, capsys):
    from techne.hooks import post_tool_use

    _forbid_inference(monkeypatch)
    payload = {
        "session_id": SID,
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/hook.py", "old_string": "x", "new_string": "y"},
        "tool_response": "ok",
        "cwd": "/tmp",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        post_tool_use.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert "capture failed" not in capsys.readouterr().err
    (row,) = get_observations(home.db_path, SID)
    assert row["kind"] is None, "the hook ran inference instead of deferring it"


def test_pending_backfill_tags_each_pending_row_once(home, monkeypatch):
    _forbid_inference(monkeypatch)
    for i in range(2):
        add_observation(home.db_path, SID, "Bash", f"pending {i}", infer_tags=False)
    _stub_inference(monkeypatch)
    # Already inferred, with zero tags: `only_missing` would rescan it, pending must not.
    add_observation(home.db_path, SID, "Bash", "inferred, no tags")
    monkeypatch.setattr(
        inference,
        "infer",
        lambda content, cfg=None, **kw: InferenceResult(tags=["alpha"], kind="fact-shaped"),
    )
    from techne.core.database import connect

    conn = connect(home.db_path)
    conn.execute("UPDATE observations SET tags = NULL WHERE content = 'inferred, no tags'")
    conn.commit()
    conn.close()

    assert backfill_tags(home.db_path, home, pending_only=True) == {"scanned": 2, "tagged": 2}
    assert backfill_tags(home.db_path, home, pending_only=True) == {"scanned": 0, "tagged": 0}
    assert all(r["kind"] for r in get_observations(home.db_path, SID))


def test_pending_backfill_writes_nothing_when_the_model_is_not_loadable(home, monkeypatch):
    """A cold cache makes infer() return a default kind. Stamping it would mark
    the row done without inferring it, so pending mode must write nothing."""
    _forbid_inference(monkeypatch)
    add_observation(home.db_path, SID, "Bash", "pending", infer_tags=False)
    _stub_inference(monkeypatch, prototypes=False)
    assert backfill_tags(home.db_path, home, pending_only=True) == {"scanned": 0, "tagged": 0}
    (row,) = get_observations(home.db_path, SID)
    assert row["kind"] is None


def test_reflect_sees_the_kind_of_rows_the_hook_stored_untagged(home, monkeypatch):
    """Reflect usually runs on the session that just ended, before the next
    SessionStart backfill, and `_render_observations` shows each row's kind to
    the model. So loading a session for reflection tags its pending rows first."""
    from techne.core.reflect import load_session_for_reflection

    _forbid_inference(monkeypatch)
    add_observation(home.db_path, SID, "Bash", "decided to pin fastembed", infer_tags=False)
    ensure_session(home.db_path, "other-session")
    add_observation(home.db_path, "other-session", "Bash", "elsewhere", infer_tags=False)
    _stub_inference(monkeypatch)

    _threads, observations = load_session_for_reflection(home, SID)

    assert [o["kind"] for o in observations] == ["fact-shaped"]
    other = get_observations(home.db_path, "other-session")
    assert [o["kind"] for o in other] == [None], "only the reflected session is tagged"


def test_tagging_refreshes_observations_the_caller_already_fetched(home, monkeypatch):
    """`reflect_on_session` gets its observations from `run_session_end`, fetched
    before tagging, so the helper must update those dicts, not only the rows."""
    from techne.core.reflect import _tag_pending_observations

    _forbid_inference(monkeypatch)
    add_observation(home.db_path, SID, "Bash", "decided to pin fastembed", infer_tags=False)
    observations = get_observations(home.db_path, SID)
    assert observations[0]["kind"] is None
    _stub_inference(monkeypatch)

    _tag_pending_observations(home, SID, observations)

    assert (observations[0]["tags"], observations[0]["kind"]) == ("alpha", "fact-shaped")


def test_headless_reflect_tags_pending_rows_before_rendering(home, monkeypatch):
    """The SessionEnd/PreCompact path: run_session_end -> reflect_on_session."""
    from techne.core import reflect

    seen = []
    monkeypatch.setattr(
        reflect, "_tag_pending_observations", lambda cfg, sid, obs=None: seen.append((sid, obs))
    )
    home.reflect.enabled = True
    obs = [{"id": 1, "kind": None}]
    reflect.reflect_on_session(home, SID, [], obs, generate_fn=lambda *a, **k: "[]")
    assert seen == [(SID, obs)]
