"""MCP server tests — JSON-RPC routing in handle_request().

Coverage gap noted in v0.5.0 review: handle_request had no direct tests.
This file exercises the method dispatch, version reporting, ping support,
and per-tool error paths.
"""

import json
import os
import subprocess
import sys

import pytest

from techne import __version__
from techne.config import Config
from techne.core.database import init_db
from techne.mcp_server import handle_request


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "techne.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestHandleRequest:
    def test_initialize_returns_current_version(self, config):
        """serverInfo.version must come from techne.__version__, not a literal."""
        response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, config)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert response["result"]["serverInfo"] == {"name": "techne", "version": __version__}

    def test_initialize_advertises_tools_capability(self, config):
        response = handle_request({"id": 1, "method": "initialize"}, config)
        assert "tools" in response["result"]["capabilities"]
        assert response["result"]["protocolVersion"] == "2024-11-05"

    def test_initialize_includes_server_instructions(self, config):
        """The MCP `instructions` field teaches the agent the memory loop (#109/#81 follow-up).
        Without it an agent sees a pile of tools and no judgment about when to use them."""
        response = handle_request({"id": 1, "method": "initialize"}, config)
        instructions = response["result"].get("instructions", "")
        assert isinstance(instructions, str) and len(instructions) > 200
        low = instructions.lower()
        # the four-step loop must be teachable from this text
        assert "recall" in low and "store" in low
        assert "techne_reflect_emit" in instructions  # session-end consolidation path
        assert "verify" in low  # the freshness/uncertain honesty cue

    def test_store_and_recall_descriptions_lead_with_trigger(self, config):
        """store/recall/search descriptions must lead with WHEN to use them, not just WHAT."""
        from techne.mcp_server import TOOLS

        by_name = {t["name"]: t["description"] for t in TOOLS}
        assert by_name["techne_store"].startswith("Call this when you learn")
        assert by_name["techne_recall"].startswith("Call this before relying")
        assert by_name["techne_search"].startswith("Call this when you need breadth")

    def test_ping_returns_empty_result(self, config):
        """MCP `ping` should return an empty object, not -32601. Without this,
        clients that probe the server with ping treat it as unreachable."""
        response = handle_request({"id": 7, "method": "ping"}, config)
        assert response == {"jsonrpc": "2.0", "id": 7, "result": {}}

    def test_tools_list_returns_all_tools(self, config):
        response = handle_request({"id": 2, "method": "tools/list"}, config)
        tools = response["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "techne_search",
            "techne_store",
            "techne_observe",
            "techne_threads",
            "techne_resume",
            "techne_status",
            "techne_capabilities",
            "techne_recall",
            "codex_search",
            "codex_blast_radius",
            "pattern_search",
            "pattern_related",
            "techne_reflect_emit",
            "techne_reflect_ingest",
            "techne_consolidate_emit",
            "techne_consolidate_ingest",
            "techne_dream_emit",
            "techne_dream_ingest",
            "techne_memory_claims_emit",
            "techne_memory_claims_ingest",
            "techne_conflict_emit",
            "techne_conflict_ingest",
            "techne_check_evidence",
            "techne_faithfulness_emit",
            "techne_faithfulness_ingest",
            "techne_federation_conflict_ingest",
            "techne_reminder_add",
            "techne_reminder_list",
            "techne_reminder_dismiss",
            "techne_send",
            "techne_inbox",
            "techne_mark_read",
            "techne_coord_threads",
        }

    def test_notifications_initialized_returns_none(self, config):
        """JSON-RPC notifications get no reply."""
        response = handle_request({"method": "notifications/initialized"}, config)
        assert response is None

    def test_unknown_method_returns_minus_32601(self, config):
        response = handle_request({"id": 3, "method": "totally/made-up"}, config)
        assert response["error"]["code"] == -32601
        assert "totally/made-up" in response["error"]["message"]

    def test_unknown_tool_returns_minus_32601(self, config):
        response = handle_request(
            {"id": 4, "method": "tools/call", "params": {"name": "nonexistent_tool"}},
            config,
        )
        assert response["error"]["code"] == -32601
        assert "nonexistent_tool" in response["error"]["message"]


class TestTechneStoreContentValidation:
    def test_missing_content_returns_iserror(self, config):
        """techne_store schema requires `content`, but the server didn't enforce
        it pre-fix and would crash with KeyError. After the fix it returns a
        clean isError response with a descriptive message."""
        response = handle_request(
            {
                "id": 5,
                "method": "tools/call",
                "params": {"name": "techne_store", "arguments": {}},
            },
            config,
        )
        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "content" in text.lower()

    def test_empty_content_returns_iserror(self, config):
        response = handle_request(
            {
                "id": 6,
                "method": "tools/call",
                "params": {"name": "techne_store", "arguments": {"content": ""}},
            },
            config,
        )
        assert response["result"]["isError"] is True

    def test_valid_content_succeeds(self, config):
        response = handle_request(
            {
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "techne_store",
                    "arguments": {"content": "a real fact", "type": "fact"},
                },
            },
            config,
        )
        # No isError, content array carries the success payload as JSON text
        assert "isError" not in response["result"]
        assert "content" in response["result"]

    def test_pattern_type_not_exposed_and_rejected(self, config):
        """techne#137: `pattern` is a team-domain type — not in the techne_store enum,
        and a write attempt is rejected (it belongs in architect.db)."""
        from techne.mcp_server import TOOLS

        store_tool = next(t for t in TOOLS if t["name"] == "techne_store")
        assert "pattern" not in store_tool["inputSchema"]["properties"]["type"]["enum"]

        response = handle_request(
            {
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "techne_store",
                    "arguments": {"content": "a lifted pattern", "type": "pattern"},
                },
            },
            config,
        )
        assert response["result"]["isError"] is True
        assert "architect" in response["result"]["content"][0]["text"].lower()


