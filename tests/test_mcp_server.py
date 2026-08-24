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
            "archivist_search",
            "librarian_search",
            "architect_search",
            "techne_search",
            "techne_store",
            "techne_observe",
            "techne_threads",
            "techne_resume",
            "techne_status",
            "techne_capabilities",
            "techne_archivist_query",
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
        stdin = (
            "\n".join(
                [
                    "null",
                    "42",
                    "[1, 2, 3]",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                ]
            )
            + "\n"
        )
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
    add_symbol(
        codex_db, id="a.py::verify::function::1", file="a.py", name="verify", kind="function"
    )
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
        add_symbol(
            codex_db, id=f"m{i}.py::dup::function::1", file=f"m{i}.py", name="dup", kind="function"
        )
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


def test_mcp_tools_doc_is_complete():
    """docs/mcp-tools.md must name every registered tool (techne#647).

    A generated reference drifts the moment someone adds a tool and forgets the
    doc, which is exactly how 10 of 33 tools came to appear in no document at
    all. This makes the omission a test failure rather than a discovery months
    later. Regenerate the doc when this fails; do not delete the assertion.
    """
    from pathlib import Path

    from techne.mcp_server import TOOLS

    doc = Path(__file__).resolve().parent.parent / "docs" / "mcp-tools.md"
    assert doc.exists(), "docs/mcp-tools.md is missing"
    text = doc.read_text(encoding="utf-8")
    # MATCH THE HEADING, NOT A MENTION. A bare-name search passes on any prose
    # reference — the preamble names `techne_capabilities` in passing, so deleting
    # its actual entry still read as documented. Found by mutation-probing this
    # test, which is the only reason it is worth having.
    missing = [t["name"] for t in TOOLS if f"### `{t['name']}`" not in text]
    assert not missing, f"tools registered but undocumented in docs/mcp-tools.md: {missing}"


def test_the_four_domains_section_names_all_four():
    """techne#744. `test_mcp_tools_doc_is_complete` above measures a PROXY: it asks
    whether a `### \u0060name\u0060` heading exists ANYWHERE in the file. That is
    satisfiable without the property that matters — `codex_search` was documented in a
    "Code graph" section of its own, so the section whose whole job is teaching the
    four-name vocabulary listed THREE of them and the suite was green.

    Same shape `docs/arch/liveness.md` names: the signal was measured on a proxy that
    could be satisfied without the thing being true. So this asserts on the SECTION.
    """
    from pathlib import Path

    doc = Path(__file__).resolve().parent.parent / "docs" / "mcp-tools.md"
    text = doc.read_text(encoding="utf-8")

    start = text.index("## The four domains")
    end = text.index("\n## ", start)
    section = text[start:end]

    for domain in ("archivist", "librarian", "architect", "codex"):
        assert f"### `{domain}_search`" in section, (
            f"`{domain}_search` must be documented INSIDE the four-domains section — "
            "a vocabulary section that lists three of four teaches the gap"
        )


def _codex_db_with_fan_in(tmp_path, n: int, proven: int | None = None):
    """A hub symbol with *n* direct dependents — the shape that blew the token ceiling."""
    from techne.core.codegraph import add_edge, add_symbol
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    db = tmp_path / "codex.db"
    SqliteStore("codex", db).migrate(CODEX_SCHEMA)
    add_symbol(db, id="hub.py::hub::function::1", file="hub.py", name="hub", kind="function")
    # `proven` controls how many edges RESOLVE to a real symbol. The rest carry a bare
    # name, which is what `only_proven` filters out — the branch where the walk's own
    # bound and the filter interact.
    keep = n if proven is None else proven
    for i in range(n):
        sid = f"caller_{i}.py::caller_{i}::function::{i + 1}"
        add_symbol(db, id=sid, file=f"caller_{i}.py", name=f"caller_{i}", kind="function")
        if i < keep:
            add_edge(
                db,
                from_symbol=sid,
                to_name="hub",
                kind="call",
                to_symbol="hub.py::hub::function::1",
                resolution_status="resolved",
            )
        else:
            add_edge(db, from_symbol=sid, to_name="hub", kind="call")
    return db


