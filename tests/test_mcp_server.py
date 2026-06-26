"""MCP server tests — JSON-RPC routing in handle_request().

Coverage gap noted in v0.5.0 review: handle_request had no direct tests.
This file exercises the method dispatch, version reporting, ping support,
and per-tool error paths.
"""

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
            "techne_recall",
            "codex_search",
            "codex_blast_radius",
            "pattern_search",
            "pattern_related",
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
