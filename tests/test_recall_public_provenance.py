"""techne#1102 — techne_recall emits `store`/`role` alongside the deprecated `_store`/`_role`.

The migration is additive: every row that carries the underscore provenance keys
also carries the public ones with the same values, and the underscore keys stay.
Driven end to end through the MCP handler with a real attached store, so the check
holds on the actual tool output rather than on a helper.
"""

from __future__ import annotations

import json

from techne.config import Config, FederationConnectionSpec
from techne.core.database import init_db
from techne.core.knowledge import store

TOPIC = "provenancemarker"


def _db(path):
    init_db(path).close()
    return path


def _recall(cfg) -> list[dict]:
    from techne.mcp_server import handle_request

    resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "techne_recall", "arguments": {"topic": TOPIC}},
        },
        cfg,
    )
    assert "error" not in resp, resp.get("error")
    return json.loads(resp["result"]["content"][0]["text"])["search_results"]


def _cfg(local_db, remote_db=None) -> Config:
    cfg = Config()
    cfg.db_path = local_db
    if remote_db is not None:
        # Name and role differ on purpose, so `role` can't pass by copying `store`.
        cfg.connections = {
            "shared": FederationConnectionSpec(role="static", db_path=str(remote_db))
        }
    return cfg


def test_federated_rows_carry_public_and_deprecated_provenance(tmp_path):
    local_db = _db(tmp_path / "local.db")
    remote_db = _db(tmp_path / "remote.db")
    for i in range(2):
        store(local_db, f"local note {i} about {TOPIC}", type="fact")
        store(remote_db, f"shared note {i} about {TOPIC}", type="fact")

    rows = [r for r in _recall(_cfg(local_db, remote_db)) if r.get("origin") == "memory"]

    stores = {r.get("_store") for r in rows}
    assert stores == {"local", "shared"}, f"fixture: both stores on the page, got {stores}"
    for r in rows:
        # The old keys stay (additive migration) ...
        assert "_store" in r and "_role" in r, r
        # ... and the public ones mirror them exactly.
        assert r["store"] == r["_store"], r
        assert r["role"] == r["_role"], r
    remote = [r for r in rows if r["store"] == "shared"]
    assert remote and all(r["role"] == "static" for r in remote)
    local = [r for r in rows if r["store"] == "local"]
    assert local and all(r["role"] == "local" for r in local)


def test_unfederated_rows_carry_neither_name(tmp_path):
    """No connections: rows are local by construction and never had `_store`/`_role`
    (tiers.tier_of reads absence as local). The alias adds no key the old shape lacked."""
    local_db = _db(tmp_path / "local.db")
    store(local_db, f"local note about {TOPIC}", type="fact")

    rows = _recall(_cfg(local_db))

    assert rows, "fixture: the local note is recalled"
    for r in rows:
        assert ("store" in r) == ("_store" in r)
        assert ("role" in r) == ("_role" in r)
        assert r.get("store") == r.get("_store")
        assert r.get("role") == r.get("_role")


def test_tool_description_marks_underscore_keys_deprecated():
    from techne.mcp_server import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "techne_recall")["description"]
    assert "`store`/`role`" in desc
    assert "`_store`/`_role`" in desc and "DEPRECATED" in desc
