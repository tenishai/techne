"""Tier-0 knowledge egress — bounded, priority-ordered injection for SessionStart.

Tier-0 is the first phase of selective context injection (techne#93). It
replaces the naive "dump all high-confidence rows" approach with a priority
queue that fills a hard char budget, ensuring the context window is never
flooded regardless of how many high-confidence memories are stored.

Selection rules:
  1. Eligibility: superseded_by IS NULL AND confidence='high' AND type != 'pattern'
     (patterns are pull-only reference material — never auto-injected)
  2. Priority order (fills until budget exhausted):
     a. Identity    — tagged memory-type:user
     b. Feedback    — tagged memory-type:feedback
     c. Project state — single most-recent entry tagged memory-type:project
                        whose name: tag contains 'platform-state'
     d. Remaining recent high-confidence decision/fact/evaluation by updated_at DESC
  3. Each rendered line is prefixed with ⟦techne#<id>⟧ for attribution/traceability.
  4. Budget is config-driven (injection.char_budget, default 2000 chars).

Deferred (Tier-1, techne#92): relevance ranking against the current query.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from techne.core.database import connect

logger = logging.getLogger(__name__)


class EgressRow(TypedDict):
    id: int
    type: str
    content: str
    tags: str


def _tag_contains(tags: str | None, fragment: str) -> bool:
    """Check whether *fragment* appears as a complete comma-delimited token in *tags*."""
    if not tags:
        return False
    return any(t.strip() == fragment or t.strip().startswith(fragment) for t in tags.split(","))


def _tag_has_prefix(tags: str | None, prefix: str) -> bool:
    """Check whether any tag in *tags* starts with *prefix*."""
    if not tags:
        return False
    return any(t.strip().startswith(prefix) for t in tags.split(","))


def select_tier0_injection(
    db_path: Path,
    *,
    char_budget: int = 2000,
) -> list[EgressRow]:
    """Select knowledge rows for Tier-0 session-start injection.

    Returns a list of EgressRow dicts in render order (priority groups first,
    then recency). Each row's `content` is the full text — truncation happens
    during rendering, not selection, so callers can inspect the raw content.

    The returned list is already deduplicated by id.
    """
    conn = connect(db_path)
    try:
        # Fetch all eligible candidates in one query, ordered by updated_at DESC
        # so within each priority tier we take the most recent entries.
        rows = conn.execute(
            """SELECT id, type, content, tags
               FROM knowledge
               WHERE superseded_by IS NULL
                 AND confidence = 'high'
                 AND type != 'pattern'
               ORDER BY updated_at DESC""",
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
            id=row["id"], type=row["type"], content=row["content"], tags=row["tags"] or ""
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

    # Build the ordered candidate list:
    # identity → feedback → single most-recent platform-state → remaining
    most_recent_project_state = project_state_rows[:1]  # already ordered by updated_at DESC

    candidates = identity_rows + feedback_rows + most_recent_project_state + remaining_rows

    # Fill until budget exhausted.
    selected: list[EgressRow] = []
    seen_ids: set[int] = set()
    chars_used = 0

    for r in candidates:
        if r["id"] in seen_ids:
            continue
        # Estimate rendered size: prefix (≈15 chars) + truncated content + newline
        line = _render_line(r, max_content=200)
        if chars_used + len(line) > char_budget:
            logger.debug(
                "egress.tier0: budget exhausted after %d rows (%d chars)",
                len(selected),
                chars_used,
            )
            break
        selected.append(r)
        seen_ids.add(r["id"])
        chars_used += len(line)

    logger.debug(
        "egress.tier0: selected %d/%d eligible rows, %d chars (budget %d)",
        len(selected),
        len(candidates),
        chars_used,
        char_budget,
    )
    return selected


def _render_line(row: EgressRow, max_content: int = 200) -> str:
    """Render a single knowledge row as a bullet line with provenance prefix."""
    content = row["content"][:max_content]
    return f"- ⟦techne#{row['id']}⟧ [{row['type']}] {content}\n"


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
