"""techne MCP server — agent interface for memory, knowledge, and threads.

Runs as a stdio MCP server. Registers with Claude Code (or any MCP client)
via `techne init`.
"""

import json
import sys

from techne import __version__
from techne.config import Config

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


def _techne_recall(args: dict, config: Config) -> dict:
    """Retrieve context for a specific topic — searches all layers."""
    from techne.core.anchor import annotate_freshness
    from techne.core.identity import list_entries as list_identity
    from techne.core.knowledge import annotate_uncertain
    from techne.core.search import search

    topic = args.get("topic", "")
    # techne#216: techne_recall is DEFAULT recall — the topic-scoped pull agents hit
    # every task — so soft-archived knowledge is excluded here. Explicit techne_search
    # (via _techne_search) keeps the inclusive default and still returns archived.
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
            "Requires `techne codex index <repo>` to have been run first. "
            "Experimental: the index is built manually and may be stale — re-run "
            "`techne codex index` after code changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Symbol name or signature fragment to search",
                },
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codex_blast_radius",
        "description": (
            "Compute the transitive reachability (blast radius) of a Codex symbol — "
            "out=what this symbol reaches (dependencies), in=what reaches this symbol "
            "(dependents). Uses recursive CTE BFS over the call/import graph. "
            "Experimental: the index is built manually and may be stale — re-run "
            "`techne codex index` after code changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Symbol id (file::name::kind::line) from codex_search",
                },
                "depth": {"type": "integer", "default": 5},
                "direction": {
                    "type": "string",
                    "enum": ["out", "in"],
                    "default": "out",
                    "description": (
                        "out=dependencies (what this reaches), in=dependents (what reaches this)"
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
            "most recent session."
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
                    "description": "Max threads to emit prompts for (default: all).",
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
]


def _codex_search(args: dict, config: Config) -> dict:
    """FTS over Codex symbols — search across all indexed repos."""
    from techne.core.codegraph import annotate_freshness, search_symbols
    from techne.core.codex import codex_db

    query = args.get("query", "")
    limit = args.get("limit", 20)
    db_path = codex_db(config)
    hits = annotate_freshness(db_path, search_symbols(db_path, query, limit=limit))
    stale_count = sum(1 for h in hits if h.get("freshness") in ("stale", "missing"))
    result: dict = {"results": hits, "count": len(hits)}
    if stale_count:
        result["notice"] = (
            f"{stale_count} result(s) may be outdated — re-run `techne codex index` to refresh."
        )
    return result


def _codex_blast_radius(args: dict, config: Config) -> dict:
    """Transitive reachability (blast-radius / dependency walk) for a symbol."""
    from techne.core.codegraph import annotate_freshness, reachable
    from techne.core.codex import codex_db

    symbol_id = args.get("symbol_id", "")
    depth = args.get("depth", 5)
    direction = args.get("direction", "out")
    db_path = codex_db(config)
    raw = reachable(db_path, symbol_id, direction=direction, max_depth=depth)
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
    session does the work. Defaults to the most recent session.
    """
    from techne.core.database import connect
    from techne.core.extension_state import set_state
    from techne.core.reflect import emit_reflection_batch, load_session_for_reflection

    session_id = args.get("session_id")
    if not session_id:
        conn = connect(config.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        session_id = row["id"] if row else None
    if not session_id:
        return {"batch": [], "count": 0, "note": "no session to reflect on"}

    threads, observations = load_session_for_reflection(config, session_id)
    batch = emit_reflection_batch(config, session_id, threads, observations)
    limit = args.get("limit")
    if limit:
        # NB: the marker below covers the WHOLE session, so threads beyond `limit`
        # won't be re-surfaced by the auto-trigger — call without a limit, or re-emit
        # with an explicit session_id, to reflect the rest.
        batch = batch[:limit]

    # Mark the session emitted so the SessionStart auto-trigger won't re-nudge it,
    # even if the agent's answers ultimately yield zero durable candidates (techne#19).
    conn = connect(config.db_path)
    try:
        set_state(conn, f"reflect:emitted:{session_id}", {"count": len(batch)})
    finally:
        conn.close()

    return {"session_id": session_id, "count": len(batch), "batch": batch}


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

    return ingest_reflection_response(
        config,
        args.get("session_id", ""),
        args.get("thread_id", ""),
        args.get("prompt_hash", ""),
        response,
        model_used=args.get("model_used", "agent-assisted"),
    )


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
