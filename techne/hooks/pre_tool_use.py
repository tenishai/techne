#!/usr/bin/env python3
"""PreToolUse hook — graph-augment the agent's own Grep with Codex (techne#206).

When the agent issues a Grep tool call AND the opt-in flag
``hooks.codex_grep_assist`` is ON, this hook runs a Codex symbol search for the
search pattern and injects the matching symbols into the model's context via
``hookSpecificOutput.additionalContext``.

Discipline (same fail-safe posture as post_tool_use / session_start):
  - NEVER blocks the tool. EVERY path exits 0.
  - stdout is ONLY ever the PreToolUse protocol JSON; when there's nothing to
    add, nothing is printed. ALL logging/errors go to stderr.
  - Any error (config load, missing codex.db, empty search) degrades to a no-op.

Protocol shape (confirmed: PreToolUse honors additionalContext without blocking):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow",
                          "additionalContext": "..."}}
"""

from __future__ import annotations

import json
import os
import sys

# Tools whose pattern is a symbol-ish search worth augmenting with the code graph.
# Grep only: Glob's "pattern" is a file glob (e.g. **/*.py), not a symbol name, so
# feeding it to the symbol FTS yields noise/nothing — excluded (techne#206 dogfood).
ASSIST_TOOLS = {"Grep"}

# Liveness key namespace: `hook:codex_grep_assist:last_run` (techne#468). Named
# for the RAIL rather than the hook file, because that is the thing an operator
# turns on in config and therefore the thing they expect a status for.
HOOK_NAME = "codex_grep_assist"


def _build_context(pattern: str, results: list[dict]) -> str:
    """Render matching symbols into a compact additionalContext block.

    Mirrors cli.py's `codex search` formatting. Freshness honesty (techne#89):
    any row whose freshness is NOT fresh/unknown carries a visible marker so the
    model knows the graph may be behind the working tree.
    """
    lines = [
        f"## Codex — symbols matching `{pattern}`",
        "> Experimental graph-assist (techne#206); the graph may be stale — verify before relying.",
        "",
    ]
    for s in results:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        lines.append(f"  {s['id']}  [{s['kind']}] {s['name']}  {s.get('signature') or ''}{marker}")
        lines.append(f"    {s['file']}")
    return "\n".join(lines)


def main() -> None:
    # ------------------------------------------------------------------
    # Parse stdin. Malformed → no-op.
    # ------------------------------------------------------------------
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in ASSIST_TOOLS:
        sys.exit(0)

    # ------------------------------------------------------------------
    # Config load can raise (corrupt/unreadable config). A PreToolUse hook that
    # throws would surface as a tool-call failure — degrade to a no-op instead.
    # ------------------------------------------------------------------
    try:
        from techne.config import Config

        config = Config.load()
    except Exception as e:  # noqa: BLE001
        print(f"techne: config load failed, skipping grep-assist: {e}", file=sys.stderr)
        sys.exit(0)

    # Default-off gate: this is opt-in, experimental. Deliberately NOT recorded
    # as a liveness event — a disabled rail is DORMANT by configuration, not a
    # rail that ran and did nothing, and conflating the two is what makes
    # "enabled" indistinguishable from "working" (techne#468).
    if not config.hooks.codex_grep_assist:
        sys.exit(0)

    # From here on the rail is ON, so every exit path names itself. Before this
    # (techne#468) all five returned a silent exit 0 and wrote nothing anywhere,
    # which made this hook's liveness undecidable rather than merely unknown.
    from techne.hooks._liveness import record_hook_run

    # NB: deliberately NOT named `db_path` — that name is rebound below to the
    # per-repo CODEX db, and liveness belongs in the main techne db.
    techne_db = config.db_path

    tool_input = data.get("tool_input", {})
    pattern = tool_input.get("pattern", "")
    if not pattern:
        record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-pattern")
        sys.exit(0)

    # Resolve which repo the agent is actually working in: the Grep's target
    # `path` when given, else the session cwd. This scopes the injected symbols
    # to the repo being searched (techne#209 db-per-repo) — the fix for the
    # #206 signal/noise problem, where a single global graph injected an
    # unrelated repo's symbols.
    start_path = tool_input.get("path") or data.get("cwd") or os.getcwd()

    from techne.hooks._timeout import bounded_hook

    # ------------------------------------------------------------------
    # Resolve the per-repo codex db and run the query inside the wall-clock
    # budget. No indexed db for this repo → no-op. Any failure (search_symbols
    # raises; slow SQLite → HookTimeout) is swallowed and degrades to emitting
    # nothing.
    # ------------------------------------------------------------------
    try:
        from techne.core.codegraph import annotate_freshness, search_symbols
        from techne.core.codex import resolve_repo_db_for_query

        db_path = resolve_repo_db_for_query(config, start_path)
        if db_path is None:
            # The single most likely reason this rail looks "on but does
            # nothing": the repo being searched was never indexed.
            record_hook_run(
                techne_db, HOOK_NAME, outcome="noop:no-repo-db", detail=str(start_path)
            )
            sys.exit(0)
        with bounded_hook(config.hooks.timeout_s, label="pre_tool_use"):
            results = annotate_freshness(
                db_path,
                search_symbols(db_path, pattern, limit=config.hooks.codex_grep_assist_limit),
            )
    except Exception as e:  # noqa: BLE001  — catches HookTimeout too
        print(f"techne: grep-assist degraded: {e}", file=sys.stderr)
        record_hook_run(
            techne_db,
            HOOK_NAME,
            outcome="noop:degraded",
            error={"stage": "search", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)

    if not results:
        record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-results")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Emit the protocol JSON (allow + additionalContext) and exit 0.
    # ------------------------------------------------------------------
    try:
        context = _build_context(pattern, results)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "additionalContext": context,
                    }
                }
            )
        )
    except Exception as e:  # noqa: BLE001
        print(f"techne: grep-assist emit failed: {e}", file=sys.stderr)
        record_hook_run(
            techne_db,
            HOOK_NAME,
            outcome="noop:emit-failed",
            error={"stage": "emit", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)

    # The one path where the rail actually did its job. `detail` carries the
    # symbol count so `techne status` can show utilization, not just liveness.
    record_hook_run(techne_db, HOOK_NAME, outcome="ok", detail=f"{len(results)} symbols")
    sys.exit(0)


if __name__ == "__main__":
    main()
