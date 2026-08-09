"""techne MCP server — agent interface for memory, knowledge, and threads.

Runs as a stdio MCP server. Registers with Claude Code (or any MCP client)
via `techne init`.
"""

import json
import logging
import sys

from techne import __version__
from techne.config import Config

log = logging.getLogger(__name__)

# Server-level guidance surfaced to the agent at MCP initialize (the protocol's
# `instructions` field). This is techne's one chance to teach an agent the memory
# *loop* — not just what each tool does, but when to reach for it. Keep it tight;
# it ships in the agent's context on every session that connects this server.
SERVER_INSTRUCTIONS = (
    "techne is your persistent memory across sessions. Use it as a loop, not a filing cabinet.\n"
    "\n"
    "1. RECALL before you assume. Starting a task — or about to rely on a belief about this "
    "project, person, or past decision — call techne_recall(topic) (techne_resume at session "
    "start). Don't answer from assumption when memory may already hold it.\n"
    "2. STORE when you learn something durable. Call techne_store the moment you learn a fact "
    "that will matter later, make or are given a decision, receive user feedback or a "
    "correction, or finish reasoning you'd hate to redo. Pick the type (fact/decision/"
    "evaluation/procedure) and store the conclusion, not the transcript.\n"
    "3. SEARCH for breadth. techne_search spans sessions/threads/knowledge; techne_recall is "
    "focused, topic-scoped context including the identity layer.\n"
    "4. At SESSION END, consolidate. Run techne_reflect_emit, answer each prompt with YOUR OWN "
    "model, then techne_reflect_ingest — turning the session into durable candidates. Same "
    "emit/ingest shape for techne_consolidate_* (dedup) — then techne_faithfulness_emit/ingest to "
    "check the merge/supersede candidates consolidate just staged: does the replacement preserve "
    "each source's load-bearing claims before the operator reviews (flags a warning, never blocks). "
    "Also techne_dream_emit/ingest (recombine — "
    "speculative; surfaces non-obvious A+B→C connections as low-confidence candidates), and "
    "techne_memory_claims_* (evidence-check). Before asserting completed work (committed, tested, "
    "wrote-file), call techne_check_evidence — it verifies your claims against the observation "
    "ledger. These use your model — no API key.\n"
    "5. COORDINATE with peer sessions (techne#376). Other live Claude sessions on this machine "
    "are shown in the session-start banner and pushed to you as a 📬 in-context nudge — you can "
    "talk to them: techne_send(body, to='coord') broadcasts to every live session; techne_inbox "
    "reads unread + who's live; techne_mark_read clears them. REFLEX, no prompting: announce on "
    "'coord' when you claim a git branch/worktree or are about to commit/push in a shared repo, "
    "and reply when a peer messages you — settle collisions between yourselves, not via the human.\n"
    "\n"
    "Honesty: recall results tagged `freshness` (modified/moved/deleted) or marked uncertain "
    "mean 'verify before relying', not fact. Stored knowledge is operator-owned — prefer "
    "proposing candidates over overwriting.\n"
    "Domains are opt-in and queried deliberately, not part of personal recall: pattern_search "
    "(architectural patterns), codex_search/codex_blast_radius (code graph; needs "
    "`techne codex index` first). Reach for them when the task is about that domain.\n"
    "Capture is automatic where a host hook exists; otherwise call techne_observe each turn."
)


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
                "instructions": SERVER_INSTRUCTIONS,
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
    results = search(
        config.db_path,
        query,
        table=table,
        limit=limit,
        half_life_days=config.knowledge_confidence_half_life_days,
    )
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
        anchor=args.get("anchor"),
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


def _resolve_session_id(args: dict) -> str | None:
    """Identity for the coordination mailbox (techne#376): the caller's self-asserted
    session_id, else the MCP server's own CLAUDE_CODE_SESSION_ID env — the server is
    spawned per-session by Claude Code, so its env carries this session's id. No
    token: the local techne.db is the whole trust boundary."""
    import os

    return args.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")


