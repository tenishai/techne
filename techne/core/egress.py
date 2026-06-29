"""Tier-0 knowledge egress — bounded, priority-ordered injection for SessionStart.

Tier-0 is the first phase of selective context injection (techne#93). It
replaces the naive "dump all high-confidence rows" approach with a priority
queue that fills a hard char budget, ensuring the context window is never
flooded regardless of how many high-confidence memories are stored.

Selection rules:
  1. Eligibility: superseded_by IS NULL AND confidence='high' AND type NOT IN
     PULL_ONLY_TYPES (patterns + documents + document cards are pull-only
     reference material — recall-able but never auto-injected)
  2. Bands fill in order; each appends to the render list until the budget runs out:
     a. Identity      — tagged memory-type:user (the anchor; always first)
     b. Recent results — the freshest decisions/evaluations/facts NOT in another
                        band, capped at `recent_reserve_frac` of the budget so the
                        large, static feedback band can never starve "what changed
                        since last session" (techne#93 egress-starvation fix).
     c. Feedback      — tagged memory-type:feedback (stable behavioral anchors)
     d. Project state — single most-recent entry tagged memory-type:project
                        whose name: tag contains 'platform-state'
     e. Leftover recent — any recent rows the reservation in (b) couldn't fit
  3. Each rendered line is prefixed with ⟦techne#<id>⟧ for attribution/traceability.
  4. Budget is config-driven (injection.char_budget, default 2000 chars).

Why the reservation (b): the original order was identity → feedback → project →
recent, so on a store with many high-confidence feedback facts the budget was
exhausted before the recency band was ever reached — the single most
decision-relevant memory (e.g. "the acceptance test just passed") was the most
likely to be dropped. Reserving a slice for fresh results fixes that inversion.

Deferred (Tier-1, techne#92): relevance ranking against the current query.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from techne.core.database import connect
from techne.core.knowledge import PULL_ONLY_TYPES

logger = logging.getLogger(__name__)


class EgressRow(TypedDict):
    id: int
    type: str
    content: str
    tags: str
    status: str


def _tag_contains(tags: str | None, fragment: str) -> bool:
    """Check whether *fragment* appears as a complete comma-delimited token in *tags*.

    Exact-token match only: the prior ``startswith`` clause contradicted this
    docstring and meant a future tag prefixed with a band classifier (e.g.
    ``memory-type:projectX``) would silently match the ``memory-type:project`` band.
    Prefix routing has its own helper, ``_tag_has_prefix`` — keep the two distinct.
    (techne#168)
    """
    if not tags:
        return False
    return any(t.strip() == fragment for t in tags.split(","))


def _tag_has_prefix(tags: str | None, prefix: str) -> bool:
    """Check whether any tag in *tags* starts with *prefix*."""
    if not tags:
        return False
    return any(t.strip().startswith(prefix) for t in tags.split(","))


def select_tier0_injection(
    db_path: Path,
    *,
    char_budget: int = 2000,
    recent_reserve_frac: float = 0.4,
) -> list[EgressRow]:
    """Select knowledge rows for Tier-0 session-start injection.

    Returns a list of EgressRow dicts in render order (bands as documented in
    the module docstring). Each row's `content` is the full text — truncation
    happens during rendering, not selection, so callers can inspect the raw
    content.

    `recent_reserve_frac` (0.0-1.0) is the share of `char_budget` reserved for
    the freshest decisions/evaluations/facts before the feedback band fills; set
    to 0.0 for the legacy identity→feedback→project→recent ordering.

    The returned list is already deduplicated by id.
    """
    conn = connect(db_path)
    try:
        # Fetch all eligible candidates in one query, ordered by updated_at DESC
        # so within each priority tier we take the most recent entries.
        placeholders = ",".join("?" * len(PULL_ONLY_TYPES))
        # status='uncertain' rows stay eligible but sort last within their band
        # (techne#117): a high-confidence row whose basis was superseded must not
        # be auto-asserted ahead of a trustworthy current row. Current-first is a
        # global ORDER, so within each partitioned band the uncertain rows trail.
        rows = conn.execute(
            f"""SELECT id, type, content, tags, status
               FROM knowledge
               WHERE superseded_by IS NULL
                 AND confidence = 'high'
                 AND type NOT IN ({placeholders})
               ORDER BY (status = 'uncertain'), updated_at DESC""",
            PULL_ONLY_TYPES,
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Partition into priority buckets — O(n) single pass.
    identity_rows: list[EgressRow] = []
    feedback_rows: list[EgressRow] = []
    project_state_rows: list[EgressRow] = []  # all platform-state rows; take 1 below
    remaining_rows: list[EgressRow] = []

    for row in rows:
        r = EgressRow(
            id=row["id"],
            type=row["type"],
            content=row["content"],
            tags=row["tags"] or "",
            status=row["status"],
        )
        tags = r["tags"]

        if _tag_contains(tags, "memory-type:user"):
            identity_rows.append(r)
        elif _tag_contains(tags, "memory-type:feedback"):
            feedback_rows.append(r)
        elif _tag_contains(tags, "memory-type:project") and _tag_has_prefix(
            tags, "name:platform-state"
        ):
            project_state_rows.append(r)
        else:
            remaining_rows.append(r)

    # current-first then updated_at DESC (the SELECT's ORDER) — so if a current and
    # an uncertain platform-state row are both live, this deliberately prefers the
    # trustworthy one over a strictly-newer uncertain one (techne#117).
    most_recent_project_state = project_state_rows[:1]
    recent_rows = remaining_rows  # decisions/evaluations/facts — the "what changed" band

    selected: list[EgressRow] = []
    seen_ids: set[int] = set()
    chars_used = 0

    # Phase 1 — reserved bands that must not be starved by feedback volume:
    #   identity (the anchor, always) then the freshest results, capped at a
    #   reserved fraction of the budget. This is the egress-starvation fix.
    chars_used = _fill_band(identity_rows, char_budget, selected, seen_ids, chars_used)
    recent_cap = min(char_budget, chars_used + int(char_budget * recent_reserve_frac))
    chars_used = _fill_band(recent_rows, recent_cap, selected, seen_ids, chars_used)

    # Phase 2 — fill the remaining budget with stable anchors, then any leftover
    #   recent rows the reservation couldn't fit: feedback → platform-state → recent.
    for band in (feedback_rows, most_recent_project_state, recent_rows):
        chars_used = _fill_band(band, char_budget, selected, seen_ids, chars_used)

    logger.debug(
        "egress.tier0: selected %d rows, %d chars (budget %d, recent_reserve_frac %.2f)",
        len(selected),
        chars_used,
        char_budget,
        recent_reserve_frac,
    )
    return selected


def _fill_band(
    candidates: list[EgressRow],
    budget_cap: int,
    selected: list[EgressRow],
    seen_ids: set[int],
    chars_used: int,
) -> int:
    """Greedily append rows from *candidates* to *selected* until *budget_cap*
    (cumulative rendered chars) would be exceeded. Skips already-seen ids and
    stops at the first row that doesn't fit. Returns the new chars_used total.
    """
    for r in candidates:
        if r["id"] in seen_ids:
            continue
        line = _render_line(r, max_content=200)
        if chars_used + len(line) > budget_cap:
            break
        selected.append(r)
        seen_ids.add(r["id"])
        chars_used += len(line)
    return chars_used


def _render_line(row: EgressRow, max_content: int = 200) -> str:
    """Render a single knowledge row as a bullet line with provenance prefix.

    An `uncertain` row (techne#117) carries an inline marker so it is never
    auto-injected as a confident fact — recall annotates, egress mirrors it.
    """
    content = row["content"][:max_content]
    marker = " [uncertain]" if row.get("status") == "uncertain" else ""
    return f"- ⟦techne#{row['id']}⟧{marker} [{row['type']}] {content}\n"


def render_tier0_block(rows: list[EgressRow], max_content: int = 200) -> str:
    """Render a list of selected EgressRows as a '## Key Knowledge' block.

    Returns an empty string when *rows* is empty so the caller can skip the
    section cleanly.
    """
    if not rows:
        return ""
    lines = ["## Key Knowledge\n"]
    for row in rows:
        lines.append(_render_line(row, max_content=max_content))
    return "".join(lines)
