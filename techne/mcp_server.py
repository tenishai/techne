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
    "THE FOUR DOMAINS — address them BY NAME, not by mechanism. archivist_search (what the "
    "team knows), librarian_search (what is written down), architect_search (what is worth "
    "borrowing), codex_search/codex_blast_radius (what the code looks like). Every answer "
    "carries the `binding` it resolved to — read it and say what it was; `techne bindings` "
    "reports all four at once. These are deliberate calls, not part of personal recall: "
    "reach for one when the task is already about that domain, and prefer techne_recall "
    "when you do not yet know which.\n"
    "GETTING CONTENT IN. An EMPTY domain and an UNPOPULATED one are indistinguishable in "
    "the result, so name the feeder the moment you see zero rows — not after the operator "
    "concludes something from it. The archivist fills from this session's own memory "
    "(techne_store, and the reflect rail); the librarian from `techne librarian "
    "index-transcripts`, which NOTHING runs for you; the architect from `techne architect "
    "add` for one pattern or `techne architect ingest FILE.md` for a corpus; the codex from "
    "`techne codex index <repo>`. The codex is the exception that qualifies its own empty "
    "answer — it says which kind of nothing it means. The other three cannot, which is why "
    "that zero is yours to explain.\n"
    "THE GUIDES SHIP WITH techne — call `techne_guide` (or `techne guide <name>` if "
    "you have a shell). Read `four-functions` before answering which domain to ask, "
    "`reflect-guide` before draining a backlog, and `investigating` before mining a "
    "URL the operator pastes — that one carries the read-only-subagent sandbox and "
    "the verdict tiers, and `techne architect dedup <url>` is its first step.\n"
    "Capture is automatic where a host hook exists; otherwise call techne_observe each turn."
)


#: This process's view of itself (techne#696). Built lazily on the first tool call
#: rather than at import, so importing `mcp_server` (tests, the CLI) costs nothing.
#: Module-level because it is genuinely per-process state — the thing being measured.
_RUNTIME_WATCH = None


