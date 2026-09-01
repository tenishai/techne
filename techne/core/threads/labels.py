"""Label generation for heuristic thread detection.

generate_label, GENERIC_FAMILIES, and label helper functions.
"""

from __future__ import annotations

from techne.core.threads.features import META_FAMILY, ObsFeatures
from techne.core.threads.scoring import USER_PROMPT_FAMILY
from techne.core.threads.segmentation import (
    _segment_dominant_family,
    _segment_dominant_repo,
)

# --- Label generation ------------------------------------------------------

# Families that don't generate informative "<Family> investigation" labels
# on their own — we only use them when there's nothing else. user_prompt is
# structural (a user turn happened) not semantic (what the work was about),
# so it never wants to drive a label either.
GENERIC_FAMILIES = {"filesystem", "bash", "meta", "other", "user_prompt"}


def _segment_dominant_entity(segment: list[int], features: list[ObsFeatures]) -> str | None:
    """Most-common non-trivial entity in a segment, or None."""
    # SAME TIE-BREAK AS THE TWO DOMINANCE HELPERS IN segmentation.py, and it was
    # missing here (Prudence, #873 r1) — in the file that IMPORTS them. `entities`
    # is a set, so the counts dict got a hash-seeded insertion order and `max()`
    # took the first of any tie: measured 8 distinct winners across 12 processes
    # on a 12-way tie, which propagates into `generate_label` and therefore into
    # `threads.label`, an FTS-indexed column that `link_threads` searches to pick
    # a parent_thread_id — a persisted FK chosen per-process.
    counts: dict[str, int] = {}
    for i in segment:
        for e in features[i].entities:
            counts[e] = counts.get(e, 0) + 1
    if not counts:
        return None
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _humanize_family(family: str) -> str:
    """Render a family name for display."""
    if family.startswith("mcp:"):
        return family[4:].replace("_", " ").title()
    return family.replace("_", " ").title()


def generate_label(segment: list[int], features: list[ObsFeatures]) -> str:
    """Build a short, informative label for a segment.

    Priority:
    1. Dominant non-generic family -> "<Family> investigation" (e.g.,
       "Datadog investigation"). Generic families = filesystem/bash/meta/other.
    2. Dominant repo -> "Work on <repo>".
    3. Append top entity if present -> ": <entity>".
    4. Fallback: "Session work" + the two most-used families.
    """
    if not segment:
        return "Session work"

    fam = _segment_dominant_family(segment, features)
    repo = _segment_dominant_repo(segment, features)
    entity = _segment_dominant_entity(segment, features)

    if fam and fam not in GENERIC_FAMILIES:
        base = f"{_humanize_family(fam)} investigation"
        if entity:
            return f"{base}: {entity}"
        return base

    if repo:
        base = f"Work on {repo}"
        if entity:
            return f"{base}: {entity}"
        return base

    # Fallback: top families by count. Skip purely-structural markers
    # (user_prompt, meta) — they carry no semantic information about the work
    # itself, only that user turns / orchestration calls happened. "Session
    # work (Filesystem, User Prompt)" is noisier than "Session work
    # (Filesystem)" without adding signal.
    fam_counts: dict[str, int] = {}
    for i in segment:
        f = features[i]
        if f.tool_family and f.tool_family not in {USER_PROMPT_FAMILY, META_FAMILY}:
            fam_counts[f.tool_family] = fam_counts.get(f.tool_family, 0) + 1
    top = sorted(fam_counts.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if top:
        names = ", ".join(_humanize_family(k) for k, _ in top)
        return f"Session work ({names})"
    return "Session work"
