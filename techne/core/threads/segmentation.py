"""Segmentation helpers for heuristic thread detection.

split_at_boundaries, merge_pass, and segment helper functions.
"""

from __future__ import annotations

from techne.core.threads.features import ObsFeatures
from techne.core.threads.scoring import (
    BOUNDARY_THRESHOLD,
    CONTINUING_INVESTIGATION_GAP,
    GAP_HARD,
    MIN_SEGMENT_SIZE,
    BoundaryScore,
)


def split_at_boundaries(
    n_observations: int,
    scores: list[BoundaryScore],
    threshold: float = BOUNDARY_THRESHOLD,
) -> list[list[int]]:
    """Split ``n_observations`` into segments using any score >= threshold."""
    if n_observations == 0:
        return []

    boundaries = {s.a_index for s in scores if s.total >= threshold}
    segments: list[list[int]] = []
    current: list[int] = []
    for i in range(n_observations):
        current.append(i)
        if i in boundaries:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


#: TIES ARE BROKEN BY NAME, AND WITHOUT THAT THREAD DETECTION IS NON-DETERMINISTIC
#: ACROSS PROCESSES (techne#871). `ObsFeatures.repos` is a SET, so iterating it to
#: build `counts` gives the dict a hash-seeded insertion order, and `max()` returns
#: the FIRST of any tie — so which repo counts as dominant depended on
#: PYTHONHASHSEED. That decides merges, so the whole segmentation moved with it:
#: measured on one 521-observation session, the boundary SCORES were byte-identical
#: across seeds while the thread count came out 13 or 15. Content-addressed thread
#: ids are derived from the resulting segments, so ids moved too, which is how a
#: reflection-queue row stopped matching its own session's detection and read as
#: "superseded" one run in five.
#:
#: `min` on `(-count, name)` rather than `max` on `count`: same winner by count,
#: lowest name on a tie, and the tie-break is visible in the expression rather
#: than inherited from whatever order the caller happened to produce.
def _segment_dominant_repo(segment: list[int], features: list[ObsFeatures]) -> str | None:
    counts: dict[str, int] = {}
    for i in segment:
        for r in features[i].repos:
            counts[r] = counts.get(r, 0) + 1
    if not counts:
        return None
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _segment_dominant_family(segment: list[int], features: list[ObsFeatures]) -> str | None:
    """Most-common non-meta family in a segment, or None if it's all-meta."""
    counts: dict[str, int] = {}
    for i in segment:
        f = features[i]
        if f.is_meta:
            continue
        counts[f.tool_family] = counts.get(f.tool_family, 0) + 1
    if not counts:
        return None
    # Same explicit tie-break as `_segment_dominant_repo`. Here the insertion
    # order happens to follow `segment`, which is already deterministic, so this
    # is not a live defect — it is spelled out anyway so the two helpers cannot
    # be read as disagreeing about how a tie resolves.
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _entity_overlap(seg_a: list[int], seg_b: list[int], features: list[ObsFeatures]) -> int:
    ents_a: set[str] = set()
    ents_b: set[str] = set()
    for i in seg_a:
        ents_a.update(features[i].entities)
    for i in seg_b:
        ents_b.update(features[i].entities)
    return len(ents_a & ents_b)


def _inter_segment_gap(seg_a: list[int], seg_b: list[int], features: list[ObsFeatures]) -> float:
    """Gap from the end of seg_a to the start of seg_b."""
    if not seg_a or not seg_b:
        return 0.0
    return max(0.0, (features[seg_b[0]].timestamp - features[seg_a[-1]].timestamp).total_seconds())


def merge_pass(
    segments: list[list[int]],
    features: list[ObsFeatures],
    min_size: int = MIN_SEGMENT_SIZE,
) -> list[list[int]]:
    """Apply merge rules until stable.

    Rule 1 — Tiny segments (< ``min_size``) merge into the neighbor with
    higher entity overlap. Ties merge forward (small segment is setup for
    what follows). Tail tiny segment with no following neighbor merges back.

    Rule 2 — Meta-only segments are preamble and merge into the following
    segment. Trailing meta-only segment merges back.

    Rule 3 — Same-repo adjacent segments merge if no time gap >= GAP_HARD
    between them. Same-repo means same *dominant* repo on both sides.

    Rule 4 — Same dominant tool family + nonzero entity overlap -> merge,
    tolerating gaps up to ``CONTINUING_INVESTIGATION_GAP`` (3h). Captures
    the "long break in the middle of one investigation" pattern that the
    same-repo rule can't catch when the work is over services/dashboards
    rather than code.
    """
    if len(segments) <= 1:
        return [list(s) for s in segments]

    segments = [list(s) for s in segments]
    max_passes = len(segments) * 4 + 4  # safe upper bound; each pass eliminates >=1 segment

    for _ in range(max_passes):
        changed = False

        # Rule 1: tiny segments
        i = 0
        while i < len(segments):
            seg = segments[i]
            if len(seg) < min_size and len(segments) > 1:
                prev_overlap = _entity_overlap(segments[i - 1], seg, features) if i > 0 else -1
                next_overlap = (
                    _entity_overlap(seg, segments[i + 1], features) if i + 1 < len(segments) else -1
                )

                if next_overlap >= prev_overlap and i + 1 < len(segments):
                    segments[i + 1] = seg + segments[i + 1]
                    del segments[i]
                    changed = True
                    continue
                if i > 0:
                    segments[i - 1] = segments[i - 1] + seg
                    del segments[i]
                    changed = True
                    continue
            i += 1

        # Rule 2: meta-only segments
        i = 0
        while i < len(segments):
            seg = segments[i]
            if all(features[idx].is_meta for idx in seg):
                if i + 1 < len(segments):
                    segments[i + 1] = seg + segments[i + 1]
                    del segments[i]
                    changed = True
                    continue
                if i > 0:
                    segments[i - 1] = segments[i - 1] + seg
                    del segments[i]
                    changed = True
                    continue
            i += 1

        # Rule 3: same-repo adjacent
        i = 0
        while i + 1 < len(segments):
            repo_a = _segment_dominant_repo(segments[i], features)
            repo_b = _segment_dominant_repo(segments[i + 1], features)
            gap = _inter_segment_gap(segments[i], segments[i + 1], features)
            if repo_a and repo_a == repo_b and gap < GAP_HARD:
                segments[i] = segments[i] + segments[i + 1]
                del segments[i + 1]
                changed = True
                continue
            i += 1

        # Rule 4: continuing investigation (same family + entity overlap)
        i = 0
        while i + 1 < len(segments):
            fam_a = _segment_dominant_family(segments[i], features)
            fam_b = _segment_dominant_family(segments[i + 1], features)
            overlap = _entity_overlap(segments[i], segments[i + 1], features)
            gap = _inter_segment_gap(segments[i], segments[i + 1], features)
            if fam_a and fam_a == fam_b and overlap > 0 and gap < CONTINUING_INVESTIGATION_GAP:
                segments[i] = segments[i] + segments[i + 1]
                del segments[i + 1]
                changed = True
                continue
            i += 1

        if not changed:
            break

    return segments