class TestTechneObserve:
    """techne_observe — the platform-agnostic capture path (#57)."""

    def _call(self, config, args, req_id=20):
        return handle_request(
            {
                "id": req_id,
                "method": "tools/call",
                "params": {"name": "techne_observe", "arguments": args},
            },
            config,
        )

    def test_missing_session_id_returns_iserror(self, config):
        r = self._call(config, {"content": "x"})
        assert r["result"]["isError"] is True
        assert "session_id" in r["result"]["content"][0]["text"]

    def test_missing_content_returns_iserror(self, config):
        r = self._call(config, {"session_id": "s1"})
        assert r["result"]["isError"] is True
        assert "content" in r["result"]["content"][0]["text"].lower()

    def test_records_observation_and_ensures_session(self, config):
        from techne.core.database import connect

        r = self._call(
            config,
            {"session_id": "oc-session-1", "content": "edited foo.ts", "platform": "opencode"},
        )
        assert "isError" not in r["result"]
        import json

        payload = json.loads(r["result"]["content"][0]["text"])
        assert payload == {"recorded": True, "session_id": "oc-session-1"}

        conn = connect(config.db_path)
        try:
            sess = conn.execute(
                "SELECT platform FROM sessions WHERE id = ?", ("oc-session-1",)
            ).fetchone()
            obs = conn.execute(
                "SELECT content, tool_name FROM observations WHERE session_id = ?",
                ("oc-session-1",),
            ).fetchall()
        finally:
            conn.close()
        # session row created and stamped with the client's platform (#57 coupling #4)
        assert sess["platform"] == "opencode"
        assert len(obs) == 1
        assert obs[0]["content"] == "edited foo.ts"
        assert obs[0]["tool_name"] == "Observation"  # default

    def test_second_observe_reuses_session(self, config):
        from techne.core.database import connect

        self._call(config, {"session_id": "oc-2", "content": "one", "platform": "opencode"})
        self._call(config, {"session_id": "oc-2", "content": "two", "platform": "opencode"})
        conn = connect(config.db_path)
        try:
            n_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", ("oc-2",)
            ).fetchone()[0]
            n_obs = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE session_id = ?", ("oc-2",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n_sessions == 1  # ensure_session is idempotent
        assert n_obs == 2

    def test_platform_defaults_to_config(self, config):
        from techne.core.database import connect

        config.platform = "claude-code"
        self._call(config, {"session_id": "oc-3", "content": "x"})  # no platform arg
        conn = connect(config.db_path)
        try:
            sess = conn.execute("SELECT platform FROM sessions WHERE id = ?", ("oc-3",)).fetchone()
        finally:
            conn.close()
        assert sess["platform"] == "claude-code"

    def test_redaction_applied(self, config):
        """Capture must go through the same redaction path as the hook."""
        from techne.core.database import connect

        self._call(
            config,
            {"session_id": "oc-4", "content": "key AKIAIOSFODNN7EXAMPLE here", "platform": "x"},
        )
        conn = connect(config.db_path)
        try:
            content = conn.execute(
                "SELECT content FROM observations WHERE session_id = ?", ("oc-4",)
            ).fetchone()["content"]
        finally:
            conn.close()
        assert "AKIAIOSFODNN7EXAMPLE" not in content


class TestTechneStatus:
    def test_status_surfaces_version_and_package_path(self, config):
        """#71: the status payload must answer 'is the running server current?'
        — version + resolved package path, not just the initialize handshake."""
        import json
        from pathlib import Path

        import techne

        response = handle_request(
            {
                "id": 10,
                "method": "tools/call",
                "params": {"name": "techne_status", "arguments": {}},
            },
            config,
        )
        assert "isError" not in response["result"]
        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload["version"] == __version__
        assert payload["package_path"] == str(Path(techne.__file__).resolve().parent)
        # pre-existing fields preserved
        assert payload["db_path"] == str(config.db_path)
        assert "sync_adapter" in payload


