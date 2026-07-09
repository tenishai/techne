#!/usr/bin/env python3
"""UserPromptSubmit hook — inject per-turn recalled knowledge (techne#109/#277).

The DYNAMIC per-turn complement to the STABLE SessionStart injection: on each user
prompt, when the opt-in flag ``hooks.per_turn_recall`` is ON, run a knowledge
search scoped to the submitted prompt and inject the most relevant durable facts
into the model's context via ``hookSpecificOutput.additionalContext``.

Design note (techne#109): SessionStart content is injected once and stays stable
for the session; per-turn recall belongs on UserPromptSubmit so it lands *after*
that stable prefix — the dynamic tail that changes per turn without disturbing the
stable head. (Whether Claude Code exploits Anthropic prompt caching across that
boundary is harness-controlled and undocumented — this hook delivers the per-turn
recall capability regardless; any cache benefit is a bonus we can't set from here.)

Discipline (same fail-safe posture as pre_tool_use / session_start):
  - NEVER blocks the turn. EVERY path exits 0 — exit 2 would ERASE the prompt.
  - stdout is ONLY ever the UserPromptSubmit protocol JSON; when there's nothing to
    add, nothing is printed. ALL logging/errors go to stderr.
  - Any error (config load, DB unreachable, empty search, HookTimeout) → no-op.

Protocol shape (verified against code.claude.com/docs/en/hooks, 2026-07-09):
  {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                          "additionalContext": "..."}}

Prompt-field name: the CC hooks reference does NOT document the stdin field that
carries the prompt text (verified against the primary source 2026-07-09). So we
accept any of the known candidate keys and no-op safely if none is present — a
wrong/renamed field yields an empty prompt and thus a clean no-op, never a fire on
garbage.
"""

from __future__ import annotations

import json
import sys

# The stdin key holding the submitted prompt text is undocumented (see module
# docstring). Accept known candidates in priority order.
PROMPT_KEYS = ("prompt", "user_prompt", "text")


def _build_context(rows: list[dict]) -> str:
    """Render recalled knowledge rows into a compact additionalContext block.

    Mirrors egress `_render_line` formatting: each row carries its ⟦techne#id⟧
    provenance prefix so injected facts are attributable and honestly framed as
    recalled memory (verify-before-relying), not asserted ground truth.
    """
    lines = [
        "## techne — durable facts relevant to this prompt",
        "> Per-turn recall (techne#109); recalled memory, not ground truth — verify first.",
        "",
    ]
    for r in rows:
        rid = r.get("id")
        rtype = r.get("type") or "note"
        content = (r.get("content") or "")[:200]
        lines.append(f"- ⟦techne#{rid}⟧ [{rtype}] {content}")
    return "\n".join(lines)


def main() -> None:
    # ------------------------------------------------------------------
    # Parse stdin. Malformed → no-op.
    # ------------------------------------------------------------------
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    # Resolve the prompt text from whichever known key is present (field name is
    # undocumented). No usable prompt → no-op.
    prompt = ""
    for key in PROMPT_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            prompt = val.strip()
            break
    if not prompt:
        sys.exit(0)

    # ------------------------------------------------------------------
    # Config load can raise (corrupt/unreadable config). A hook that throws would
    # surface as an error on the turn — degrade to a no-op instead.
    # ------------------------------------------------------------------
    try:
        from techne.config import Config

        config = Config.load()
    except Exception as e:  # noqa: BLE001
        print(f"techne: config load failed, skipping per-turn recall: {e}", file=sys.stderr)
        sys.exit(0)

    # Default-off gate: this is opt-in, experimental.
    if not config.hooks.per_turn_recall:
        sys.exit(0)

    from techne.hooks._timeout import bounded_hook

    # ------------------------------------------------------------------
    # Run the query inside the wall-clock budget. Scope to durable knowledge (not
    # raw observations) and exclude soft-archived rows — mirrors egress discipline:
    # archived stays searchable but is never auto-injected. Any failure
    # (DB unreachable, slow SQLite → HookTimeout) is swallowed → emit nothing.
    # ------------------------------------------------------------------
    try:
        from techne.core.search import search

        with bounded_hook(config.hooks.timeout_s):
            rows = search(
                config.db_path,
                prompt,
                table="knowledge",
                limit=config.hooks.per_turn_recall_limit,
                include_archived=False,
            )
    except Exception as e:  # noqa: BLE001  — catches HookTimeout too
        print(f"techne: per-turn recall degraded: {e}", file=sys.stderr)
        sys.exit(0)

    if not rows:
        sys.exit(0)

    # ------------------------------------------------------------------
    # Emit the protocol JSON (additionalContext) and exit 0.
    # ------------------------------------------------------------------
    try:
        context = _build_context(rows)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context,
                    }
                }
            )
        )
    except Exception as e:  # noqa: BLE001
        print(f"techne: per-turn recall emit failed: {e}", file=sys.stderr)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
