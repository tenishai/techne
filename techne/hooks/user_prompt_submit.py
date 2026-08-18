#!/usr/bin/env python3
"""UserPromptSubmit hook — recall (read) + seed (write) + startup (techne#109/#277/#643).

The DYNAMIC per-turn complement to the STABLE SessionStart injection. Three
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
  - STARTUP (``hooks.startup_directive``, techne#643): once per session, on the
    FIRST prompt and only when reflection work is actually pending, ask the
    in-session agent to spawn a subagent that drains it. Lives here rather than
    on SessionStart because ``prompt`` and ``agent`` hooks are barred from that
    event and MCP servers have not connected yet — the first prompt is the
    earliest point where a model and techne's own tools both exist.

Any block can fire alone; a recall failure never suppresses the seed nudge.

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


#: State key prefix for the once-per-session startup gate. Session-scoped, so a
#: new session re-arms without any cleanup pass; `once: true` on the hook entry
#: could not do this job — the CC docs say it is "only honored for hooks declared
#: in skill frontmatter; ignored in settings files" (verified 2026-08-18).
STARTUP_STATE_PREFIX = "startup_directive"


def _startup_block(config, session_id: str | None) -> str | None:
    """First-prompt startup directive (techne#643), or None to stay silent.

    WHY THIS IS NOT A SessionStart HOOK, which is where an operator looks for it
    first: `prompt` and `agent` hooks are barred from SessionStart ("subagents
    aren't available at session initialization"), and SessionStart fires before
    MCP servers finish connecting, so techne's own tools are absent there too
    (both verified against code.claude.com/docs/en/hooks, 2026-08-18). The first
    prompt is the earliest point where a model and the MCP tools both exist.

    THE NUDGE IS GATED ON WORK EXISTING, not merely on the session being new. A
    directive that fires on every session regardless is the failure this whole
    change is fixing — the banner has carried a reflection-debt line since
    2026-08-14 and it stopped being read. Silence when there is nothing to do is
    what keeps the noisy case meaningful.

    SIGNAL IS `pending_reflection`, NOT `pending_reflection_queue`, and the
    difference is load-bearing. The queue is the honest ledger of debt, but it
    currently holds rows that can never produce a prompt (#646: the
    min_observations gate is evaluated twice against separately-derived buckets),
    so gating on it would nudge every session forever about work no drain can
    clear. `pending_reflection` self-corrects instead: emit sets the
    `reflect:emitted:<id>` marker even on an empty batch, so a non-reflectable
    session is nudged at most once. That self-correction holds only on the MCP
    path today — the CLI never sets the cursor (#645) — which is the second
    reason the directive below names the MCP tools explicitly.

    Any failure returns None. A startup nudge is the least important thing this
    hook does; it must never cost a turn.
    """
    from techne.core.extension_state import get_state, set_state
    from techne.core.reflect import pending_reflection

    # No session id → no gate is possible, and an ungated directive would fire on
    # EVERY prompt. Stay silent rather than nag.
    if not session_id:
        return None

    from techne.core.database import connect

    key = f"{STARTUP_STATE_PREFIX}:{session_id}"
    conn = connect(config.db_path)
    try:
        if get_state(conn, key) is not None:
            return None
        pending = pending_reflection(config)
        # MARK FIRED EVEN WHEN THERE IS NOTHING TO SAY. The gate's job is "once
        # per session", not "once per session that had work" — without this the
        # check re-runs on every prompt of a quiet session, which is a per-turn
        # DB read for a nudge that is not coming.
        set_state(conn, key, {"fired": bool(pending), "sessions": len(pending)})
    finally:
        conn.close()

    if not pending:
        return None

    total = sum(p.get("observations", 0) for p in pending)
    return (
        "## techne — startup drain (techne#643)\n"
        "> First prompt of this session. Fires once, only when work is pending.\n"
        "\n"
        f"{len(pending)} session(s) carrying ~{total} unreflected observation(s). "
        "Spawn a subagent to drain them so the work stays out of this session's "
        "context, and keep going with what the operator actually asked for — "
        "report the result when it lands.\n"
        "\n"
        "The subagent MUST use the MCP tools `techne_reflect_emit` / "
        "`techne_reflect_ingest`, NOT `techne reflect` on the CLI: the CLI path "
        "never marks the queue drained (#644) and never sets the emitted cursor "
        "(#645), so a CLI drain re-does the same threads every session. Pass an "
        "explicit session_id — emit defaults to the most recent session, which is "
        "this freshly-created empty one (techne#628).\n"
        "\n"
        "Threads that stay queued after a clean drain are #646, not a failure: "
        "they are sub-threshold at emit time and cannot produce a prompt. Leave "
        "them queued and say so — do not force them."
    )


# PULL-ONLY MEANS PULL-ONLY ON EVERY PUSH RAIL, NOT JUST SESSION_START (techne#561).
#
# `PULL_ONLY_TYPES` was enforced in exactly ONE place — egress.py, the
# session_start selector — and its own comment scoped itself to "never
# auto-injected into session-start context". This rail post-dates it and calls
# `search()` directly, which applies no type filter, so pull-only rows have been
# arriving on a push path the constant never covered: `pattern` and
# `document_card` both.
#
# `pattern` is the flat-out leak. It belongs in the Architect store;
# REDIRECTED_TYPES records that rows written to techne.db "leaked team patterns
# into personal recall (recall doesn't filter by type)", and `store()` rejects new
# ones — but legacy rows survive on un-cleaned installs, which is exactly why
# PULL_ONLY_TYPES still lists the type.
#
# `document_card` is the decided half (Richard, 2026-08-13). The case for keeping
# arch cards here was that a query-driven per-turn rail IS the "pull at the moment
# of need" #383 and techne#945 wanted. What decided it against: `_build_context`
# truncates every row to 200 chars, and an arch card's value is its FULL body —
# that is the entire premise of the gist-plus-pointer design. A 200-char slice
# chosen by embedding similarity is a pointer at best, and at worst it is the
# #539 hazard in miniature: prose that vouches for a seam nobody re-read. The
# right delivery already has a design — #449's edit-fired rail, exact path match
# via `anchor:` lines, no topic guessing.
#
# So this set is now PULL_ONLY_TYPES exactly, and is imported rather than
# restated: a second copy is how the first one came to be scoped to one rail.
#
# Filtered HERE rather than inside `search()` so the PULL path — techne_recall /
# techne_search — keeps returning these rows, which is the whole point of
# "pull-only". Trade-off: dropping post-search can return fewer than
# `per_turn_recall_limit` rows. That shrink is already visible in
# context_injections (hit_count vs k_requested, techne#413) and is preferable to
# re-ranking to backfill, which would change what the rail selects.


def _drop_pull_only(db_path, rows: list[dict]) -> list[dict]:
    """Drop rows whose knowledge.type is push-excluded, by looking the type UP.

    THE ROW'S OWN ``type`` FIELD CANNOT BE USED. ``search()`` sets ``type`` to the
    SOURCE TABLE ("knowledge"), not to the knowledge row's ``type`` column — which
    is why a `pattern` row riding this rail was indistinguishable from a fact at a
    glance (techne#561).

    THAT RENDERING BUG IS FIXED (techne#566): ``_build_context`` now reads
    ``knowledge_type``, so lines render ``[fact]`` / ``[decision]`` / ``[pattern]``.
    This paragraph asserted the opposite in the present tense until the same diff
    that fixed it 66 lines below — stale prose outliving its subject, which is the
    defect class this rail keeps producing.

    THE LOOKUP STAYS ANYWAY — but NOT for the reason an earlier revision of this
    paragraph gave, which was false and is corrected here rather than quietly
    deleted. It claimed the id lookup "fails CLOSED" where a ``knowledge_type``
    filter fails open. It does not: ``if not ids: return rows`` hands the batch
    back UNFILTERED, and a row carrying no ``id`` is kept. Both fail open. I wrote
    that sentence at a reviewer's prompting and neither of us checked it against
    the code four lines below.

    The real difference is AUTHORITY. This re-reads ``knowledge.type`` from the
    table, so its answer does not depend on ``search()``'s row shape — the very
    thing techne#566 just showed can carry a field that means something other than
    what a caller assumes. Filtering on a value the search layer supplies makes
    this guard only as correct as that layer's contract; reading the table makes it
    independent of it.

    Behaviourally the two are equivalent today (measured: the suite passes either
    way, and a control with the filter disabled fails 3, so the equivalence is not
    vacuous). Equivalence today is not the argument for keeping it.

    One primary-key lookup over the ids just returned. Raising here degrades the
    whole recall to nothing via the caller's handler — failing CLOSED, which is the
    right direction for a filter whose job is to keep rows out.
    """
    ids = [r.get("id") for r in rows if r.get("id") is not None]
    if not ids:
        return rows
    from techne.core.knowledge import PULL_ONLY_TYPES
    from techne.core.stores import plain_connect

    conn = plain_connect(db_path)
    try:
        placeholders = ",".join("?" * len(ids))
        excluded = {
            row[0]
            for row in conn.execute(
                f"SELECT id, type FROM knowledge WHERE id IN ({placeholders})", ids
            )
            if row[1] in PULL_ONLY_TYPES
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
        # `knowledge_type` FIRST, and the ordering is the whole fix (techne#566).
        # `search()` sets `type` to the SOURCE TABLE — "knowledge" / "observation"
        # / "thread" — and returns the row's own kind separately as
        # `knowledge_type`. Reading `type` here labelled EVERY injected row
        # `[knowledge]`, regardless of whether it was a fact, decision, procedure,
        # evaluation, document_card or pattern.
        #
        # WHY THAT LOOKED RIGHT: this rendering mirrors `egress._render_line`,
        # which does read `row["type"]` — correctly, because egress reads knowledge
        # rows STRAIGHT FROM THE TABLE, where `type` IS the kind. Same field name,
        # two different shapes, one of them arriving through `search()`. The
        # SessionStart block has always shown `[fact]` / `[procedure]` / `[decision]`
        # while the per-turn block showed `[knowledge]` for the same rows — the two
        # rails disagreeing in plain sight for a month.
        #
        # It is not cosmetic. The docstring above promises this block frames memory
        # as "recalled memory (verify-before-relying), not asserted ground truth",
        # and a `decision` warrants different weight than an `evaluation`. Collapsing
        # every kind to one label removes the distinction at exactly the point the
        # agent is deciding how much to trust the line — and it is what let a legacy
        # `pattern` row ride the push rail unnoticed for a month (techne#561).
        #
        # `type` remains the fallback because non-knowledge rows (thread,
        # observation) carry no `knowledge_type`, and for those the table IS the
        # most honest label available.
        rtype = r.get("knowledge_type") or r.get("type") or "note"
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
    if not (
        config.hooks.per_turn_recall or config.hooks.per_turn_seed or config.hooks.startup_directive
    ):
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

    # ------------------------------------------------------------------
    # Startup block (techne#643) — once per session, only when reflection work
    # is actually pending. LAST in the block order deliberately: recall answers
    # the prompt the operator just typed, and a chore directive must not push it
    # down. Failure degrades to no block, never to a lost turn.
    # ------------------------------------------------------------------
    if config.hooks.startup_directive:
        try:
            startup_block = _startup_block(config, data.get("session_id"))
            if startup_block:
                blocks.append(startup_block)
        except Exception as e:  # noqa: BLE001
            print(f"techne: startup directive skipped: {e}", file=sys.stderr)

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