def test_blast_radius_caps_the_response_and_says_it_did(tmp_path, config, monkeypatch):
    """tenishai/agents#53. A hot symbol's radius exceeded the MCP tool's token ceiling
    outright — measured on techne, `execute` returned 500 nodes / 175,020 characters,
    about 44k tokens — so the reviewer got `Error: result exceeds maximum allowed tokens`
    instead of an impact set. An answer too large to return is the same as no answer,
    except it also costs the call.

    `reachable` orders by (depth, name), so the head is the CLOSEST dependents — which is
    what "what breaks if I change this" wants. The cut is stated, never silent.
    """
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, 120)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)

    r = mcp_server._codex_blast_radius({"symbol_id": "hub.py::hub::function::1"}, config)

    assert r["returned"] == 50, "default cap not applied"
    assert len(r["nodes"]) == 50
    assert r["walked"] == 120, "the pre-cap size must survive into the response"
    assert r["truncated"] is True
    assert "Showing the 50 closest of 120" in r["notice"], (
        "the cap was applied silently — a truncated radius that does not say so reads as "
        "the complete impact set, which is how 'I checked every caller' gets believed"
    )


def test_blast_radius_does_not_flag_truncation_when_it_did_not_truncate(
    tmp_path, config, monkeypatch
):
    """The other half: a small radius must not claim to be cut. A `truncated` flag that
    is always true is the same as no flag."""
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, 3)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)

    r = mcp_server._codex_blast_radius({"symbol_id": "hub.py::hub::function::1"}, config)

    assert r["returned"] == 3
    assert r["walked"] == 3
    assert r["truncated"] is False
    assert "Showing the" not in r.get("notice", "")


def test_blast_radius_clamps_a_nonsense_limit(tmp_path, config, monkeypatch):
    """The LOWER clamp is load-bearing: `limit=0` would otherwise return an empty node
    list, which reads exactly like "nothing depends on this symbol" — the
    absence-shaped answer techne#303 exists to prevent.

    THE UPPER CLAMP IS NOT INDEPENDENTLY TESTABLE and is kept as belt-and-braces, which
    is worth saying rather than pretending: `reachable`'s own `limit` already stops the
    walk at 500, so `limit=100000` truncates a list that never exceeds 500 either way.
    A mutation removing `min(..., _BLAST_WALK_CAP)` passes this whole file. It earns its
    place only if that walk bound is ever raised.
    """
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, 120)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)
    sym = {"symbol_id": "hub.py::hub::function::1"}

    # limit=0 is FALSY, so `args.get("limit") or DEFAULT` coalesces it to the default
    # before any clamp runs. Asserting on 0 therefore tests the `or`, not `max(1, ...)` —
    # a mutation removing the clamp passed that version of this test.
    zero = mcp_server._codex_blast_radius({**sym, "limit": 0}, config)
    assert zero["returned"] == mcp_server._BLAST_DEFAULT_LIMIT

    # NEGATIVE is what reaches the lower clamp. Without it, `rows[:-5]` is a valid slice
    # returning 115 of 120 nodes — so the cap is silently ABANDONED, which is the
    # response-size blowout this whole change exists to prevent, reachable by one
    # mistyped argument. (An earlier version of this assertion said `>= 1`, which 115
    # satisfies; the mutation survived it.)
    neg = mcp_server._codex_blast_radius({**sym, "limit": -5}, config)
    assert neg["returned"] == 1, (
        f"a negative limit was passed straight to a slice: returned {neg['returned']} "
        "nodes, abandoning the cap entirely"
    )

    huge = mcp_server._codex_blast_radius({**sym, "limit": 100_000}, config)
    assert huge["returned"] <= mcp_server._BLAST_WALK_CAP

    small = mcp_server._codex_blast_radius({**sym, "limit": 5}, config)
    assert small["returned"] == 5, "an explicit smaller limit must still be honoured"


def test_blast_radius_reports_the_walk_cap_as_a_floor(tmp_path, config, monkeypatch):
    """PRE-EXISTING AND PREVIOUSLY SILENT. `reachable`'s own limit defaults to 500, so a
    500-node walk has always been indistinguishable from a complete one. Reporting that
    floor as a total is how "covered everything" gets believed — the same defect class
    this whole change is about, one layer down."""
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, mcp_server._BLAST_WALK_CAP + 20)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)

    r = mcp_server._codex_blast_radius({"symbol_id": "hub.py::hub::function::1"}, config)

    assert r["walked"] == mcp_server._BLAST_WALK_CAP
    assert "floor rather than the true size" in r["notice"], (
        "a walk that hit its own bound reported the bound as the radius size"
    )


