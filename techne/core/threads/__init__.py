"""Heuristic thread detection subpackage.

Public API re-exported here so that:
  - New code can import from the submodules directly.
  - Legacy imports of ``techne.core.threads`` (previously threads.py) keep
    working — ``Thread``, ``detect_and_summarize``, etc. are all still here.
  - Legacy imports of ``techne.core.heuristic_threads`` keep working via the
    shim that replaces that file.

Heuristic detection API (from heuristic_threads.py, now split into submodules):

``__all__`` defines the public surface of this package.  Private symbols
(``_xxx``) are still re-exported for backwards compatibility with existing
importers (reflect.py, heuristic_threads.py shim, tests) but are not part of
the public API and will not appear in ``from techne.core.threads import *``.
"""

__all__ = [
    # Core public types
    "Thread",
    "BoundaryScore",
    "ObsFeatures",
    # Top-level detection entry-point (defined in this module)
    "detect_threads",
    # Orchestrator public functions
    "age_threads",
    "detect_and_summarize",
    "detect_summarize_with_metadata",
    "link_threads",
    "persisted_thread_id",
    "save_threads",
    "thread_row_id",
    # Features
    "DEFAULT_ENTITY_PATTERNS",
    "DEFAULT_FAMILIES",
    "FILE_PATH_RE",
    "META_FAMILY",
    "REPO_MARKERS",
    "extract_entities",
    "extract_features",
    "extract_file_paths",
    "find_repo",
    "resolve_tool_family",
    # Labels
    "GENERIC_FAMILIES",
    "generate_label",
    # Persistence
    "boundary_indices_from_segments",
    "bucket_hash",
    "load_boundaries",
    "save_boundaries",
    # Scoring constants + function
    "BOUNDARY_THRESHOLD",
    "CONTINUING_INVESTIGATION_GAP",
    "FAMILY_CHANGE_SCORE",
    "GAP_HARD",
    "GAP_MEDIUM",
    "GAP_SOFT",
    "MIN_SEGMENT_SIZE",
    "REPO_CHANGE_SCORE",
    "USER_PROMPT_FAMILY",
    "USER_PROMPT_SCORE",
    "ZERO_ENTITY_OVERLAP_SCORE",
    "ZERO_FILE_OVERLAP_SCORE",
    "score_boundaries",
    # Segmentation
    "merge_pass",
    "split_at_boundaries",
    # Summary
    "ERROR_LINE_RE",
    "FILE_OP_TOOLS",
    "build_resume_point",
    "fallback_summary",
    "format_combined_summary",
]

# --- orchestrator (formerly threads.py) ---------------------------------------
# Public names callers imported from techne.core.threads before this refactor.
from techne.core.thread_orchestrator import Thread as Thread
from techne.core.thread_orchestrator import _make_generate_fn as _make_generate_fn
from techne.core.thread_orchestrator import _parse_threads as _parse_threads
from techne.core.thread_orchestrator import _summarize_single as _summarize_single
from techne.core.thread_orchestrator import age_threads as age_threads
from techne.core.thread_orchestrator import detect_and_summarize as detect_and_summarize
from techne.core.thread_orchestrator import (
    detect_summarize_with_metadata as detect_summarize_with_metadata,
)
from techne.core.thread_orchestrator import link_threads as link_threads
from techne.core.thread_orchestrator import persisted_thread_id as persisted_thread_id
from techne.core.thread_orchestrator import save_threads as save_threads
from techne.core.thread_orchestrator import thread_row_id as thread_row_id

# --- features -----------------------------------------------------------------
from techne.core.threads.features import DEFAULT_ENTITY_PATTERNS as DEFAULT_ENTITY_PATTERNS
from techne.core.threads.features import DEFAULT_FAMILIES as DEFAULT_FAMILIES
from techne.core.threads.features import FILE_PATH_RE as FILE_PATH_RE
from techne.core.threads.features import META_FAMILY as META_FAMILY
from techne.core.threads.features import REPO_MARKERS as REPO_MARKERS
from techne.core.threads.features import ObsFeatures as ObsFeatures
from techne.core.threads.features import _parse_timestamp as _parse_timestamp
from techne.core.threads.features import extract_entities as extract_entities
from techne.core.threads.features import extract_features as extract_features
from techne.core.threads.features import extract_file_paths as extract_file_paths
from techne.core.threads.features import find_repo as find_repo
from techne.core.threads.features import resolve_tool_family as resolve_tool_family

# --- labels -------------------------------------------------------------------
from techne.core.threads.labels import GENERIC_FAMILIES as GENERIC_FAMILIES
from techne.core.threads.labels import _humanize_family as _humanize_family
from techne.core.threads.labels import _segment_dominant_entity as _segment_dominant_entity
from techne.core.threads.labels import generate_label as generate_label

