#!/usr/bin/env python3
"""UserPromptSubmit hook — recall (read) + seed (write) + startup (techne#109/#277/#643).

The DYNAMIC per-turn complement to the STABLE SessionStart injection. Independent
blocks land in ``additionalContext`` after the stable prefix: the three below are
opt-in, and unread coord messages (``hooks.coord_on_prompt``, techne#1328) are on by
default, placed before recall:

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
        "explicit session_id — a bare emit reflects on the calling session, which "
        "for a subagent is this freshly-created empty one (techne#628, #1333).\n"
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
# truncates every row to `hooks.per_turn_recall_chars` (1000 by default since #1020; see techne#826
# made it a knob and the cut boundary-aware), and an arch card's value is its FULL
# body — that is the entire premise of the gist-plus-pointer design. A capped slice
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


def _apply_relevance_floor(
    rows: list[dict], config, floors: dict | None = None, *, keep_unjudged: bool = True
) -> list[dict]:
    """Drop hits that do not clear their channel's floor (techne#414).

    THE POINT IS THAT "INJECT NOTHING" BECOMES POSSIBLE. Measured across all 1,430
    live per_turn_recall rows: hit_count was 5 on essentially every one, and the
    zero-hit rate was 0% — the rail never declined. So the cost was unconditional
    (~316 tok/turn, every turn) and "five relevant hits" was indistinguishable from
    "five barrel-scraped ones".

    PER-KIND, NEVER MERGED. BM25 `rank` (more-negative = better) and vec `distance`
    (lower = nearer) are opposite-signed; #402's review rejected collapsing them
    into one comparable column, and this floor honours that by thresholding each
    scale separately via `_retrieval_signal`, which is the contract that records
    the discriminator.

    THE TWO FLOORS ARE NOT EQUALLY SOUND, and _injection_log's docstring says so
    outright — "do not build a floor without reading it". For a VEC row, distance
    IS the placement value: search_vec sorts by ascending distance and the union
    preserves that order, so a distance floor rejects exactly what ranked worst.
    For an FTS row, `rank` is an INPUT to placement — the real ordering value
    (`_combined`) is popped before the row is returned and is not observable at
    all. Raw BM25 is at least stable across queries, which the normalized blend is
    not, so a raw-rank floor is defensible; it is simply a weaker claim than the
    vec one. Live traffic is ~99% vec_distance, so this rarely arises.

    A row whose signal is unreadable is KEPT. A floor exists to reject hits it can
    judge, not to silently drop ones it cannot — dropping the unmeasurable would
    make the floor look effective by discarding its own blind spot.

    EXCEPT ON THE INBOUND RAIL (techne#1018 part 4), which passes its own
    ``floors`` and ``keep_unjudged=False``. That rail fires on text nobody asked a
    question with, so its only licence to inject is a floor that vouched for the
    row; a row the floor cannot judge has no such voucher and is dropped.
    """
    from techne.hooks._injection_log import _retrieval_signal

    if floors is None:
        floors = {
            "vec_distance": config.hooks.per_turn_recall_floor_vec_distance,
            "bm25": config.hooks.per_turn_recall_floor_bm25,
        }
    if not any(v is not None for v in floors.values()):
        return rows  # no floor configured — the shipped default

    kept = []
    for r in rows:
        score, kind = _retrieval_signal(r)
        threshold = floors.get(kind) if kind else None
        if score is None or threshold is None:
            if keep_unjudged:
                kept.append(r)  # unjudgeable by this floor; keep rather than hide
            continue
        # Both scales are "higher = worse" at this point: vec distance grows with
        # dissimilarity, and BM25 rank is negative with more-negative = better.
        if score > threshold:
            continue
        kept.append(r)
    return kept


#: How many candidates to fetch per requested slot before re-send suppression.
#: 3x is a guess bounded by cost, not a tuned value: at the default k=5 it is 15
#: rows from an already-indexed query, and it only has to survive the worst
#: measured session. If suppression starts routinely emptying the block, raise it
#: — the symptom would be per_turn_recall logging hit_count=0 while the store
#: plainly holds relevant rows.
_RESEND_OVERFETCH = 3

#: THE DEFAULT `search()` APPLIES TO A KNOWLEDGE ROW WHEN A CALLER ASKS FOR
#: NOTHING. It was a hard ceiling until techne#842 and is not one now: `search()`
#: takes `row_chars`, and the knob governs at any value, as its own docstring had
#: always claimed it did.
#:
#: THIS RAIL PASSES `per_turn_recall_chars + 1`, NOT `per_turn_recall_chars`, and
#: the +1 is the load-bearing part rather than a rounding allowance. At exactly
#: the cap, `_truncate_for_injection` (whose test is `len(content) <= limit`)
#: cannot tell a severed row from a whole one and suppresses the `…`. Asking for
#: one more character leaves it the overflow it needs to tell them apart — and
#: `cap + 1` is precisely that function's own peek window, `content[:limit + 1]`,
#: so boundary-seeking picks the same cut point it would have chosen given the
#: entire row. Dropping the +1 turns a test red.
#:
#: `_ROW_CHAR_CAP` in `core/search.py` holds the same value, and the observation
#: and thread tables still take it unconditionally — a tool ledger loses nothing
#: to truncation, and curated knowledge does.
_SEARCH_ROW_CEILING = 500


def _claim_deferred_session_context(db_path, session_id: str | None) -> str | None:
    """Take this session's deferred SessionStart context, once (techne#562).

    SessionStart stashes its model-facing block instead of injecting it, because
    74.5% of that spend went to sessions that produce nothing and 94% of those
    never reach a prompt. Arriving here means this session DID ask something, so
    it earns the context now.

    CLAIMED, NOT READ — the stash is deleted in the same call. A second prompt must
    not re-inject it, which would trade the old waste for a worse one (#576 exists
    because the per-turn rail re-sent 46% of its deliveries).

    Best-effort: any failure returns None, and the session simply runs without the
    session-start block rather than the hook failing. The operator still saw the
    banner at startup — that half was never deferred.
    """
    if not session_id:
        return None
    try:
        from techne.core.database import connect
        from techne.core.extension_state import get_state, set_state
        from techne.hooks.session_start import DEFERRED_CONTEXT_KEY

        key = f"{DEFERRED_CONTEXT_KEY}:{session_id}"
        conn = connect(db_path)
        try:
            payload = get_state(conn, key)
            if not payload:
                return None
            summary = payload.get("summary") or None
            # Clear FIRST, so a failure after this point costs one block rather
            # than re-injecting on every subsequent turn.
            set_state(conn, key, {})
            return summary
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — never fail the turn over a deferred block
        print(f"techne: deferred session context unavailable: {e}", file=sys.stderr)
        return None


def _already_sent_this_session(db_path, session_id: str | None) -> set:
    """Knowledge ids this session has ALREADY been given, since its last compaction.

    THE MEASURED WASTE (techne#576). Re-measured 2026-08-18 on the live store:
    3,227 of 7,004 per_turn_recall deliveries — **46.1%** — went to a session that
    had already received that exact row. The rail has no memory of what it said
    this turn-loop, so it re-selects and re-delivers the same rows turn after turn.
    Worst repeater is knowledge#208 at 223 redundant re-sends, and it is a
    `project-current-state` note dated 2026-06-10.

    At ~316 tokens per injection this is the largest identified waste in the read
    path, and it needs no model, no ranking change and no new capture:
    `context_injections.hit_ids` already records every delivery per session.

    THE COMPACTION BOUNDARY IS LOAD-BEARING, and it is why this reads `checkpoints`
    rather than the whole session. After a compact, previously injected text may no
    longer be in the model's context — so suppressing on the strength of a delivery
    that predates the compact would withhold something the agent no longer has.
    Only deliveries SINCE the last checkpoint count. A session that has never
    compacted has no boundary and the whole session counts.

    Best-effort: any failure returns an empty set, which degrades to the old
    re-sending behaviour rather than to no recall at all.
    """
    if not session_id:
        return set()
    try:
        from techne.core.database import connect

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            boundary = row[0] if row else None
            if boundary:
                cur = conn.execute(
                    "SELECT hit_ids FROM context_injections "
                    "WHERE session_id = ? AND timestamp > ? AND hit_ids IS NOT NULL",
                    (session_id, boundary),
                )
            else:
                cur = conn.execute(
                    "SELECT hit_ids FROM context_injections "
                    "WHERE session_id = ? AND hit_ids IS NOT NULL",
                    (session_id,),
                )
            sent: set = set()
            for (raw,) in cur:
                if not raw:
                    continue
                try:
                    sent.update(json.loads(raw))
                except (ValueError, TypeError):
                    continue
            return sent
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — suppression is an optimisation, never a gate
        print(f"techne: re-send suppression degraded: {e}", file=sys.stderr)
        return set()


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

    The real difference is AUTHORITY, and its SCOPE is worth stating exactly
    (techne#609): this re-reads ``knowledge.type`` from the table, so its answer
    does not depend on ``search()``'s ``type`` FIELD — the very thing techne#566
    showed can carry a value meaning something other than what a caller assumes.
    It still depends on ``search()`` supplying a correct ``id``. Authority over
    the kind, not over the whole row. Filtering on a value the search layer
    supplies makes this guard only as correct as that layer's contract; reading
    the table makes it independent of it for the field that decides the outcome.

    Behaviourally the two are equivalent today (measured: the suite passes either
    way, and a control with the filter disabled fails 3, so the equivalence is not
    vacuous). Equivalence today is not the argument for keeping it.

    One primary-key lookup over the ids just returned. Raising here degrades the
    whole recall to nothing via the caller's handler — failing CLOSED, which is the
    right direction for a filter whose job is to keep rows out.
    """
    # SCOPED BY SOURCE TABLE (techne#608). knowledge ids are per-table
    # autoincrement, and this lookup reads the `knowledge` table — so collecting
    # ids from rows of ANY table would resolve an observation's id 42 against
    # KNOWLEDGE row 42. Correct today only because the single caller hardcodes
    # table="knowledge"; widen that to "all" — plausible, since recall already
    # spans tables — and an observation gets dropped because an unrelated
    # knowledge row happens to be a `pattern`. Silent, wrong, and it would read
    # as the guard working.
    #
    # Filtering here rather than asserting: the function already discriminates on
    # `type` elsewhere, a non-knowledge row simply is not this guard's business,
    # and passing it through is the correct answer rather than a crash.
    ids = [
        r.get("id")
        for r in rows
        if r.get("id") is not None and r.get("source_table") == "knowledge"
    ]
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
    # The DROP is scoped too, not just the id collection (techne#608). Filtering
    # on id alone re-introduces the collision one line later: a same-id row from
    # another table would still be removed by a verdict computed from `knowledge`.
    return [
        r for r in rows if not (r.get("source_table") == "knowledge" and r.get("id") in excluded)
    ]


#: What `_truncate_for_injection` appends to a row it cut. Named because
#: `chars_dropped` (techne#853) must subtract it to count what the reader
#: LOST rather than what we added.
_CUT_SUFFIX = " …"


def _truncate_for_injection(content: str, limit: int) -> tuple[str, bool]:
    """Cut ``content`` to ``limit`` chars at a boundary, reporting whether it cut.

    techne#826. This rail shipped with a bare ``content[:200]`` from the day it
    landed, and measured over one real session it severed 58 of 58 injected facts
    mid-word — a cross-reference cut at ``CORRECTS techne#``, a remedy command cut
    mid-path. The failure is quiet in the worst way: the agent receives something
    that looks like the rule, is topically correct, and is missing the operative
    half, so it reads as "I have this" rather than "I have the first sentence of
    this".

    TWO CHANGES, AND NEITHER IS A BIGGER BUDGET. The cut lands on a sentence
    boundary if one is available in the last 40% of the budget, else on a word
    boundary, else hard; and a cut row ends with a single ``…`` so it ANNOUNCES
    itself instead of impersonating a complete one. The caller adds one header
    line naming the cap when any row was cut — one line per block rather than a
    suffix per row, because a marker that costs more than the content it explains
    is its own kind of dishonest.

    The floor of 40% is what stops boundary-seeking from being worse than the bare
    cut: without it, a row whose only full stop sits at char 12 would be truncated
    to 12 chars in the name of tidiness.
    """
    if limit <= 0:
        # A NON-POSITIVE CAP MUST NOT MEAN "UNBOUNDED" (techne#839 r1). The first
        # version returned the row untouched here, so `per_turn_recall_chars: 0`
        # produced UNLIMITED per-row injection on every prompt — and the sibling
        # knob one line above sets the opposite expectation, since
        # `per_turn_recall_limit: 0` feeds `rows[:0]` and means "inject nothing".
        # An operator writing 0 to switch the cap off would have got nothing from
        # one knob and everything from the other, with "everything" being the
        # expensive direction on the rail #808 already calls the dominant cost.
        # The hardcoded 200 meant this path did not exist; adding the knob added it.
        return "", True

    if len(content) <= limit:
        return content, False

    # `limit + 1`, not `limit`: a terminator STRADDLING the window edge — a full
    # stop at limit-1 and its space at limit — is invisible to a search bounded at
    # `limit`, and the cut falls back a whole word for no reason. Peeking one
    # character past the budget costs nothing, because the extra character is only
    # ever the SEPARATOR and is never emitted.
    window = content[: limit + 1]
    floor = int(limit * 0.6)

    # Prefer a sentence end; `. ` and friends, not a bare `.`, so version numbers
    # and `techne#1.2`-shaped tokens are not read as terminators.
    best = -1
    for end in (". ", ".\n", "! ", "?\n", "? ", "!\n"):
        best = max(best, window.rfind(end))
    if best >= floor:
        # `best` indexes the terminator itself, which is <= limit-1 whenever the
        # match was found in the peeked position, so the emitted slice never
        # exceeds the budget.
        return window[: best + 1].rstrip() + " …", True

    cut = window[:limit].rfind(" ")
    if cut >= floor:
        return window[:cut].rstrip() + " …", True

    return window[:limit].rstrip() + " …", True


#: The per-turn block's header. Another rail passes its own through
#: ``recall_block(header=...)``; each must start "## techne —", the prefix
#: `_recall_query.strip_own_injections` removes from pasted and inbound text.
_PER_TURN_HEADER = (
    "## techne — durable facts relevant to this prompt",
    "> Per-turn recall (techne#109); recalled memory, not ground truth — verify first.",
)


def _build_context(
    rows: list[dict],
    char_limit: int = 1000,
    stats: dict | None = None,
    header: tuple[str, ...] = _PER_TURN_HEADER,
) -> str:
    """Render recalled knowledge rows into a compact additionalContext block.

    Mirrors egress `_render_line` formatting: each row carries its ⟦techne#id⟧
    provenance prefix so injected facts are attributable and honestly framed as
    recalled memory (verify-before-relying), not asserted ground truth.

    THE ``r.get("type")`` AND ``"note"`` FALLBACKS BELOW ARE UNREACHABLE ON THIS
    RAIL (techne#609). This recall hardcodes ``table="knowledge"``, which
    guarantees ``knowledge_type`` on every row. They are kept for shared-shape
    safety, not because the key is ever absent here — said out loud so a future
    reader does not read the fallback as EVIDENCE that it sometimes is, and
    weaken a guard on that basis. The tests already record this.
    """
    from techne.core.egress import one_line

    header = list(header)
    lines: list[str] = []
    truncated_any = False
    _cut_rows = 0
    _cut_chars = 0
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
        rtype = r.get("knowledge_type") or r.get("source_table") or "note"
        original = r.get("content") or ""
        # WHAT THE READER LOST IS MEASURED AGAINST THE STORED ROW, NOT THE ONE WE
        # WERE HANDED (techne#853, repaired by techne#842). `search()` truncates
        # before this rail ever sees a row, so `len(original)` is the length of an
        # ALREADY-CUT row and differencing against it counts almost nothing. It was
        # merely misleading before #842 and became useless after: this rail now asks
        # for `cap + 1`, so `original` is always exactly one character longer than
        # what we render and `chars_dropped` collapsed to the marker's width —
        # measured 2/4/2/2 at caps 200/450/1200/2000 on a 3,199-char row whose true
        # losses are 3000/2752/2000/1200. A constant is worse than a wrong number,
        # because #853 exists precisely so that raising or lowering
        # `per_turn_recall_chars` can be evaluated from the data, and a constant
        # answers every cap identically. `content_chars_total` is the stored length,
        # recorded by search at its own slice; falling back to `len(original)`
        # keeps rows from any caller that does not supply it accounted for, under-
        # reporting rather than crashing.
        stored_len = r.get("content_chars_total")
        if not isinstance(stored_len, int) or stored_len < len(original):
            stored_len = len(original)
        content, was_cut = _truncate_for_injection(original, char_limit)
        if not content:
            # WITHHELD ROWS ARE DROPPED, NOT RENDERED EMPTY (#839 r2 Important 3).
            # At `char_limit <= 0` every row returns "", and emitting them produced a
            # block whose header promised "Rows ending in …" above rows that ended in
            # nothing — the block contradicting itself, and a header plus one line per
            # row of pure overhead on every prompt, on the rail #808 calls the
            # dominant cost. Dropping them empties `lines`, so the caller's existing
            # `if rows else ""` suppresses the block entirely and `0` means "inject
            # nothing" on THIS knob exactly as it already does on
            # `per_turn_recall_limit`, which feeds `rows[:0]`.
            # THE COUNTERS STILL FIRE FOR A WITHHELD ROW (#854 r1 Important 2).
            # The row is dropped from the BLOCK, not from the accounting: at
            # `char_limit <= 0` every row is severed to nothing, and reporting
            # `{0, 0}` for a payload where everything was cut is the telemetry
            # saying the opposite of what happened. `chars_dropped` is the whole
            # original, since none of it was delivered.
            #
            # AND THIS COMMENT DISAGREES WITH THE ONE AT THE LOG SITE BELOW,
            # which wins (Prudence, #869 r3). At `char_limit <= 0` every row is
            # withheld, so `lines` is empty, so the recall block is "", so the
            # caller forces BOTH counters to 0 — measured end-to-end with a
            # 3,000-char row withheld in full: `('per_turn_recall', '[]', 0, 0)`.
            # The number computed here never reaches the table, so the claim
            # above that we avoid "telemetry saying the opposite of what
            # happened" describes an outcome this rail does not have. It
            # PRE-DATES techne#842 (#854 era) and is left standing rather than
            # fixed here, because `hit_ids=[]` and an empty `injected_text`
            # already tell an operator that nothing shipped. Recorded because
            # #842 is what made this number meaningful, so the next reader
            # arrives with a reason to trust it that the log site does not honour.
            if was_cut:
                truncated_any = True
                _cut_rows += 1
                _cut_chars += stored_len
            continue
        truncated_any = truncated_any or was_cut
        if was_cut:
            # COUNTED AT THE CUT, NOT RE-DERIVED AT THE LOG SITE (techne#853).
            # Recomputing "would this row have been cut?" downstream would be a
            # second copy of the truncation decision — the exact shape of the bug
            # #852 fixed, where the cap was declared twice and the dead copy drifted
            # the moment the live one moved.
            #
            # `chars_dropped` counts CONTENT withheld from the reader, so it
            # measures against the rendered body with the cut marker removed: the
            # " …" is what we added, not what they lost. Without the strip every
            # cut row would under-report by the marker's width.
            _cut_rows += 1
            _cut_chars += stored_len - (len(content) - len(_CUT_SUFFIX))
        # One line per row (techne#1252): a stored line break must not start a
        # line that reads as another row.
        lines.append(f"- ⟦techne#{rid}⟧ [{rtype}] {one_line(content)}")
    # Populated on EVERY exit, including the empty one: "the rail fired and cut
    # nothing" and "the rail never fired" are different facts, and a caller that
    # only sees stats on the happy path cannot tell them apart.
    if stats is not None:
        stats["rows_truncated"] = _cut_rows
        stats["chars_dropped"] = _cut_chars
    if not lines:
        return ""
    if truncated_any:
        header.append(
            f"> Rows ending in … are cut at {char_limit} chars "
            "(hooks.per_turn_recall_chars); recall the id for the full entry."
        )
    return "\n".join(header + [""] + lines)


#: extension_state key prefix for the inbound rail's once-per-prompt slot
#: (techne#1018 part 3). UserPromptSubmit writes ``{"delivered": false}`` on every
#: prompt; post_tool_use flips it to true when it delivers. PostToolUse carries no
#: turn id, so this marker is the only way that hook can tell one prompt from the
#: next.
INBOUND_TURN_PREFIX = "inbound_recall:turn"


def inbound_recall_armed(config) -> bool:
    """True when the inbound rail may run: switched on AND given a vector floor.

    THE FLOOR IS PART OF THE SWITCH (techne#1018 part 4). The per-turn floor
    shipped unset and measured at a 0% zero-hit rate (#414): with no floor, a
    search always finds k rows. That is tolerable when someone asked a question,
    and is noise on every hand-back and fetched page. So the flag alone arms
    nothing, and `check_hooks` reports the rail DORMANT until both are set.
    """
    h = config.hooks
    return bool(h.inbound_recall) and h.inbound_recall_floor_vec_distance is not None


def _stamp_prompt_turn(db_path, session_id: str | None) -> None:
    """Reset this session's inbound-recall slot for the new prompt. Best-effort.

    A failure leaves the previous prompt's marker in place. If that prompt
    already delivered, this one gets no inbound delivery; if it did not, this one
    can still deliver once. Either way the cap holds and the turn is not lost.
    """
    if not session_id:
        return
    try:
        import time

        from techne.core.database import connect
        from techne.core.extension_state import set_state

        conn = connect(db_path)
        try:
            set_state(conn, f"{INBOUND_TURN_PREFIX}:{session_id}", {"delivered": False},
                      commit=False)
            # Pruned like the other per-session keys (post_tool_use's reminder and
            # coord sets), so one row per session does not accumulate forever.
            conn.execute(
                "DELETE FROM extension_state WHERE name LIKE ? AND updated_at < ?",
                (f"{INBOUND_TURN_PREFIX}:%", int(time.time()) - 7 * 24 * 60 * 60),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — never fail the turn over a marker
        print(f"techne: inbound-recall turn marker skipped: {e}", file=sys.stderr)


def recall_block(
    config,
    session_id: str | None,
    query: str,
    *,
    source: str,
    k: int,
    floors: dict | None = None,
    keep_unjudged: bool = True,
    header: tuple[str, ...] = _PER_TURN_HEADER,
    claim=None,
    timeout_label: str = "user_prompt_submit",
) -> tuple[str, list]:
    """Search, filter, render and log one recall; return (block, rows).

    ONE PIPELINE FOR EVERY RECALL RAIL (techne#1018 part 2). Per-turn recall and
    the inbound rail in post_tool_use both come through here, so the pull-only
    filter, the re-send suppression, the floor, the row cap and the log cannot
    drift apart between them. ``source`` is the context_injections source, and
    because `_already_sent_this_session` reads every source, a row either rail
    delivered is suppressed on both for the rest of the compaction segment.

    ``floors`` and ``keep_unjudged`` go to `_apply_relevance_floor`; None means
    the per-turn floors. ``claim``, when given, is called once there is a block
    to deliver and must return True for it to be delivered and logged.

    ``rows`` is the post-cap selection, returned even when the block renders
    empty. Raises on search failure; the caller decides what degraded means.
    """
    from techne.core.search import clean_query_text, search
    from techne.hooks._timeout import bounded_hook

    # techne#1249: `search()` cleans its own copy, but the query is also the
    # logged trigger below, and a lone surrogate there made the best-effort log
    # drop the row without a word. Clean it once so both see the same text.
    query = clean_query_text(query)
    with bounded_hook(config.hooks.timeout_s, label=timeout_label):
        # OVER-FETCH, THEN SUPPRESS, so the session gets k FRESH rows rather
        # than k-minus-the-duplicates (techne#576). Dropping without
        # over-fetching would trade duplicate delivery for thinner
        # delivery, which is not the trade the issue asks for — the rows
        # being re-sent are legitimately high-scoring, they have just
        # already been said.
        # An empty query means the prompt was only techne's own injected
        # context (a pasted recall block, say). Searching on it would
        # re-select the rows it already names, so inject nothing.
        rows = (
            []
            if not query
            else search(
                config.db_path,
                query,
                table="knowledge",
                limit=k * _RESEND_OVERFETCH,
                include_archived=False,
                # techne#842 — ASK FOR ONE MORE CHARACTER THAN WE WILL
                # RENDER, and the +1 is load-bearing rather than defensive.
                # `_truncate_for_injection` decides "was this cut?" by
                # `len(content) > limit`, so a row that `search()` already
                # sliced to EXACTLY the cap reads as whole: `was_cut` stays
                # False and both the announcing marker and the header notice
                # are suppressed. That is the dishonest state techne#826
                # exists to prevent, and threading the cap unchanged would
                # have spread it from cap>=500 to EVERY cap. Measured before
                # the +1: at caps 600, 500 and 200 the row ended mid-word
                # with no marker.
                #
                # At cap+1 the renderer sees the overflow it needs: a row
                # shorter than the cap still arrives whole and is correctly
                # unmarked; a longer one arrives one char over and is cut and
                # marked by the code that owns that decision.
                row_chars=config.hooks.per_turn_recall_chars + 1,
                # techne#1078: this knowledge-scope rail is what the rarity
                # fraction is tuned for, so it must honour the operator's knobs.
                identifier_head_slots=config.search.identifier_head_slots,
                identifier_rare_fraction=config.search.identifier_rare_fraction,
            )
        )
        rows = _drop_pull_only(config.db_path, rows)
        already = _already_sent_this_session(config.db_path, session_id)
        if already:
            fresh = [r for r in rows if r.get("id") not in already]
            # An all-duplicate turn legitimately yields NOTHING. That is a
            # real outcome, not a failure — the log records an empty recall
            # as signal, and #414 is the issue arguing "inject nothing"
            # must be legal. Do NOT fall back to the duplicates here; that
            # would restore exactly the waste this suppresses.
            rows = fresh
        # AFTER suppression, BEFORE the k-cut: a floored-out row should not
        # consume one of the k slots that a clearing row could have used.
        rows = _apply_relevance_floor(rows, config, floors, keep_unjudged=keep_unjudged)
        rows = rows[:k]
    # THE CAP IS PASSED THROUGH UNCLAMPED (techne#842). It was clamped to
    # `_SEARCH_ROW_CEILING - 1` from #839 until #842: `search()` cut every
    # knowledge row at a hidden 500, so a cap at or above that made
    # `_truncate_for_injection` see a whole row and suppress both the marker
    # and the header notice — the one state in which this rail was less
    # honest than before techne#826. `search()` now takes `row_chars` and
    # this rail asks for `cap + 1` (see the call above, where the +1 is
    # explained), so the row arrives one character over whatever was asked
    # for and that state is unreachable at any cap. The whole clamp
    # paragraph that stood here is deleted rather than annotated: it
    # described a mechanism that no longer exists, and a retraction bolted
    # under a still-assertive paragraph leaves the wrong half quotable.
    chars = config.hooks.per_turn_recall_chars
    # techne#853 — the cap's effect is otherwise unobservable: this table
    # recorded what was injected and never what was severed, so raising or
    # lowering `per_turn_recall_chars` could not be evaluated from data.
    trunc_stats: dict = {}
    block = _build_context(rows, chars, trunc_stats, header) if rows else ""
    # techne#1018 — a rail with a delivery cap claims its slot HERE, after it
    # knows it has something to say and before the log, because the log is what
    # `_already_sent_this_session` reads: a block logged and then dropped for a
    # lost claim would suppress its rows without delivering them. A lost claim
    # returns before logging, so nothing is recorded as sent.
    if block and claim is not None and not claim():
        return "", []
    # techne#329 — make the READ path auditable (the audit that motivated
    # this couldn't see the rail at all). Best-effort; the helper never
    # raises. Log even an empty recall (fired but matched nothing = signal).
    if config.hooks.per_turn_recall_log:
        from techne.hooks._injection_log import log_context_injection

        log_context_injection(
            config.db_path,
            source=source,
            session_id=session_id,
            # techne#1018 — log what was SEARCHED, so this row can rebuild
            # the turn. For a typed prompt that is the prompt itself; an
            # empty query is named, not left looking like a no-match.
            trigger=query or "[no query: the paste held only techne's own context]",
            # `rows` IS WHAT WAS INJECTED (_injection_log.py:217, its own
            # words). Logging withheld rows as delivered is not merely an
            # inaccurate audit row: `_already_sent_this_session` reads
            # `hit_ids` back with no filter on whether anything was emitted,
            # so a row logged-but-not-sent is SUPPRESSED for the rest of the
            # session segment. Measured: at `per_turn_recall_chars: 0` turn
            # one emits nothing and logs hit_ids=[1]; turn two with the cap
            # raised to 200 then emits nothing either, because the row is
            # already "sent". Editing config.yaml mid-session is a modelled
            # operator behaviour (AGENTS.md lists it as a standing trigger),
            # and a silent recall miss is this project's named failure class.
            rows=rows if block else [],
            # Mirrors `rows` above: a row that was withheld was not
            # delivered, so it cannot have been truncated on delivery
            # either. Reporting cuts for a block that never shipped would
            # make `chars_dropped` count losses the reader never sustained.
            rows_truncated=trunc_stats.get("rows_truncated") if block else 0,
            chars_dropped=trunc_stats.get("chars_dropped") if block else 0,
            injected_text=block,
            k_requested=k,
        )
    return block, rows


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

    # The hook's five independent blocks; with none of them on it is a no-op. Doctor's
    # `checks._user_prompt_submit_enabled` mirrors this gate (#1328).
    # No marker: see the note at the top of main().
    if not (
        config.hooks.per_turn_recall
        or config.hooks.per_turn_seed
        or config.hooks.startup_directive
        or config.hooks.coord_on_prompt
        or inbound_recall_armed(config)
    ):
        sys.exit(0)

    blocks: list[str] = []

    # techne#562 — claim this session's deferred SessionStart context, if any.
    # FIRST in `blocks`, so the session-state block still precedes per-turn recall
    # exactly as it did when SessionStart injected it inline. Ordering is the one
    # thing deferral could quietly change, and it does not.
    deferred_ctx = _claim_deferred_session_context(config.db_path, data.get("session_id"))
    if deferred_ctx:
        blocks.append(deferred_ctx)
        # Logged as its OWN source, not folded into per_turn_recall: the audit must
        # still be able to say what session_start cost and whether it was used.
        # Without this the 74.5%-waste measurement that motivated #562 becomes
        # unrepeatable the moment the fix lands.
        if config.hooks.per_turn_recall_log:
            try:
                from techne.hooks._injection_log import log_context_injection

                log_context_injection(
                    config.db_path,
                    source="session_start_deferred",
                    session_id=data.get("session_id"),
                    trigger=None,
                    rows=[],
                    injected_text=deferred_ctx,
                )
            except Exception as e:  # noqa: BLE001
                print(f"techne: deferred-context log skipped: {e}", file=sys.stderr)

    # techne#1328 — unread coord messages at the start of every turn, not only after
    # a tool call. Right after the session-state block and before recall: a peer's
    # claim on a branch is what this turn most needs to see before it starts work.
    # The same injector as PostToolUse, so the per-session surfaced-set keeps a
    # message from arriving twice. Best-effort like every block here.
    coord_bodies: list[str] = []
    if config.hooks.coord_on_prompt and data.get("session_id"):
        try:
            from techne.hooks.post_tool_use import _inject_coord_messages

            coord_block = _inject_coord_messages(config, data["session_id"], coord_bodies)
            if coord_block:
                blocks.append(coord_block)
        except Exception as e:  # noqa: BLE001
            print(f"techne: coord block skipped: {e}", file=sys.stderr)
    degraded: str | None = None
    rows: list = []

    # techne#1045 — a background-task notification is not a question, so recall
    # does not search on it. Only recall is skipped: the deferred session context
    # above and the startup directive below still ride whichever turn comes first.
    # No per_turn_recall row goes to context_injections, because nothing was
    # searched; the liveness marker records the skip instead, as the outcome when
    # nothing else is injected and in the `ok` detail when something is.
    from techne.hooks._recall_query import is_task_notification

    notification = is_task_notification(prompt)
    # Only a skip when recall would otherwise have run.
    recall_skipped = config.hooks.per_turn_recall and notification

    # techne#1018 — open this prompt's delivery slot for the inbound rail, which
    # runs in PostToolUse and has no turn id of its own. Not on a task
    # notification, which is not a prompt: reopening there would let one prompt
    # and N finished background tasks draw N+1 deliveries. Before recall, so a
    # slow search cannot leave the previous prompt's claim standing.
    if inbound_recall_armed(config) and not notification:
        _stamp_prompt_turn(config.db_path, data.get("session_id"))
    # A coord message delivered just above is marked surfaced, so PostToolUse will
    # not deliver it again, and its inbound search would have nothing to search
    # (Prudence, #1328 round 2). Run that search here, on the bodies this hook
    # delivered. Not gated on the notification check (round 3): a notification turn
    # opens no slot, but it still delivers coord, and `_inbound_slot` decides
    # whether a slot is open; using an open one does not reopen it.
    if inbound_recall_armed(config):
        if coord_bodies:
            try:
                from techne.hooks.post_tool_use import _inject_inbound_recall

                inbound_block = _inject_inbound_recall(
                    config, data["session_id"], "", None, coord_bodies
                )
                if inbound_block:
                    blocks.append(inbound_block)
            except Exception as e:  # noqa: BLE001
                print(f"techne: inbound recall on coord skipped: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Recall block (techne#109) — durable facts relevant to this prompt. Scope
    # to knowledge (not raw observations), exclude soft-archived rows. Any
    # failure (DB unreachable, slow SQLite → HookTimeout) degrades to no recall
    # block WITHOUT aborting — the seed block can still emit.
    # ------------------------------------------------------------------
    if config.hooks.per_turn_recall and not notification:
        try:
            from techne.hooks._recall_query import build_recall_query

            # techne#1018 — search on the question, not on whatever was
            # pasted around it; a short typed prompt passes through as is.
            query = build_recall_query(prompt, config.hooks.per_turn_recall_long_prompt_chars)
            block, rows = recall_block(
                config,
                data.get("session_id"),
                query,
                source="per_turn_recall",
                k=config.hooks.per_turn_recall_limit,
            )
            if block:
                blocks.append(block)
        except Exception as e:  # noqa: BLE001  — catches HookTimeout too
            print(f"techne: per-turn recall degraded: {e}", file=sys.stderr)
            degraded = str(e)[:200]

        # A DEAD VECTOR CHANNEL DOES NOT RAISE, SO THERE IS NOTHING TO CATCH ABOVE
        # (techne#728 r3 Important 1). Since techne#708 `search_vec` degrades to FTS
        # and returns [] rather than propagating, so `search()` succeeds, this handler
        # never fires, `degraded` stays None, and `_record` writes outcome="ok" over a
        # backend that answered nothing — the shape `_record`'s own docstring names as
        # the bug this module exists to surface. `take_vec_channel_error()` is how a
        # caller learns about a degradation that did not raise.
        #
        # AND IT IS THE ONLY SIGNAL THAT CAN EXIST HERE. `search_vec` also prints to
        # stderr, but this is a `UserPromptSubmit` hook that exits 0, and Claude Code
        # discards a zero-exit hook's stderr — "goes to the debug log only, never the
        # transcript, and Claude never sees it". Exit 2 would surface it and BLOCK the
        # prompt, which is not on the table. So the marker is not the second half of
        # the signal; on this path it is the whole of it.
        #
        # An existing `degraded` WINS: recall failing outright is the more specific
        # fact, and the vector channel is one input to it.
        from techne.core.search import take_vec_channel_error

        vec_error = take_vec_channel_error()
        if vec_error and degraded is None:
            degraded = vec_error[:200]

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
        outcome = "noop:task-notification" if recall_skipped else "noop:nothing-to-inject"
        _record(record_hook_run, config, degraded, outcome=outcome)
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
        detail = f"{len(rows)} rows"
        if recall_skipped:
            detail = "0 rows (recall skipped: task-notification)"
        _record(record_hook_run, config, degraded, outcome="ok", detail=detail)
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