def test_blast_radius_floor_notice_survives_the_only_proven_filter(tmp_path, config, monkeypatch):
    """PRUDENCE, #722 — A FOURTH SURVIVING MUTATION, and she found it by asking which
    variable the guard reads rather than whether it fires.

    `test_blast_radius_reports_the_walk_cap_as_a_floor` passes whether the guard keys on
    the pre-filter walk or the post-filter count, so it does not discriminate. The
    500-node bound is hit by the WALK; `only_proven` filters AFTERWARDS. Keying on the
    post-filter count silenced the floor notice in precisely the branch where truncation
    does the most damage — that result is the intersection of two independently-capped
    walks.

    The regression this pins is not the empty node list, which is techne#606's and
    predates the cap. It is that the response went from saying NOTHING to affirmatively
    asserting `truncated: false` while missing real dependents: silence became a false
    completeness claim.
    """
    from techne import mcp_server

    # ONLY A FEW PROVEN, so the filter cuts the walk's 500 down to a handful. With the
    # guard on the POST-FILTER count it sees ~5 and stays silent; the walk still hit its
    # bound. A first version of this test made every dependent proven, so the filter
    # removed nothing, the two counts were equal, and the mutation survived.
    db = _codex_db_with_fan_in(tmp_path, mcp_server._BLAST_WALK_CAP + 20, proven=5)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)

    r = mcp_server._codex_blast_radius(
        {"symbol_id": "hub.py::hub::function::1", "only_proven": True}, config
    )
    assert r["walked"] < mcp_server._BLAST_WALK_CAP, (
        "fixture did not produce a post-filter count below the walk bound"
    )

    assert "floor rather than the true size" in r.get("notice", ""), (
        "the walk hit its bound and the response did not say so, because the guard read "
        "the post-filter count instead of the walk"
    )


def test_blast_radius_names_the_depth_of_what_it_cut(tmp_path, config, monkeypatch):
    """ "The head is the closest dependents" is not "all the closest". A hub whose
    dependents are ALL at depth 1 loses 70 DIRECT callers to a cap of 50, and a caller
    reading "showing 50 of 120" cannot tell that from a deep tail being trimmed."""
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, 120)  # every dependent is a direct caller
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)

    r = mcp_server._codex_blast_radius({"symbol_id": "hub.py::hub::function::1"}, config)

    assert "70 more at depth 1" in r["notice"], (
        f"the notice did not say the cut was direct callers: {r['notice']!r}"
    )


def test_blast_radius_passes_its_walk_bound_rather_than_mirroring_it(tmp_path, config, monkeypatch):
    """PRUDENCE, #722(b). `_BLAST_WALK_CAP` was a COPY of `reachable`'s own default, and
    a copy can drift silently — the same defect class as the floor guard. Passing it
    makes the constant true by construction, and this test is what makes that
    observable: lower the constant and the walk must actually shrink. Mirroring cannot
    do that, because the callee would keep using its own 500."""
    from techne import mcp_server

    db = _codex_db_with_fan_in(tmp_path, 300)
    monkeypatch.setattr(mcp_server, "_codex_repo_db", lambda args, cfg: db)
    monkeypatch.setattr(mcp_server, "_BLAST_WALK_CAP", 30)

    r = mcp_server._codex_blast_radius(
        {"symbol_id": "hub.py::hub::function::1", "limit": 200}, config
    )

    assert r["walked"] == 30, (
        f"the walk ignored the module's bound and used the callee's default: {r['walked']}"
    )
    assert "floor rather than the true size" in r.get("notice", "")

    # THE SECOND `reachable()` CALL IS NOT GUARDED BY THIS TEST, and saying so beats
    # pretending. Prudence removed its bound and the whole file still passed. My first
    # attempt to close it asserted `walked <= 30` on an `only_proven` call — which
    # cannot discriminate, because `only_proven` filters the FIRST walk's already-capped
    # rows, so that number is <= 30 either way. Detecting it needs a fixture whose
    # proven nodes sort late enough to fall outside one cap window but not the other.
    # Filed rather than half-built: an assertion that cannot fail is the decoration this
    # suite keeps catching. (Prudence, #722 nit.)