# --- persistence --------------------------------------------------------------
from techne.core.threads.persistence import (
    boundary_indices_from_segments as boundary_indices_from_segments,
)
from techne.core.threads.persistence import bucket_hash as bucket_hash
from techne.core.threads.persistence import load_boundaries as load_boundaries
from techne.core.threads.persistence import save_boundaries as save_boundaries

# --- scoring ------------------------------------------------------------------
from techne.core.threads.scoring import BOUNDARY_THRESHOLD as BOUNDARY_THRESHOLD
from techne.core.threads.scoring import (
    CONTINUING_INVESTIGATION_GAP as CONTINUING_INVESTIGATION_GAP,
)
from techne.core.threads.scoring import FAMILY_CHANGE_SCORE as FAMILY_CHANGE_SCORE
from techne.core.threads.scoring import GAP_HARD as GAP_HARD
from techne.core.threads.scoring import GAP_MEDIUM as GAP_MEDIUM
from techne.core.threads.scoring import GAP_SOFT as GAP_SOFT
from techne.core.threads.scoring import MIN_SEGMENT_SIZE as MIN_SEGMENT_SIZE
from techne.core.threads.scoring import REPO_CHANGE_SCORE as REPO_CHANGE_SCORE
from techne.core.threads.scoring import USER_PROMPT_FAMILY as USER_PROMPT_FAMILY
from techne.core.threads.scoring import USER_PROMPT_SCORE as USER_PROMPT_SCORE
from techne.core.threads.scoring import (
    ZERO_ENTITY_OVERLAP_SCORE as ZERO_ENTITY_OVERLAP_SCORE,
)
from techne.core.threads.scoring import ZERO_FILE_OVERLAP_SCORE as ZERO_FILE_OVERLAP_SCORE
from techne.core.threads.scoring import BoundaryScore as BoundaryScore
from techne.core.threads.scoring import _nearest_non_meta as _nearest_non_meta
from techne.core.threads.scoring import _score_signals as _score_signals
from techne.core.threads.scoring import score_boundaries as score_boundaries

# --- segmentation -------------------------------------------------------------
from techne.core.threads.segmentation import _entity_overlap as _entity_overlap
from techne.core.threads.segmentation import _inter_segment_gap as _inter_segment_gap
from techne.core.threads.segmentation import _segment_dominant_family as _segment_dominant_family
from techne.core.threads.segmentation import _segment_dominant_repo as _segment_dominant_repo
from techne.core.threads.segmentation import merge_pass as merge_pass
from techne.core.threads.segmentation import split_at_boundaries as split_at_boundaries

# --- summary ------------------------------------------------------------------
from techne.core.threads.summary import ERROR_LINE_RE as ERROR_LINE_RE
from techne.core.threads.summary import FILE_OP_TOOLS as FILE_OP_TOOLS
from techne.core.threads.summary import _humanize_relative as _humanize_relative
from techne.core.threads.summary import _last_error_snippet as _last_error_snippet
from techne.core.threads.summary import _recent_files as _recent_files
from techne.core.threads.summary import _short as _short
from techne.core.threads.summary import build_resume_point as build_resume_point
from techne.core.threads.summary import fallback_summary as fallback_summary
from techne.core.threads.summary import format_combined_summary as format_combined_summary


def detect_threads(
    observations: list[dict],
    user_families: dict | None = None,
    extra_entity_patterns=None,
    boundary_threshold: float = BOUNDARY_THRESHOLD,
    user_prompt_score: float = USER_PROMPT_SCORE,
    min_segment_size: int = MIN_SEGMENT_SIZE,
    fs_check=None,
    features=None,
):
    """Detect thread boundaries from a session's observations.

    Returns ``(segments, scores)``. Segments are lists of observation
    indices; scores are returned alongside so callers can persist them for
    the audit surface.

    ``features`` lets a caller that already extracted the per-observation
    feature vectors (e.g. the orchestrator, which reuses them for labels and
    summaries) pass them in so we don't extract them a second time. When
    ``None`` we build them here, sharing one per-pass ``repo_cache`` so the
    repeated file paths across a session resolve their repo root once.
    """
    if not observations:
        return [], []

    if features is None:
        repo_cache: dict[str, str | bool | None] = {}
        features = [
            extract_features(
                i, obs, user_families, extra_entity_patterns, fs_check, repo_cache=repo_cache
            )
            for i, obs in enumerate(observations)
        ]
    scores = score_boundaries(features, user_prompt_score=user_prompt_score)
    raw_segments = split_at_boundaries(len(observations), scores, threshold=boundary_threshold)
    segments = merge_pass(raw_segments, features, min_size=min_segment_size)
    return segments, scores
