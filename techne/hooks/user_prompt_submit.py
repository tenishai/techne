#!/usr/bin/env python3
"""UserPromptSubmit hook — per-turn recall (read) + seed (write) (techne#109/#277).

The DYNAMIC per-turn complement to the STABLE SessionStart injection. Two
independent, opt-in (default-OFF) blocks land in ``additionalContext`` after the
stable prefix:

  - RECALL (``hooks.per_turn_recall``, techne#109): search knowledge scoped to
    the submitted prompt and inject the most relevant durable facts — the READ
    half. Injected only when the search returns rows.
  - SEED (``hooks.per_turn_seed``, techne#277): a static nudge asking the
    in-session agent to ``techne_store`` a durable fact/decision/correction if
    one emerged this turn — the WRITE half, riding the already-warm context.
    Best-effort and model-self-gated; the session-end reflect batch
    reconciles/dedups the stream of seeds. This is the interactive complement to
    end-of-session reflect, NOT a replacement — the batch stays the safety net.

Either block can fire alone; a recall failure never suppresses the seed nudge.

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


# Per-turn SEED nudge (techne#277). Static text — rides the already-warm
# context, costs no DB query, and the model self-gates (stores only a genuine
# durable). The session-end reflect batch reconciles/dedups the stream of seeds.
_SEED_NUDGE = (
    "## techne — seed check (techne#277)\n"
    "> Per-turn seed nudge; best-effort, reconciled + deduped at session end.\n"
    "\n"
    "If a durable **fact**, **decision**, or **correction** emerged this turn — "
    "one worth recalling in a future session — call `techne_store` once (one "
    "line; pick the type: fact / decision / evaluation / procedure — store a "
    "correction as a fact). If nothing durable emerged, do nothing. Skip "
    "transient chatter, restatements of existing memory, and this turn's "
    "mechanics."
)


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
    # Valid-but-non-object JSON ([], "foo", 5) would make data.get raise below;
    # keep the "malformed stdin → clean no-op" contract for those too.
    if not isinstance(data, dict):
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

    # Both per-turn injections are opt-in (default OFF). Neither on → no-op.
    if not (config.hooks.per_turn_recall or config.hooks.per_turn_seed):
        sys.exit(0)

    blocks: list[str] = []

    # ------------------------------------------------------------------
    # Recall block (techne#109) — durable facts relevant to this prompt. Scope
    # to knowledge (not raw observations), exclude soft-archived rows. Any
    # failure (DB unreachable, slow SQLite → HookTimeout) degrades to no recall
    # block WITHOUT aborting — the seed block can still emit.
    # ------------------------------------------------------------------
    if config.hooks.per_turn_recall:
        try:
            from techne.core.search import search
            from techne.hooks._timeout import bounded_hook

            with bounded_hook(config.hooks.timeout_s):
                rows = search(
                    config.db_path,
                    prompt,
                    table="knowledge",
                    limit=config.hooks.per_turn_recall_limit,
                    include_archived=False,
                )
            recall_block = _build_context(rows) if rows else ""
            if recall_block:
                blocks.append(recall_block)
            # techne#329 — make the READ path auditable (the audit that motivated
            # this couldn't see the rail at all). Best-effort; the helper never
            # raises. Log even an empty recall (fired but matched nothing = signal).
            if config.hooks.per_turn_recall_log:
                from techne.hooks._recall_log import log_recall_injection

                log_recall_injection(
                    config.db_path,
                    session_id=data.get("session_id"),
                    prompt=prompt,
                    rows=rows,
                    injected_text=recall_block,
                )
        except Exception as e:  # noqa: BLE001  — catches HookTimeout too
            print(f"techne: per-turn recall degraded: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Seed block (techne#277) — static nudge to store a durable if one emerged.
    # No DB query; the model self-gates and the session-end batch reconciles.
    # ------------------------------------------------------------------
    if config.hooks.per_turn_seed:
        blocks.append(_SEED_NUDGE)

    if not blocks:
        sys.exit(0)

    # ------------------------------------------------------------------
    # Emit the combined protocol JSON (additionalContext) and exit 0.
    # ------------------------------------------------------------------
    try:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": "\n\n".join(blocks),
                    }
                }
            )
        )
    except Exception as e:  # noqa: BLE001
        print(f"techne: per-turn hook emit failed: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
