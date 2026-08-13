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

#: Liveness key — must match the entry in ``checks._HOOK_EXPECTATIONS``, since
#: that is what turns a written marker into a reported verdict (techne#479).
HOOK_NAME = "user_prompt_submit"


def _record(record_hook_run, config, degraded: str | None, *, outcome: str, detail: str = ""):
    """Write the liveness marker, folding in a degraded recall if one happened.

    A DEGRADED RECALL MUST NOT READ AS A CLEAN RUN. The recall block is caught
    and swallowed by design — the seed block should still emit when search dies —
    so without this, a rail whose every search raises would keep writing
    ``outcome=ok`` forever and report LIVE. That is the shape of the bug this
    module exists to surface, one level down. When recall degraded, the run is
    recorded as failed and carries the exception, whatever else succeeded.
    """
    if degraded is not None:
        record_hook_run(
            config.db_path,
            HOOK_NAME,
            outcome="failed",
            error={"stage": "recall", "msg": degraded},
        )
        return
    record_hook_run(config.db_path, HOOK_NAME, outcome=outcome, detail=detail or None)


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


# Types this PUSH rail must not inject (techne#561).
#
# `PULL_ONLY_TYPES` in core/knowledge.py is the real list, but it is enforced in
# exactly ONE place — egress.py, the session_start selector — and its own comment
# scopes itself to "never auto-injected into session-start context". This rail
# post-dates it and calls `search()` directly, which applies no type filter, so
# pull-only rows have been arriving on a push path the constant never covered.
#
# `pattern` is the unambiguous half and the only one fixed here. It lives in the
# Architect store; REDIRECTED_TYPES records that rows written to techne.db
# "leaked team patterns into personal recall (recall doesn't filter by type)",
# and `store()` rejects new ones — but legacy rows survive on un-cleaned installs,
# which is exactly why PULL_ONLY_TYPES still lists it. Auto-injecting those is the
# leak the write-time invariant was built to stop.
#
# `document_card` is DELIBERATELY NOT HERE. Whether an arch card belongs on a
# query-driven per-turn rail is a live design question (#561): it is arguably the
# "pull at the moment of need" that #383/techne#945 wanted, and it is also
# arguably the startup flood relocated. Adding it is a one-line change once that
# is settled; guessing now would silently decide it.
#
# Filtered here rather than inside `search()` so the PULL path — techne_recall /
# techne_search — keeps returning these rows, which is the whole point of
# "pull-only". Trade-off: dropping post-search can return fewer than
# `per_turn_recall_limit` rows. That shrink is already visible in
# context_injections (hit_count vs k_requested, techne#413) and is preferable to
# re-ranking to backfill, which would change what the rail selects.
_PUSH_EXCLUDED_TYPES = frozenset({"pattern", "document"})


