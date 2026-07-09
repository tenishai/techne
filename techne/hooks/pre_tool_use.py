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
import sys

# Tools whose pattern is a symbol-ish search worth augmenting with the code graph.
# Grep only: Glob's "pattern" is a file glob (e.g. **/*.py), not a symbol name, so
# feeding it to the symbol FTS yields noise/nothing — excluded (techne#206 dogfood).
ASSIST_TOOLS = {"Grep"}


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

    # Default-off gate: this is opt-in, experimental.
    if not config.hooks.codex_grep_assist:
        sys.exit(0)

    pattern = data.get("tool_input", {}).get("pattern", "")
    if not pattern:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    # ------------------------------------------------------------------
    # Run the Codex query inside the wall-clock budget. Any failure (missing/empty
    # codex.db → search_symbols raises; slow SQLite → HookTimeout) is swallowed and
    # degrades to emitting nothing.
    # ------------------------------------------------------------------
    try:
        with bounded_hook(config.hooks.timeout_s):
            from techne.core.codegraph import annotate_freshness, search_symbols
            from techne.core.codex import codex_db

            db_path = codex_db(config)
            results = annotate_freshness(
                db_path,
                search_symbols(db_path, pattern, limit=config.hooks.codex_grep_assist_limit),
            )
    except Exception as e:  # noqa: BLE001  — catches HookTimeout too
        print(f"techne: grep-assist degraded: {e}", file=sys.stderr)
        sys.exit(0)

    if not results:
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
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