class TestMainLoopNonDictGuard:
    """techne#166: the stdio loop must not crash on well-formed JSON that isn't a
    request object. `null`, `42`, and `[]` all parse via json.loads but have no
    .get(), so handle_request would AttributeError and kill the whole server,
    dropping every subsequent valid request on the connection."""

    def test_non_dict_lines_skipped_valid_request_still_served(self, tmp_path):
        # Mixed stdin: a JSON null, a JSON int, a JSON array, then a real request.
        # The server must ignore the first three and answer the initialize.
        stdin = "\n".join([
            "null",
            "42",
            "[1, 2, 3]",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        ]) + "\n"
        env = {**os.environ, "TECHNE_HOME": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, "-m", "techne.mcp_server"],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"server crashed: {result.stderr}"
        # Exactly one response line — the non-dict lines produced none.
        out_lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(out_lines) == 1, f"expected 1 response, got {out_lines!r}"
        resp = json.loads(out_lines[0])
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "techne"


def test_codex_blast_radius_unresolvable_symbol_returns_error(tmp_path, config, monkeypatch):
    """techne#303: a bare name / bogus id on the MCP codex_blast_radius tool
    must return an explicit error (not an empty node list that reads as
    'nothing reachable') — the MCP twin of the CLI fix in #300.
    """
    from techne import mcp_server
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    codex_db = tmp_path / "codex.db"
    SqliteStore("codex", codex_db).migrate(CODEX_SCHEMA)
    add_symbol(codex_db, id="a.py::verify::function::1", file="a.py", name="verify", kind="function")
    add_symbol(codex_db, id="b.py::verify::method::5", file="b.py", name="verify", kind="method")
    add_symbol(codex_db, id="c.py::solo::function::1", file="c.py", name="solo", kind="function")

    # Point the handler at our hand-built db regardless of cwd.
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: codex_db)

    # Bogus name → explicit error, no silent-empty nodes.
    r = mcp_server._codex_blast_radius({"symbol_id": "does_not_exist"}, config)
    assert r["nodes"] == []
    assert "no symbol named" in r.get("error", "")

    # Ambiguous bare name → error carrying the candidates.
    r2 = mcp_server._codex_blast_radius({"symbol_id": "verify"}, config)
    assert r2["nodes"] == []
    assert {c["id"] for c in r2.get("candidates", [])} == {
        "a.py::verify::function::1",
        "b.py::verify::method::5",
    }

    # Unique bare name → resolves to its id, no error key.
    r3 = mcp_server._codex_blast_radius({"symbol_id": "solo"}, config)
    assert "error" not in r3
    assert r3["symbol_id"] == "c.py::solo::function::1"

    # Overflowing candidate list is flagged as truncated, not silently capped.
    for i in range(12):
        add_symbol(codex_db, id=f"m{i}.py::dup::function::1", file=f"m{i}.py", name="dup", kind="function")
    r4 = mcp_server._codex_blast_radius({"symbol_id": "dup"}, config)
    assert len(r4["candidates"]) == 10
    assert r4.get("candidates_truncated") is True

    # Through the dispatch envelope: a resolution error is normal content
    # (NOT isError) carrying the error text — the agent still sees it, and the
    # structured candidates aren't flattened into an isError string.
    resp = handle_request(
        {
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "codex_blast_radius",
                "arguments": {"symbol_id": "does_not_exist"},
            },
        },
        config,
    )
    assert resp["result"].get("isError") is not True
    assert "no symbol named" in resp["result"]["content"][0]["text"]


def test_status_surfaces_checks_deferred_to_agents(db_path):
    """The key that actually answers techne#488 for an agent, anchored.

    `_techne_status` returns `collect_health(config)` verbatim today, so the
    guarantee that the MCP surface carries `checks_deferred` rests on the
    absence of key projection — true now, and it would break SILENTLY if
    anyone introduced one. The arch card claims this surface; this pins it.
    """
    from techne.core.checks import deferred_check_names
    from techne.mcp_server import _techne_status

    cfg = Config()
    cfg.db_path = db_path

    payload = _techne_status({}, cfg)

    assert "checks_deferred" in payload
    assert payload["checks_deferred"] == deferred_check_names()
    # The three states an agent must be able to tell apart stay disjoint.
    assert set(payload["checks_deferred"]).isdisjoint({c["name"] for c in payload["checks"]})


def test_status_keeps_checks_deferred_when_it_is_none(db_path, monkeypatch):
    """The case a key-dropping projection actually removes.

    The test above only exercises a NON-EMPTY list, and the projection its
    sibling docstring warns about — `{k: v for k, v in ... if v is not None}` —
    is invisible to that shape. Reviewed and reproduced: with that projection
    added to `_techne_status`, the list-shaped test still passed. `None` is
    exactly the value such a filter eats, and it is the value that means "the
    registry could not be read", so dropping it puts a consumer's
    `.get("checks_deferred", [])` back to reading "nothing was deferred" at the
    moment every check was skipped — this PR's founding bug, on the surface the
    arch card claims for it.
    """
    import builtins

    from techne.mcp_server import _techne_status

    real_import = builtins.__import__

    def _fail_on_checks(name, *a, **kw):
        if name == "techne.core.checks":
            raise ImportError("simulated: checks module unavailable")
        return real_import(name, *a, **kw)

    cfg = Config()
    cfg.db_path = db_path
    monkeypatch.setattr(builtins, "__import__", _fail_on_checks)

    payload = _techne_status({}, cfg)

    assert "checks_deferred" in payload, "key vanished on the MCP surface"
    assert payload["checks_deferred"] is None