def _techne_send(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError("techne_send: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)")
    body = args.get("body")
    if not body:
        raise ValueError("techne_send requires 'body' (non-empty)")
    to = args.get("to") or coord.DEFAULT_CHANNEL
    mid = coord.send(
        config.db_path, sid, body, to=to,
        thread_id=args.get("thread_id"), reply_to=args.get("reply_to"),
    )
    return {"id": mid, "from_session": sid, "to": to}


def _techne_coord_threads(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError("techne_coord_threads: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)")
    return {
        "session_id": sid,
        "threads": coord.threads(config.db_path, sid, limit=args.get("limit", 20)),
    }


def _techne_inbox(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError("techne_inbox: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)")
    msgs = coord.inbox(config.db_path, sid, mark_read=bool(args.get("mark_read", False)))
    return {
        "session_id": sid,
        "unread": len(msgs),
        "messages": msgs,
        "live_sessions": coord.live_sessions(exclude_session=sid),
    }


def _techne_mark_read(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError("techne_mark_read: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)")
    n = coord.mark_read(config.db_path, sid, args.get("message_ids"))
    return {"marked_read": n}


def _fold_in_documents(config: Config, topic: str, memory_results: list[dict]) -> list[dict]:
    """Label memory rows ``origin='memory'`` and append the top library documents
    (``origin='library'``) so a recall request returns both, each clearly sourced
    (techne#340). Best-effort — ``recall_documents`` swallows its own errors, so a
    missing/broken librarian.db degrades to memory-only."""
    from techne.core.librarian import recall_documents

    for r in memory_results:
        r.setdefault("origin", "memory")
    return memory_results + recall_documents(config, topic)


def _effective_connections(config: Config) -> dict:
    """config.connections plus the local base Archivist as a `team` connection when
    not standalone (techne#346 PR2) — a non-standalone instance recalls over its own
    archivist.db without manual config. setdefault so an explicit config entry wins."""
    from techne.core.archivist import ARCHIVIST_CONNECTION_NAME, archivist_connection

    conns = dict(config.connections)
    ac = archivist_connection(config)
    if ac is not None:
        conns.setdefault(ARCHIVIST_CONNECTION_NAME, ac)
    return conns


def _techne_recall(args: dict, config: Config) -> dict:
    """Retrieve context for a specific topic — searches all layers.

    When config.connections is non-empty (techne#222 federation), fans out to
    team/static stores, deduplicates, detects cross-store contradictions, and
    returns a merged ranked set. When empty (the common case), returns local-only
    with an identical shape — no behaviour change for unconfigured instances.
    """
    from techne.core.anchor import annotate_freshness
    from techne.core.identity import list_entries as list_identity
    from techne.core.knowledge import annotate_uncertain
    from techne.core.search import search

    topic = args.get("topic", "")

    # Effective connections = configured remotes + the local base Archivist as a
    # `team` store when not standalone (techne#346 PR2), so a non-standalone
    # instance recalls over its own archivist.db without manual config.
    connections = _effective_connections(config)

    # Helper: shared identity search (same in both paths)
    def _identity_matches() -> list[dict]:
        results_i = []
        for entry in list_identity(config.db_path):
            if topic.lower() in entry["key"].lower() or topic.lower() in entry["value"].lower():
                results_i.append(entry)
        return results_i[:5]

    # ---- Noop gate (techne#222 §2): no connections → local-only, unchanged shape ----
    if not connections:
        # techne#216: DEFAULT recall excludes soft-archived knowledge.
        results = search(
            config.db_path,
            topic,
            table="all",
            limit=10,
            half_life_days=config.knowledge_confidence_half_life_days,
            include_archived=False,
        )
        # Graded staleness (techne#90): grade anchored knowledge results against the
        # current working tree and attach a freshness tag. Non-anchored results are
        # untouched; this fires only here, on the surfacing path — never on
        # touch/relevance updates.
        annotate_freshness(config.db_path, results)
        # Dependency uncertainty (techne#117): a row whose basis was superseded is
        # still returned (it's live) but must not read as confidently current —
        # attach an `uncertainty` marker. Distinct key from `freshness`; a row can
        # carry both.
        annotate_uncertain(config.db_path, results)
        return {
            "search_results": _fold_in_documents(config, topic, results),
            "identity_matches": _identity_matches(),
        }

    # ---- Federation path (techne#222) ----
    from techne.core.federation import (
        _cross_store_dedup,
        detect_cross_store_contradictions,
        fan_out_search,
        merge_and_rank,
    )

    # Local recall MUST always succeed. Compute it first, standalone, so that a
    # failure anywhere in the remote fan-out / dedup / detect / merge path degrades
    # to local-only rather than losing the local results entirely. fan_out_search
    # already guards each remote's open+query internally (one bad remote → skipped),
    # but the pooling/detection steps below are wrapped too as defence in depth.
    local_results = search(
        config.db_path,
        topic,
        table="all",
        limit=10,
        half_life_days=config.knowledge_confidence_half_life_days,
        include_archived=False,
    )
    for r in local_results:
        r["_store"] = "local"
        r["_role"] = "local"

    merged = local_results
    conflicts: list = []
    try:
        # Pass the precomputed local_results so fan_out_search skips the local
        # search entirely (techne#254 — avoids the 2× local search cost).  The
        # rows are already tagged _store/role above; fan_out_search will NOT
        # re-tag them.  fan_out_search returns our own list as its first element,
        # but we already hold it — we only need remote_results here.
        _, remote_results = fan_out_search(
            config.db_path,
            connections,
            topic,
            limit=10,
            half_life_days=config.knowledge_confidence_half_life_days,
            recency_weight=config.search.recency_weight,
            bm25_weight=config.search.bm25_weight,
            local_results=local_results,
        )

        # Step B — content-hash dedup before the contradiction check
        remote_deduped = _cross_store_dedup(local_results, remote_results)

        # Step C — cross-store contradiction detection (read-only; returns prompts)
        conflicts = detect_cross_store_contradictions(
            config.db_path,
            local_results,
            remote_deduped,
            max_candidates=config.conflict.max_comparison_candidates,
        )

        # Step D — merge + rank (per-type BM25 normalisation handles cross-store, #56)
        merged = merge_and_rank(
            local_results,
            remote_deduped,
            limit=10,
            recency_weight=config.search.recency_weight,
            bm25_weight=config.search.bm25_weight,
            half_life_days=config.knowledge_confidence_half_life_days,
        )
    except Exception:
        log.warning(
            "federation: fan-out/merge failed — degrading to local-only recall", exc_info=True
        )
        merged = local_results
        conflicts = []

    # Freshness + uncertainty annotations: LOCAL rows ONLY. Remote and local knowledge
    # ids are both small sequential ints and collide as the common case — annotating a
    # remote row against the local db by its (colliding) id would stamp a team fact with
    # YOUR file provenance (anchor_path/freshness) and a spurious `uncertain` tag. The
    # `_store == "local"` discriminator keeps annotation on rows the local db actually owns.
    local_only = [r for r in merged if r.get("_store") == "local"]
    annotate_freshness(config.db_path, local_only)
    annotate_uncertain(config.db_path, local_only)

    out: dict = {
        "search_results": _fold_in_documents(config, topic, merged),
        "identity_matches": _identity_matches(),
    }

    # Step E — surface conflict prompts; recall itself does NOT write
    if conflicts:
        out["federation_conflicts"] = conflicts
        out["_federation_note"] = (
            "Cross-store contradictions detected. For each item in federation_conflicts: "
            "answer the prompt field using your own model (JSON verdict array), then call "
            "techne_federation_conflict_ingest with the result to stage a reviewable candidate."
        )

    return out


# --- Reminder helpers (#207) ---

import re as _re


def _parse_due_at(s: str) -> str:
    """Parse a due_at string and return a UTC ISO timestamp ("YYYY-MM-DD HH:%M:%S").

    Accepts two forms:
    - Relative offset: ``+<N><unit>`` where unit ∈ d/h/m (days/hours/minutes).
      Examples: ``+1d``, ``+3h``, ``+30m``.  Offset is computed from
      ``datetime.now(timezone.utc)`` at call time.
    - Absolute ISO timestamp: ``YYYY-MM-DD HH:MM:SS``.  Assumed to be already
      UTC — the caller is responsible for supplying UTC values. (techne#207)

    Raises ValueError for any input that matches neither form.
    """
    from datetime import datetime, timedelta, timezone

    s = s.strip()

    # Relative offset: +<N><unit>
    m = _re.fullmatch(r"\+(\d+)([dhm])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise ValueError(f"_parse_due_at: offset must be positive, got {s!r}")
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")

    # Absolute ISO timestamp: YYYY-MM-DD HH:MM:SS
    try:
        datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError(
            f"_parse_due_at: unrecognised format {s!r}. "
            "Use '+<N>d/h/m' (e.g. '+1d', '+3h', '+30m') "
            "or 'YYYY-MM-DD HH:MM:SS' (UTC)."
        )
    return s


# --- Tool definitions ---

TOOLS = [
    {
        "name": "techne_search",
        "description": (
            "Call this when you need breadth across history — search across sessions, "
            "threads, knowledge, and identity. (For focused topic context, prefer "
            "techne_recall.)"
        ),
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
        "description": (
            "Call this when you learn a durable fact, make or are given a decision, get "
            "user feedback or a correction, or finish reasoning you'd hate to redo — to "
            "store a durable personal-memory entry (fact, decision, evaluation, procedure). "
            "Store the conclusion, not the transcript. Architectural patterns go to the "
            "Architect store via `techne architect ingest`, not here (techne#137)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The knowledge to store"},
                "type": {
                    "type": "string",
                    "enum": ["fact", "decision", "evaluation", "procedure"],
                    "default": "fact",
                },
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "medium",
                },
                "source": {"type": "string", "default": "agent"},
                "anchor": {
                    "type": "string",
                    "description": (
                        "Optional evidence anchor pinning this memory to a repo file. "
                        "Pass a path, or 'path::symbol' to also record the symbol. "
                        "Captures the file's content hash + git HEAD sha so recall can "
                        "grade staleness (unchanged/modified/moved/deleted). Omit for "
                        "behavioral/relational memory — those are never staleness-checked."
                    ),
                },
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
            "Get the current resume state — active threads and hot facts for session start"
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
            "Call this before relying on an assumption about this project, person, or past "
            "decision — to retrieve context for a specific topic across all layers "
            "including identity. Git-anchored knowledge results that need "
            "attention carry an explicit `freshness` tag (modified/moved/"
            "deleted/unknown) — spend verification effort only on those rather "
            "than re-checking every result by hand. An entry with no `freshness` "
            "tag needs no file check: it is either non-anchored or "
            "anchored-and-unchanged (anchored entries carry an `anchor_path` field)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to recall context for"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "codex_search",
        "description": (
            "Search Codex symbols (functions, classes, methods) by name or signature. "
            "Requires `techne codex index <repo>` to have been run first. Scoped to one "
            "repo (db-per-repo, techne#209): pass `repo` to pick it, else the server's "
            "working directory's repo is used. Experimental: the index is built manually "
            "and may be stale — re-run `techne codex index` after code changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Symbol name or signature fragment to search",
                },
                "repo": {
                    "type": "string",
                    "description": "Path within the repo to search (default: server cwd)",
                },
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codex_blast_radius",
        "description": (
            "Compute the transitive reachability (blast radius) of a Codex symbol. "
            "direction defaults to `in`=what reaches this symbol (dependents) — the "
            "\"what breaks if I change this\" impact set; `out`=what this symbol reaches "
            "(dependencies). Uses recursive CTE BFS over the call/import graph. "
            "tests/fixtures/** nodes are excluded by default (name-collision noise in the "
            "v1 name-based edge model; set include_fixtures=true to keep them). Scoped to "
            "one repo (db-per-repo, techne#209): pass `repo` to pick it, else the "
            "server's working directory's repo is used. Experimental: the index is built "
            "manually and may be stale — re-run `techne codex index` after code changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Symbol id (file::name::kind::line) from codex_search",
                },
                "repo": {
                    "type": "string",
                    "description": "Path within the repo to query (default: server cwd)",
                },
                "depth": {"type": "integer", "default": 5},
                "direction": {
                    "type": "string",
                    "enum": ["in", "out"],
                    "default": "in",
                    "description": (
                        "in=dependents (what breaks if you change this — the blast radius), "
                        "out=dependencies (what this reaches)"
                    ),
                },
                "include_fixtures": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include tests/fixtures/** nodes (excluded by default; techne#325)."
                    ),
                },
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "pattern_search",
        "description": (
            "Search the Architect's architectural-pattern corpus (architect.db) — design "
            "patterns lifted from other projects' code, the team-shared 'what's worth "
            "stealing' store. Optionally scope to patterns that apply to a project "
            "(target=techne|cabal|limenal|agent-infra); with a target and no query, lists "
            "all patterns for that project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text query over name + body"},
                "target": {
                    "type": "string",
                    "description": "Restrict to patterns applying to this project",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "pattern_related",
        "description": (
            "Given a pattern id (from pattern_search), return the patterns it links to and "
            "from via the [[wikilink]] relation graph — outbound (incl. unresolved/dangling "
            "refs) and inbound (resolved)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern_id": {"type": "string", "description": "Pattern id from pattern_search"},
            },
            "required": ["pattern_id"],
        },
    },
    {
        "name": "techne_reflect_emit",
        "description": (
            "Consolidate a finished session into durable memory using YOUR OWN model "
            "(no API key, no sampling). techne renders one reflection prompt per thread of "
            "work; answer each `prompt` in the returned batch as a JSON candidate array "
            "(scope/type/content/tags/confidence), then call techne_reflect_ingest once per "
            "thread with your answer. Run this when wrapping up a chunk of work, or when the "
            "SessionStart banner flags a session with unreflected threads. Defaults to the "
            "most recent session with unreflected work (skipping empty/already-emitted ones)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": (
                        "Session to reflect on (default: most recent). Prefer passing an "
                        "explicit id — e.g. the one from the SessionStart nudge — when "
                        "consolidating a FINISHED session, so a freshly-started session "
                        "isn't reflected while still empty."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max threads to emit prompts for. Default: reflect.emit_max_threads "
                        "(25). The batch is also trimmed by size to stay under the transport "
                        "cap; a truncated response sets `truncated` + `remaining`."
                    ),
                },
            },
        },
    },
    {
        "name": "techne_reflect_ingest",
        "description": (
            "Feed back one thread's reflection answer produced by techne_reflect_emit. "
            "`response` is the JSON candidate array (as a string) you wrote for that thread's "
            "`prompt`; `thread_id` and `prompt_hash` come from the emit batch item. Proposals "
            "land in knowledge_candidates for review (techne reflect review). Call once per "
            "answered thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "From the emit batch item."},
                "thread_id": {"type": "string", "description": "From the emit batch item."},
                "prompt_hash": {"type": "string", "description": "From the emit batch item."},
                "response": {
                    "type": "string",
                    "description": "Your JSON candidate array (string) answering the prompt.",
                },
            },
            "required": ["session_id", "thread_id", "response"],
        },
    },
    {
        "name": "techne_consolidate_emit",
        "description": (
            "Find duplicate/overlapping knowledge entries and judge them with YOUR OWN "
            "model (techne#108 'sleep' — fights memory bloat; no API key, no sampling). "
            "Returns clusters; for each, answer the `prompt` with a JSON verdict "
            "(merge into one, or keep separate), then call techne_consolidate_ingest with "
            "the same cluster_hash + target_ids. Proposals land in the review queue — "
            "never auto-applied. Run when wrapping up or when knowledge feels cluttered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_clusters": {
                    "type": "integer",
                    "description": "Cap on clusters returned (default 20).",
                },
            },
        },
    },
    {
        "name": "techne_consolidate_ingest",
        "description": (
            "Feed back one cluster's consolidation verdict from techne_consolidate_emit. "
            "`response` is your JSON verdict object; `target_ids` + `cluster_hash` come "
            "from the emit batch item. A 'merge' verdict queues a proposal for review "
            "(promoting it collapses the rows); a 'keep' verdict writes nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The cluster's knowledge ids (from the emit batch item).",
                },
                "cluster_hash": {"type": "string", "description": "From the emit batch item."},
                "response": {
                    "type": "string",
                    "description": "Your JSON verdict object answering the cluster prompt.",
                },
            },
            "required": ["target_ids", "cluster_hash", "response"],
        },
    },
    {
        "name": "techne_dream_emit",
        "description": (
            "Recombine pass (techne#108 'dream'): find non-obvious connections across "
            "stored knowledge using YOUR OWN model — speculative, low-confidence, "
            "review-gated, abstention-first. No API key, no sampling. Returns groups; "
            "for each, answer the `prompt` with a JSON verdict (connect or none), then "
            "call techne_dream_ingest with the same group_hash + group_ids. 'none' is "
            "the easy default — most groups will NOT yield a real connection. Proposals "
            "land in the review queue as low-confidence candidates — never auto-applied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_groups": {
                    "type": "integer",
                    "description": "Cap on groups returned (default 10).",
                },
            },
        },
    },
    {
        "name": "techne_dream_ingest",
        "description": (
            "Feed back one group's dream verdict from techne_dream_emit. "
            "`response` is your JSON verdict object (verdict='connect'|'none'); "
            "`group_ids` + `group_hash` come from the emit batch item. A 'connect' "
            "verdict queues a low-confidence pending candidate for human review "
            "(the basis facts stay live — this is a new derived insight, not a merge); "
            "a 'none' verdict writes a keep-memory marker so the group is never "
            "re-judged."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The group's knowledge ids (from the emit batch item).",
                },
                "group_hash": {"type": "string", "description": "From the emit batch item."},
                "response": {
                    "type": "string",
                    "description": "Your JSON verdict object answering the group prompt.",
                },
            },
            "required": ["group_ids", "group_hash", "response"],
        },
    },
    {
        "name": "techne_memory_claims_emit",
        "description": (
            "Extract claims from curated memory and judge each against the deed-log using "
            "YOUR OWN model (techne#107 PR3 — no API key, no sampling). "
            "FIDELITY CAVEAT: the deed-log contains truncated ~500-char summaries — file "
            "paths are reliable but claim text is lossy. 'uncorroborated' means no deed "
            "evidence found, NOT that the claim is false. "
            "Returns the emit dict (prompt + metadata); answer the prompt with your model "
            "and feed the response to techne_memory_claims_ingest. Output is ephemeral — "
            "nothing is persisted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_days": {
                    "type": "integer",
                    "description": "Only consider deeds from the last N days (default: all).",
                },
            },
        },
    },
    {
        "name": "techne_memory_claims_ingest",
        "description": (
            "Feed back the claim-judgement JSON array from techne_memory_claims_emit. "
            "`response` is your JSON array of claim objects (each with `claim`, `status`, "
            "`evidence`). Returns an ephemeral report with per-claim verdicts and counts "
            "(corroborated / uncorroborated / contradicted). Nothing is persisted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt_hash": {
                    "type": "string",
                    "description": "From the emit result (advisory; for round-trip tracking).",
                },
                "response": {
                    "type": "string",
                    "description": "Your JSON claim-judgement array answering the emit prompt.",
                },
            },
            "required": ["response"],
        },
    },
    {
        "name": "techne_conflict_emit",
        "description": (
            "Contradiction detection pass (techne#213 D1 — coherence layer): find live "
            "knowledge pairs that share entities/tags + same type and may contradict each "
            "other. Structural selection — NOT similarity (cosine AUROC ~0.59 for "
            "contradictions). PREREQUISITE: detection requires a SHARED TAG token between "
            "two entries (or an overlapping content word when an entry is untagged) — "
            "consistent tagging is a prerequisite for contradiction recall, and untagged "
            "antonyms in different vocabulary (e.g. 'use postgres' vs 'we switched to "
            "sqlite' with no shared tag) will NOT be compared. Returns prompts for YOUR "
            "OWN model to judge; answer each with a JSON verdict array "
            "(SUPERSEDE/UPDATE/NOOP/DISTINCT) and call techne_conflict_ingest. SUPERSEDE "
            "retires the older entry (by event_at) into the newer one; UPDATE retires the "
            "new refinement into the canonical existing entry. Both LINK existing rows via "
            "superseded_by — no duplicate row minted. Proposals land in the review queue — "
            "never auto-applied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer",
                    "description": (
                        "Cap on conflict groups returned (default from config, typically 20)."
                    ),
                },
            },
        },
    },
    {
        "name": "techne_conflict_ingest",
        "description": (
            "Feed back one conflict verdict from techne_conflict_emit (techne#213). "
            "`response` is your JSON verdict array; `focus_id`, `comparison_ids`, and "
            "`check_hash` come from the emit batch item. SUPERSEDE → pending 'retire' "
            "candidate linking the older entry (by event_at) into the newer survivor "
            "(no row minted; operator reviews before promote). UPDATE → pending 'retire' "
            "candidate folding the new refinement into the canonical existing entry, with "
            "the merged wording kept for optional edit. NOOP/DISTINCT → discarded markers "
            "(group not re-judged unless its membership changes). Nothing auto-applies — "
            "sovereignty preserved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "focus_id": {
                    "type": "integer",
                    "description": "The focus entry id (from the emit batch item).",
                },
                "comparison_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The comparison entry ids (from the emit batch item).",
                },
                "check_hash": {
                    "type": "string",
                    "description": "Stable group identifier (from the emit batch item).",
                },
                "response": {
                    "type": "string",
                    "description": "Your JSON verdict array answering the conflict prompt.",
                },
            },
            "required": ["focus_id", "comparison_ids", "check_hash", "response"],
        },
    },
    {
        "name": "techne_federation_conflict_ingest",
        "description": (
            "Feed back your verdict on a cross-store contradiction surfaced by techne_recall "
            "(techne#222). Use the fields from a federation_conflicts item returned by "
            "techne_recall: focus_content, focus_store, focus_event_at, comparison_ids, "
            "check_hash. Answer the prompt with a JSON verdict array (same format as "
            "techne_conflict_ingest) and pass it as response. "
            "SUPERSEDE + team newer → pending 'retire' candidate targeting the LOCAL row "
            "(operator reviews before promote). "
            "SUPERSEDE + local newer → discarded informational marker (team is stale; "
            "no push to the foreign store — sovereignty preserved). "
            "NOOP/DISTINCT → discarded markers. Nothing auto-applies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "focus_content": {
                    "type": "string",
                    "description": "The remote row's content (federation_conflicts.focus_content).",
                },
                "focus_store": {
                    "type": "string",
                    "description": "The remote connection name (federation_conflicts.focus_store).",
                },
                "focus_event_at": {
                    "type": "string",
                    "description": (
                        "The remote row's event_at for the temporal tiebreak "
                        "(federation_conflicts.focus_event_at)."
                    ),
                },
                "comparison_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "LOCAL knowledge row ids to compare against "
                        "(federation_conflicts.comparison_ids)."
                    ),
                },
                "check_hash": {
                    "type": "string",
                    "description": "Anti-re-proposal hash (federation_conflicts.check_hash).",
                },
                "response": {
                    "type": "string",
                    "description": "Your JSON verdict array answering the conflict prompt.",
                },
            },
            "required": [
                "focus_content",
                "focus_store",
                "comparison_ids",
                "check_hash",
                "response",
            ],
        },
    },
    {
        "name": "techne_check_evidence",
        "description": (
            "Call this before asserting you did something (committed, ran tests, wrote a "
            "file) to verify the claim against the observation ledger — enforces 'no "
            "unbacked word'. Returns per-claim backed/unbacked/unverifiable verdicts by "
            "parsing what the agent actually DID (Write/Edit/Bash tool records) in the "
            "given session window. Advisory — does not block; use unbacked_count > 0 as "
            "the flag. Structured claims only; free-text extraction is out of scope. NOTE: "
            "commit/tests_green/ran_action come from Bash, which is off by default — those "
            "return 'unverifiable' (not 'unbacked') unless capture_bash is enabled."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session to query observations for.",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 timestamp. When given, only observations with "
                        "timestamp > since are checked (turn-boundary approximation)."
                    ),
                },
                "claims": {
                    "type": "array",
                    "description": (
                        "Structured claims to check. Each object must have a 'type' field. "
                        "Supported types: 'completion'/'did_work', 'commit' (optional 'sha'), "
                        "'tests_green', 'ran_action' (required 'match'), "
                        "'wrote_file' (required 'path')."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["session_id", "claims"],
        },
    },
    {
        "name": "techne_faithfulness_emit",
        "description": (
            "Faithfulness gate (techne#217 / coherence-layer D4): find pending merge/"
            "supersede candidates whose replacement content has NOT yet been checked for "
            "faithfulness to their source entries, and emit a prompt for each so you can "
            "judge it with YOUR OWN model (no API key, no sampling). "
            "Returns prompts; for each, answer the `prompt` and call "
            "techne_faithfulness_ingest with the same `candidate_id`. "
            "A 'faithful' verdict leaves the candidate unchanged. An 'unfaithful' verdict "
            "annotates the candidate with a warning visible at `reflect review` so the "
            "operator can decide before promotion. The gate is ADVISORY — a flagged "
            "candidate is never blocked or auto-discarded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_candidates": {
                    "type": "integer",
                    "description": "Cap on candidates returned (default 50).",
                },
            },
        },
    },
    {
        "name": "techne_faithfulness_ingest",
        "description": (
            "Feed back one candidate's faithfulness verdict from techne_faithfulness_emit. "
            "`response` is your JSON verdict object (verdict='faithful'|'unfaithful', "
            "dropped_claims, reason). `candidate_id` comes from the emit batch item. "
            "An 'unfaithful' verdict annotates the candidate row with a warning visible "
            "at `techne reflect review` — it does NOT change the candidate status. "
            "Sovereignty invariant: the operator still decides whether to promote or "
            "discard; the gate only adds the annotation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "integer",
                    "description": "The candidate to annotate (from the emit batch item).",
                },
                "response": {
                    "type": "string",
                    "description": "Your JSON verdict object answering the faithfulness prompt.",
                },
            },
            "required": ["candidate_id", "response"],
        },
    },
    {
        "name": "techne_reminder_add",
        "description": (
            "Arm a durable reminder that will surface in future sessions via the "
            "PostToolUse hook. Provide a note (what to remember) and a due_at time. "
            "due_at accepts a relative offset (+1d, +3h, +30m) or an absolute ISO "
            "timestamp in UTC ('YYYY-MM-DD HH:MM:SS'). Returns the new reminder id "
            "and the normalised UTC due_at. (techne#207)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Human-readable reminder text (what to remember).",
                },
                "due_at": {
                    "type": "string",
                    "description": (
                        "When the reminder becomes due. Relative: '+<N>d/h/m' (e.g. "
                        "'+1d', '+3h', '+30m'). Absolute: 'YYYY-MM-DD HH:MM:SS' "
                        "(UTC). Absolute values are assumed to be UTC — caller is "
                        "responsible for UTC consistency."
                    ),
                },
            },
            "required": ["note", "due_at"],
        },
    },
    {
        "name": "techne_reminder_list",
        "description": (
            "List all ACTIVE reminders (including future ones) for in-session "
            "management. Returns id, note, due_at, and last_surfaced_at for each. "
            "Use techne_reminder_dismiss to dismiss a reminder. (techne#207)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "techne_reminder_dismiss",
        "description": (
            "Dismiss a reminder by id so it no longer surfaces. The row is kept as "
            "status='done' for audit; it will not appear in future hook injections. "
            "(techne#207)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The reminder id returned by techne_reminder_add or techne_reminder_list.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "techne_send",
        "description": (
            "Send a message to another Claude Code session on this machine — coordinate "
            "without a human router (techne#376). Post to the broadcast channel 'coord' "
            "(default; every live session sees it) or address a specific session_id. Your "
            "identity is taken from the server env — you don't pass it. REFLEX: announce "
            "here when you claim a git branch/worktree or are about to commit, and reply "
            "when you have unread from a peer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "The message text."},
                "to": {
                    "type": "string",
                    "description": "Channel name ('coord', the default), a live session's display name (e.g. 'claude_2'), or a target session_id.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional thread id to group a back-and-forth.",
                },
                "reply_to": {
                    "type": "integer",
                    "description": "Optional message id you're replying to — threads this message with it (see techne_coord_threads).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Usually omitted — defaults to this session (CLAUDE_CODE_SESSION_ID). Pass only from a client without that env.",
                },
            },
            "required": ["body"],
        },
    },
    {
        "name": "techne_coord_threads",
        "description": (
            "List coordination conversation threads you're party to (messages grouped by "
            "thread), most-recently-active first. Reply into one with "
            "techne_send(reply_to=<message id>). (techne#376)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max threads to return (default 20)."},
                "session_id": {
                    "type": "string",
                    "description": "Usually omitted — defaults to this session (CLAUDE_CODE_SESSION_ID).",
                },
            },
        },
    },
    {
        "name": "techne_inbox",
        "description": (
            "Read unread coordination messages addressed to you or your channels, plus who "
            "else is live (from Claude Code's session registry). Oldest first. Pass "
            "mark_read=true to mark them read as you read them. (techne#376)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mark_read": {
                    "type": "boolean",
                    "description": "Mark the returned messages read (default false).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Usually omitted — defaults to this session (CLAUDE_CODE_SESSION_ID).",
                },
            },
        },
    },
    {
        "name": "techne_mark_read",
        "description": (
            "Mark coordination messages read so they stop surfacing. With 'message_ids' "
            "marks exactly those; otherwise marks everything currently unread. (techne#376)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Specific message ids to mark read. Omit to mark all unread.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Usually omitted — defaults to this session (CLAUDE_CODE_SESSION_ID).",
                },
            },
        },
    },
]


def _codex_repo_db(args: dict, config: Config):
    """Resolve the per-repo codex db for an MCP codex call (techne#209).

    ``repo`` arg (a path within the repo) if given, else the server's cwd.
    Returns the db Path, or None when there's no indexed repo there — callers
    return an empty result with a hint rather than erroring.
    """
    import os

    from techne.core.codex import resolve_repo_db_for_query

    return resolve_repo_db_for_query(config, args.get("repo") or os.getcwd())


_NO_REPO_HINT = (
    "no codex index for the active repo — run `techne codex index <repo>`, "
    "or pass `repo` to select an indexed repo."
)


def _codex_search(args: dict, config: Config) -> dict:
    """FTS over Codex symbols, scoped to the active repo (db-per-repo, techne#209)."""
    from techne.core.codegraph import annotate_freshness, search_symbols

    query = args.get("query", "")
    limit = args.get("limit", 20)
    db_path = _codex_repo_db(args, config)
    if db_path is None:
        return {"results": [], "count": 0, "notice": _NO_REPO_HINT}
    hits = annotate_freshness(db_path, search_symbols(db_path, query, limit=limit))
    stale_count = sum(1 for h in hits if h.get("freshness") in ("stale", "missing"))
    result: dict = {"results": hits, "count": len(hits)}
    if stale_count:
        result["notice"] = (
            f"{stale_count} result(s) may be outdated — re-run `techne codex index` to refresh."
        )
    return result


def _codex_blast_radius(args: dict, config: Config) -> dict:
    """Transitive reachability for a symbol, scoped to the active repo (techne#209)."""
    from techne.core.codegraph import (
        SymbolResolutionError,
        annotate_freshness,
        reachable,
        resolve_symbol_id,
    )

    symbol_id = args.get("symbol_id", "")
    depth = args.get("depth", 5)
    direction = args.get("direction", "in")
    include_fixtures = args.get("include_fixtures", False)
    db_path = _codex_repo_db(args, config)
    if db_path is None:
        return {
            "symbol_id": symbol_id, "direction": direction, "depth": depth,
            "nodes": [], "notice": _NO_REPO_HINT,
        }
    # Resolve a bare name / reject an unresolvable id, so a mistyped argument
    # returns an explicit error instead of an empty node list that reads as
    # "nothing reachable" (techne#303 — the MCP twin of the CLI fix in #300).
    try:
        symbol_id = resolve_symbol_id(db_path, symbol_id)
    except SymbolResolutionError as exc:
        result = {
            "symbol_id": args.get("symbol_id", ""),
            "direction": direction, "depth": depth,
            "nodes": [], "error": str(exc),
        }
        if exc.candidates:
            result["candidates"] = [
                {"id": c["id"], "file": c["file"]} for c in exc.candidates[:10]
            ]
            # Signal the cap rather than silently truncating (parity with the
            # CLI's "... and N more"; the total is already in the error text).
            if len(exc.candidates) > 10:
                result["candidates_truncated"] = True
        return result
    raw = reachable(
        db_path, symbol_id, direction=direction, max_depth=depth,
        exclude_fixtures=not include_fixtures,
    )
    rows = annotate_freshness(db_path, raw)
    stale_count = sum(1 for r in rows if r.get("freshness") in ("stale", "missing"))
    result: dict = {"symbol_id": symbol_id, "direction": direction, "depth": depth, "nodes": rows}
    if stale_count:
        result["notice"] = (
            f"{stale_count} node(s) may be outdated — re-run `techne codex index` to refresh."
        )
    return result


def _pattern_search(args: dict, config: Config) -> dict:
    """FTS over the Architect's pattern corpus (architect.db). Optionally scoped to
    patterns that apply to a given project target."""
    from techne.core.architect import architect_db
    from techne.core.patterns import patterns_for_target, search_patterns

    db = architect_db(config)
    query = args.get("query", "")
    limit = args.get("limit", 20)
    target = args.get("target")
    if target and not query:
        hits = patterns_for_target(db, target, limit=limit)
    else:
        hits = search_patterns(db, query, limit=limit)
        if target:
            # Post-filter FTS hits to the target's pattern set. The cap is a high
            # ceiling vs. the corpus (hundreds), not a real bound; revisit if a
            # single target ever exceeds it (it would silently drop valid hits).
            allowed = {p["id"] for p in patterns_for_target(db, target, limit=100_000)}
            hits = [h for h in hits if h["id"] in allowed]
    return {"results": hits, "count": len(hits)}


def _pattern_related(args: dict, config: Config) -> dict:
    """Patterns linked to/from a given pattern via the [[wikilink]] relation graph."""
    from techne.core.architect import architect_db
    from techne.core.patterns import related_patterns

    pattern_id = args.get("pattern_id", "")
    return {"pattern_id": pattern_id, **related_patterns(architect_db(config), pattern_id)}


def _techne_reflect_emit(args: dict, config: Config) -> dict:
    """Render a session's reflection prompts for the agent to answer with its OWN model.

    The resident-model extraction path (techne#19): techne groups a session's
    observations into threads and renders one prompt per thread; the in-session agent
    answers each `prompt` as a JSON candidate array, then feeds them back via
    techne_reflect_ingest. No API key, no MCP sampling — the model already running this
    session does the work. Defaults to the most recent session with unreflected
    work (skipping empty/already-emitted sessions), and caps the batch so a large
    session can't exceed the transport's token cap (tenishai/techne#267).
    """
    from techne.core.database import connect
    from techne.core.extension_state import get_state, set_state
    from techne.core.reflect import (
        emit_reflection_batch,
        load_session_for_reflection,
        pending_reflection,
    )

    session_id = args.get("session_id")
    if not session_id:
        # Default to the most-recent session that actually has unreflected work,
        # NOT simply the newest session by started_at: the canonical reflect-before-
        # restart call fires while a fresh (empty) session is the newest, so the raw
        # "ORDER BY started_at DESC" default landed on a count:0 session and forced a
        # manual hunt for the real id (tenishai/techne#267).
        pend = pending_reflection(config)
        session_id = pend[0]["session_id"] if pend else None
    if not session_id:
        return {"batch": [], "count": 0, "note": "no session with unreflected work"}

    threads, observations = load_session_for_reflection(config, session_id)
    batch = emit_reflection_batch(config, session_id, threads, observations)
    total = len(batch)

    # Per-thread emit cursor (techne#269/#236): a session with more reflectable threads
    # than the emit cap must drain losslessly across passes — the #268 marker collapsed
    # the whole session to "emitted" on the first partial emit, so threads past the cap
    # were never reflected. Load the prompt_hashes already delivered and exclude them, so
    # each pass takes the NEXT slice of unemitted threads: no overlap, no loss. The cursor
    # is keyed on prompt_hash — the stable, always-present per-thread id (thread_id can be
    # NULL for unpersisted threads) and the same key #236 dedups candidates on.
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    already = set(marker.get("emitted", [])) if marker else set()
    # Stability invariant: prompt_hash is derived from the rendered prompt INCLUDING the
    # per-thread existing-knowledge block (reflect._build_prompt), so it must not shift
    # between cursor passes mid-drain — otherwise an already-emitted thread re-appears.
    # This holds on the DEFAULT path (write_to='candidates'): that path never mutates live
    # knowledge, and a completed session's observations are frozen, so the hash is stable.
    # KNOWN LIVE-MODE LIMITATION (write_to='live'): promoting knowledge mid-drain can alter
    # a drained thread's existing-knowledge block, changing its prompt_hash. The cursor
    # would then re-emit it, and #236's (prompt_hash, content) dedup would be sidestepped
    # (duplicate candidate / re-promotion). Re-engineering the cursor key is out of scope
    # for this PR; document here as a known limitation of the live write path.
    batch = [item for item in batch if item["prompt_hash"] not in already]

    # Bound the payload so a large session can't produce a batch that exceeds the MCP
    # transport's token cap — an oversized emit used to error out AND still mark the
    # session reflected, silently dropping the whole session's memory (techne#267).
    # `is not None` (not truthiness) so an explicit limit=0 is honored as "emit none"
    # rather than silently reinterpreted as the default.
    limit = args.get("limit")
    if limit is None:
        limit = config.reflect.emit_max_threads
    batch = batch[:limit]
    # Trim by serialized size to a char budget (proxy for the transport token cap),
    # keeping >=1 prompt so a lone huge thread still makes progress. Single O(n) pass
    # — accumulate a prefix that fits — rather than re-dumping the whole batch per pop.
    max_chars = config.reflect.emit_max_chars
    kept, running = [], 2  # 2 = the enclosing "[]"
    for item in batch:
        item_len = len(json.dumps(item)) + 1  # + separator headroom
        if kept and running + item_len > max_chars:
            break
        kept.append(item)
        running += item_len
    batch = kept if kept else batch[:1]

    # Cursor-relative truncation: `remaining` counts threads NOT YET emitted across all
    # passes (total minus the cumulative emitted set), so a multi-pass drain reports its
    # true shortfall and reaches 0 only when the whole session is done (techne#269).
    newly = [item["prompt_hash"] for item in batch]
    emitted_ids = sorted(already | set(newly))
    truncated = len(emitted_ids) < total
    remaining = total - len(emitted_ids)
    # A lone thread that alone exceeds the budget can't be delivered whole. Marking
    # the session emitted here would silently skip it — footgun 3, unchanged (Prudence,
    # #268). Detect it, leave the cursor UNADVANCED so the auto-trigger keeps surfacing
    # it, and flag the payload loudly. (True per-thread chunking of one oversized prompt
    # is still out of scope; the cursor drains whole threads only.)
    oversized = len(batch) == 1 and len(json.dumps(batch)) > max_chars
    delivered = bool(batch) and not oversized

    # Advance the cursor: record the cumulative set of emitted thread prompt_hashes plus
    # the session's total, so pending_reflection re-surfaces the session until every
    # thread is drained (techne#269) and re-emits skip already-delivered threads. This is
    # ALSO the exactly-once reflected-marker (techne#236 Gap 2) — one unified marker, not
    # two. `count`/`remaining` are kept for continuity with the #268 payload. We do NOT
    # advance when nothing deliverable was produced (empty or oversized single thread).
    if delivered:
        conn = connect(config.db_path)
        try:
            set_state(
                conn,
                f"reflect:emitted:{session_id}",
                {
                    "emitted": emitted_ids,
                    "total": total,
                    "remaining": remaining,
                    "count": len(emitted_ids),
                },
            )
        finally:
            conn.close()

    result = {"session_id": session_id, "count": len(batch), "batch": batch}
    if truncated:
        result["truncated"] = True
        result["remaining"] = remaining
        result["note"] = (
            f"Session has {total} threads; emitted {len(batch)} this pass (cap={limit}). "
            f"{remaining} not yet emitted — re-emit with the same session_id (the "
            "per-thread cursor auto-resumes from where this pass stopped) to drain the rest."
        )
    if oversized:
        result["oversized"] = True
        result["note"] = (
            f"A single thread's serialized prompt exceeds emit_max_chars ({max_chars}) "
            "and can't be delivered whole; the session cursor is left UNADVANCED so it "
            "isn't silently skipped. Needs per-thread prompt chunking (#269 follow-up). "
            "Because the cursor drains threads IN ORDER, an oversized lone thread also "
            "blocks every thread after it in the session — the session remains pending "
            "until per-thread chunking lands (#269 follow-up)."
        )
    return result


def _techne_reflect_ingest(args: dict, config: Config) -> dict:
    """Persist one thread's agent-produced reflection answer (techne#19).

    `response` is the JSON candidate array (as a string) the agent produced for a
    `prompt` from techne_reflect_emit; `thread_id` and `prompt_hash` are the values
    from that emit batch item. Candidates land in knowledge_candidates for review
    (`techne reflect review`); they only touch live knowledge/identity when
    config.reflect.write_to == 'live'. Call once per answered thread.
    """
    from techne.core.reflect import ingest_reflection_response

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""
    if not response.strip():
        return {
            "candidates_proposed": 0,
            "candidates_written": 0,
            "candidates_promoted": 0,
            "note": "empty response — nothing to ingest",
        }

    thread_id = args.get("thread_id", "")
    result = ingest_reflection_response(
        config,
        args.get("session_id", ""),
        thread_id,
        args.get("prompt_hash", ""),
        response,
        model_used=args.get("model_used", "agent-assisted"),
    )
    # Close the #514 loop here: this is the only point where a thread is known to
    # have been reflected. Without it nothing ever calls drain_reflection_queue,
    # the queued-debt banner never clears, and it saturates into noise — the drain
    # half of the mechanism-present-but-not-load-bearing shape #514 is about.
    if thread_id:
        from techne.core.reflect import drain_reflection_queue

        result["reflection_debt_drained"] = drain_reflection_queue(config.db_path, [thread_id])
    return result


def _techne_consolidate_emit(args: dict, config: Config) -> dict:
    """Find duplicate/overlapping knowledge clusters for the agent to judge with its
    own model (techne#108 sleep). Returns prompts; answer each and feed back via
    techne_consolidate_ingest."""
    from techne.core.sleepdream import consolidate_emit

    batch = consolidate_emit(config, max_clusters=args.get("max_clusters", 20))
    return {"count": len(batch), "batch": batch}


def _techne_consolidate_ingest(args: dict, config: Config) -> dict:
    """Persist one cluster's consolidation verdict (techne#108). `response` is the JSON
    verdict object; `target_ids` + `cluster_hash` come from the emit batch item. A
    'merge' verdict writes a proposal to knowledge_candidates for review."""
    from techne.core.sleepdream import consolidate_ingest

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return consolidate_ingest(
        config,
        args.get("target_ids") or [],
        args.get("cluster_hash", ""),
        response,
    )


def _techne_dream_emit(args: dict, config: Config) -> dict:
    """Find related knowledge groups for the agent to judge with its own model
    (techne#108 dream/recombine). Returns prompts; answer each and feed back via
    techne_dream_ingest. Abstention-first — most groups yield nothing."""
    from techne.core.sleepdream import dream_emit

    batch = dream_emit(config, max_groups=args.get("max_groups", 10))
    return {"count": len(batch), "batch": batch}


def _techne_dream_ingest(args: dict, config: Config) -> dict:
    """Persist one group's dream verdict (techne#108). `response` is the JSON verdict
    object; `group_ids` + `group_hash` come from the emit batch item. A 'connect'
    verdict writes a low-confidence pending candidate for review."""
    from techne.core.sleepdream import dream_ingest

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return dream_ingest(
        config,
        args.get("group_ids") or [],
        args.get("group_hash", ""),
        response,
    )


def _techne_faithfulness_emit(args: dict, config: Config) -> dict:
    """Find pending merge/supersede candidates without a faithfulness check and emit
    prompts for the agent to answer with its own model (techne#217 / D4)."""
    from techne.core.faithfulness import DEFAULT_MAX_CANDIDATES, faithfulness_emit

    batch = faithfulness_emit(
        config, max_candidates=args.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    )
    return {"count": len(batch), "batch": batch}


def _techne_faithfulness_ingest(args: dict, config: Config) -> dict:
    """Annotate one merge/supersede candidate with a faithfulness verdict
    (techne#217 / D4). Sovereignty invariant: never changes candidate status."""
    from techne.core.faithfulness import faithfulness_ingest

    candidate_id = args.get("candidate_id")
    if not isinstance(candidate_id, int):
        return {"error": "candidate_id must be an integer"}
    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return faithfulness_ingest(config, candidate_id, response)


def _techne_memory_claims_emit(args: dict, config: Config) -> dict:
    """Emit the claim-reconciliation prompt for the agent to answer with its own model
    (techne#107 PR3). Returns the prompt + metadata; feed the response back via
    techne_memory_claims_ingest. Output is ephemeral."""
    from techne.core.memory_claims import claims_emit

    return claims_emit(config, since_days=args.get("since_days"))


def _techne_memory_claims_ingest(args: dict, config: Config) -> dict:
    """Process the agent's claim-judgement response (techne#107 PR3). Returns an
    ephemeral report with per-claim verdicts and counts. Nothing is persisted."""
    from techne.core.memory_claims import claims_ingest

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return claims_ingest(config, args.get("prompt_hash", ""), response)


def _techne_conflict_emit(args: dict, config: Config) -> dict:
    """Find live knowledge pairs that may conflict and emit prompts for the agent
    (techne#213 D1 — contradiction detection). Selection uses shared tags + same type,
    NOT similarity (AUROC ~0.59 for contradictions). Returns prompts; answer each
    and feed back via techne_conflict_ingest. Proposals land in review — never
    auto-applied. Run this pass alongside consolidate to catch contradictions that
    FTS clustering misses."""
    from techne.core.conflict import conflict_check_emit

    max_items = args.get("max_items", None)
    batch = conflict_check_emit(config, max_items=max_items)
    return {"count": len(batch), "batch": batch}


def _techne_conflict_ingest(args: dict, config: Config) -> dict:
    """Feed back one conflict-check verdict from techne_conflict_emit (techne#213).
    `response` is the agent's JSON verdict array; `focus_id`, `comparison_ids`, and
    `check_hash` come from the emit batch item. SUPERSEDE routes to a pending
    supersession candidate (the older event_at loses); UPDATE to a pending update
    candidate; NOOP/DISTINCT to discarded markers. Nothing auto-applies."""
    from techne.core.conflict import conflict_check_ingest

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return conflict_check_ingest(
        config,
        int(args.get("focus_id", 0)),
        [int(i) for i in (args.get("comparison_ids") or [])],
        args.get("check_hash", ""),
        response,
    )


def _techne_federation_conflict_ingest(args: dict, config: Config) -> dict:
    """Feed back the agent's verdict on a cross-store contradiction surfaced by
    techne_recall (techne#222 step E). focus_content, focus_store, focus_event_at,
    comparison_ids, check_hash, and response come from the federation_conflicts item
    returned by techne_recall. SUPERSEDE + team newer → pending 'retire' candidate
    targeting the LOCAL row. SUPERSEDE + local newer → discarded marker (team is stale;
    no push to foreign store). NOOP/DISTINCT → discarded markers. Nothing auto-applies.
    Writes the LOCAL db only — the foreign team store is never modified."""
    from techne.core.federation import federation_conflict_ingest

    response = args.get("response", "")
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""

    return federation_conflict_ingest(
        config,
        focus_content=str(args.get("focus_content", "")),
        focus_store=str(args.get("focus_store", "")),
        focus_event_at=args.get("focus_event_at") or None,
        comparison_ids=[int(i) for i in (args.get("comparison_ids") or [])],
        check_hash=str(args.get("check_hash", "")),
        response=response,
    )


def _techne_check_evidence(args: dict, config: Config) -> dict:
    """Verify structured claims against the observation ledger (techne#105).

    Calls query_turn_evidence to build the evidence window, then check_claims
    to produce per-claim verdicts.  Returns a summary dict with the evidence
    view, the verdict list, and an unbacked_count for the caller to flag.
    """
    from techne.core.evidence import check_claims, query_turn_evidence

    session_id = args.get("session_id", "")
    if not session_id:
        raise ValueError("techne_check_evidence requires 'session_id'")

    since = args.get("since")
    claims = args.get("claims") or []
    if not isinstance(claims, list):
        raise ValueError("'claims' must be an array of objects")

    ev = query_turn_evidence(
        config.db_path,
        session_id,
        since_iso=since,
        bash_captured=config.hooks.capture_bash,
    )
    verdicts = check_claims(claims, ev)
    unbacked_count = sum(1 for v in verdicts if v["status"] == "unbacked")

    return {
        "evidence": {
            "any_tool_call": ev.any_tool_call,
            "active_tool_call": ev.active_tool_call,
            "observation_count": ev.observation_count,
            "artifacts": ev.artifacts,
            "commits": ev.commits,
            "actions": ev.actions,
            "tests": {
                "passed": ev.tests.passed,
                "failed": ev.tests.failed,
                "errors": ev.tests.errors,
                "suite_green": ev.tests.suite_green,
            },
        },
        "verdicts": verdicts,
        "unbacked_count": unbacked_count,
    }


def _techne_reminder_add(args: dict, config: Config) -> dict:
    """Arm a new reminder and return its id + normalised due_at. (techne#207)

    Accepts relative offsets (+1d/+3h/+30m) or absolute UTC ISO timestamps.
    All due_at values are stored as UTC; the post_tool_use hook compares against
    UTC now() so UTC consistency here is critical.
    """
    from techne import reminders

    note = args.get("note", "").strip()
    if not note:
        raise ValueError("techne_reminder_add requires 'note' (non-empty string)")
    raw_due = args.get("due_at", "").strip()
    if not raw_due:
        raise ValueError("techne_reminder_add requires 'due_at'")

    due_at_utc = _parse_due_at(raw_due)
    reminder_id = reminders.add(config.db_path, note, due_at_utc)
    return {"id": reminder_id, "due_at_utc": due_at_utc}


def _techne_reminder_list(args: dict, config: Config) -> dict:
    """List all active reminders (including future ones) for in-session management. (techne#207)

    Returns id, note, due_at, and last_surfaced_at for each active reminder so
    the operator can review what's armed and decide what to dismiss.
    """
    from techne import reminders

    rows = reminders.list_active(config.db_path)
    return {"reminders": rows}


def _techne_reminder_dismiss(args: dict, config: Config) -> dict:
    """Dismiss a reminder so it no longer surfaces. (techne#207)

    The row is kept as status='done' for audit; it will not appear in
    list_due() or list_active() after dismissal.
    """
    from techne import reminders

    reminder_id = args.get("id")
    if reminder_id is None:
        raise ValueError("techne_reminder_dismiss requires 'id' (integer)")
    reminder_id = int(reminder_id)
    reminders.dismiss(config.db_path, reminder_id)
    return {"dismissed": reminder_id}


TOOL_HANDLERS = {
    "techne_search": _techne_search,
    "techne_store": _techne_store,
    "techne_observe": _techne_observe,
    "techne_threads": _techne_threads,
    "techne_resume": _techne_resume,
    "techne_status": _techne_status,
    "techne_recall": _techne_recall,
    "codex_search": _codex_search,
    "codex_blast_radius": _codex_blast_radius,
    "pattern_search": _pattern_search,
    "pattern_related": _pattern_related,
    "techne_reflect_emit": _techne_reflect_emit,
    "techne_reflect_ingest": _techne_reflect_ingest,
    "techne_consolidate_emit": _techne_consolidate_emit,
    "techne_consolidate_ingest": _techne_consolidate_ingest,
    "techne_dream_emit": _techne_dream_emit,
    "techne_dream_ingest": _techne_dream_ingest,
    "techne_faithfulness_emit": _techne_faithfulness_emit,
    "techne_faithfulness_ingest": _techne_faithfulness_ingest,
    "techne_memory_claims_emit": _techne_memory_claims_emit,
    "techne_memory_claims_ingest": _techne_memory_claims_ingest,
    "techne_check_evidence": _techne_check_evidence,
    "techne_conflict_emit": _techne_conflict_emit,
    "techne_conflict_ingest": _techne_conflict_ingest,
    "techne_federation_conflict_ingest": _techne_federation_conflict_ingest,
    "techne_reminder_add": _techne_reminder_add,
    "techne_reminder_list": _techne_reminder_list,
    "techne_reminder_dismiss": _techne_reminder_dismiss,
    "techne_send": _techne_send,
    "techne_inbox": _techne_inbox,
    "techne_mark_read": _techne_mark_read,
    "techne_coord_threads": _techne_coord_threads,
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

        # A well-formed JSON scalar/array (e.g. `null`, `42`, `[]`) parses fine but
        # isn't a JSON-RPC request object; handle_request does request.get(...) and
        # would crash the whole server on it. Skip anything that isn't an object.
        # (techne#166)
        if not isinstance(request, dict):
            continue

        response = handle_request(request, config)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
