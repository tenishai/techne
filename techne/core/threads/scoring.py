"""Boundary scoring for heuristic thread detection.

BoundaryScore, _score_signals, score_boundaries, and score constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from techne.core.threads.features import ObsFeatures

# --- Boundary scoring ------------------------------------------------------

# Gap thresholds in seconds and the additive score each contributes.
GAP_HARD = 3600  # >= 60 min — almost always a hard break
GAP_MEDIUM = 1800  # >= 30 min — usually a break
GAP_SOFT = 600  # >= 10 min — mild signal

FAMILY_CHANGE_SCORE = 0.4
REPO_CHANGE_SCORE = 0.5
ZERO_ENTITY_OVERLAP_SCORE = 0.3
ZERO_FILE_OVERLAP_SCORE = 0.2
# User prompt = candidate thread anchor. Doesn't break alone, but amplifies any
# real signal next to it (gap_medium, repo_change, etc.) so the prompt becomes
# the clean boundary line instead of a nearby tool call.
USER_PROMPT_SCORE = 0.3
USER_PROMPT_FAMILY = "user_prompt"

BOUNDARY_THRESHOLD = 0.7
MIN_SEGMENT_SIZE = 3

# A gap up to this duration can still represent one continuing investigation
# if the same dominant tool family + shared entities span both sides (Rule 4).
CONTINUING_INVESTIGATION_GAP = 3 * GAP_HARD  # 3 hours


@dataclass
class BoundaryScore:
    """Signal-by-signal breakdown for one consecutive observation pair."""

    a_index: int
    b_index: int
    gap_seconds: float
    family_a: str
    family_b: str
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.signals.values())


def _nearest_non_meta(
    features: list[ObsFeatures], start: int, direction: int
) -> ObsFeatures | None:
    """Walk ``features`` in ``direction`` (+1 forward, -1 back) from index
    ``start`` until a non-meta observation is found, or fall off the end."""
    i = start + direction
    while 0 <= i < len(features):
        if not features[i].is_meta:
            return features[i]
        i += direction
    return None


def _score_signals(
    a: ObsFeatures,
    b: ObsFeatures,
    gap: float,
    user_prompt_score: float = USER_PROMPT_SCORE,
) -> dict[str, float]:
    """Compute non-family signals — these don't care about meta substitution."""
    signals: dict[str, float] = {}

    if gap >= GAP_HARD:
        signals["gap_hard"] = 1.0
    elif gap >= GAP_MEDIUM:
        signals["gap_medium"] = 0.6
    elif gap >= GAP_SOFT:
        signals["gap_soft"] = 0.3

    if a.repos and b.repos and not (a.repos & b.repos):
        signals["repo_change"] = REPO_CHANGE_SCORE

    if a.entities and b.entities and not (a.entities & b.entities):
        signals["zero_entity_overlap"] = ZERO_ENTITY_OVERLAP_SCORE

    if a.file_paths and b.file_paths and not (a.file_paths & b.file_paths):
        signals["zero_file_overlap"] = ZERO_FILE_OVERLAP_SCORE

    if b.tool_family == USER_PROMPT_FAMILY:
        signals["user_prompt"] = user_prompt_score

    return signals


def score_boundaries(
    features: list[ObsFeatures],
    user_prompt_score: float = USER_PROMPT_SCORE,
) -> list[BoundaryScore]:
    """Score every consecutive pair.

    Time gap and other non-family signals are computed on the actual pair.
    The *family-change* signal is computed using each side's nearest non-meta
    neighbor — so a ToolSearch (meta) sandwiched between two Datadog calls
    doesn't generate a spurious family-change.
    """
    scores: list[BoundaryScore] = []
    for i in range(len(features) - 1):
        a, b = features[i], features[i + 1]
        gap = max(0.0, (b.timestamp - a.timestamp).total_seconds())

        signals = _score_signals(a, b, gap, user_prompt_score=user_prompt_score)

        a_eff = a if not a.is_meta else (_nearest_non_meta(features, i, -1) or a)
        b_eff = b if not b.is_meta else (_nearest_non_meta(features, i + 1, +1) or b)
        # Suppress family_change when crossing into a UserPrompt — the prompt's
        # synthetic family would otherwise double-count with the user_prompt
        # signal (a tool → prompt transition always changes family), pushing
        # every mid-flow prompt to the boundary threshold.
        if (
            not a_eff.is_meta
            and not b_eff.is_meta
            and a_eff.tool_family != b_eff.tool_family
            and b_eff.tool_family != USER_PROMPT_FAMILY
        ):
            signals["family_change"] = FAMILY_CHANGE_SCORE

        scores.append(
            BoundaryScore(
                a_index=a.index,
                b_index=b.index,
                gap_seconds=gap,
                family_a=a.tool_family,
                family_b=b.tool_family,
                signals=signals,
            )
        )
    return scores