def _drop_pull_only(db_path, rows: list[dict]) -> list[dict]:
    """Drop rows whose knowledge.type is push-excluded, by looking the type UP.

    THE ROW'S OWN ``type`` FIELD CANNOT BE USED. ``search()`` sets ``type`` to the
    SOURCE TABLE ("knowledge"), not to the knowledge row's ``type`` column — which
    is also why every line this rail renders reads ``[knowledge]`` regardless of
    what it actually is, and therefore why a `pattern` row riding this rail was
    indistinguishable from a fact at a glance (techne#561).

    One primary-key lookup over the ids just returned. Raising here degrades the
    whole recall to nothing via the caller's handler — failing CLOSED, which is the
    right direction for a filter whose job is to keep rows out.
    """
    ids = [r.get("id") for r in rows if r.get("id") is not None]
    if not ids:
        return rows
    from techne.core.stores import plain_connect

    conn = plain_connect(db_path)
    try:
        placeholders = ",".join("?" * len(ids))
        excluded = {
            row[0]
            for row in conn.execute(
                f"SELECT id, type FROM knowledge WHERE id IN ({placeholders})", ids
            )
            if row[1] in _PUSH_EXCLUDED_TYPES
        }
    finally:
        conn.close()
    return [r for r in rows if r.get("id") not in excluded]


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
    # LIVENESS (techne#479). This hook hosts per_turn_recall — half the founding
    # incident techne#468 cites, and one of the two rails techne is FOR. Until
    # now it reported nothing at all, so a dead retrieval rail and an idle
    # operator produced identical evidence.
    #
    # NOTHING IS RECORDED BEFORE THE CONFIG GATE, deliberately. Both flags
    # default OFF, this fires on EVERY prompt, and a marker write on a rail the
    # operator has not enabled would be a per-turn write for no signal.
    # `check_hooks` reads `_HOOK_CONFIG_GATE` FIRST and reports DORMANT from the
    # config alone, so the disabled case is already answerable without a marker.
    from techne.hooks._liveness import record_hook_run, resolve_db_path

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
        # Recorded even though the gate is unknown here: a config that cannot be
        # read is a real failure, and it is precisely the state where every other
        # signal about this rail is unavailable.
        db = resolve_db_path()
        if db:
            record_hook_run(
                db,
                HOOK_NAME,
                outcome="failed",
                error={"stage": "config_load", "msg": str(e)[:200]},
            )
        sys.exit(0)

    # Both per-turn injections are opt-in (default OFF). Neither on → no-op.
    # No marker: see the note at the top of main().
    if not (config.hooks.per_turn_recall or config.hooks.per_turn_seed):
        sys.exit(0)

    blocks: list[str] = []
    degraded: str | None = None
    rows: list = []

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

            with bounded_hook(config.hooks.timeout_s, label="user_prompt_submit"):
                rows = search(
                    config.db_path,
                    prompt,
                    table="knowledge",
                    limit=config.hooks.per_turn_recall_limit,
                    include_archived=False,
                )
                rows = _drop_pull_only(config.db_path, rows)
            recall_block = _build_context(rows) if rows else ""
            if recall_block:
                blocks.append(recall_block)
            # techne#329 — make the READ path auditable (the audit that motivated
            # this couldn't see the rail at all). Best-effort; the helper never
            # raises. Log even an empty recall (fired but matched nothing = signal).
            if config.hooks.per_turn_recall_log:
                from techne.hooks._injection_log import log_context_injection

                log_context_injection(
                    config.db_path,
                    source="per_turn_recall",
                    session_id=data.get("session_id"),
                    trigger=prompt,
                    rows=rows,
                    injected_text=recall_block,
                    k_requested=config.hooks.per_turn_recall_limit,
                )
        except Exception as e:  # noqa: BLE001  — catches HookTimeout too
            print(f"techne: per-turn recall degraded: {e}", file=sys.stderr)
            degraded = str(e)[:200]

    # ------------------------------------------------------------------
    # Seed block (techne#277) — static nudge to store a durable if one emerged.
    # No DB query; the model self-gates and the session-end batch reconciles.
    # ------------------------------------------------------------------
    if config.hooks.per_turn_seed:
        blocks.append(_SEED_NUDGE)

    if not blocks:
        # "INJECTED NOTHING" IS A LEGAL OUTCOME, not a failure (#414) — a recall
        # that fired and matched nothing is a fact worth recording, and it is the
        # only thing that distinguishes a working-but-quiet rail from a dead one.
        _record(record_hook_run, config, degraded, outcome="noop:nothing-to-inject")
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
        _record(record_hook_run, config, degraded, outcome="ok", detail=f"{len(rows)} rows")
    except Exception as e:  # noqa: BLE001
        print(f"techne: per-turn hook emit failed: {e}", file=sys.stderr)
        record_hook_run(
            config.db_path,
            HOOK_NAME,
            outcome="failed",
            error={"stage": "emit", "msg": str(e)[:200]},
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