def _drift_notice(config) -> str | None:
    """One line when this server is serving code that no longer matches disk.

    THE SERVER IS THE RIGHT PLACE TO ASK, and that is the whole design. Answering this
    from outside costs `ps` plus git (`check_runtime_loaded`, `@expensive`, and the
    reason PR #697 was withdrawn). Answering it from inside costs one `stat` behind a
    TTL, because a process already knows when it started and what it imported.

    Never raises and never blocks a tool result: a diagnostic that can fail a call is
    worse than no diagnostic.
    """
    global _RUNTIME_WATCH
    try:
        from techne.core import runtime_drift

        if _RUNTIME_WATCH is None:
            _RUNTIME_WATCH = runtime_drift.begin(config)
        drift = runtime_drift.poll(_RUNTIME_WATCH)
        if drift:
            # Publish for OTHER processes (techne#696 part A). ONLY on a real finding:
            # `poll` reports once per deployment, so this writes once too, and the
            # presence of a state file therefore means something is actually wrong
            # rather than merely "a server ran". Writing unconditionally would litter
            # one file per process forever and destroy that signal.
            runtime_drift.publish(_RUNTIME_WATCH, drift)
            return drift.message()
        return None
    except Exception:  # noqa: BLE001
        return None


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
                # `reflect._sent_chars` budgets emit against this exact serialization
                # (techne#993). Change one, change both.
                content = [{"type": "text", "text": json.dumps(result, indent=2)}]
                # techne#696 — a SEPARATE content block, never a key inside `result`.
                # The drift notice is about the SERVER, not about what the tool
                # returned; folding it into the payload would change 34 tools' response
                # shapes to say something none of them is about, and #695 spent three
                # review rounds on how expensive a response-shape change is.
                notice = _drift_notice(config)
                if notice:
                    content.append({"type": "text", "text": notice})
                return _response(req_id, {"content": content})
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
        # techne#501: an EXPLICIT search SHOWS untrusted rows rather than hiding
        # them — the reader asked for them. They arrive carrying `status`, so the
        # agent sees "on record, not believed" instead of inferring trust from
        # rank position. The automatic injection rails filter instead, because
        # nobody CHOSE to look at what those surface.
        include_uncertain=True,
        curated_head_slots=config.search.curated_head_slots,
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
        supersedes=args.get("supersedes"),
    )
    result = {"id": entry_id, "stored": True}
    if args.get("supersedes") is not None:
        # Echo what was retired so the caller need not re-read to confirm it
        # (techne#887). store() is all-or-nothing, so reaching here means all were.
        from techne.core.knowledge import _normalize_ids

        result["superseded"] = _normalize_ids(args["supersedes"])
    return result


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
    session_id, else the MCP server's own CLAUDE_CODE_SESSION_ID env. No token: the
    local techne.db is the whole trust boundary.

    THE ENV FALLBACK RESTED ON A PREMISE THAT IS FALSE (techne#573). This docstring
    used to say "the server is spawned per-session by Claude Code, so its env carries
    this session's id." It is not. The server is long-lived and outlives the session
    that launched it — measured 2026-08-14, two mcp_server processes started Aug 7
    12:24 and 14:03 were still serving sessions started days later. `os.environ` is
    frozen at PROCESS start, so the fallback returns whichever session happened to
    spawn the server, forever. Every coord message from one session that day was
    signed with the id of a session that had wound down six days earlier, and a peer
    then propagated that wrong id because it "verified" it against the corrupted bus.

    So the fallback is now CHECKED: if the env id is not among the sessions Claude
    Code currently considers live, we raise rather than attribute silently. Wrong
    attribution is worse than a refusal here — a refusal is recoverable in one retry
    (the caller passes `session_id`, which an agent always has), whereas a bad
    attribution is durable, invisible, and poisons `coord_messages` and anything that
    reads it.

    THE CHECK IS `present_session_ids()`, NOT `live_sessions()`, AND THAT CHOICE IS
    THE WHOLE GUARD (techne#581 review). `live_sessions` requires a fresh `updatedAt`
    heartbeat, which Claude Code writes only for `entrypoint: "cli"` sessions. Every
    headless `claude -p` run — i.e. every agent persona — writes `updatedAt: null`,
    so `live_sessions` returns [] in an agent's config dir and the `if live and ...`
    precondition below is never satisfied. The guard would be INERT for exactly the
    population most likely to be talking to a long-lived server: measured, a
    fabricated id and a genuinely dead 5-day-old session both sailed through. A
    guard's precondition is a way to switch the guard off; this one was switched off
    by default for headless callers.

    "COULD NOT CHECK" IS NOT "CHECKED AND FALSE". If the registry yields nothing at
    all — unreadable, or no sidecars on the box — we cannot distinguish a stale id
    from a working one, so we do NOT block. Absence of evidence is not evidence of
    absence, and failing closed on an unreadable registry would take the whole coord
    bus down on a machine where presence simply is not visible. Note this fail-open
    is now much NARROWER than it was: it means "no session sidecars exist at all",
    not "no session posted a heartbeat recently".

    A caller-supplied `session_id` is NOT checked: that is a self-assertion, it
    already took precedence, and a session too new to appear in the registry must
    still be able to speak.
    """
    import os

    asserted = args.get("session_id")
    if asserted:
        return asserted

    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not env_sid:
        return None

    from techne.core.coord import present_session_ids

    try:
        live = present_session_ids()
    except Exception:  # noqa: BLE001 — presence is best-effort; see "could not check"
        return env_sid

    if live and env_sid not in live:
        raise ValueError(
            "techne#573: this MCP server's CLAUDE_CODE_SESSION_ID is "
            f"{env_sid!r}, which names no session present on this host — the server "
            "outlived the session that launched it and its env is frozen at process start. "
            "Pass an explicit 'session_id' (your own), or restart the server so it "
            "re-captures the environment. Refusing rather than attributing this "
            "call to a session that has ended."
        )
    return env_sid


def _techne_send(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError(
            "techne_send: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)"
        )
    body = args.get("body")
    if not body:
        raise ValueError("techne_send requires 'body' (non-empty)")
    to = args.get("to") or coord.DEFAULT_CHANNEL
    mid = coord.send(
        config.db_path,
        sid,
        body,
        to=to,
        thread_id=args.get("thread_id"),
        reply_to=args.get("reply_to"),
    )
    return {"id": mid, "from_session": sid, "to": to}


def _techne_coord_threads(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError(
            "techne_coord_threads: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)"
        )
    return {
        "session_id": sid,
        "threads": coord.threads(config.db_path, sid, limit=args.get("limit", 20)),
    }


def _techne_inbox(args: dict, config: Config) -> dict:
    from techne.core import coord

    sid = _resolve_session_id(args)
    if not sid:
        raise ValueError(
            "techne_inbox: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)"
        )
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
        raise ValueError(
            "techne_mark_read: no session_id (pass 'session_id' or set CLAUDE_CODE_SESSION_ID)"
        )
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
    """config.connections plus the base Archivist as a `team` connection WHEN IT IS
    BOUND TO A STORE (techne#346 PR2, rebound by techne#552).

    THIS DOCSTRING USED TO SAY "a non-standalone instance recalls over its own
    archivist.db without manual config", which is now false for exactly the
    default-path case it described: a `local` binding contributes NO connection,
    because tier 2 is tier 1's own table and fanning out to it would read the same rows
    twice and report one source as two. setdefault so an explicit config entry wins."""
    from techne.core.archivist import ARCHIVIST_CONNECTION_NAME, archivist_connection

    conns = dict(config.connections)
    ac = archivist_connection(config)
    if ac is not None:
        conns.setdefault(ARCHIVIST_CONNECTION_NAME, ac)
    return conns


def _librarian_available(config: Config) -> bool:
    """Does a librarian store exist to escalate TO? (S4)

    Distinguishes tier 4 `empty` from tier 4 `unavailable`. "The library has
    nothing on this" and "there is no library" are different answers, and S5's
    middle outcome depends on being able to say which one happened.
    """
    try:
        from techne.core.librarian import librarian_db

        return librarian_db(config).exists()
    except Exception:  # noqa: BLE001 — a diagnostic must never break recall
        return False


def _with_ladder(
    config: Config,
    topic: str,
    rows: list[dict],
    fan_out_connections: dict,
    archivist_name: str,
    archivist_binding: str,
    extra: dict,
) -> dict:
    """Attach the stated ladder to a recall response (techne#552, S1/S4/S5).

    ADDITIVE BY CONSTRUCTION. `search_results` keeps its exact prior contents and
    ordering; the ladder is new keys alongside it. That matters more than usual
    here — the argument for doing this NOW is that open-sourcing freezes a response
    shape, so this is the last cheap moment to ADD to it, and the change is only
    cheap if nothing existing moves.

    Wrapped so a fault in the diagnostic can never cost the caller their results:
    the ladder is what teaches, but the rows are what was asked for.
    """
    from techne.core import tiers

    out = {"search_results": rows, **extra}
    try:
        tiers.annotate_rows(rows, archivist_name)
        reports = tiers.build_reports(
            rows=rows,
            connections=fan_out_connections,
            archivist_name=archivist_name,
            archivist_binding=archivist_binding,
            librarian_available=_librarian_available(config),
        )
        out["tiers"] = [r.as_dict() for r in reports]
        # S5's experience signal, asked of the knowledge layer directly — see
        # tiers.OUTCOME_LOOSE_ONLY for why neither distance nor match_source works.
        from techne.core.search import has_direct_match

        out["outcome"] = tiers.classify_outcome(
            reports, direct=has_direct_match(config.db_path, topic, table="knowledge")
        )
    except Exception:  # noqa: BLE001
        log.warning("recall: tier ladder failed — returning results without it", exc_info=True)
    return out


def _techne_recall(args: dict, config: Config) -> dict:
    """Retrieve context for a specific topic — searches all layers.

    When config.connections is non-empty (techne#222 federation), fans out to
    team/static stores, deduplicates, detects cross-store contradictions, and
    returns a merged ranked set. When empty (the common case), returns local-only
    with an identical shape — no behaviour change for unconfigured instances.
    """
    from techne.core import tiers as tier_model
    from techne.core.anchor import annotate_freshness
    from techne.core.archivist import ARCHIVIST_CONNECTION_NAME
    from techne.core.identity import list_entries as list_identity
    from techne.core.knowledge import annotate_uncertain
    from techne.core.search import search

    topic = args.get("topic", "")

    # Effective connections = configured remotes + the base Archivist as a `team`
    # store WHEN IT IS BOUND TO ONE (techne#346 PR2, rebound by techne#552). A `local`
    # binding contributes NO connection: tier 2 is tier 1's own table, so fanning out
    # to it would read the same rows twice and report one source as two.
    connections = _effective_connections(config)

    # S2 (knowledge#1085) — THE LOCAL ARCHIVIST IS A RUNG, NOT A SECOND FILE.
    # `_effective_connections` auto-registers this machine's own archivist.db as a
    # `team` connection, so at n=1 recall was opening a store measured to be a 98.9%
    # MIRROR of local knowledge, with drift — paying a second read to answer the same
    # question slightly worse. The rung stays fully visible in the ladder below; only
    # the redundant file read goes away. An archivist the operator configured
    # EXPLICITLY is a real binding and is fanned out to as before, which is why this
    # tests for BOTH ways an operator can express that intent:
    #   * a `connections` entry named `archivist` — a federated peer, and
    #   * a non-empty `archivist_db_path` — this instance pointed at a SHARED store,
    #     which is the whole multi-agent team-recall case (techne#346). That store is
    #     emphatically not a mirror of local knowledge, and skipping it would silently
    #     break team recall. An existing end-to-end test caught exactly that when this
    #     gate checked only `connections`; the default-path case is the ONLY one S2
    #     is about.
    # ASK THE RESOLVER; DO NOT RE-DERIVE (techne#552). This block used to work the
    # binding out from `standalone` + `connections` + `archivist_db_path` itself, which
    # made it the seventh place with an opinion about what the archivist is — and the
    # only one the WRITE path could disagree with. `core.archivist.archivist_binding`
    # is now the single answer, and read, write, the query surface and this ladder all
    # read it. That is the whole of the issue: one seam, one decision, several bindings.
    from techne.core.archivist import archivist_binding as _resolve_binding

    archivist_binding = _resolve_binding(config)
    archivist_is_local = archivist_binding == tier_model.ARCHIVIST_LOCAL
    fan_out_connections = dict(connections)
    if archivist_is_local:
        fan_out_connections.pop(ARCHIVIST_CONNECTION_NAME, None)

    # Helper: shared identity search (same in both paths)
    def _identity_matches() -> list[dict]:
        results_i = []
        for entry in list_identity(config.db_path):
            if topic.lower() in entry["key"].lower() or topic.lower() in entry["value"].lower():
                results_i.append(entry)
        return results_i[:5]

    # ---- Noop gate (techne#222 §2): nothing to fan out to → local-only ----
    if not fan_out_connections:
        # techne#216: DEFAULT recall excludes soft-archived knowledge.
        results = search(
            config.db_path,
            topic,
            table="all",
            limit=10,
            half_life_days=config.knowledge_confidence_half_life_days,
            include_archived=False,
            curated_head_slots=config.search.curated_head_slots,
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
        return _with_ladder(
            config,
            topic,
            _fold_in_documents(config, topic, results),
            {},
            ARCHIVIST_CONNECTION_NAME,
            archivist_binding,
            {"identity_matches": _identity_matches()},
        )

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
        curated_head_slots=config.search.curated_head_slots,
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
            fan_out_connections,
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
            min_shared_keywords=config.conflict.min_shared_keywords,
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

    out: dict = _with_ladder(
        config,
        topic,
        _fold_in_documents(config, topic, merged),
        fan_out_connections,
        ARCHIVIST_CONNECTION_NAME,
        archivist_binding,
        {"identity_matches": _identity_matches()},
    )

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

#: THE DESCRIPTION NO LONGER ENUMERATES THE TARGETS, and that is deliberate
#: (techne#1008). It used to interpolate the configured set, which put ten of
#: THIS project's repo names into every connecting agent's context. Making the
#: set configurable fixed the leak at the source but left two problems:
#: docs/mcp-tools.md is generated from this description, so an operator with a
#: non-default roster regenerated OUR project names straight back into the
#: shipped doc; and a description built at import time cannot describe a set
#: that is per-install anyway.
#:
#: What #674 actually needed was for a wrong target to FAIL rather than return
#: zero rows silently — `_pattern_search` now says which targets are valid, at
#: call time, from the operator's own config. That is better than advertising
#: a list nobody can keep current, and it cannot leak.

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
            "Architect store via `techne architect ingest`, not here (techne#137). When "
            "this entry replaces or corrects an existing one, pass its id in `supersedes`: "
            'writing "SUPERSEDES #N" in the content does nothing, and #N keeps surfacing '
            "as current (techne#887)."
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
                "supersedes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Ids of existing knowledge entries this one replaces. Each is "
                        "retired into the new entry in the same write: recall stops "
                        "returning it, it is kept (not deleted), and entries that "
                        "depended on it are marked uncertain. If any id is unknown or "
                        "already superseded, nothing is stored."
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
        "name": "techne_archivist_query",
        "description": (
            "Call this to INTERROGATE team memory — ask the Archivist a question nobody "
            "wrote an endpoint for, as read-only SQL. techne_recall pushes results at you "
            "for a topic string; this lets you ask precisely: which decisions touch a "
            "project, what was written last week that superseded something, how many "
            "procedures are anchored to files that moved, who wrote what (written_by, "
            "techne#553). Call with NO sql first to get the readable schema. Read-only and "
            "guarded: one SELECT, over the distilled knowledge tables only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "One SELECT (CTEs and FTS5 MATCH on knowledge_fts are fine). "
                        "Omit to get the readable schema instead of a result set."
                    ),
                },
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "techne_guide",
        "description": (
            "Read one of techne's bundled guides. Call with no name to list them. These "
            "are the procedures techne ships: `four-functions` (which domain answers "
            "what, and how to read the `binding` in an answer), `reflect-guide` "
            "(draining a backlog), `investigating` (mining a URL the user pasted for "
            "patterns — carries the read-only-subagent sandbox and the verdict tiers), "
            "`surveying` (mapping a whole field), `db-per-domain`, `deployment`, "
            "`welcome`, `mcp-tools`, `AGENTS`, `self-care`, `techne-promote`. Returns "
            "the raw markdown to read into your context, not a rendered page. Exists so "
            "a client with no shell can still reach them — `techne guide <name>` is the "
            "same content from the CLI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Guide to read. Omit to list what is available.",
                }
            },
        },
    },
    {
        "name": "techne_capabilities",
        "description": (
            "What techne can do on THIS install, and what is switched OFF — grounded in "
            "the user's own store. Call this when the user asks what techne is, what it "
            "can do, what changed after an upgrade, or why a feature they heard about "
            "does not seem to happen. Returns data, not prose: narrate it as a short "
            "conversation, lead with what is off and what it would buy THEM, and quote "
            "the evidence counts — they are real numbers from their ledger. Also reports "
            "bundled workflows that are ABSENT FROM DISK, which no config flag can "
            "reveal. Not a feature list; do not read it out verbatim. Offer to apply "
            "each OFF item and apply nothing without the operator's yes. When the "
            "operator leaves an item off ON PURPOSE, pass acknowledge=<flag> so it "
            "stops counting toward the session-start invitation; acknowledgements do "
            "not expire, and unacknowledge=<flag> clears one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "acknowledge": {
                    "type": "string",
                    "description": (
                        "A capability `flag` the operator has decided to leave off. "
                        "Only after they say so, never on your own judgment."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "With acknowledge: the operator's reason, in a few words.",
                },
                "unacknowledge": {
                    "type": "string",
                    "description": "A capability `flag` whose acknowledgement to clear.",
                },
            },
        },
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
            '"what breaks if I change this" impact set; `out`=what this symbol reaches '
            "(dependencies). Uses recursive CTE BFS over the call/import graph. "
            "tests/fixtures/** nodes are excluded by default (name-collision noise in the "
            "v1 name-based edge model; set include_fixtures=true to keep them). Scoped to "
            "one repo (db-per-repo, techne#209): pass `repo` to pick it, else the "
            "server's working directory's repo is used. Each node carries `proven`: "
            "false means it is reached only by matching a bare call name and may not be "
            "a real dependent (techne#606) — see only_proven. Returns the closest `limit` "
            "nodes by depth (default 50), with `walked` and `truncated` stating what was "
            "cut — a hot symbol's full radius exceeds the response token ceiling. "
            "Experimental: the index is built "
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
                "only_proven": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Return only nodes reached entirely through edges resolved to a "
                        "real symbol. Off by default, because most edges are still "
                        "name-matched and this would empty the radius (techne#606)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": (
                        "Max nodes returned, closest-by-depth first; clamped to 500. "
                        "Measured uncapped worst case on one repo: 500 nodes / ~44k "
                        "tokens, which returns a token-ceiling ERROR instead of an "
                        "impact set (tenishai/agents#10)."
                    ),
                },
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "archivist_search",
        "description": (
            "What the TEAM knows. Resolves to this instance's own knowledge when "
            "the archivist is bound `local`, or to the team store when bound to "
            "one — the answer says which. "
            "One of techne's four knowledge domains (archivist, librarian, architect, "
            "codex), addressable by name. Every answer carries the `binding` it "
            "resolved to, so the same call keeps working as the domain grows from a "
            "local store into a persona or a service. For 'what do we know about X' "
            "across all four, use techne_recall instead — it escalates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "librarian_search",
        "description": (
            "What is WRITTEN DOWN — documents, transcripts and cards. Today that "
            "is the operator's own corpus; the role is the question, not the "
            "file set. "
            "One of techne's four knowledge domains (archivist, librarian, architect, "
            "codex), addressable by name. Every answer carries the `binding` it "
            "resolved to, so the same call keeps working as the domain grows from a "
            "local store into a persona or a service. For 'what do we know about X' "
            "across all four, use techne_recall instead — it escalates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "architect_search",
        "description": (
            "Architectural PATTERNS — the 'what is worth borrowing' corpus. Same "
            "corpus as pattern_search, addressed by the domain name rather than "
            "the mechanism. "
            "One of techne's four knowledge domains (archivist, librarian, architect, "
            "codex), addressable by name. Every answer carries the `binding` it "
            "resolved to, so the same call keeps working as the domain grows from a "
            "local store into a persona or a service. For 'what do we know about X' "
            "across all four, use techne_recall instead — it escalates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pattern_search",
        "description": (
            "Search the Architect's architectural-pattern corpus (architect.db) — design "
            "patterns borrowed from other projects' code, the team-shared 'what's worth "
            "borrowing' store. Optionally scope to patterns that apply to a project "
            "(target=<one of your configured architect.pattern_targets — run "
            "`techne architect targets` to see them>); with a target and no query, lists "
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
            "`prompt`; `thread_id`, `prompt_hash` and `obs_key` come from the emit batch item — "
            "PASS `obs_key` WHENEVER THE ITEM HAS ONE (a thread with no persisted row), or emit "
            "keeps offering the thread you just answered. Proposals land in knowledge_candidates "
            "for review (techne reflect review). Call once per answered thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "From the emit batch item."},
                "thread_id": {"type": "string", "description": "From the emit batch item."},
                "prompt_hash": {"type": "string", "description": "From the emit batch item."},
                "obs_key": {
                    "type": "string",
                    "description": "From the emit batch item, when present (a thread "
                    "with no persisted row). Pass it so emit stops offering the thread.",
                },
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
            "Call this before asserting either that you DID something (committed, ran "
            "tests, wrote a file) or that PROJECT STATE is a certain way (whether a PR "
            "is merged, whether we already do X) — enforces 'no "
            "unbacked word'. Deed claims are checked against the observation ledger; "
            "state claims are checked against GitHub and our own source "
            "(techne#584). State claims are the ones the operator has historically had to "
            "catch, because conversation has no diff and no suite for a reviewer to read. "
            "Returns per-claim backed/unbacked/unverifiable verdicts in your claim order. "
            "Advisory — does not block; use unbacked_count > 0 as the flag. Structured "
            "claims only; free-text extraction is out of scope. NOTE: "
            "commit/tests_green/checks_green/ran_action come from Bash, which is off by "
            "default — those "
            "return 'unverifiable' (not 'unbacked') unless capture_bash is enabled. "
            "'unverifiable' NEVER means the claim is false — it means the claim could "
            "not be established, either because the source was unreadable or because it "
            "was read and does not speak to the claim (an absence claim whose anchor is "
            "not in the file). Check unverifiable_count before reading 0 unbacked as "
            "'all clear'."
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
                "repo_root": {
                    "type": "string",
                    "description": (
                        "Optional root for resolving relative 'path' in capability claims. "
                        "Defaults to the server's cwd."
                    ),
                },
                "claims": {
                    "type": "array",
                    "description": (
                        "Structured claims to check. Each object must have a 'type' field. "
                        "DEED types (checked against the ledger): 'completion'/'did_work', "
                        "'commit' (optional 'sha'), 'tests_green', "
                        "'checks_green' (a linter/type-checker/formatter reported no "
                        "problems; backed only when a recognised tool STATED success and "
                        "none stated failure -- tools silent on success return "
                        "'unverifiable', never 'backed'), "
                        "'ran_action' (required 'match'), 'wrote_file' (required 'path'). "
                        "STATE types (techne#584): "
                        "'artifact_state' (required 'state' of open/closed/merged, and "
                        "either 'ref' as 'owner/repo#N' or 'repo'+'number'; checked via gh. "
                        "'merged' is distinct from 'closed'), "
                        "'capability' (required 'path' and 'pattern', optional "
                        "'present' default true and 'regex' default false; checked by "
                        "reading the file — use this for 'we already do X' / 'X is "
                        "missing'. An absence claim (present=false) ALSO requires "
                        "'anchor': a pattern that must be in the file, proving it is the "
                        "right one — without it a wrong path confirms the absence on no "
                        "evidence)."
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
            "identity defaults to the server's env, which can be STALE (techne#573) — if "
            "the call refuses, pass your own 'session_id'. REFLEX: announce "
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
                    "description": (
                        "Usually omitted — defaults to the server's env session id, "
                        "which can be STALE if the server outlived the session that "
                        "launched it (techne#573). Pass your own explicitly when the "
                        "call refuses, or from a client without that env."
                    ),
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
                    "description": (
                        "Usually omitted — defaults to the server's env session id, "
                        "which can be STALE if the server outlived the session that "
                        "launched it (techne#573). Pass your own explicitly when the "
                        "call refuses, or from a client without that env."
                    ),
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
                    "description": (
                        "Usually omitted — defaults to the server's env session id, "
                        "which can be STALE if the server outlived the session that "
                        "launched it (techne#573). Pass your own explicitly when the "
                        "call refuses, or from a client without that env."
                    ),
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
                    "description": (
                        "Usually omitted — defaults to the server's env session id, "
                        "which can be STALE if the server outlived the session that "
                        "launched it (techne#573). Pass your own explicitly when the "
                        "call refuses, or from a client without that env."
                    ),
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


def _with_binding(config, payload: dict, start_path: str | None = None) -> dict:
    """Add the codex domain's binding to an existing payload, WITHOUT changing its keys.

    `codex_search` predates `core.domains` and callers depend on its shape, so the
    binding is added rather than the response replaced. It matters because
    `docs/mcp-tools.md` states that all four domains report what they resolved to —
    and codex was the one that did not, so the doc was making a claim for a tool that
    could not back it. The domain tests asserted the envelope of `domains.codex_search`,
    which no MCP caller reaches. (Prudence, #725.)
    """
    from techne.core.bindings import domain_binding

    # THE SAME START PATH THE READ USED. `_codex_repo_db` resolves
    # `args["repo"] or cwd`; deriving the binding from cwd alone made the two disagree
    # the moment a caller passed `repo` — and the no-repo hint tells them to.
    report = domain_binding(config, "codex", start_path)
    return {
        **payload,
        "domain": "codex",
        "binding": report.binding,
        "state": report.state,
        "target": str(report.target) if report.target else None,
    }


def _codex_search(args: dict, config: Config) -> dict:
    """FTS over Codex symbols, scoped to the active repo (db-per-repo, techne#209)."""
    from techne.core.codegraph import (
        annotate_freshness,
        index_freshness,
        search_symbols,
        zero_result_notice,
    )

    query = args.get("query", "")
    limit = args.get("limit", 20)
    # ONE EXPRESSION FOR "WHICH TREE ARE WE ASKING ABOUT", used by the binding and the
    # freshness root. `_codex_repo_db` still re-derives it internally, so this is two of
    # three consumers, not three — the comment said three and Prudence checked. It was
    # spelled out
    # three times — `args["repo"] or cwd` inside `_codex_repo_db`, again for the
    # envelope, again for `index_freshness` — and every defect in this function this
    # week has been two of those three disagreeing. They agree today; the point is that
    # they now CANNOT disagree.
    import os as _os

    _start = args.get("repo") or _os.getcwd()
    db_path = _codex_repo_db(args, config)
    if db_path is None:
        return _with_binding(config, {"results": [], "count": 0, "notice": _NO_REPO_HINT}, _start)
    hits = annotate_freshness(db_path, search_symbols(db_path, query, limit=limit))
    # "unknown" included (techne#595 r1): a hit whose file could not be read or
    # resolved is not a trustworthy hit, and counting only stale/missing meant an
    # unreadable file returned a result with NO qualification at all.
    stale_count = sum(1 for h in hits if h.get("freshness") in ("stale", "missing", "unknown"))
    result: dict = {"results": hits, "count": len(hits)}
    if stale_count:
        result["notice"] = (
            f"{stale_count} result(s) may be outdated or unverifiable — "
            "re-run `techne codex index` to refresh."
        )
    elif not hits:
        # A ZERO RESULT MUST DESCRIBE ITS OWN RELIABILITY (techne#595). The notice
        # above is derived from the ROWS returned, so an empty result silently
        # gets none — and that is the one case where the caller most needs the
        # warning. A symbol added since the last index has no row to flag, so
        # "not indexed" is served in the same shape as "does not exist", and the
        # caller reasonably concludes the latter. Measured: `recall_attribution`
        # shipped 2026-08-13 and this tool reported it absent three days later,
        # while flagging a sibling symbol from the SAME stale file as stale.
        #
        # Same principle _codex_blast_radius already applies below (techne#303 /
        # #300): an empty result that reads as a finding must be qualified.
        # Sibling cause, fixed separately: techne#392, multi-term AND to zero.
        #
        # Only on the empty path — the scan is ~21 ms over 228 files here, worth
        # it to qualify a nothing, not worth it on every hit.
        # repo_root passed explicitly rather than left to codex_meta: a db written
        # before the meta key existed (or by a path that never set it) resolves
        # every RELATIVE file path to "unknown", and an all-unknown scan reported
        # as "current" would be this very bug wearing the fix's clothes.
        from pathlib import Path as _Path

        # PRIMARY, NOT THE WORKTREE (Prudence, #725 r3). `db_path` is the PRIMARY
        # checkout's index — #719 made the read resolve there — while `find_repo_root`
        # stops at the worktree, so freshness compared the primary's index against a
        # different tree's files and called it stale. It fires on EVERY zero-result
        # search from inside a review worktree: the envelope names the primary's db and
        # the notice beside it calls that db stale on the strength of a tree it was
        # never built from. Both halves predate this PR; the fix is the resolver #719
        # already added for exactly this.
        from techne.core.repo import primary_repo_root

        idx = index_freshness(db_path, repo_root=primary_repo_root(_Path(_start)))
        result["index"] = idx
        # Rendered by core.codegraph so this tool and the grep-assist HOOK say the
        # same thing about the same nothing (techne#602). A second copy would be a
        # second definition of "how much can this absence be trusted".
        result["notice"] = zero_result_notice(idx)
    return _with_binding(config, result, _start)


def _techne_archivist_query(args: dict, config: Config) -> dict:
    """Guarded read-only SQL against the team Archivist (techne#554).

    A denial is returned as a RESULT, not raised. The dispatcher turns an exception
    into `Error: <str>` with isError set, which tells a caller that something went
    wrong but not what the surface is — and "you may read these three tables" is the
    single most useful thing to say to someone who just asked for a fourth. Genuine
    SQL errors still raise: those ARE the caller's bug and the sqlite message is the
    best description of it.
    """
    from techne.core.archivist_query import QueryDenied, query

    try:
        return query(config, sql=args.get("sql"), limit=args.get("limit"))
    except QueryDenied as e:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "outcome": {"code": "denied", "summary": str(e)},
        }


def _techne_guide(args: dict, config: Config) -> dict:
    """Serve a bundled guide over MCP (techne#754).

    WHY THIS EXISTS SEPARATELY FROM THE CLI. The guides ship as files and `techne guide`
    reads them, which reaches any agent that has a shell. The MCP instructions tell every
    connected agent those guides exist — and a client with no shell could not collect
    what that sentence advertises. A pointer to something unreachable is worse than no
    pointer, so the same content is served here through the same accessor.

    ONE COPY STILL. This calls `techne.docs.bundled_guide`, exactly as the CLI command
    does; there is no second rendering path to drift.
    """
    from techne.docs import _MANIFEST, bundled_guide

    name = (args or {}).get("name")
    if not name:
        return {
            "guides": list(_MANIFEST),
            "hint": ("Call techne_guide with a name to read one. Start with four-functions."),
        }
    try:
        return {"name": name, "markdown": bundled_guide(name)}
    except KeyError:
        return {
            "error": f"no bundled guide named {name!r}",
            "guides": list(_MANIFEST),
        }


def _techne_capabilities(args: dict, config: Config) -> dict:
    """What is on, what is off, and what is not installed (techne#638).

    Vendor-neutral by construction: this returns structured data and the client's
    model does the narrating, so it works in any MCP client rather than being a
    Claude-only skill (techne#499). It also needs no new install step — techne
    already registers its MCP server.
    """
    from techne.core.capabilities import (
        CAPABILITIES,
        acknowledge,
        capability_report,
        clear_acknowledgement,
    )

    ack, unack = args.get("acknowledge"), args.get("unacknowledge")
    if not ack and not unack:
        return capability_report(config)
    if ack and unack:
        return {"error": "pass acknowledge or unacknowledge, not both"}

    # Opt-outs (techne#1022) ride this tool rather than a second one: the agent
    # records one in the same conversation where the report was offered.
    flags = [c.flag for c in CAPABILITIES]
    target = ack or unack
    if target not in flags:
        return {"error": f"no capability named {target!r}", "flags": flags}
    if not config.db_path.exists():
        return {"error": "techne not initialized. Run: techne init"}
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        if ack:
            try:
                recorded = acknowledge(conn, config, ack, args.get("reason") or "")
            except ValueError as exc:
                return {"error": str(exc)}
            changed = {"acknowledged": ack, **recorded}
        else:
            changed = {"unacknowledged": unack, "cleared": clear_acknowledgement(conn, unack)}
    finally:
        conn.close()
    return {**capability_report(config), "change": changed}


#: How many nodes a blast-radius response returns by default. 50 x ~350 chars keeps a
#: response near 4k tokens; the measured uncapped worst case on this repo was 44k.
_BLAST_DEFAULT_LIMIT = 50
#: `core.codegraph.reachable`'s own default walk bound. Named here so the response can
#: SAY when it has been hit instead of reporting a floor as a total.
_BLAST_WALK_CAP = 500


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
    only_proven = args.get("only_proven", False)
    # Clamped, not trusted: a caller passing limit=100000 would recreate the exact
    # token blowout this bound exists to prevent.
    limit = max(1, min(int(args.get("limit") or _BLAST_DEFAULT_LIMIT), _BLAST_WALK_CAP))
    db_path = _codex_repo_db(args, config)
    if db_path is None:
        return {
            "symbol_id": symbol_id,
            "direction": direction,
            "depth": depth,
            "nodes": [],
            "notice": _NO_REPO_HINT,
        }
    # Resolve a bare name / reject an unresolvable id, so a mistyped argument
    # returns an explicit error instead of an empty node list that reads as
    # "nothing reachable" (techne#303 — the MCP twin of the CLI fix in #300).
    try:
        symbol_id = resolve_symbol_id(db_path, symbol_id)
    except SymbolResolutionError as exc:
        result = {
            "symbol_id": args.get("symbol_id", ""),
            "direction": direction,
            "depth": depth,
            "nodes": [],
            "error": str(exc),
        }
        if exc.candidates:
            result["candidates"] = [{"id": c["id"], "file": c["file"]} for c in exc.candidates[:10]]
            # Signal the cap rather than silently truncating (parity with the
            # CLI's "... and N more"; the total is already in the error text).
            if len(exc.candidates) > 10:
                result["candidates_truncated"] = True
        return result
    # techne#606: blast radius is where the bare-name fallback does the most
    # damage — one invented hop drags in that phantom's whole upstream cone, and
    # the caller cannot tell it from a real dependent. So compute the radius
    # TWICE: the permissive walk, and the walk over proven (to_symbol) edges only.
    # Every node is then labelled, and the count resting on a guess is stated.
    # `limit=` PASSED EXPLICITLY, NOT MIRRORED. `_BLAST_WALK_CAP` was a copy of
    # `reachable`'s own default, and a copy can drift silently — the same defect class
    # as the floor bug below. Passing it makes the constant true by construction and
    # the clamp observable instead of merely labelled. (Prudence, #722.)
    raw = reachable(
        db_path,
        symbol_id,
        direction=direction,
        max_depth=depth,
        exclude_fixtures=not include_fixtures,
        limit=_BLAST_WALK_CAP,
    )
    # THE WALK SIZE, BEFORE ANY FILTER. The floor notice must key on this: the 500-node
    # bound is hit by the WALK, and `only_proven` filters afterwards — so keying on the
    # post-filter count silenced the notice in exactly the branch where truncation does
    # the most damage, that result being the intersection of two independently-capped
    # walks. Base said nothing there; the first version of this change affirmatively
    # asserted `truncated: false` while missing every real dependent, turning silence
    # into a false completeness claim. (Prudence, #722.)
    walk_size = len(raw)
    proven_ids = {
        r["id"]
        for r in reachable(
            db_path,
            symbol_id,
            direction=direction,
            max_depth=depth,
            exclude_fixtures=not include_fixtures,
            include_inferred=False,
            limit=_BLAST_WALK_CAP,
        )
    }
    hidden_inferred = 0
    if only_proven:
        filtered = [r for r in raw if r["id"] in proven_ids]
        hidden_inferred = len(raw) - len(filtered)
        raw = filtered
    for r in raw:
        r["proven"] = r["id"] in proven_ids
    rows = annotate_freshness(db_path, raw)
    stale_count = sum(1 for r in rows if r.get("freshness") in ("stale", "missing"))
    inferred_count = sum(1 for r in rows if not r["proven"])

    # CAP THE RESPONSE (tenishai/agents#10). A hot symbol's radius blew the MCP tool's
    # token ceiling outright: measured on this repo, `execute` returns 500 nodes /
    # 175,020 characters — about 44k tokens in one response — and the reviewer who hit it
    # got `Error: result exceeds maximum allowed tokens` instead of an impact set. An
    # answer too large to return is the same as no answer, except it also costs the call.
    #
    # `reachable` orders by (depth, name), so the head is the CLOSEST dependents — which
    # is what "what breaks if I change this" actually wants. Truncating the tail is the
    # cheap end of the trade.
    walked = len(rows)
    nodes = rows[:limit]
    result: dict = {
        "symbol_id": symbol_id,
        "direction": direction,
        "depth": depth,
        "nodes": nodes,
        "returned": len(nodes),
        "walked": walked,
        "truncated": walked > limit,
    }
    notices = []
    # AN EMPTY PROVEN RADIUS IS NOT "NOTHING DEPENDS ON THIS" (techne#940). Say how
    # many name-matched dependents only_proven removed, so an empty `nodes` cannot
    # be read as a clean bill.
    if hidden_inferred:
        result["hidden_inferred"] = hidden_inferred
        notices.append(
            f"only_proven hid {hidden_inferred} node(s) reached only by a bare-name match; "
            "they may be real dependents the resolver could not prove."
        )
    # NO SILENT CAPS — state every bound, including one that predates this change.
    if walked > limit:
        # NAME THE DEPTH OF WHAT WAS CUT. "The head is the closest dependents" is not
        # "all the closest": a hub with 120 dependents ALL at depth 1 loses 70 DIRECT
        # callers to a cap of 50, and those are the highest-value nodes in the radius.
        # Saying so is the difference between a truncated radius a caller can reason
        # about and one they cannot. (Prudence, #722.)
        _cut = rows[limit:]
        _d0 = min(r["depth"] for r in _cut)
        _n0 = sum(1 for r in _cut if r["depth"] == _d0)
        notices.append(
            f"Showing the {len(nodes)} closest of {walked} node(s) by depth; "
            f"{_n0} more at depth {_d0}; pass limit= (max {_BLAST_WALK_CAP}) for more."
        )
    if walk_size >= _BLAST_WALK_CAP:
        # PRE-EXISTING AND PREVIOUSLY SILENT: `reachable`'s own `limit` defaults to 500,
        # so a 500-node walk has always been indistinguishable from a complete one. That
        # makes `walked` a FLOOR, and a floor reported as a total is how "covered
        # everything" gets believed.
        notices.append(
            f"The graph walk itself stops at {_BLAST_WALK_CAP} node(s), so {walk_size} is "
            "a floor rather than the true size of the radius."
        )
    if stale_count:
        notices.append(
            f"{stale_count} node(s) may be outdated — re-run `techne codex index` to refresh."
        )
    if inferred_count:
        notices.append(
            f"{inferred_count} of {walked} walked node(s) are reached only by matching a "
            "bare call name, not a resolved symbol — treat them as candidates, not "
            "confirmed dependents. Pass only_proven=true for the resolved-only radius."
        )
    if notices:
        result["notice"] = " ".join(notices)
    return result


def _domain_search(domain: str):
    """Address one knowledge domain BY NAME (techne#552, the connection seam).

    The four names — archivist, librarian, architect, codex — are the vocabulary an
    operator learns in techne and keeps when the infrastructure grows into liminal and
    cabal. A local librarian may become a cabal persona and later a deployed service;
    the CALL does not change, only the `binding` in the answer.

    RECALL IS STILL THE DEFAULT. `techne_recall` escalates across all four rungs and is
    the right answer to "what do we know about X". These are for when you already know
    which function you want.
    """

    def handler(args: dict, config: Config) -> dict:
        from techne.core.domains import SEARCH

        return SEARCH[domain](config, args.get("query", ""), limit=args.get("limit", 20))

    handler.__name__ = f"_{domain}_search"
    return handler


_archivist_search = _domain_search("archivist")
_librarian_search = _domain_search("librarian")
_architect_search = _domain_search("architect")


def _pattern_search(args: dict, config: Config) -> dict:
    """FTS over the Architect's pattern corpus (architect.db). Optionally scoped to
    patterns that apply to a given project target."""
    from techne.core.architect import architect_db
    from techne.core.patterns import patterns_for_target, search_patterns

    db = architect_db(config)
    query = args.get("query", "")
    limit = args.get("limit", 20)
    target = args.get("target")
    notice = None
    if target:
        from techne.core.patterns import known_targets

        allowed_targets = known_targets()
        if target not in allowed_targets:
            # THE ROSTER GOVERNS WRITING, NOT READING, and conflating the two
            # turned an upgrade into a read regression (#1014 r2). A store whose
            # rows predate the config would have had them declared "unknown"
            # while `techne architect applies` still listed them — the CLI and
            # MCP disagreeing about the same row.
            #
            # So the store decides. Rows present: serve them, and say the target
            # is unconfigured, because that is exactly when the operator needs
            # to hear it. No rows: this is #674's case, where silence read as
            # "no patterns exist" — error instead.
            existing = patterns_for_target(db, target, limit=1)
            if not existing:
                return {
                    "error": (
                        f"unknown target {target!r}. Configured targets: "
                        f"{', '.join(sorted(allowed_targets))}. "
                        "Set architect.pattern_targets, or run `techne architect targets`."
                    ),
                    "results": [],
                    "count": 0,
                }
            notice = (
                f"{target!r} holds patterns but is not in architect.pattern_targets "
                f"({', '.join(sorted(allowed_targets))}) — these rows are served, but NEW "
                "patterns will not be tagged with it. Run `techne architect targets`."
            )
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
    out: dict = {"results": hits, "count": len(hits)}
    if notice:
        out["notice"] = notice
    return out


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

    Thin wrapper: the pass itself lives in core.reflect.emit_reflection_pass, shared
    with `techne reflect emit`. It was inlined here, and the CLI's separate
    implementation silently skipped the cursor advance (tenishai/techne#645).
    """
    from techne.core.reflect import emit_reflection_pass

    return emit_reflection_pass(config, args.get("session_id"), limit=args.get("limit"))


def _techne_reflect_ingest(args: dict, config: Config) -> dict:
    """Persist one thread's agent-produced reflection answer (techne#19).

    `response` is the JSON candidate array (as a string) the agent produced for a
    `prompt` from techne_reflect_emit; `thread_id` and `prompt_hash` are the values
    from that emit batch item. Candidates land in knowledge_candidates for review
    (`techne reflect review`); they only touch live knowledge/identity when
    config.reflect.write_to == 'live'. Call once per answered thread.
    """
    from techne.core.reflect import ingest_one_and_drain

    response = args.get("response", "")
    # Hand the raw response straight to the core. An earlier wrapper here special-cased
    # a whitespace/empty response with {"note": "nothing to ingest"} and no `errors`
    # key — while the core it wraps calls that identical input `parse_failures=1,
    # errors=1` ("empty response"). That erased the exact distinction #465 exists to
    # draw — A DISCARDED REFLECTION IS NOT AN EMPTY ONE — at the boundary the agent
    # actually talks through. Hand every response to the core and report what it
    # reports; the counts shape then comes from new_ingest_counts() upstream and
    # cannot drift from this call site again.
    #
    # The drain half now lives in ingest_one_and_drain, shared with `techne reflect
    # ingest` — it was inlined here, so the CLI path never marked debt paid (#644).
    result = ingest_one_and_drain(
        config,
        args.get("session_id", ""),
        args.get("thread_id", ""),
        args.get("prompt_hash", ""),
        response,
        model_used=args.get("model_used", "agent-assisted"),
        obs_key=args.get("obs_key") or None,
    )
    # SAY SO IN THE RESULT, NOT ONLY IN THE SCHEMA (Prudence, #955 r2). An emit item
    # with no `thread_id` is claimed by its `obs_key`; without it nothing records the
    # answer and the documented drain loop re-offers the same thread forever. The
    # agent reads the result, so the warning goes where it will be seen.
    if not args.get("thread_id") and not args.get("obs_key"):
        result["warning"] = (
            "no thread_id and no obs_key: this answer was stored, but the thread stays "
            "unclaimed and emit will offer it again. Pass the emit item's obs_key."
        )
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
    """Verify structured claims against the ledger and against project state.

    Two gates behind one call:

    * DEED claims (techne#105) — "I committed", "tests pass", "I wrote X" —
      go to check_claims() against the observation ledger.
    * STATE claims (techne#584) — artifact_state, capability — go to
      check_state_claims() against GitHub and our own source.  (An ownership
      type, mined from the coord bus, was built and cut before merge — #894.)
      These are the conversational half: assertions with no diff and no suite,
      where the operator has been the only gate.

    Verdicts come back in the caller's claim order, so a mixed list reads as one
    result.  Both halves are advisory: flag, never block.
    """
    from pathlib import Path

    from techne.core.evidence import check_claims, query_turn_evidence
    from techne.core.state_claims import (
        STATE_CLAIM_TYPES,
        StateContext,
        check_state_claims,
    )

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

    # Split by type, check each half, then reassemble IN THE CALLER'S ORDER.
    # A caller that sent one list should get one list back, aligned with it.
    deed_idx = [i for i, c in enumerate(claims) if str(c.get("type", "")) not in STATE_CLAIM_TYPES]
    state_idx = [i for i, c in enumerate(claims) if str(c.get("type", "")) in STATE_CLAIM_TYPES]

    verdicts: list[dict] = [{} for _ in claims]
    for i, v in zip(deed_idx, check_claims([claims[i] for i in deed_idx], ev)):
        verdicts[i] = v
    if state_idx:
        ctx = StateContext(
            repo_root=Path(args["repo_root"]) if args.get("repo_root") else Path.cwd(),
        )
        for i, v in zip(state_idx, check_state_claims([claims[i] for i in state_idx], ctx)):
            verdicts[i] = v

    unbacked_count = sum(1 for v in verdicts if v["status"] == "unbacked")
    unverifiable_count = sum(1 for v in verdicts if v["status"] == "unverifiable")

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
        # Surfaced separately so a caller cannot read "0 unbacked" as "all
        # checked". Unverifiable means the claim COULD NOT BE ESTABLISHED — the
        # source was unreadable, OR it was read and simply does not speak to the
        # claim. Both are states the operator needs to see rather than infer.
        # Saying only "could not be read" understates it: an absence claim whose
        # anchor is missing from the file is unverifiable after a wholly
        # successful read (Constance, #892; anchor added #892 r3).
        "unverifiable_count": unverifiable_count,
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
    "techne_archivist_query": _techne_archivist_query,
    "techne_guide": _techne_guide,
    "techne_capabilities": _techne_capabilities,
    "archivist_search": _archivist_search,
    "librarian_search": _librarian_search,
    "architect_search": _architect_search,
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

    # techne#696 — take the module baseline HERE, before serving anything. Building it
    # lazily on the first tool call was wrong: `mcp_server` alone carries 46
    # function-local `from techne…` imports, so by the first call the process has
    # already pulled in modules whose files may have changed, and each would be
    # baselined at its POST-change mtime and never reported. One stat-walk of the
    # modules already loaded (~21, measured 0.29 ms), once, at startup.
    global _RUNTIME_WATCH
    try:
        from techne.core import runtime_drift

        _RUNTIME_WATCH = runtime_drift.begin(config)
    except Exception:  # noqa: BLE001 — a diagnostic must never cost the server its start
        _RUNTIME_WATCH = None

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
