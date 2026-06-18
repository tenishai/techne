"""techne MCP server — agent interface for memory, knowledge, and threads.

Runs as a stdio MCP server. Registers with Claude Code (or any MCP client)
via `techne init`.
"""

import json
import sys

from techne import __version__
from techne.config import Config


def handle_request(request: dict, config: Config) -> dict:
    """Route a JSON-RPC request to the appropriate handler."""
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return _response(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "techne", "version": __version__},
            },
        )

    if method == "ping":
        return _response(req_id, {})

    if method == "tools/list":
        return _response(req_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(tool_name)
        if handler:
            try:
                result = handler(arguments, config)
                return _response(
                    req_id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
                )
            except Exception as e:
                return _response(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                )
        return _error(req_id, -32601, f"Unknown tool: {tool_name}")

    # Per JSON-RPC spec, notifications (requests without an "id" field) must
    # receive no response. Silently ignore all notification/* methods and any
    # other request that arrived without an id.
    if method.startswith("notifications/") or req_id is None:
        return None

    return _error(req_id, -32601, f"Unknown method: {method}")


# --- Tool implementations ---


def _techne_search(args: dict, config: Config) -> dict:
    from techne.core.search import search

    query = args.get("query", "")
    table = args.get("table", "all")
    limit = args.get("limit", 20)
    results = search(config.db_path, query, table=table, limit=limit)
    return {"results": results, "count": len(results)}


def _techne_store(args: dict, config: Config) -> dict:
    from techne.core.knowledge import store

    content = args.get("content")
    if not content:
        raise ValueError("techne_store requires 'content' (non-empty string)")
    entry_id = store(
        config.db_path,
        content=content,
        type=args.get("type", "fact"),
        tags=args.get("tags", ""),
        confidence=args.get("confidence", "medium"),
        source=args.get("source", "agent"),
    )
    return {"id": entry_id, "stored": True}


def _techne_observe(args: dict, config: Config) -> dict:
    """Record an observation from any MCP client — the platform-agnostic capture path.

    This is the MCP equivalent of the Claude Code PostToolUse hook: where Claude
    Code captures via a subprocess hook, a client without subprocess hooks
    (opencode's TS plugin, a standalone MCP setup) captures by calling this tool.
    The client supplies its own `session_id`; techne ensures the session row
    (stamped with the client's `platform`) and writes the observation through the
    same redaction + tag/kind-inference path as the hook. (#57)
    """
    from techne.core.state import record_observation

    return record_observation(
        config,
        args.get("session_id"),
        args.get("content"),
        tool_name=args.get("tool_name", "Observation"),
        platform=args.get("platform"),
        project_path=args.get("project_path", ""),
        agent_identity=args.get("agent_identity", ""),
        tier=args.get("tier", "hot"),
        event_at=args.get("event_at"),
    )


def _techne_threads(args: dict, config: Config) -> list[dict]:
    from techne.core.database import connect

    status_filter = args.get("status", "active")
    limit = args.get("limit", 10)

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            """SELECT id, label, summary, resume_points, status, updated_at
               FROM threads WHERE status = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (status_filter, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _techne_resume(args: dict, config: Config) -> dict:
    from techne.core.state import get_resume_context

    project_path = args.get("project_path", "")
    return get_resume_context(config.db_path, project_path=project_path)


def _techne_status(args: dict, config: Config) -> dict:
    # collect_health (#76) is the single source of truth shared with the
    # SessionStart banner, so the MCP surface and the login banner can't drift.
    # It carries version + package_path (#71: "is the running server current?")
    # plus the editable-vs-installed build hint and a db_ok flag.
    from techne.core.health import collect_health

    return collect_health(config)


def _techne_recall(args: dict, config: Config) -> dict:
    """Retrieve context for a specific topic — searches all layers."""
    from techne.core.identity import list_entries as list_identity
    from techne.core.search import search

    topic = args.get("topic", "")
    results = search(config.db_path, topic, table="all", limit=10)

    # Also search identity
    identity_results = []
    for entry in list_identity(config.db_path):
        if topic.lower() in entry["key"].lower() or topic.lower() in entry["value"].lower():
            identity_results.append(entry)

    return {
        "search_results": results,
        "identity_matches": identity_results[:5],
    }


# --- Tool definitions ---

TOOLS = [
    {
        "name": "techne_search",
        "description": "Search across sessions, threads, knowledge, and identity",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "table": {
                    "type": "string",
                    "enum": ["all", "threads", "observations", "knowledge"],
                    "default": "all",
                },
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "techne_store",
        "description": "Store a new observation, fact, decision, or pattern",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The knowledge to store"},
                "type": {
                    "type": "string",
                    "enum": ["fact", "decision", "pattern", "evaluation", "procedure"],
                    "default": "fact",
                },
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "medium",
                },
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "techne_observe",
        "description": (
            "Record an observation into memory from any client. The platform-agnostic "
            "capture path — call this on each turn/tool-use when no native hook exists. "
            "Supply your own session_id; techne ensures the session and tags its platform."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The client's session identifier (1:1 with a techne session)",
                },
                "content": {"type": "string", "description": "The observation text to record"},
                "tool_name": {
                    "type": "string",
                    "description": "Tool or event name (e.g. Edit, Bash, turn_complete)",
                    "default": "Observation",
                },
                "platform": {
                    "type": "string",
                    "description": "Originating client (e.g. opencode, codex). Stamps the session.",
                },
                "project_path": {"type": "string", "default": ""},
                "agent_identity": {"type": "string", "default": ""},
                "tier": {"type": "string", "enum": ["hot", "warm", "cold"], "default": "hot"},
                "event_at": {
                    "type": "string",
                    "description": "ISO-8601 event time; omit for live capture",
                },
            },
            "required": ["session_id", "content"],
        },
    },
    {
        "name": "techne_threads",
        "description": "List threads — active, recent, or by status",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "completed", "stale"],
                    "default": "active",
                },
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "techne_resume",
        "description": (
            "Get the current resume state — active threads and hot facts "
            "for session start"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "default": ""},
            },
        },
    },
    {
        "name": "techne_status",
        "description": "System health and database statistics",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "techne_recall",
        "description": (
            "Retrieve context for a specific topic — searches all layers "
            "including identity"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to recall context for"},
            },
            "required": ["topic"],
        },
    },
]

TOOL_HANDLERS = {
    "techne_search": _techne_search,
    "techne_store": _techne_store,
    "techne_observe": _techne_observe,
    "techne_threads": _techne_threads,
    "techne_resume": _techne_resume,
    "techne_status": _techne_status,
    "techne_recall": _techne_recall,
}


# --- JSON-RPC helpers ---


def _response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main():
    """Run the MCP server over stdio."""
    config = Config.load()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(request, config)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
